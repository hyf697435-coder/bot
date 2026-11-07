# -*- coding: utf-8 -*-
"""
交易所交互层 (exchange)：Hyperliquid 行情数据 (只读 /info) + 下单执行 (签名 /exchange)。

== 行情数据 ==
  - 拉取 5m / 1h / 4h K 线并组装为 pandas.DataFrame (open/high/low/close/volume/ts)；
  - 拉取 Hyperliquid fundingHistory (每小时结算) 生成资金费率序列统计 (FR / FR_mean / σ_FR)；
  - 拉取 allMids 作为轻量实时标记价 (持仓期 1s 轮询专用)。

行情方法 fail-fast：网络/解析失败抛 MarketDataError，由调用方决定降级策略。
Hyperliquid /info 两种 payload 风格 (坑位，混淆即 422)：
  - candleSnapshot 使用 req 包装：{"type": "candleSnapshot", "req": {...}}；
  - fundingHistory 字段平铺顶层：{"type": "fundingHistory", "coin": ..., "startTime": ...}。

== 执行引擎 ==
从旧 core.py 抽取的 HyperliquidExecutor，接口按新架构改造：
  - 开仓以"名义金额 (sz_notional)"为输入 (由 position.calculate_position_size 计算)，
    杠杆按 ceil(sz_notional / equity) 自动设置并夹紧到 [1, 5]；
  - 开仓确认后同步挂载风控单：AI/本地最终止损 + 本地硬止损安全垫 + 止盈；
  - 保留 fail-closed 原则：只有拿到交易所 status == "ok" 确认才推进状态，
    风控单挂载失败立即回滚平掉裸奔仓位。
"""

__version__ = "1.0.0"
__milestone__ = "v1.0-RELEASE"
__release_date__ = "2026-11-01"


import http.server
import math
import os
import socket
import socketserver
import subprocess
import threading
import time
from decimal import ROUND_DOWN, Decimal
from typing import Dict, List, Optional, Tuple

import httpx
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from alerts import logger, send_alert

def _setup_resilient_session(session: requests.Session) -> requests.Session:
    """为 Hyperliquid SDK 的 requests.Session 装配自动重试与高鲁棒连接池"""
    retry_strategy = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _to_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_BRIDGE_SERVER = None
_BRIDGE_PORT = 8765

class _ReusableBridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

def _is_bridge_alive(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.5):
            return True
    except Exception:
        return False

def _ensure_android_curl_bridge(target_base: str) -> str:
    global _BRIDGE_SERVER
    if not os.path.exists("/system/bin/curl"):
        return target_base
    if "127.0.0.1" in target_base or "localhost" in target_base:
        return target_base

    if _is_bridge_alive(_BRIDGE_PORT):
        return f"http://127.0.0.1:{_BRIDGE_PORT}"

    if _BRIDGE_SERVER is None:
        class _CurlBridgeHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                tmp_file = ""
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(length) if length > 0 else b'{}'
                    tmp_id = f"bridge_{os.getpid()}_{threading.get_ident()}_{int(time.time()*1000)%100000}"
                    tmp_file = f"/data/local/tmp/{tmp_id}.tmp"
                    cmd = [
                        "/system/bin/curl", "-s", "-S",
                        "--connect-timeout", "10",
                        "--max-time", "25",
                        "-X", "POST", f"{target_base}{self.path}",
                        "-H", "Content-Type: application/json",
                        "--data-binary", "@-",
                        "-o", tmp_file,
                        "-w", "%{http_code}"
                    ]
                    p = subprocess.run(cmd, input=body, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
                    code_str = p.stdout.decode('ascii', errors='ignore').strip()
                    code = int(code_str) if code_str.isdigit() else 200
                    resp_body = b''
                    if os.path.exists(tmp_file):
                        try:
                            with open(tmp_file, "rb") as f:
                                resp_body = f.read()
                        finally:
                            try:
                                os.remove(tmp_file)
                            except Exception:
                                pass
                    elif p.returncode != 0:
                        code = 502
                        err_msg = p.stderr.decode('utf-8', errors='ignore').strip()
                        resp_body = f'{{"error": "curl failed code {p.returncode}: {err_msg}"}}'.encode('utf-8')

                    self.send_response(code)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(resp_body)))
                    self.end_headers()
                    self.wfile.write(resp_body)
                except Exception as ex:
                    if tmp_file and os.path.exists(tmp_file):
                        try:
                            os.remove(tmp_file)
                        except Exception:
                            pass
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(f'{{"error": "{ex}"}}'.encode('utf-8'))

            def log_message(self, format, *args):
                pass

        try:
            _BRIDGE_SERVER = _ReusableBridgeServer(('127.0.0.1', _BRIDGE_PORT), _CurlBridgeHandler)
            t = threading.Thread(target=_BRIDGE_SERVER.serve_forever, daemon=True)
            t.start()
            logger.info(f"[网络桥] Android系统级 BoringSSL-cURL 桥接已启动 | port={_BRIDGE_PORT} | upstream={target_base}")
        except Exception as e:
            logger.warning(f"[网络桥] 启动桥接失败: {e}")
            return target_base

    return f"http://127.0.0.1:{_BRIDGE_PORT}"


def resolve_base_url() -> str:
    """根据 HYPE_ENV 决定 Hyperliquid REST 地址 (默认测试网)。"""
    env = os.getenv("HYPE_ENV", "testnet").lower()
    by_env = {
        "testnet": "https://api.hyperliquid-testnet.xyz",
        "mainnet": "https://api.hyperliquid.xyz",
    }
    target = os.getenv("HYPE_BASE_URL", "").strip() or by_env.get(env, by_env["testnet"])
    return _ensure_android_curl_bridge(target)


class MarketDataError(Exception):
    """行情获取异常。"""


class ExecutionError(Exception):
    """真实下单执行层异常：签名失败、网络异常、交易所拒单等。"""


# ==========================================
# 行情数据 (只读 /info)
# ==========================================

class MarketData:
    """Hyperliquid 公共行情客户端 (只读，无需鉴权)。"""

    INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    _http_client: Optional[httpx.Client] = None

    @classmethod
    def get_http_client(cls) -> httpx.Client:
        if cls._http_client is None or cls._http_client.is_closed:
            cls._http_client = httpx.Client(
                timeout=httpx.Timeout(15.0, connect=5.0),
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20, keepalive_expiry=60.0),
            )
        return cls._http_client

    def __init__(self, coin: Optional[str] = None):
        self.base_url = resolve_base_url().rstrip("/")
        self.coin = (coin or os.getenv("HYPE_COIN", "ETH")).strip()
        # 各周期 K 线缓存：{timeframe: (DataFrame, fetched_at)}，避免空仓期 5s 轮询打爆 API
        self._cache_ttl = {
            "5m": _env_float("CACHE_TTL_5M", 5.0),
            "1h": _env_float("CACHE_TTL_1H", 300.0),   # 1H K线在小时内相对稳定，整点级5分钟复用
            "4h": _env_float("CACHE_TTL_4H", 900.0),   # 4H K线复用 15 分钟
        }
        self._ohlcv_cache: Dict[str, Tuple[pd.DataFrame, float]] = {}
        # fundingHistory 缓存 (统计窗口为 168h 均值/方差，10分钟复用大幅降低 5m 临界区 I/O)
        self._fr_cache: Optional[Tuple[Dict[str, float], float]] = None
        self._fr_ttl = _env_float("CACHE_TTL_FR", 600.0)

    def set_coin(self, coin: str):
        """动态切换行情币种并清空本地缓存"""
        new_c = str(coin).strip().upper()
        if self.coin != new_c:
            self.coin = new_c
            self._ohlcv_cache.clear()
            self._fr_cache = None

    # ------------------------------------------------------------------
    # K 线
    # ------------------------------------------------------------------

    def get_ohlcv(self, timeframe: str = "5m", lookback_bars: int = 120,
                  use_cache: bool = True) -> pd.DataFrame:
        """
        拉取 K 线并返回 DataFrame，列: ts(毫秒), open, high, low, close, volume，
        按时间升序，最后一根为最新 (可能未闭合)。
        """
        if timeframe not in self.INTERVAL_SECONDS:
            raise MarketDataError(f"不支持的周期: {timeframe}")

        if use_cache:
            cached = self._ohlcv_cache.get(timeframe)
            if cached is not None and time.monotonic() - cached[1] < self._cache_ttl.get(timeframe, 5.0):
                return cached[0]

        now_ms = int(time.time() * 1000)
        span_ms = self.INTERVAL_SECONDS[timeframe] * 1000 * max(lookback_bars + 10, 30)
        payload = {
            "type": "candleSnapshot",
            "req": {"coin": self.coin, "interval": timeframe, "startTime": now_ms - span_ms},
        }
        client = self.get_http_client()
        try:
            resp = client.post(self.base_url + "/info", json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise MarketDataError(f"行情请求失败 ({self.coin}/{timeframe}): {e}")

        if not isinstance(data, list) or not data:
            raise MarketDataError(f"行情数据为空 ({self.coin}/{timeframe})")

        try:
            df = pd.DataFrame(data)
            df = df.rename(columns={
                "t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
            })
            for col in ("open", "high", "low", "close", "volume"):
                df[col] = df[col].astype(float)
            df["ts"] = df["ts"].astype("int64")
            df = df.sort_values("ts").reset_index(drop=True)
        except (KeyError, TypeError, ValueError) as e:
            raise MarketDataError(f"行情字段解析失败 ({timeframe}): {e}")

        self._ohlcv_cache[timeframe] = (df, time.monotonic())
        return df

    def get_closed_ohlcv(self, timeframe: str = "5m", lookback_bars: int = 120,
                          use_cache: bool = True) -> Tuple[pd.DataFrame, float]:
        """
        基于时间戳严格校验：
        若最后一根 K 线的 open_time + interval > 当前系统时间戳(ms)，确认为未闭合 K 线并剔除，
        确保返回的 DataFrame 最后一根为完全闭合的最新收盘 K 线；
        同时返回最新 tick 现价 (未闭合 K 线的 close 或闭合 K 线的 close)。
        返回: (closed_df, latest_tick_price)
        """
        df_raw = self.get_ohlcv(timeframe=timeframe, lookback_bars=lookback_bars + 1, use_cache=use_cache)
        if df_raw.empty:
            return df_raw, 0.0

        try:
            latest_price = float(df_raw["close"].iloc[-1])
            if not math.isfinite(latest_price) or latest_price <= 0:
                latest_price = 0.0
        except (ValueError, TypeError, IndexError):
            latest_price = 0.0

        now_ms = int(time.time() * 1000)
        interval_sec = self.INTERVAL_SECONDS.get(timeframe)
        if interval_sec is None:
            raise MarketDataError(f"不支持的未注册 timeframe 周期: {timeframe}")
        interval_ms = interval_sec * 1000

        try:
            last_open_time = int(df_raw["ts"].iloc[-1])
        except (ValueError, TypeError, IndexError):
            last_open_time = 0
        # 严格比对：只要当前时间尚未超过该 K 线的收盘时间点，即判定为未闭合，安全剔除
        if (last_open_time + interval_ms) > now_ms and len(df_raw) > 1:
            closed_df = df_raw.iloc[:-1].reset_index(drop=True)
        else:
            closed_df = df_raw

        return closed_df, latest_price

    # ------------------------------------------------------------------
    # 实时标记价 (轻量)
    # ------------------------------------------------------------------

    def mark_price(self) -> float:
        """allMids 标记价 (持仓期 1s 轮询用，单请求权重最低)。"""
        client = self.get_http_client()
        try:
            resp = client.post(self.base_url + "/info", json={"type": "allMids"}, timeout=10)
            resp.raise_for_status()
            mids = resp.json()
        except Exception as e:
            raise MarketDataError(f"allMids 请求失败: {e}")
        try:
            return float(mids[self.coin])
        except (KeyError, TypeError, ValueError) as e:
            raise MarketDataError(f"allMids 中无 {self.coin} 价格: {e}")

    # ------------------------------------------------------------------
    # 资金费率 (Hyperliquid 每小时结算)
    # ------------------------------------------------------------------

    def get_funding_stats(self, window_hours: int = 168, use_cache: bool = True) -> Dict[str, float]:
        """
        拉取 fundingHistory 最近 window_hours 小时样本 (默认 168h = 7 天滑动窗口)，
        返回:
          {
            "fr":          最新一小时资金费率 (小时口径)，
            "fr_mean":     窗口均值，
            "fr_std":      窗口标准差 (σ_FR，总体口径)，
            "fr_samples":  实际样本数，
          }
        Hyperliquid 资金费率为每小时结算，旧指南的 8h 结算口径按 Q2 结论适配为
        小时样本统计；"8h 等效费率" 由调用方按 fr * 8 折算。
        """
        if use_cache and self._fr_cache is not None:
            stats, fetched_at = self._fr_cache
            if time.monotonic() - fetched_at < self._fr_ttl:
                return stats

        now_ms = int(time.time() * 1000)
        start_ms = now_ms - window_hours * 3600 * 1000
        # fundingHistory 字段平铺顶层 (官方 SDK 口径)，与 candleSnapshot 的 req 包装不同
        payload = {"type": "fundingHistory", "coin": self.coin, "startTime": start_ms}
        client = self.get_http_client()
        try:
            resp = client.post(self.base_url + "/info", json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise MarketDataError(f"fundingHistory 请求失败: {e}")

        if not isinstance(data, list) or not data:
            raise MarketDataError(f"fundingHistory 数据为空 ({self.coin})")

        try:
            ordered = sorted(data, key=lambda x: int(x.get("time") or 0))
            rates = [float(item["fundingRate"]) for item in ordered]
        except (KeyError, TypeError, ValueError) as e:
            raise MarketDataError(f"fundingHistory 字段解析失败: {e}")

        valid_rates = [r for r in rates if math.isfinite(r)]
        if not valid_rates:
            raise MarketDataError(f"fundingHistory 缺少有效数据 ({self.coin})")

        std_val = float(pd.Series(valid_rates).std(ddof=0))
        if math.isnan(std_val):
            std_val = 0.0

        stats = {
            "fr": valid_rates[-1],
            "fr_mean": float(sum(valid_rates) / len(valid_rates)),
            "fr_std": std_val,
            "fr_samples": float(len(valid_rates)),
        }
        self._fr_cache = (stats, time.monotonic())
        return stats


# ==========================================
# 下单执行 (签名 /exchange)
# ==========================================

# 新架构名义敞口上限 3x equity，杠杆设置为 ceil(sz/equity) 并夹紧到 [1, 5]
LEVERAGE_MIN, LEVERAGE_MAX = 1, 5


class HyperliquidExecutor:
    """
    真实下单执行层。所有方法遵循 fail-closed：
    只有在拿到交易所确认 (status == "ok") 后才返回非空结果；
    未配置密钥、SDK 缺失或交易所拒单时返回 None / 抛 ExecutionError，
    调用方绝不能在未确认的情况下推进状态机 (尤其不能置为 TRADING)。
    """

    def cancel_non_trigger_orders(self):
        """仅撤销普通限价单，保留带有 isTrigger 的止损/止盈保护单"""
        if not self.ready or not self._info or not self._exchange:
            return
        try:
            open_orders = self._info.open_orders(self.user)
            if not isinstance(open_orders, list):
                logger.warning(f"[Exchange] 查询挂单返回非列表结构: {open_orders}")
                return
            for o in open_orders:
                if not isinstance(o, dict):
                    continue
                if o.get("coin") == self.coin and not o.get("isTrigger", False):
                    raw_oid = o.get("oid")
                    if raw_oid is None:
                        continue
                    try:
                        oid = int(str(raw_oid).strip())
                    except (ValueError, TypeError):
                        logger.warning(f"[Exchange] 撤单遇到非法 oid 格式: {raw_oid}")
                        continue
                    try:
                        resp = self._exchange.cancel(self.coin, oid)
                        if self._status_ok(resp):
                            logger.info(f"[Exchange] 撤单成功 | coin={self.coin} | oid={oid}")
                        else:
                            logger.warning(f"[Exchange] 撤单未确认 | coin={self.coin} | oid={oid} | resp={resp}")
                    except Exception as ce:
                        logger.warning(f"[Exchange] 撤销订单失败 | oid={oid} | err={ce}")
        except Exception as e:
            logger.warning(f"[Exchange] 撤销普通挂单异常: {e}")

    def __init__(self):
        self.coin = os.getenv("HYPE_COIN", "ETH").strip()
        self.base_url = resolve_base_url()
        self.user = os.getenv("HYPE_USER_ADDRESS", "").strip()      # 主账户: 资金与持仓归属
        self.secret = os.getenv("HYPE_SECRET_KEY", "").strip()      # 签名私钥: 主账户或 API 钱包
        self.api_wallet = os.getenv("HYPE_API_WALLET_ADDRESS", "").strip()
        self.slippage = _env_float("HYPE_SLIPPAGE", 0.01)

        self._exchange = None
        self._info = None
        self._sz_decimals = None
        self._position_size = None       # 本通道开出的持仓币数量
        self._position_direction = None  # "LONG" / "SHORT"
        self._signer_address = None      # 私钥派生出的签名地址

        # 风控单记账：最终止损 (FSM 会移动它) + 本地硬止损安全垫 (定时刷新) + 止盈
        self._ai_sl_price = None
        self._ai_sl_oid = None
        self._hard_sl_price = None
        self._hard_sl_oid = None
        self._tp_price = None
        self._tp_oid = None
        self._last_tighten_time: float = 0.0  # 记录上次成功移损时间戳，用于防频控节流
        self.wallet_mode = "disabled"    # "direct" | "api_wallet" | "disabled"
        self._init_sdk()

    def _init_sdk(self):
        if not self.user or not self.secret:
            logger.warning("[执行] 未配置签名密钥 | 写通道=禁用 | mode=fail-closed")
            return
        try:
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            from eth_account import Account

            wallet = Account.from_key(self.secret)
            self._signer_address = wallet.address
            self._info = Info(self.base_url, skip_ws=True)
            if hasattr(self._info, "session") and isinstance(self._info.session, requests.Session):
                _setup_resilient_session(self._info.session)

            if wallet.address.lower() == self.user.lower():
                self.wallet_mode = "direct"
                self._exchange = Exchange(wallet, self.base_url)
                if hasattr(self._exchange, "session") and isinstance(self._exchange.session, requests.Session):
                    _setup_resilient_session(self._exchange.session)
                logger.info("[执行] 签名通道就绪 | mode=direct | coin=%s | account=%s",
                            self.coin, wallet.address)
                return

            # 私钥地址 != 主账户地址：按 API 钱包(agent)代签处理，交易归属主账户
            self.wallet_mode = "api_wallet"
            if self.api_wallet and self.api_wallet.lower() != wallet.address.lower():
                logger.warning(
                    "[执行] HYPE_API_WALLET_ADDRESS (%s) 与私钥派生地址 (%s) 不匹配 "
                    "| 以私钥派生地址为准进行授权校验",
                    self.api_wallet, wallet.address)
            if not self._verify_api_wallet(wallet.address, self.user):
                logger.error("[执行] API钱包未授权或过期 | signer=%s | account=%s | 写通道=禁用 | mode=fail-closed",
                             wallet.address, self.user)
                self._exchange = None
                return
            self._exchange = Exchange(wallet, self.base_url, account_address=self.user)
            if hasattr(self._exchange, "session") and isinstance(self._exchange.session, requests.Session):
                _setup_resilient_session(self._exchange.session)
            logger.info("[执行] 签名通道就绪 | mode=api_wallet | signer=%s | account=%s | coin=%s",
                        wallet.address, self.user, self.coin)
        except ImportError as e:
            logger.error("[执行] 缺少 SDK依赖 | 写通道=禁用 | err=%s", e)
            self._exchange = None
        except Exception as e:
            logger.error("[执行] 初始化签名钱包失败 | 写通道=禁用 | err=%s", e)
            self._exchange = None

    def _verify_api_wallet(self, signer: str, user: str) -> bool:
        """校验 signer 是否为 user 已授权且未过期的 API 钱包(agent)。查询失败 fail-closed。"""
        try:
            agents = self._info.extra_agents(user) or []
        except Exception as e:
            logger.error("[执行] API钱包授权查询失败 | 写通道=禁用 | mode=fail-closed | err=%s", e)
            return False
        for a in agents:
            if str(a.get("address", "")).lower() != signer.lower():
                continue
            valid_until = a.get("validUntil")
            if valid_until and float(valid_until) < time.time() * 1000:
                logger.error("[执行] API钱包授权过期 | signer=%s | valid_until=%s(ms)", signer, valid_until)
                return False
            return True
        return False

    @property
    def ready(self) -> bool:
        return self._exchange is not None and self._info is not None

    def _fail(self, reason: str):
        logger.error("[执行] %s", reason)
        send_alert(f"[执行层] {reason}")

    @staticmethod
    def _status_ok(resp) -> bool:
        """
        严密校验 Hyperliquid 回报状态：
        不仅检查顶层 status == 'ok'，更深度穿透 response.data.statuses 数组，
        确保无 'error' 拒绝（例如 'Order was never placed'、'Insufficient margin' 等）。
        """
        if not resp or resp.get("status") != "ok":
            return False
        try:
            data = resp.get("response", {}).get("data", {})
            statuses = data.get("statuses", [])
            for s in statuses:
                if isinstance(s, dict) and s.get("error"):
                    return False
                if isinstance(s, str) and s.lower() == "error":
                    return False
        except Exception:
            pass
        return True

    @staticmethod
    def _extract_filled(resp) -> Tuple[Optional[float], Optional[float]]:
        """从交易所回报中提取 (成交数量 sz, 成交均价 avgPx)。非成交单返回 (None, None)。"""
        try:
            status = resp["response"]["data"]["statuses"][0]
            filled = status.get("filled")
            if filled:
                sz = float(filled.get("totalSz") or filled.get("sz") or 0) or None
                avg_px = filled.get("avgPx")
                return sz, (float(avg_px) if avg_px is not None else None)
        except (KeyError, IndexError, TypeError, ValueError):
            return None, None
        return None, None

    def _meta_sz_decimals(self) -> int:
        if self._sz_decimals is not None:
            return self._sz_decimals
        self._sz_decimals = 4
        try:
            meta = self._info.meta()
            for u in meta.get("universe", []):
                if u.get("name") == self.coin:
                    self._sz_decimals = int(u.get("szDecimals", 4))
                    break
        except Exception as e:
            logger.warning("[执行] 获取 szDecimals 失败 | 默认=4 | err=%s", e)
        return self._sz_decimals

    def _round_sz(self, sz: float) -> float:
        return round(float(sz), self._meta_sz_decimals())

    def _round_px(self, px: float) -> float:
        px = float(px)
        if px <= 0:
            raise ExecutionError(f"非法价格: {px}")
        d = Decimal(str(px))
        max_decimals = max(6 - self._meta_sz_decimals(), 0)
        sig_decimals = 5 - (d.adjusted() + 1)
        if sig_decimals >= 0:
            eff_decimals = min(max_decimals, sig_decimals)
            d = d.quantize(Decimal(1).scaleb(-eff_decimals), rounding=ROUND_DOWN)
        else:
            step = Decimal(10) ** abs(sig_decimals)
            d = (d // step) * step
        result = float(d)
        if result <= 0:
            raise ExecutionError(f"价格精度截断无效: {px}")
        return result

    # ------------------------------------------------------------------
    # 账户与行情查询
    # ------------------------------------------------------------------

    def account_value(self) -> float:
        if self._info is None:
            raise ExecutionError("行情通道未就绪，无法读取账户权益。")
        if not self.user:
            raise ExecutionError("未配置 HYPE_USER_ADDRESS，无法读取账户权益。")
        try:
            state = self._info.user_state(self.user)
            value = float(state["marginSummary"]["accountValue"])
        except ExecutionError:
            raise
        except Exception as e:
            raise ExecutionError(f"读取账户权益失败: {e}")
        if value <= 0:
            raise ExecutionError(f"账户权益({value})非法。")
        return value

    def mark_price(self) -> float:
        if self._info is None:
            raise ExecutionError("交易所执行层未初始化，无法获取标记价")
        try:
            mids = self._info.all_mids()
            if not isinstance(mids, dict) or self.coin not in mids:
                raise ExecutionError(f"all_mids 未包含币种 {self.coin} 或返回非法")
            px = float(mids[self.coin])
            if not math.isfinite(px) or px <= 0:
                raise ExecutionError(f"标记价非法: {px}")
            return px
        except ExecutionError:
            raise
        except Exception as e:
            raise ExecutionError(f"获取标记价失败: {e}")

    def current_position(self) -> Tuple[float, Optional[float]]:
        """返回 (szi 带符号数量, 开仓均价)；无仓位或通道未就绪返回 (0.0, None)；查询报错抛出 MarketDataError。"""
        if self._info is None or not self.user:
            return 0.0, None
        try:
            state = self._info.user_state(self.user)
        except Exception as e:
            logger.warning("[执行] 查询交易所持仓失败 | err=%s", e)
            raise MarketDataError(f"查询交易所持仓失败: {e}")
        for item in state.get("assetPositions", []):
            pos = item.get("position", {})
            if pos.get("coin") == self.coin:
                szi = float(pos.get("szi", 0) or 0)
                entry = pos.get("entryPx")
                return szi, (float(entry) if entry else None)
        return 0.0, None

    def adopt_open_position(self, szi: float, entry: Optional[float] = None) -> None:
        """把交易所遗留仓位挂到本地执行层记账，并尽量绑定已有风控单 oid。"""
        self._position_size = abs(float(szi))
        self._position_direction = "LONG" if float(szi) > 0 else "SHORT"
        self._bind_open_protection_orders()

    def _bind_open_protection_orders(self) -> None:
        """扫描本币种未成交触发单，回填主止损/硬止损/止盈 oid。"""
        if self._info is None or not self.user or not self._position_direction:
            return
        try:
            orders = self._info.open_orders(self.user) or []
            mark = self.mark_price()
        except Exception as e:
            logger.warning("[执行] 接管仓位查询挂单失败 | err=%s", e)
            return
        sl_orders: List[tuple] = []
        tp_orders: List[tuple] = []
        for o in orders:
            if o.get("coin") != self.coin:
                continue
            oid = o.get("oid")
            if oid is None:
                continue
            trigger = o.get("triggerPx") or o.get("limitPx")
            try:
                px = float(trigger)
            except (TypeError, ValueError):
                continue
            tpsl = str(o.get("tpsl") or "").lower()
            if tpsl == "tp":
                tp_orders.append((px, oid))
            elif tpsl == "sl":
                sl_orders.append((px, oid))
            elif self._position_direction == "LONG":
                (sl_orders if px < mark else tp_orders).append((px, oid))
            else:
                (sl_orders if px > mark else tp_orders).append((px, oid))
        if tp_orders:
            tp_orders.sort(key=lambda x: x[0])
            self._tp_price, self._tp_oid = (
                tp_orders[-1] if self._position_direction == "LONG" else tp_orders[0]
            )
        if sl_orders:
            sl_orders.sort(key=lambda x: x[0])
            if self._position_direction == "LONG":
                self._ai_sl_price, self._ai_sl_oid = sl_orders[-1]
                if len(sl_orders) > 1:
                    self._hard_sl_price, self._hard_sl_oid = sl_orders[0]
            else:
                self._ai_sl_price, self._ai_sl_oid = sl_orders[0]
                if len(sl_orders) > 1:
                    self._hard_sl_price, self._hard_sl_oid = sl_orders[-1]

    # ------------------------------------------------------------------
    # 杠杆与风控单管理
    # ------------------------------------------------------------------

    def _set_leverage(self, leverage: float) -> int:
        """设置全仓杠杆，返回实际生效的杠杆倍数 (已按 [LEVERAGE_MIN, LEVERAGE_MAX] 截断)。"""
        lev = int(round(float(leverage)))
        lev = max(LEVERAGE_MIN, min(lev, LEVERAGE_MAX))
        resp = self._exchange.update_leverage(lev, self.coin, is_cross=True)
        if not self._status_ok(resp):
            raise ExecutionError(f"设置杠杆失败: {resp}")
        logger.info("[执行] 杠杆设置完成 | leverage=%sx", lev)
        return lev

    def _clear_protection_tracking(self):
        """清空风控单记账 (平仓后调用，避免脏引用)。"""
        self._ai_sl_price = None
        self._ai_sl_oid = None
        self._hard_sl_price = None
        self._hard_sl_oid = None
        self._tp_price = None
        self._tp_oid = None
        self._last_tighten_time = 0.0

    def clear_position_state(self):
        """交易所在外部平仓 (如风控单成交) 后，清空本地仓位与风控单记账。"""
        self._position_size = None
        self._position_direction = None
        self._clear_protection_tracking()

    def cancel_all_orders(self):
        """公开的撤单入口：撤销本币种全部挂单 (含历史止损/止盈触发单)。"""
        self._cancel_all_orders()

    def _cancel_all_orders(self):
        """撤销本币种全部挂单 (含历史止损/止盈触发单)。"""
        if not self.ready:
            return
        try:
            orders = self._info.open_orders(self.user)
            if not isinstance(orders, list):
                logger.warning(f"[执行] 查询挂单返回非列表结构: {orders}")
                return
        except Exception as e:
            raise ExecutionError(f"查询挂单失败: {e}")
        for o in orders:
            if not isinstance(o, dict) or o.get("coin") != self.coin:
                continue
            raw_oid = o.get("oid")
            if raw_oid is None:
                continue
            try:
                oid = int(str(raw_oid).strip())
                resp = self._exchange.cancel(self.coin, oid)
                if self._status_ok(resp):
                    logger.info("[执行] 撤单成功 | oid=%s", oid)
                else:
                    logger.warning("[执行] 撤单未确认 | oid=%s | resp=%s", oid, resp)
            except Exception as e:
                logger.warning("[执行] 撤单失败 | oid=%s | err=%s", raw_oid, e)
        self._ai_sl_oid = None
        self._hard_sl_oid = None
        self._tp_oid = None

    def _cancel_order(self, oid):
        """撤销指定挂单，用于单独替换本地硬止损。防御字符串/非整型转换异常与撤单失败状态校验。"""
        if not oid or not self.ready:
            return
        try:
            order_id = int(str(oid).strip())
        except (ValueError, TypeError):
            logger.warning("[执行] 非法 oid 格式，跳过撤单: %s", oid)
            return

        try:
            resp = self._exchange.cancel(self.coin, order_id)
            if self._status_ok(resp):
                logger.info("[执行] 撤单成功 | oid=%s", order_id)
            else:
                logger.warning("[执行] 撤单被交易所拒绝或未确认 | oid=%s | resp=%s", order_id, resp)
        except Exception as e:
            logger.warning("[执行] 撤单网络或系统异常 | oid=%s | err=%s", order_id, e)

    def _place_trigger(self, kind: str, price: float):
        """挂 reduce_only 触发单。kind: 'sl' 或 'tp'。返回交易所 oid (取不到返回 None)。"""
        if not self.ready:
            raise ExecutionError("风控单通道未就绪。")
        if not self._position_size or self._position_size <= 0:
            raise ExecutionError("缺少持仓数量，无法挂风控单。")
        if price is None or float(price) <= 0:
            raise ExecutionError(f"非法风控价: {price}")

        close_is_buy = self._position_direction == "SHORT"
        price = self._round_px(float(price))
        order_type = {"trigger": {"triggerPx": float(price), "isMarket": True, "tpsl": kind}}
        resp = self._exchange.order(
            self.coin, close_is_buy, self._position_size, float(price), order_type, True
        )
        if not self._status_ok(resp):
            raise ExecutionError(f"{kind.upper()} 挂单未确认: {resp}")

        oid = None
        try:
            statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
            if not statuses:
                raise ExecutionError(f"{kind.upper()} 挂单缺少 statuses 节点: {resp}")
            status = statuses[0]
            if isinstance(status, dict) and status.get("error"):
                raise ExecutionError(f"{kind.upper()} 挂单被交易所拒绝: {status['error']}")
            resting = status.get("resting") if isinstance(status, dict) else None
            if isinstance(resting, dict):
                oid = resting.get("oid")
            if oid is None:
                filled = status.get("filled") if isinstance(status, dict) else None
                if isinstance(filled, dict):
                    oid = filled.get("oid")
            if oid is None:
                raise ExecutionError(f"{kind.upper()} 挂单返回未包含有效 oid: {resp}")
        except ExecutionError:
            raise
        except Exception as e:
            raise ExecutionError(f"{kind.upper()} 解析挂单响应异常: {e}")
        logger.info("[执行] 挂单成功 | kind=%s | price=%s | oid=%s", kind.upper(), price, oid)
        return oid

    def _place_protective_stops(self, final_sl, hard_sl, take_profit) -> dict:
        """
        同时挂载风控单：最终止损 (主) + 本地硬止损安全垫 + 可选止盈。

        - 主止损挂载失败即抛错 (由调用方回滚裸奔仓位)；
        - 本地硬止损是更宽的安全垫，挂载失败仅告警，保留主止损；
        - 两者价格过近时去重，避免同一价位重复触发。
        返回 {"sl": oid, "hard": oid, "tp": oid}。
        """
        oids = {}
        final_sl = float(final_sl) if final_sl and float(final_sl) > 0 else None
        hard_sl = float(hard_sl) if hard_sl and float(hard_sl) > 0 else None

        if final_sl:
            self._ai_sl_price = final_sl
            oids["sl"] = self._place_trigger("sl", final_sl)
            self._ai_sl_oid = oids["sl"]

        if hard_sl:
            too_close = final_sl is not None and abs(hard_sl - final_sl) / max(abs(final_sl), 1e-9) < 1e-4
            if too_close:
                # 之前若有硬止损单残留，必须撤销，防止孤儿挂单
                if getattr(self, "_hard_sl_oid", None):
                    try:
                        self._cancel_order(self._hard_sl_oid)
                    except Exception as ce:
                        logger.warning("[执行] 撤销过近旧硬止损单异常 | oid=%s | err=%s", self._hard_sl_oid, ce)
                self._hard_sl_price = None
                self._hard_sl_oid = None
                logger.info("[执行] 硬止损与主止损过近 | 硬止损=%.2f | action=仅保留主止损", hard_sl)
            else:
                try:
                    self._hard_sl_price = hard_sl
                    oids["hard"] = self._place_trigger("sl", hard_sl)
                    self._hard_sl_oid = oids["hard"]
                except ExecutionError as e:
                    self._hard_sl_price = None
                    self._hard_sl_oid = None
                    logger.error("[执行] 硬止损挂载失败 | action=保留主止损 | err=%s", e)
                    send_alert(f"本地硬止损挂载失败，仅保留主止损: {e}")

        if take_profit and float(take_profit) > 0:
            self._tp_price = float(take_profit)
            oids["tp"] = self._place_trigger("tp", float(take_profit))
            self._tp_oid = oids["tp"]
        return oids

    def refresh_hard_stop(self, new_hard_stop: Optional[float]) -> Optional[float]:
        """
        按最新 ATR 单独刷新本地硬止损单 (仅替换硬止损，不触碰主止损/止盈)。

        价格已被现价击穿时跳过 (交由交易所侧触发/平仓流程处理)，避免挂出立即成交的单。
        """
        if not self.ready or new_hard_stop is None or float(new_hard_stop) <= 0:
            return None
        if not self._position_size or self._position_size <= 0:
            return None
        new_hard_stop = float(new_hard_stop)

        try:
            mark = self.mark_price()
        except Exception as e:
            logger.warning("[执行] 刷新硬止损取价失败 | action=跳过本次刷新 | err=%s", e)
            return None
        if self._position_direction == "LONG" and new_hard_stop >= mark:
            logger.warning("[执行] 硬止损已被击穿 | 硬止损=%.2f | 现价=%.2f | action=跳过刷新",
                           new_hard_stop, mark)
            return None
        if self._position_direction == "SHORT" and new_hard_stop <= mark:
            logger.warning("[执行] 硬止损已被击穿 | 硬止损=%.2f | 现价=%.2f | action=跳过刷新",
                           new_hard_stop, mark)
            return None

        if self._hard_sl_price is not None and \
                abs(new_hard_stop - self._hard_sl_price) / max(abs(self._hard_sl_price), 1e-9) < 1e-4:
            return self._hard_sl_price

        if self._ai_sl_price:
            extra = max(abs(float(self._ai_sl_price)) * 1e-4, 1e-6)
            if self._position_direction == "LONG":
                new_hard_stop = min(new_hard_stop, float(self._ai_sl_price) - extra)
            elif self._position_direction == "SHORT":
                new_hard_stop = max(new_hard_stop, float(self._ai_sl_price) + extra)

        old_oid = self._hard_sl_oid
        try:
            oid = self._place_trigger("sl", new_hard_stop)
            if old_oid:
                self._cancel_order(old_oid)
            self._hard_sl_price = new_hard_stop
            self._hard_sl_oid = oid
            logger.info("[执行] 硬止损刷新完成 | price=%.2f | oid=%s", new_hard_stop, oid)
            return new_hard_stop
        except ExecutionError as e:
            send_alert(f"本地硬止损刷新失败，保留旧硬止损: {e}")
            return None

    def _rollback_after_failed_protection(self):
        """开仓成交但风控单挂载失败时，优先市价回滚；回滚未确认则保留现有风控单记账。"""
        rollback_ok = False
        try:
            resp = self._exchange.market_close(self.coin)
            rollback_ok = self._status_ok(resp)
            if not rollback_ok:
                send_alert("回滚平仓未获确认，存在裸奔持仓，需人工立即介入！")
            else:
                logger.warning("[执行] 回滚平仓完成 | reason=保护单挂载失败")
        except Exception as e:
            send_alert(f"回滚平仓异常，存在裸奔持仓，需人工立即介入: {e}")
        if not rollback_ok:
            logger.error("[执行] 回滚未确认 | action=保留仓位与风控单记账 | 等待对账接管")
            return
        try:
            self._cancel_all_orders()
        except Exception as e:
            logger.error("[执行] 回滚撤单失败 | err=%s", e)
        finally:
            self._position_size = None
            self._position_direction = None
            self._clear_protection_tracking()

    # ------------------------------------------------------------------
    # 开仓 / 平仓 / 移损
    # ------------------------------------------------------------------

    def set_coin(self, coin: str):
        self.coin = coin

    def market_open(self, direction: str, sz_notional: float, stop_loss: float,
                    take_profit: Optional[float] = None,
                    hard_stop_distance: Optional[float] = None) -> Optional[dict]:
        """
        市价开仓名义金额 sz_notional，并同步挂载风控单 (主止损 + 本地硬止损 + 止盈)。

        杠杆按 ceil(sz_notional / equity) 自动设置并夹紧到 [LEVERAGE_MIN, LEVERAGE_MAX]，
        使 3x equity 内的名义敞口均可被保证金覆盖。任一主止损挂载失败即回滚平仓。

        hard_stop_distance 为硬止损相对主止损的额外宽度 (默认 0.2xATR)，
        挂在主止损更外侧作为断网兜底。
        """
        if not self.ready:
            self._fail("下单通道未就绪，拒绝市价开仓。")
            return None
        if (
            sz_notional is None
            or not math.isfinite(float(sz_notional))
            or float(sz_notional) <= 0
        ):
            self._fail(f"非法开仓名义金额: {sz_notional}，拒绝下单。")
            return None
        if (
            stop_loss is None
            or not math.isfinite(float(stop_loss))
            or float(stop_loss) <= 0
        ):
            self._fail(f"非法止损价格: {stop_loss}，拒绝下单。")
            return None
        try:
            is_buy = direction == "LONG"
            equity = self.account_value()
            if equity <= 0:
                raise ExecutionError(f"账户权益({equity})非法，拒绝下单。")

            # 杠杆自适应：确保名义敞口 <= equity * 杠杆
            target_lev = max(1.0, math.ceil(float(sz_notional) / equity))
            self._set_leverage(target_lev)

            mark_px = self.mark_price()
            if mark_px <= 0:
                raise ExecutionError(f"标记价({mark_px})非法，拒绝下单。")
            sz = self._round_sz(float(sz_notional) / mark_px)
            if sz <= 0:
                raise ExecutionError(
                    f"下单数量取整后为 0 (notional={sz_notional}, px={mark_px})，拒绝下单。")

            resp = self._exchange.market_open(self.coin, is_buy, sz, None, self.slippage)
            if not self._status_ok(resp):
                self._fail(f"市价开仓未获确认: {resp}")
                return None

            filled, avg_px = self._extract_filled(resp)
            if filled is None:
                # 逐单 error / 无成交回报: 视为未确认，绝不推进状态机 (fail-closed)
                self._fail(f"市价开仓未获成交确认 (可能被逐单拒收): {resp}")
                return None
            self._position_size = filled
            self._position_direction = direction
            logger.info("[执行] 市价开仓成交 | sz=%s | avg_px=%s", filled, avg_px)

            # 本地硬止损：比主止损再宽 hard_stop_distance (默认 0.2xATR)，避免正常滑点先打掉兜底单
            hard_sl = None
            if hard_stop_distance and hard_stop_distance > 0 and stop_loss and float(stop_loss) > 0:
                sl_px = float(stop_loss)
                hard_sl = sl_px - float(hard_stop_distance) if is_buy else sl_px + float(hard_stop_distance)

            try:
                self._place_protective_stops(stop_loss, hard_sl, take_profit)
            except ExecutionError as e:
                self._fail(f"风控单挂载失败，回滚刚开的仓位: {e}")
                self._rollback_after_failed_protection()
                raise

            return resp
        except ExecutionError:
            raise
        except Exception as e:
            raise ExecutionError(f"市价开仓异常: {e}")

    def close_all(self) -> Optional[dict]:
        """先市价全平，确认成交后再清残留挂单。返回确认信息；未确认返回 None。"""
        if not self.ready:
            self._fail("平仓通道未就绪，拒绝市价全平。")
            return None
        try:
            resp = self._exchange.market_close(self.coin)
            if not self._status_ok(resp):
                self._fail(f"市价全平未获确认: {resp}")
                return None
            try:
                self._cancel_all_orders()
            except Exception as e:
                logger.warning("[执行] 平仓后清理残留挂单失败 | err=%s", e)
            self._position_size = None
            self._position_direction = None
            self._clear_protection_tracking()
            logger.info("[执行] 市价全平确认 | resp=%s", resp)
            return resp
        except ExecutionError:
            raise
        except Exception as e:
            raise ExecutionError(f"市价全平异常: {e}")

    def tighten_stop(self, new_stop_loss: float, new_take_profit: Optional[float] = None,
                     min_interval_seconds: float = 10.0) -> Optional[dict]:
        """
        先挂新主止损，再撤旧主止损 (FSM 保本/追踪移损调用)。
        具备：
        1. 严格单向棘轮校验 (做多绝不向下放宽，做空绝不向上放宽)；
        2. 市价击穿校验 (新止损绝不能已被当前市价穿透，防止挂出立即成交)；
        3. 节流与状态强一致。
        """
        if not self.ready:
            self._fail("止损通道未就绪，拒绝移损。")
            return None
        if new_stop_loss is None or not math.isfinite(float(new_stop_loss)) or float(new_stop_loss) <= 0:
            self._fail(f"非法的新止损价: {new_stop_loss}")
            return None

        now_mono = time.monotonic()
        if (now_mono - getattr(self, "_last_tighten_time", 0.0)) < min_interval_seconds:
            logger.debug("[执行] 移损调用过于频繁 (距上次 < %.1fs) | 拒绝本次API修改", min_interval_seconds)
            return None

        try:
            szi, _ = self.current_position()
            if szi is None or abs(szi) <= 0:
                self._fail("当前无持仓，拒绝移损。")
                return None
            self._position_size = abs(szi)
            direction = "LONG" if szi > 0 else "SHORT"
            self._position_direction = direction

            new_sl = float(new_stop_loss)
            old_sl_oid = self._ai_sl_oid
            old_sl_price = self._ai_sl_price

            # 1. 严格单向棘轮防御 (防止策略计算波动导致止损不利移动)
            if old_sl_price is not None and math.isfinite(old_sl_price):
                if direction == "LONG" and new_sl < old_sl_price - 1e-6:
                    logger.warning("[执行] 做多移损违背单向棘轮 | new_sl=%.2f < old_sl=%.2f | 拒绝放宽", new_sl, old_sl_price)
                    return None
                elif direction == "SHORT" and new_sl > old_sl_price + 1e-6:
                    logger.warning("[执行] 做空移损违背单向棘轮 | new_sl=%.2f > old_sl=%.2f | 拒绝放宽", new_sl, old_sl_price)
                    return None

            # 2. 市价穿透防御 (防止新止损挂在已被击穿的价格上导致立即市价成交)
            try:
                mark_px = self.mark_price()
                if direction == "LONG" and new_sl >= mark_px:
                    logger.warning("[执行] 新止损已被现价击穿 | new_sl=%.2f >= mark=%.2f | action=跳过移损挂单", new_sl, mark_px)
                    return None
                elif direction == "SHORT" and new_sl <= mark_px:
                    logger.warning("[执行] 新止损已被现价击穿 | new_sl=%.2f <= mark=%.2f | action=跳过移损挂单", new_sl, mark_px)
                    return None
            except Exception as pe:
                logger.warning("[执行] 移损校验标记价异常 | err=%s | 保守继续挂单", pe)

            # 先挂载新 SL 触发单，成功后再撤销旧 SL 单，彻底杜绝裸奔窗口
            new_sl_oid = self._place_trigger("sl", new_sl)
            if old_sl_oid:
                try:
                    self._cancel_order(old_sl_oid)
                except Exception as ce:
                    logger.warning("[执行] 撤销旧止损单失败 (已挂新止损单) | oid=%s | err=%s", old_sl_oid, ce)
            self._ai_sl_price = new_sl
            self._ai_sl_oid = new_sl_oid
            self._last_tighten_time = time.monotonic()

            if new_take_profit and float(new_take_profit) > 0 and math.isfinite(float(new_take_profit)):
                new_tp = float(new_take_profit)
                if self._tp_price is None or abs(new_tp - float(self._tp_price)) / max(abs(self._tp_price), 1e-9) > 1e-6:
                    old_tp_oid = self._tp_oid
                    try:
                        new_tp_oid = self._place_trigger("tp", new_tp)
                        if old_tp_oid:
                            self._cancel_order(old_tp_oid)
                        self._tp_price = new_tp
                        self._tp_oid = new_tp_oid
                    except ExecutionError as e:
                        logger.warning("[执行] 移损更新止盈失败 | action=保留旧止盈 | err=%s", e)

            logger.info("[执行] 止损收紧完成 | SL=%s | TP=%s | 硬止损=%s",
                        new_stop_loss, self._tp_price, self._hard_sl_price)
            return {"sl": new_sl_oid, "hard": self._hard_sl_oid, "tp": self._tp_oid}
        except ExecutionError as e:
            self._fail(f"挂载新止损失败，保留原止损单: {e}")
            return None
        except Exception as e:
            logger.error("[执行] 移损发生非预期异常 | err=%s", e, exc_info=True)
            raise

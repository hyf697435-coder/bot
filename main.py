# -*- coding: utf-8 -*-
"""
加密货币量化与轻量 AI 混合交易系统 —— 主控制循环 (Pipeline Workflow)

架构：确定性归代码，模糊性归 AI。
  - 趋势量化 (TRS) / 信号打分 / 仓位计算 / 持仓 FSM：100% 本地 Python；
  - 仅两个 LLM 关口：低频 AI_macro (带单向护栏) + 高门槛 AI_Trade (SS >= 70)；
  - AIb / AIc / AId 已删除：本地评分 < 70 直接丢弃、本地 FSM 监控持仓、
    持仓期连续异常改为交易所侧保护单兜底 + 告警邮件。

状态机 (沿用旧系统防御体系)：
    IDLE      空仓巡逻，轻量短轮询（对账），信号仅在 5m K 线收盘 +2s 节点触发；
    TRADING   持仓期，1s 轮询标记价跑本地 FSM，双止损按 15min 刷新；
    COOL_DOWN 连续 3 次实质亏损 (含手续费滑点) 休眠 12 小时；
    SYSTEM_HALT  AI_macro 黑天鹅特例，全局挂起等待人工复核。

入口: python main.py
"""

__version__ = "1.0.0"
__milestone__ = "v1.0-RELEASE"
__release_date__ = "2026-11-01"


import json
import math
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Optional, Tuple

import numpy as np

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

import utils
from alerts import logger, send_alert, notifier
from exchange import ExecutionError, HyperliquidExecutor, MarketData, MarketDataError
from strategy import (
    AIClient,
    MacroState,
    MACRO_SCHEDULE_HOURS_UTC,
    Position,
    SIGNAL_GATE,
    apply_trend_guardrail,
    build_trade_payload,
    calculate_position_size,
    calculate_quant_trs,
    calculate_signal_score,
    check_macro_event_triggers,
    evaluate_trade_execution,
    fetch_ai_macro_state,
    format_signal_report,
    map_trs_to_state,
    regime_sl_cap,
    update_position_fsm,
    validate_and_adjust_tp_sl,
)

# ==========================================
# 运维配置 (环境变量)
# ==========================================

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


HYPE_ENV = os.getenv("HYPE_ENV", "testnet").lower()
COIN = os.getenv("HYPE_COIN", "ETH").strip()

IDLE_POLL_SECONDS = _env_float("IDLE_POLL_SECONDS", 5)          # 空仓期轻量轮询节拍 (对账)
SIGNAL_CANDLE_SECONDS = _env_int("SIGNAL_CANDLE_SECONDS", 300)  # 信号评估基准周期 (秒, 默认 5m)
SIGNAL_ALIGN_SECONDS = _env_float("SIGNAL_ALIGN_SECONDS", 2)    # 对齐到 K 线收盘后第 N 秒
TRADING_POLL_SECONDS = _env_float("TRADING_POLL_SECONDS", 1)  # 持仓期 FSM 轮询节拍 (1s)
ATR_REFRESH_SECONDS = _env_float("ATR_REFRESH_SECONDS", 60)   # 持仓期 ATR/K线缓存刷新间隔
HARD_STOP_REFRESH_SECONDS = _env_int("HARD_STOP_REFRESH_SECONDS", 900)  # 双止损硬止损刷新 (15min)
FR_WINDOW_HOURS = _env_int("FR_WINDOW_HOURS", 168)            # 资金费率统计窗口 (7 天)

# 分级轮询: 空仓期对账可低频，持仓期价格高频、仓位对账中频 (Q4)
IDLE_RECONCILE_SECONDS = _env_float("IDLE_RECONCILE_SECONDS", 10)        # 空仓期 user_state 对账节流
TRADING_RECONCILE_SECONDS = _env_float("TRADING_RECONCILE_SECONDS", 5)   # 持仓期 user_state 对账节流
# 全局 socket 兜底超时: 防止 hyperliquid SDK 等无显式 timeout 的请求无限阻塞主循环 (0=禁用)
NETWORK_TIMEOUT_SECONDS = _env_float("NETWORK_TIMEOUT_SECONDS", 20)
# 空仓期存活心跳间隔 (秒): 用于暴露主循环被挂起/系统休眠导致的信号节点整段跳过 (0=禁用)
HEARTBEAT_SECONDS = _env_float("HEARTBEAT_SECONDS", 60)

COOLDOWN_HOURS = _env_float("COOLDOWN_HOURS", 12)             # 连败休眠时长
CONSECUTIVE_LOSS_LIMIT = 3                                    # 连败熔断阈值
IDLE_ALERT_THRESHOLD = _env_int("IDLE_ALERT_THRESHOLD", 5)    # 空仓期连续异常告警阈值
TRADING_ALERT_THRESHOLD = _env_int("TRADING_ALERT_THRESHOLD", 2)  # 持仓期连续异常保护阈值
CLOSE_FAIL_ALERT_THRESHOLD = _env_int("CLOSE_FAIL_ALERT_THRESHOLD", 5)  # 平仓连续失败保护阈值
MAX_LOOPS = _env_int("MAX_LOOPS", 0)                          # 0 表示无限循环，>0 便于联调
MACRO_EVENT_COOLDOWN_SECONDS = _env_float("MACRO_EVENT_COOLDOWN_SECONDS", 900)
HARD_STOP_ATR_BUFFER = _env_float("HARD_STOP_ATR_BUFFER", 0.2)
STATE_FILE = os.getenv("STATE_FILE", "state.json").strip() or "state.json"

# 交易成本估算 (开平双边手续费 + 滑点)，用于连败判定不漏掉"暗亏"
TRADING_FEE_RATE = _env_float("TRADING_FEE_RATE", 0.001)

# 状态常量
STATE_IDLE = "IDLE"
STATE_TRADING = "TRADING"
STATE_COOL_DOWN = "COOL_DOWN"
STATE_HALT = "SYSTEM_HALT"


def signal_node_index(now_ts: float) -> int:
    """返回当前时刻所处的"K 线收盘 + 对齐秒"节点序号。

    节点固定落在 00:00:02 / 00:05:02 / 00:10:02 ... (默认 5m 周期 +2s 缓冲)，
    留缓冲以保证交易所的 5m K 线已 finalized。节点序号变化即为该跑一轮信号 Pipeline 的时机。
    """
    period = max(int(SIGNAL_CANDLE_SECONDS), 1)
    return int((now_ts - SIGNAL_ALIGN_SECONDS) // period)


def seconds_until_next_signal_node(now_ts: float) -> float:
    """返回距离下一个"K 线收盘 + 对齐秒"节点的剩余秒数，取值区间 (0, period]。

    用于让空仓主循环的休眠精确截断到节点边界：无论 IDLE_POLL_SECONDS 配置多大，
    信号 Pipeline 都能在节点处准点触发，而不会因轮询相位漂移而延迟。
    """
    period = max(int(SIGNAL_CANDLE_SECONDS), 1)
    offset = (now_ts - SIGNAL_ALIGN_SECONDS) % period
    remaining = period - offset
    return remaining if remaining > 0 else float(period)


class TradingBot:
    """主控制器：Pipeline 编排 + 四状态防御体系。"""

    def __init__(self):
        self.coin = COIN
        self.market = MarketData(coin=self.coin)
        self.exchange = HyperliquidExecutor()
        self.ai = AIClient()
        from multi_coin_router import MultiCoinRouter
        from risk_manager import RiskManager
        self.router = MultiCoinRouter(config_path="config_aggressive.json")
        self.risk_mgr = RiskManager(persistence_file="risk_state_aggressive.json")
        self._preheat_correlations()
        self.initial_equity: float = 1000.0
        self.peak_equity: float = 1000.0
        self.daily_start_equity: float = 1000.0
        self.daily_loss_pnl: float = 0.0
        self.daily_loss_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # --- 运行态 ---
        self.state = STATE_IDLE
        self.quant_state: MacroState = MacroState.NEUTRAL
        self.final_macro_state: MacroState = MacroState.NEUTRAL
        self.last_trs: float = 0.0

        # --- 宏观巡检游标：8h 槽位 (date, hour) + 变盘冷却 + AI 护栏缓存 ---
        self.last_macro_slot: Optional[tuple] = None
        self.cached_ai_macro_state: Optional[MacroState] = None
        self.latest_news_impact: str = ""
        self.latest_sentiment_data: dict = {}
        self.last_event_macro_time: Optional[datetime] = None
        self.last_evaluated_candle_time: Optional[int] = None
        self.last_signal_node: int = -(10 ** 12)            # 已处理的信号节点序号 (5m+2s)

        # --- 连败熔断 ---
        self.consecutive_failures = 0
        self.cooldown_until: Optional[datetime] = None
        self.last_closed_pnl: float = 0.0

        # --- 持仓记账 ---
        self.position: Optional[Position] = None
        self.position_entry_price: Optional[float] = None   # 结算用 (保留成交均价)
        self.position_direction: Optional[str] = None
        self.open_regime_cap: float = 1.5                   # 开仓时锁定的硬止损 ATR 倍率上限
        self.last_hard_stop_refresh: Optional[datetime] = None
        self.last_synced_sl: Optional[float] = None         # FSM SL -> 交易所同步游标
        self.last_5m_ts: Optional[int] = None               # bars_held 推进游标 (5m K 线边界)
        self.cached_atr_5m: float = 0.0                     # 持仓期 ATR 缓存
        self.last_atr_refresh: Optional[datetime] = None
        self.close_fail_count = 0

        # --- 异常计数 ---
        self.api_error_count = 0
        self.idle_api_error_count = 0
        self.idle_backoff = 60

        # --- 分级轮询节流与日志降噪游标 ---
        self.last_idle_reconcile: Optional[datetime] = None
        self.last_trading_reconcile: Optional[datetime] = None
        self.last_logged_macro_state: Optional[MacroState] = None
        self.last_macro_log_candle: Optional[int] = None
        self.last_node_candle_time: Optional[int] = None      # 上一信号节点的 5m K 线时间戳 (滚动校验)
        self._last_ss_log_candle: Optional[int] = None
        self.last_idle_cycle_seconds: float = 0.0
        self.last_iter_utc: Optional[datetime] = None        # 上一轮主循环起点 (停顿检测)
        self.last_heartbeat: Optional[datetime] = None       # 空仓期存活心跳游标
        self._recent_node_triggers: Deque[Tuple[int, float]] = deque(maxlen=12)

        self.radar_scores = {}
        self.coin_prices = {}
        self._active_candidate_signal = None
        self._last_evaluated_coin = None
        self.last_ai_trade_call_ts: float = 0.0              # AI_Trade 全局限频时间戳 (15min 冷却保护)
        self.ai_trade_cooldown_seconds: float = 900.0        # AI_Trade 最小调用间隔 (15 分钟)

        self._load_state()

    # ==================================================================
    # 主循环
    # ==================================================================

    def _write_heartbeat(self, phase: str = ""):
        """写入原子心跳文件供 watchdog.py 与 dashboard.py 监控存活性与熔断指标"""
        try:
            hb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".heartbeat")
            tmp_hb = hb_path + ".tmp"
            payload = {
                "timestamp": time.time(),
                "time_utc": datetime.now(timezone.utc).isoformat(),
                "state": self.state,
                "phase": phase,
                "pid": os.getpid(),
                "coin": self.coin,
                "has_position": self.position is not None,
                "consecutive_failures": self.consecutive_failures,
                "peak_equity": self.peak_equity,
                "daily_loss_pnl": self.daily_loss_pnl,
            }
            with open(tmp_hb, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp_hb, hb_path)
        except Exception as e:
            logger.debug("[心跳] 写入失败: %s", e)

    def _safe_sleep(self, seconds: float, phase: str = "SLEEP", step: float = 5.0):
        """受控分片休眠函数：替代单次长 sleep，休眠期间主线程持续刷新心跳，彻底防范看门狗误杀"""
        end_ts = time.monotonic() + max(0.0, float(seconds))
        while True:
            self._write_heartbeat(phase=phase)
            self._check_control_commands()
            remaining = end_ts - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(step, remaining))

    def _check_control_commands(self):
        """消费 dashboard.py 下发的 control.json 控制指令（暂停/恢复/紧急全平）"""
        ctrl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control.json")
        if not os.path.exists(ctrl_path):
            return
        cmd = None
        try:
            with open(ctrl_path, "r", encoding="utf-8") as f:
                cmd = json.load(f)
            try:
                os.remove(ctrl_path)
            except Exception:
                pass
        except Exception as e:
            logger.warning("[控制] 解析 control.json 失败: %s", e)
            return

        if not cmd or not isinstance(cmd, dict):
            return

        try:
            action = str(cmd.get("action", "")).lower()
            if action == "emergency_close":
                logger.warning("[控制] 收到看板紧急全平指令 | action=立即市价平仓")
                if self.position is not None:
                    try:
                        px = self.exchange.mark_price()
                    except Exception as e:
                        logger.warning("[控制] 紧急全平拉取标记价失败 | action=使用持仓参考价 | err=%s", e)
                        px = getattr(self.position, "peak_price", None) or getattr(self.position, "entry_price", None) or 0.0
                    self._execute_market_close("看板紧急全平", px, datetime.now(timezone.utc))
            elif action == "pause" and self.state != STATE_HALT:
                self._enter_system_halt("收到看板手动暂停指令")
            elif action == "resume" and self.state == STATE_HALT:
                logger.info("[控制] 收到看板恢复交易指令 | state=IDLE")
                self.state = STATE_IDLE
                self.cached_ai_macro_state = None
                self._persist_state()
            elif action == "refresh_macro":
                logger.info("[控制] 收到看板刷新宏观指令 | action=清除AI宏观缓存并立即触发评估")
                self.cached_ai_macro_state = None
                self.last_signal_node = -(10 ** 12)
                self._persist_state()
        except Exception as e:
            logger.warning("[控制] 执行 control.json 指令失败: %s", e)

    def _preheat_correlations(self):
        """启动阶段一次性预热各币种 1h 历史价格，计算初始相关性矩阵 (避免 5m+2s 临界区高频 I/O)"""
        if not hasattr(self, "router") or not self.router:
            return
        if not self.exchange.ready:
            logger.debug("[启动] 执行通道未就绪，跳过多币种网络预热")
            return
        logger.info("[启动] 预热多币种相关性追踪器 | coins=%s", self.router.coins)
        for c in self.router.coins:
            try:
                mkt = self.router.market_clients.get(c) or MarketData(coin=c)
                df = mkt.get_ohlcv("1h", 200)
                if df is not None and not df.empty and "ts" in df.columns:
                    self.router.corr_tracker.seed_from_df(c, df)
            except Exception as e:
                logger.warning(f"[启动] 预热 {c} 相关性异常: {e}")

    def run(self):
        logger.info("[启动] 交易机器人上线 | env=%s | coin=%s", HYPE_ENV, COIN)
        logger.info("[启动] 通道状态 | 执行层=%s | AI通道=%s",
                    "就绪" if self.exchange.ready else "禁用", self.ai.available())
        logger.info("[启动] 节拍配置 | IDLE_POLL=%ss | SIGNAL_CANDLE=%ss | SIGNAL_ALIGN=%ss "
                    "| IDLE_RECONCILE=%ss | TRADING_POLL=%ss | HEARTBEAT=%ss",
                    IDLE_POLL_SECONDS, SIGNAL_CANDLE_SECONDS, SIGNAL_ALIGN_SECONDS,
                    IDLE_RECONCILE_SECONDS, TRADING_POLL_SECONDS, HEARTBEAT_SECONDS)
        if IDLE_POLL_SECONDS >= SIGNAL_CANDLE_SECONDS:
            logger.warning(
                "[启动] 节拍配置异常 | IDLE_POLL_SECONDS=%.0fs >= SIGNAL_CANDLE_SECONDS=%ds "
                "| reason=轮询周期不小于信号周期, 将退化为每周期仅一次采样 "
                "| action=建议 IDLE_POLL_SECONDS=5",
                IDLE_POLL_SECONDS, SIGNAL_CANDLE_SECONDS)
        logger.info("[启动] 行情缓存配置 | CACHE_TTL_5M=%.1fs | CACHE_TTL_1H=%.1fs "
                    "| CACHE_TTL_4H=%.1fs | CACHE_TTL_FR=%.1fs",
                    self.market._cache_ttl.get("5m", 0.0),
                    self.market._cache_ttl.get("1h", 0.0),
                    self.market._cache_ttl.get("4h", 0.0),
                    self.market._fr_ttl)
        logger.info("[启动] 关键环境 | NETWORK_TIMEOUT=%ss | ATR_REFRESH=%ss "
                    "| HARD_STOP_REFRESH=%ss | TRADING_RECONCILE=%ss "
                    "| MACRO_EVENT_COOLDOWN=%ss | STATE_FILE=%s",
                    NETWORK_TIMEOUT_SECONDS, ATR_REFRESH_SECONDS,
                    HARD_STOP_REFRESH_SECONDS, TRADING_RECONCILE_SECONDS,
                    MACRO_EVENT_COOLDOWN_SECONDS, STATE_FILE)
        if not self.exchange.ready:
            logger.warning("[启动] 执行层未就绪 | 开平仓=fail-closed | action=检查 .env")
        if not self.ai.available():
            logger.warning("[启动] AI通道未配置 | reason=THIRD_PARTY_API_KEY 为空 | action=检查 .env")
        if self.state == STATE_HALT:
            self._enter_system_halt("启动时恢复 SYSTEM_HALT，强制清空持仓后挂起")
        else:
            self._reconcile_exchange_position()

        loop_count = 0
        while True:
            loop_count += 1
            self._write_heartbeat()
            self._check_control_commands()
            if MAX_LOOPS and loop_count > MAX_LOOPS:
                logger.info("[停止] 达到最大轮数 | MAX_LOOPS=%s | action=正常退出", MAX_LOOPS)
                return
            try:
                now_utc = datetime.now(timezone.utc)
                # 非空仓态不参与停顿检测，避免持仓/休眠/挂起期的正常长间隔误报
                if self.state != STATE_IDLE:
                    self.last_iter_utc = None

                # --- 连败休眠期 ---
                if self.state == STATE_COOL_DOWN:
                    if self.cooldown_until is None or now_utc >= self.cooldown_until:
                        logger.info("[恢复] 连败休眠结束 | state=IDLE | 连败计数=0")
                        self.state = STATE_IDLE
                        self.consecutive_failures = 0
                        self.cooldown_until = None
                        if hasattr(self, "risk_mgr") and self.risk_mgr:
                            self.risk_mgr.reset_streak_after_cooldown()
                        self._persist_state()
                    else:
                        remain_sec = (self.cooldown_until - now_utc).total_seconds()
                        local_wake = self.cooldown_until.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                        logger.info("[熔断] 连败休眠中 | 剩余=%.0fs | 预计唤醒(本地)=%s",
                                    remain_sec, local_wake)
                        self._safe_sleep(min(300.0, max(5.0, remain_sec)), phase="COOL_DOWN")
                        continue

                # --- SYSTEM_HALT 挂起期 (黑天鹅 / 人工复核) ---
                if self.state == STATE_HALT:
                    logger.info("[挂起] SYSTEM_HALT 生效中 | 交易=停止 | action=重启程序解除 | 需人工复核")
                    self._safe_sleep(300, phase="SYSTEM_HALT")
                    continue

                if self.state == STATE_TRADING:
                    self._trading_cycle()
                    continue

                if self.state == STATE_IDLE:
                    # 主循环停顿检测: 相邻两轮 IDLE 巡检间隔远超轮询节拍，说明进程被挂起
                    # (系统休眠 / 终端暂停 / CPU 饥饿)，期间信号节点会被整段跳过。
                    if self.last_iter_utc is not None:
                        stall = (now_utc - self.last_iter_utc).total_seconds()
                        stall_limit = max(IDLE_POLL_SECONDS * 6, 120)
                        if stall > stall_limit:
                            logger.warning(
                                "[巡检] 主循环停顿 | 距上轮=%.1fs | 阈值=%.0fs "
                                "| 疑似进程挂起/系统休眠/终端暂停 | action=检查电源与终端设置",
                                stall, stall_limit)
                    self.last_iter_utc = now_utc

                    cycle_started = time.monotonic()
                    self._idle_cycle()
                    self.last_idle_cycle_seconds = time.monotonic() - cycle_started
                    if self.last_idle_cycle_seconds > max(IDLE_POLL_SECONDS * 3, 30):
                        logger.warning(
                            "[巡检] IDLE Pipeline 耗时过长 | 实际=%.1fs "
                            "| 疑似网络阻塞 | action=检查行情/SDK 通道",
                            self.last_idle_cycle_seconds)
                    # 空仓期存活心跳: 即使节点门控跳过，也定期落一条，便于从日志空档反推进程停顿
                    if HEARTBEAT_SECONDS > 0:
                        now_hb = datetime.now(timezone.utc)
                        if self.last_heartbeat is None or \
                                (now_hb - self.last_heartbeat).total_seconds() >= HEARTBEAT_SECONDS:
                            self.last_heartbeat = now_hb
                            logger.info(
                                "[心跳] IDLE 存活 | node=%s | last_node=%s | 上轮耗时=%.1fs",
                                signal_node_index(now_hb.timestamp()),
                                self.last_signal_node, self.last_idle_cycle_seconds)
                    # 空仓期保持轻量短轮询: 对账高频、信号按 5m+2s 节点触发 (见 _idle_cycle)。
                    # 休眠截断到下一个节点边界, 避免轮询相位漂移导致触发时间偏离 K 线收盘。
                    if self.state == STATE_IDLE:
                        now_ts = datetime.now(timezone.utc).timestamp()
                        wait = min(IDLE_POLL_SECONDS, seconds_until_next_signal_node(now_ts))
                        self._safe_sleep(max(wait, 0.05), phase="IDLE_POLL", step=1.0)
                    continue

                # 兜底：未知状态，回到 IDLE
                logger.warning("[恢复] 未知状态 | state=%s | action=重置为IDLE", self.state)
                self.state = STATE_IDLE
                self._safe_sleep(5, phase="RECOVER_IDLE", step=1.0)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                self._handle_loop_exception(e)

    # ==================================================================
    # 空仓巡逻 Pipeline (IDLE)
    # ==================================================================

    def _idle_cycle(self):
        now_utc = datetime.now(timezone.utc)
        now_ts = now_utc.timestamp()

        # 1. 高频/定时对账: 每 IDLE_RECONCILE_SECONDS 查一次交易所持仓, 发现遗留仓位立即挂载 FSM
        if self.last_idle_reconcile is None or \
                (now_utc - self.last_idle_reconcile).total_seconds() >= IDLE_RECONCILE_SECONDS:
            self.last_idle_reconcile = now_utc
            if self._reconcile_exchange_position():
                return

        # 2. 信号节点门控: 仅当跨过新的 "5m K 线收盘 +2s" 节点时才跑完整信号 Pipeline,
        #    其余轮询只做上面的轻量对账, 避免 5s 高频空转拉行情 / 调用 AI。
        node = signal_node_index(now_ts)
        if node == self.last_signal_node:
            return
        if self.last_signal_node > -(10 ** 11):
            missed = node - self.last_signal_node - 1
            if missed > 0:
                logger.warning(
                    "[巡检] 信号节点跳过 | 上次=%s | 当前=%s | 跳过=%s 个节点 "
                    "| 疑似主循环被阻塞/系统休眠/时钟跳变",
                    self.last_signal_node, node, missed)
        prev_signal_node = self.last_signal_node
        self.last_signal_node = node
        if self._recent_node_triggers:
            prev_node, prev_ts = self._recent_node_triggers[-1]
            node_diff = node - prev_node
            gap = now_ts - prev_ts
            delay_limit = 1.5 * max(int(SIGNAL_CANDLE_SECONDS), 1)
            if node_diff == 1 and gap > delay_limit:
                logger.warning(
                    "[巡检] 节点延迟/漂移 | 上次=%s | 当前=%s | 间隔=%.1fs | 阈值=%.1fs "
                    "| action=检查主循环阻塞与休眠截断",
                    prev_node, node, gap, delay_limit)
        self._recent_node_triggers.append((node, now_ts))
        self._write_heartbeat(phase="FETCH_MARKET")

        # 3. 刷新行情数据 (5m, 1h, 4h, Funding Rate)
        #    5m K 线是信号评估的物理基准, 必须绕过本地缓存并使用 get_closed_ohlcv 进行时间戳严格闭合判定
        df_5m, price = self.market.get_closed_ohlcv("5m", 120, use_cache=False)
        df_1h = self.market.get_ohlcv("1h", 500)
        df_4h = self.market.get_ohlcv("4h", 500)
        fr_stats = self.market.get_funding_stats(FR_WINDOW_HOURS)

        if df_5m is None or df_5m.empty or "ts" not in df_5m.columns or len(df_5m) < 15:
            logger.warning("[巡检] 5m K 线数据不足 | action=跳过本轮并允许重试")
            self.last_signal_node = prev_signal_node
            return

        # 节点命中可观测性: 每个信号节点至少落一条 INFO, 并比对 5m K 线时间戳是否滚动
        current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors='coerce'))
        logger.info("[巡检] 命中信号节点 | node=%s | candle_ts=%s | prev_candle=%s "
                    "| action=执行信号 Pipeline",
                    node, current_candle_time, self.last_node_candle_time)
        if self.last_node_candle_time is not None and \
                current_candle_time == self.last_node_candle_time:
            logger.warning("[巡检] 信号节点 K 线未滚动 | 当前=%s | 上一节点=%s "
                           "| 疑似行情缓存命中/交易所接口延迟 | action=检查 CACHE_TTL_5M",
                           current_candle_time, self.last_node_candle_time)
        self.last_node_candle_time = current_candle_time

        atr_calc = utils.atr(df_5m["high"], df_5m["low"], df_5m["close"], 14)
        atr_14 = float(atr_calc) if (atr_calc is not None and math.isfinite(atr_calc)) else 0.0
        if atr_14 <= 0:
            logger.warning("[巡检] 5m ATR 不可用 | action=跳过本轮并允许重试")
            self.last_signal_node = prev_signal_node
            return

        # 4. 宏观趋势计算与校验 (本地 TRS)
        self.last_trs = calculate_quant_trs(df_1h, df_4h, fr_stats, price)
        self.quant_state = map_trs_to_state(self.last_trs)

        # 5. 判定是否唤醒 AI_macro (冷启动基线 / 8h 定时 / 变盘事件，变盘至少 15 分钟冷却)
        event_trigger = check_macro_event_triggers(df_1h)
        event_cooled = (
            self.last_event_macro_time is None
            or (now_utc - self.last_event_macro_time).total_seconds() > MACRO_EVENT_COOLDOWN_SECONDS
        )
        # 冷启动基线: 无 AI_macro 缓存时立即调用一次 API 建立护栏基线，
        # 避免首次运行 (或 SYSTEM_HALT 重启清除缓存后) 在最长 8h 内完全没有 AI 护栏。
        cold_start = self.cached_ai_macro_state is None
        scheduled = self._is_macro_schedule(now_utc)
        should_call_macro = cold_start or scheduled or (event_trigger and event_cooled)
        if event_trigger and not event_cooled:
            logger.debug("[宏观] 变盘事件冷却中 | action=沿用缓存 AI_macro 护栏")

        if should_call_macro:
            self._write_heartbeat(phase="AI_MACRO")
            macro_context = self._build_macro_context(df_1h, df_4h, fr_stats, price, now_utc)
            trigger_kind = "冷启动基线" if cold_start else ("定时" if scheduled else "变盘事件")
            logger.info("[宏观] 唤醒 AI_macro | 触发=%s | 量化态=%s | TRS=%.3f",
                        trigger_kind, self.quant_state.value, self.last_trs)
            ai_res = fetch_ai_macro_state(macro_context, self.ai, return_details=True)
            if isinstance(ai_res, tuple):
                ai_state, details = ai_res
                if isinstance(details, dict) and details.get("news_impact_analysis"):
                    self.latest_news_impact = str(details["news_impact_analysis"])
            else:
                ai_state = ai_res

            # 校验 ai_state 必须属于有效的 MacroState 枚举类型
            if isinstance(ai_state, MacroState):
                if ai_state == MacroState.SYSTEM_HALT:
                    self.cached_ai_macro_state = ai_state
                    self._enter_system_halt("AI_macro 触发 SYSTEM_HALT")
                    return
                self.cached_ai_macro_state = ai_state
            elif isinstance(ai_state, str):
                try:
                    self.cached_ai_macro_state = MacroState(ai_state)
                except ValueError:
                    logger.warning("[宏观] AI_macro 返回未知宏观状态字符串: %s | 忽略更新", ai_state)
            else:
                if ai_state is not None:
                    logger.warning("[宏观] AI_macro 返回非法类型: %s | 忽略更新", type(ai_state))
            if scheduled:
                self.last_macro_slot = (now_utc.date(), now_utc.hour)
            if event_trigger:
                self.last_event_macro_time = now_utc
            self._persist_state()

        if self.cached_ai_macro_state is not None:
            self.final_macro_state = apply_trend_guardrail(self.quant_state, self.cached_ai_macro_state)
        else:
            self.final_macro_state = self.quant_state

        # 日志降噪: 仅在宏观态切换或进入新 5m K 线时才 INFO 全量输出;
        # 即使时间戳未变也保留一条 INFO 摘要, 确保节点不会在日志中彻底消失。
        if self.final_macro_state != self.last_logged_macro_state or \
                current_candle_time != self.last_macro_log_candle:
            logger.info("[宏观] 裁决 | 量化态=%s | AI缓存=%s | 最终=%s",
                        self.quant_state.value,
                        None if self.cached_ai_macro_state is None else self.cached_ai_macro_state.value,
                        self.final_macro_state.value)
            self.last_logged_macro_state = self.final_macro_state
            self.last_macro_log_candle = current_candle_time
        else:
            logger.info("[宏观] 裁决保持 | 最终=%s (K线时间戳 %s 未变，跳过重复全量输出)",
                        self.final_macro_state.value, current_candle_time)

        # 6. 多币种同步扫描与打分 (BTC, ETH, SUI, ARB)
        if hasattr(self, "router") and self.router:
            scan_res = self.router.scan_all_coins(
                calculate_signal_fn=calculate_signal_score,
                calculate_trs_fn=calculate_quant_trs,
                macro_state=self.final_macro_state
            )
            if scan_res and isinstance(scan_res, tuple) and len(scan_res) == 3:
                signals, all_radar_scores, all_prices = scan_res
                self.radar_scores = all_radar_scores or {}
                self.coin_prices = all_prices or {}
            else:
                signals = []
                logger.warning("[多币雷达] 扫描结果异常为空或格式不匹配 | action=跳过本轮")
        else:
            signal_res = calculate_signal_score(df_5m, df_1h, self.final_macro_state, fr_stats)
            cur_px = float(df_5m["close"].iloc[-1]) if not df_5m.empty else 0.0
            self.radar_scores = {
                self.coin: {
                    "score": round(signal_res.get("score", 0.0), 1),
                    "direction": signal_res.get("direction") or "NEUTRAL",
                    "price": cur_px
                }
            }
            self.coin_prices = {self.coin: cur_px}
            signals = []
            if signal_res.get("score", 0.0) >= SIGNAL_GATE:
                from multi_coin_router import CoinSignal
                signals.append(CoinSignal(
                    coin=self.coin,
                    score=signal_res.get("score", 0.0),
                    direction=signal_res.get("direction") or "NEUTRAL",
                    price=cur_px,
                    df_5m=df_5m,
                    df_1h=df_1h,
                    atr_14=atr_14,
                    raw_signal_result=signal_res
                ))

        if getattr(self, "_last_persisted_candle", None) != current_candle_time:
            self._last_persisted_candle = current_candle_time
            self.last_evaluated_candle_time = current_candle_time
            self._persist_state()

        # 7. 多币种候选信号门槛过滤与自动择优 (SS >= 70)
        if not signals:
            if current_candle_time != self._last_ss_log_candle:
                scores_summary = {c: s.get("score", 0.0) for c, s in self.radar_scores.items()}
                logger.info("[多币雷达] 多币扫描完成 | 全部无达标信号 (门槛=%.1f) | 各币打分=%s",
                            SIGNAL_GATE, scores_summary)
                self._last_ss_log_candle = current_candle_time
            else:
                logger.info("[多币雷达] 评估完成 | 状态=重复K线跳过打印 | node=%s", node)
            self._reset_idle_counters()
            return

        if hasattr(self, "router") and self.router:
            best_signal = self.router.select_best_coin(signals, benchmark_coin="BTC")
            if best_signal is None:
                logger.info("[多币雷达] 达标信号经相关性软惩罚后无胜出标的 | action=保持IDLE")
                self._reset_idle_counters()
                return
        else:
            best_signal = signals[0]

        self._active_candidate_signal = best_signal
        logger.info("[多币雷达] 胜出标的 | coin=%s | 原始分=%.1f | 修正分=%.1f | 方向=%s | 市价=%.4f",
                    best_signal.coin, best_signal.score, best_signal.ranking_score or best_signal.score,
                    best_signal.direction, best_signal.price)

        if self.coin != best_signal.coin:
            logger.info("[多币雷达] 动态切换交易币种 | %s -> %s", self.coin, best_signal.coin)
            self._switch_coin(best_signal.coin)

        signal_res = best_signal.raw_signal_result or calculate_signal_score(
            best_signal.df_5m if best_signal.df_5m is not None else df_5m,
            best_signal.df_1h if best_signal.df_1h is not None else df_1h,
            self.final_macro_state, fr_stats
        )
        price = float(best_signal.price)
        best_atr = getattr(best_signal, "atr_14", None)
        atr_14 = float(best_atr) if (best_atr is not None and not math.isnan(best_atr) and best_atr > 0) else atr_14
        df_5m = best_signal.df_5m if best_signal.df_5m is not None else df_5m
        df_1h = best_signal.df_1h if best_signal.df_1h is not None else df_1h

        logger.info("%s", format_signal_report(signal_res, SIGNAL_GATE, "TRIGGERED"))

        # [门禁强化 1: 本地硬代码门槛] 严格强制 SS >= 70.0，绝不让平庸信号透传给大模型浪费 Token
        candidate_score = float(signal_res.get("score", 0.0))
        if candidate_score < 70.0:
            logger.info("[门禁拦截] 胜出标的 %s 本地信号分 %.1f < 70.0 门槛 | action=本地直接丢弃，严禁调用 AI_Trade 浪费 Token",
                        self.coin, candidate_score)
            self._reset_idle_counters()
            return

        # [门禁强化 2: AI_Trade 15 分钟全局限频锁] 防止横盘杂波每 5 分钟反复轰炸大模型
        now_epoch = time.time()
        elapsed_since_last_ai = now_epoch - self.last_ai_trade_call_ts
        if elapsed_since_last_ai < self.ai_trade_cooldown_seconds:
            logger.info("[AI限频] 距离上次唤醒 AI_Trade 仅 %.1fs < 冷却阈值 %.0fs (15m) | action=跳过本次大模型审查",
                        elapsed_since_last_ai, self.ai_trade_cooldown_seconds)
            self._reset_idle_counters()
            return

        if self.last_evaluated_candle_time == current_candle_time and getattr(self, "_last_evaluated_coin", None) == self.coin:
            logger.info("[AI] 本根 5m K 线该币种已评估 | candle_ts=%s | coin=%s | node=%s | action=跳过重复调用",
                        current_candle_time, self.coin, node)
            return
        self.last_evaluated_candle_time = current_candle_time
        self._last_evaluated_coin = self.coin

        # 8. 唤醒 AI_Trade 进行关口审查 (形态/插针识别 + 最终拦截)
        self._write_heartbeat(phase="AI_TRADE")
        payload = build_trade_payload(
            signal_res, df_5m, df_1h, price, atr_14, self.final_macro_state.value)
        try:
            ai_response = evaluate_trade_execution(payload, self.ai)
        except Exception as e:
            logger.error("[AI] AI_Trade 调用异常: %s | action=放弃本轮开仓", e)
            self._reset_idle_counters()
            return

        if not ai_response or not isinstance(ai_response, dict):
            logger.warning("[AI] AI_Trade 返回无效响应 | action=放弃开仓")
            self._reset_idle_counters()
            return

        self.last_ai_trade_call_ts = now_epoch
        self._persist_state()

        if ai_response.get("decision") != "EXECUTE":
            logger.info("[AI] AI_Trade 拦截 | decision=%s | reason=%s",
                        ai_response.get("decision"), ai_response.get("reject_reason"))
            self._reset_idle_counters()
            return

        # 9. AI 决策校验与最终下单
        final_sl, final_tp, valid = validate_and_adjust_tp_sl(
            ai_response.get("stop_loss"), ai_response.get("take_profit"),
            signal_res.get("raw_sl"), price, signal_res.get("direction", "LONG"),
            local_take_profit=signal_res.get("raw_tp"))
        if not valid:
            self._reset_idle_counters()
            return

        # 10. 计算动态开仓名义金额 (注解 §9: sizing 在 AI 关口与二次校验之后)
        try:
            equity = self.exchange.account_value()
        except ExecutionError as e:
            logger.error("[仓位] 读取权益失败 | err=%s", e)
            self._reset_idle_counters()
            return
        if equity <= 0:
            logger.error("[仓位] 账户权益非法 | equity=%s", equity)
            self._reset_idle_counters()
            return

        # 第一步：调用 RiskManager 获取稳健名义仓位 (固定风险比例 Fixed Fractional + ATR真实止损距离反推)
        # 默认禁用非平稳小样本动态凯利公式 (use_kelly=False)，杜绝顺风激进定仓
        sl_distance = abs(price - final_sl)
        sz_notional = 0.0
        use_kelly_env = os.getenv("USE_KELLY", "false").lower() in ("true", "1", "yes")
        if hasattr(self, "risk_mgr") and self.risk_mgr:
            sz_notional = self.risk_mgr.get_adaptive_position_size(
                stop_loss_distance=sl_distance,
                current_price=price,
                equity=equity,
                use_kelly=use_kelly_env
            )

        # 严格遵守风控与最小名义开仓限制，若被风控归零或低于交易所最小门槛($10)，绝不强行兜底开仓
        min_notional = 10.0
        if sz_notional < min_notional:
            logger.info("[风控] 仓位规模(%.2f)低于最小开仓金额($%.0f)或被组合风控阻断 | action=放弃开仓",
                        sz_notional, min_notional)
            self._reset_idle_counters()
            return

        max_lev = getattr(self.risk_mgr.config, "max_leverage", 3.0) if hasattr(self, "risk_mgr") and self.risk_mgr else 3.0
        sz_notional = float(min(sz_notional, equity * max_lev))

        # 第二步：执行全域日亏与动态回撤硬熔断检查
        est_risk_usd = sz_notional * (sl_distance / price + 2.0 * TRADING_FEE_RATE)
        can_open, reject_msg = self.risk_mgr.can_open_position(
            est_risk_usd, equity=equity, coin=self.coin, reserve_slot=True
        )
        if not can_open:
            logger.warning("[风控] 熔断拦截放弃开仓 | reason=%s", reject_msg)
            self._reset_idle_counters()
            return

        logger.info("[仓位] 完成 | 样本=%d/%d | 最终名义=%.2f USDT (杠杆=%.2fx) | 止损距离=%.2f",
                    self.risk_mgr.metrics.total_trades, self.risk_mgr.config.min_kelly_trades,
                    sz_notional, sz_notional / max(equity, 1e-6), sl_distance)

        self._execute_open_order(signal_res["direction"], sz_notional, final_sl, final_tp, atr_14)
        if self.state == STATE_IDLE:
            self._reset_idle_counters()

    def _reset_idle_counters(self):
        """正常 IDLE 迭代结束时清零异常计数。"""
        self.api_error_count = 0
        self.idle_api_error_count = 0
        self.idle_backoff = 60

    def _is_macro_schedule(self, now_utc: datetime) -> bool:
        """UTC 00/08/16 定时槽位 (每槽位仅触发一次)。"""
        if now_utc.hour not in MACRO_SCHEDULE_HOURS_UTC:
            return False
        return (now_utc.date(), now_utc.hour) != self.last_macro_slot

    def _build_macro_context(self, df_1h, df_4h, fr_stats, price, now_utc) -> dict:
        """组装 AI_macro 上下文 (指南未定义字段，按宏观决策所需摘要实现)。"""
        ema200_1h = float(utils.ema_series(df_1h["close"], 200)[-1])
        ema200_4h = float(utils.ema_series(df_4h["close"], 200)[-1])
        d_upper, d_mid, d_lower = utils.donchian(df_1h["high"], df_1h["low"], 20)
        change_24h = 0.0
        if len(df_1h) >= 25:
            ref = float(df_1h["close"].iloc[-25])
            if ref > 0:
                change_24h = (price - ref) / ref * 100

        try:
            from sentiment_feed import get_market_sentiment
            sentiment_data = get_market_sentiment()
            self.latest_sentiment_data = sentiment_data
        except Exception as e:
            logger.warning("[宏观] 获取市场情绪切片失败: %s", e)
            sentiment_data = {
                "status": "error",
                "fear_and_greed": {"value": 50, "classification": "Neutral"},
                "top_headlines_24h": []
            }

        return {
            "date": str(now_utc.date()),
            "current_price": round(price, 6),
            "quant_state": self.quant_state.value,
            "quant_trs": round(self.last_trs, 4),
            "funding_rate_hourly": round(float(fr_stats.get("fr", 0.0)), 8),
            "funding_rate_8h_equiv": round(float(fr_stats.get("fr", 0.0)) * 8, 8),
            "fr_window_hours": FR_WINDOW_HOURS,
            "fr_mean": round(float(fr_stats.get("fr_mean", 0.0)), 8),
            "fr_std": round(float(fr_stats.get("fr_std", 0.0)), 8),
            "ema200_1h": round(ema200_1h, 6) if not np.isnan(ema200_1h) else None,
            "ema200_4h": round(ema200_4h, 6) if not np.isnan(ema200_4h) else None,
            "donchian_20_1h": {"upper": round(d_upper, 6) if d_upper else None,
                               "mid": round(d_mid, 6) if d_mid else None,
                               "lower": round(d_lower, 6) if d_lower else None},
            "change_24h_pct": round(change_24h, 3),
            "recent_realized_pnl": round(self.last_closed_pnl, 6),
            "consecutive_losses": self.consecutive_failures,
            "market_sentiment_and_news": {
                "fear_and_greed": sentiment_data.get("fear_and_greed", {}),
                "top_headlines_24h": sentiment_data.get("top_headlines_24h", [])[:6],
                "updated_at": sentiment_data.get("updated_at", ""),
            },
        }

    # ==================================================================
    # 开仓
    # ==================================================================

    def _execute_open_order(self, direction: str, sz_notional: float,
                            final_sl: float, final_tp: float, atr_14: float):
        """市价开仓 (fail-closed)：交易所确认成交后才推进 TRADING 状态。"""
        self._write_heartbeat(phase="PLACE_ORDER")
        # 本地硬止损距离 = 开仓时宏观基调 x ATR (双止损安全垫，Q4 确认保留)
        regime_cap = regime_sl_cap(self.final_macro_state)
        if regime_cap <= 0:
            logger.warning("[开仓] 拦截 | reason=宏观状态禁止交易 | 宏观态=%s", self.final_macro_state.value)
            if hasattr(self, 'risk_mgr') and self.risk_mgr:
                self.risk_mgr.release_position_slot(self.coin)
            return

        try:
            if hasattr(self.exchange, "maker_or_market_open"):
                resp = self.exchange.maker_or_market_open(
                    direction, sz_notional, final_sl, final_tp,
                    hard_stop_distance=atr_14 * HARD_STOP_ATR_BUFFER,
                    alo_wait_seconds=4.0)
            else:
                resp = self.exchange.market_open(
                    direction, sz_notional, final_sl, final_tp,
                    hard_stop_distance=atr_14 * HARD_STOP_ATR_BUFFER)
        except ExecutionError as e:
            logger.error("[开仓] 失败 | reason=执行层异常 | err=%s", e)
            send_alert(f"开仓执行异常: {e}")
            if hasattr(self, 'risk_mgr') and self.risk_mgr:
                self.risk_mgr.release_position_slot(self.coin)
            return

        if resp is None:
            logger.error("[开仓] 失败 | reason=交易所未确认成交 | action=保持空仓")
            if hasattr(self, 'risk_mgr') and self.risk_mgr:
                self.risk_mgr.release_position_slot(self.coin)
            return

        _, avg_px = HyperliquidExecutor._extract_filled(resp)
        entry = avg_px
        if entry is None or not math.isfinite(entry) or entry <= 0:
            try:
                entry = self.exchange.mark_price()
            except Exception:
                entry = None
        if entry is None or not math.isfinite(entry) or entry <= 0:
            try:
                _, cur_entry = self.exchange.current_position()
                if cur_entry and math.isfinite(cur_entry) and cur_entry > 0:
                    entry = float(cur_entry)
            except Exception:
                entry = None

        if entry is None or not math.isfinite(entry) or entry <= 0:
            logger.error("[开仓] 无法确定有效成交均价 | action=放弃盲目记账，发起对账接管")
            send_alert("开仓成交但均价无法确定，启动紧急对账接管！")
            self._reconcile_exchange_position()
            return

        risk_dist = abs(entry - final_sl)
        if not math.isfinite(risk_dist) or risk_dist <= 1e-6:
            logger.warning("[开仓] 止损距离异常 (%.6f) | 采用 ATR 保护距离兜底", risk_dist)
            risk_dist = max(atr_14 * 0.5, entry * 0.002, 1e-4)

        risk_r = risk_dist
        sz_qty = (sz_notional / entry) if entry > 0 else 0.0
        self.position = Position(
            entry_price=entry,
            direction=direction,
            sz_notional=sz_notional,
            tp_price=final_tp,
            sl_price=final_sl,
            initial_risk_r=risk_r,
            peak_price=entry,
            bars_held=0,
            opened_at=time.time(),
        )
        try:
            if hasattr(self, 'risk_mgr') and self.risk_mgr:
                self.risk_mgr.confirm_position(self.coin, self.position.initial_risk_r)
            if hasattr(self, 'router') and self.router:
                from multi_coin_router import CoinSignal
                sig = getattr(self, "_active_candidate_signal", None)
                if not sig or sig.coin != self.coin:
                    sig = CoinSignal(coin=self.coin, score=regime_cap, direction=direction, price=entry)
                self.router.register_position(sig)
        except Exception as e:
            logger.debug('[风控/路由] 登记开仓异常: %s', e)
        self.position_entry_price = entry
        self.position_direction = direction
        self.open_regime_cap = regime_cap
        self.last_synced_sl = final_sl
        self.cached_atr_5m = atr_14          # 播种 ATR 缓存，首秒兜底路径不因 ATR=0 失真
        self.last_atr_refresh = None         # 进入 TRADING 首拍立即刷新一次
        self.last_hard_stop_refresh = datetime.now(timezone.utc)
        self.last_5m_ts = None
        self.close_fail_count = 0
        self.state = STATE_TRADING
        self._persist_state()
        logger.info("[开仓] 确认 | 方向=%s | 成交=%.2f | sz=%.4f | SL=%.2f | TP=%.2f | R=%.2f | 硬止损缓冲=%.1fxATR",
                    direction, entry, sz_qty, final_sl, final_tp, risk_r, HARD_STOP_ATR_BUFFER)

    # ==================================================================
    # 持仓监控 (TRADING，1s 轮询)
    # ==================================================================

    def _trading_cycle(self):
        now_utc = datetime.now(timezone.utc)

        # 防状态落空：TRADING 必须持有本地状态机
        if self.position is None:
            logger.warning("[持仓] 本地状态丢失 | state=TRADING | action=尝试对账挂载")
            if self._reconcile_exchange_position():
                return
            send_alert("TRADING 状态下本地持仓状态丢失，已强制切回 IDLE。")
            self.state = STATE_IDLE
            self._persist_state()
            return

        # 1. 交易所对账：外部平仓 (风控单成交) 检测 (节流至 TRADING_RECONCILE_SECONDS，价格轮询仍 1s)
        if self.last_trading_reconcile is None or \
                (now_utc - self.last_trading_reconcile).total_seconds() >= TRADING_RECONCILE_SECONDS:
            self.last_trading_reconcile = now_utc
            try:
                szi, entry = self.exchange.current_position()
            except Exception as e:
                logger.warning("[对账] 持仓期对账失败 | err=%s | action=等待下轮对账", e)
                szi = None
            if szi is not None and abs(szi) <= 0:
                logger.warning("[对账] 交易所已无持仓 | 判定=风控单成交/外部平仓 | action=清理挂单并结算离场")
                send_alert("检测到交易所侧已无持仓，已清理残留挂单并结算离场。")
                self._settle_external_close()
                self._safe_sleep(TRADING_POLL_SECONDS, phase="TRADING_POLL", step=0.5)
                return

        # 2. 轻量标记价 (1s 轮询): 屏蔽单次 REST 网络抖动, 避免瞬时超时误触发连续异常保护
        try:
            price = self.exchange.mark_price()
        except Exception as e:
            logger.warning("[持仓] 获取标记价瞬时失败 | err=%s | action=本轮跳过", e)
            self._safe_sleep(TRADING_POLL_SECONDS, phase="TRADING_POLL", step=0.5)
            return

        # 3. ATR / 5m K 线缓存刷新 (低频) + bars_held 推进 (5m K 线边界)
        if self.last_atr_refresh is None or \
                (now_utc - self.last_atr_refresh).total_seconds() >= ATR_REFRESH_SECONDS:
            try:
                # 严格基于时间戳剔除未闭合 K 线，避免新 K 线导致 ATR 计算与 bars_held 误判
                df_5m, _ = self.market.get_closed_ohlcv("5m", 120)
                if df_5m is not None and not df_5m.empty and len(df_5m) >= 15:
                    calc_atr = utils.atr(df_5m["high"], df_5m["low"], df_5m["close"], 14)
                    if calc_atr is not None and not np.isnan(calc_atr) and calc_atr > 0:
                        self.cached_atr_5m = calc_atr
                    if "ts" in df_5m.columns and len(df_5m["ts"]) > 0:
                        latest_ts = int(df_5m["ts"].iloc[-1])
                        if self.last_5m_ts is not None and latest_ts != self.last_5m_ts:
                            self.position.bars_held += 1
                        self.last_5m_ts = latest_ts
                self.last_atr_refresh = now_utc
            except Exception as e:
                logger.warning("[持仓] 5m 行情刷新失败 | action=沿用缓存 ATR | err=%s", e)

        current_atr = self.cached_atr_5m

        # 4. 本地 FSM 决策 (保留时间衰减与止损上移；常规止损止盈优先交由交易所撮合)
        action = update_position_fsm(self.position, price, current_atr)

        if action == "CLOSE_TP" and getattr(self.exchange, "_tp_oid", None) is not None:
            # 交易所侧已挂载 Maker 限价止盈单，等待交易所优先成交
            action = "HOLD"

        if action == "CLOSE_SL":
            # 交易所侧已挂载真实止损单，默认等待交易所撮合引擎毫秒级处理；
            # 【保留灾难兜底止损】：只有当 ATR 和 initial_risk_r 均为有效正数，且价格穿透原定止损位超过 0.5 * ATR (或亏损达到 1.3R) 且交易所持仓仍在时，
            # 才代表交易所触发单可能出现异常失效/卡单，由本地执行市价强平兜底。
            _atr = float(current_atr) if (current_atr and math.isfinite(current_atr) and current_atr > 0) else None
            _ir = float(getattr(self.position, 'initial_risk_r', 0.0) or 0.0)

            if _atr is None or not math.isfinite(_ir) or _ir <= 0:
                logger.warning("[持仓] ATR (%.4f) 或 initial_risk_r (%.4f) 非法 | action=拒绝本地强平，交由交易所SL单处理",
                               current_atr if current_atr is not None else -1.0, _ir)
                action = "HOLD"
            else:
                loss_r = -self.position.profit_r(price)
                sl_penetration = (self.position.sl_price - price) if self.position.direction == "LONG" else (price - self.position.sl_price)
                is_catastrophic_penetration = (sl_penetration >= 0.5 * _atr) or (loss_r >= 1.3)

                if is_catastrophic_penetration:
                    logger.error("[灾难兜底] 交易所止损疑似失效 | 穿透=%.2f(阈值=%.2f) | 亏损=%.2fR | action=撤单并本地市价紧急强平",
                                 sl_penetration, 0.5 * _atr, loss_r)
                    send_alert(f"[灾难兜底] 价格严重穿透止损 ({sl_penetration:.2f} > 0.5xATR, {loss_r:.2f}R)，准备本地紧急市价强平！")
                    # 先撤销交易所侧已有挂单，防止本地市价单与交易所触发单并发成交造成双倍反向裸仓
                    try:
                        self.exchange.cancel_all_orders()
                    except Exception as ce:
                        logger.warning("[灾难兜底] 撤销交易所保护单异常: %s", ce)
                    # 二次核对持仓
                    szi_check, _ = self.exchange.current_position()
                    if szi_check is None or abs(szi_check) <= 0:
                        logger.info("[灾难兜底] 交易所持仓已自行归零 | action=直接结算离场")
                        self._settle_external_close()
                        self._safe_sleep(TRADING_POLL_SECONDS, phase="TRADING_POLL", step=0.5)
                        return
                    action = "CLOSE_DISASTER_FALLBACK"
                else:
                    logger.debug("[持仓] 价格触及止损线(%.2f)，等待交易所侧硬止损触发成交", self.position.sl_price)
                    action = "HOLD"

        if action != "HOLD":
            reason_map = {
                "CLOSE_TP": "止盈触发",
                "CLOSE_SL": "止损触发",
                "CLOSE_DISASTER_FALLBACK": "灾难兜底止损",
                "CLOSE_TIME_DECAY": "时间衰减平仓",
            }
            self._execute_market_close(reason_map.get(action, action), price, now_utc)
            self._safe_sleep(TRADING_POLL_SECONDS, phase="TRADING_POLL", step=0.5)
            return

        # 5. FSM 内部 SL 移动 (保本/追踪) -> 同步交易所侧触发单
        # 【单向棘轮与频控保护】：
        # 1) 单向收紧校验 (做多必须增大，做空必须减小)
        # 2) 移动幅度 >= 0.2 * ATR
        # 3) 距离上次成功改单 >= 10 秒
        is_tightening = False
        sl_step = 0.0
        if self.last_synced_sl is not None:
            if self.position.direction == "LONG":
                is_tightening = self.position.sl_price > self.last_synced_sl
                sl_step = self.position.sl_price - self.last_synced_sl
            else:
                is_tightening = self.position.sl_price < self.last_synced_sl
                sl_step = self.last_synced_sl - self.position.sl_price

        min_step = 0.2 * current_atr if current_atr > 0 else 0.0
        if is_tightening and sl_step >= min_step:
            logger.info("[持仓] FSM 移损满足棘轮步长 | SL=%.2f->%.2f | 步长=%.2f(>=%.2f) | 浮盈=%.2fR",
                        self.last_synced_sl, self.position.sl_price, sl_step, min_step,
                        self.position.profit_r(price))
            tighten_res = self.exchange.tighten_stop(self.position.sl_price, min_interval_seconds=10.0)
            if tighten_res is not None:
                self.last_synced_sl = self.position.sl_price
                self._persist_state()
            else:
                logger.debug("[持仓] 移损请求未被交易所采纳或被节流窗口拦截，保持当前止损单继续监控。")

        # 6. 双止损：本地硬止损按最新 ATR 定时刷新 (默认 15min，Q4 确认保留)
        if self.last_hard_stop_refresh is None or \
                (now_utc - self.last_hard_stop_refresh).total_seconds() >= HARD_STOP_REFRESH_SECONDS:
            self._refresh_local_hard_stop()
            self.last_hard_stop_refresh = now_utc

        # 本轮监控全程无异常 -> 清零连续异常计数 (保持"连续异常"语义)
        self.api_error_count = 0

        self._safe_sleep(TRADING_POLL_SECONDS, phase="TRADING_POLL", step=0.5)

    def _refresh_local_hard_stop(self):
        """按最新 5m ATR 刷新本地硬止损安全垫（严格执行单向棘轮：只许收紧，不许放宽）。"""
        if self.cached_atr_5m is None or not math.isfinite(self.cached_atr_5m) or self.cached_atr_5m <= 0 or self.position is None:
            return
        direction = self.position.direction
        ref = self.position.entry_price
        if not math.isfinite(ref) or ref <= 0:
            return
        dist = self.cached_atr_5m * (self.open_regime_cap + HARD_STOP_ATR_BUFFER)
        hard = ref - dist if direction == "LONG" else ref + dist

        # 单向棘轮保护：与当前已挂载的硬止损比较，绝不向不利方向放宽
        old_hard = getattr(self.exchange, "_hard_sl_price", None)
        if old_hard is not None and old_hard > 0:
            if direction == "LONG":
                hard = max(hard, float(old_hard))
            else:
                hard = min(hard, float(old_hard))

        if self.last_synced_sl:
            extra = max(abs(self.last_synced_sl) * 1e-4, 1e-6)
            if direction == "LONG":
                hard = min(hard, self.last_synced_sl - extra)
            else:
                hard = max(hard, self.last_synced_sl + extra)
        try:
            self.exchange.refresh_hard_stop(hard)
        except Exception as e:
            logger.warning("[持仓] 硬止损定时刷新异常 | err=%s", e)

    def _execute_market_close(self, reason_label: str, latest_price: float,
                              now_utc: datetime) -> bool:
        """市价全平并结算。先撤保护单防反手，若交易所已自行平仓则直接结算。"""
        try:
            self.exchange.cancel_all_orders()
        except Exception as e:
            logger.warning("[平仓] 撤销保护单异常 | err=%s", e)

        # 检查是否已由交易所侧止损单等成交归零
        try:
            szi, _ = self.exchange.current_position()
            if szi is not None and abs(szi) <= 0:
                logger.info("[平仓] 交易所持仓已自行归零，直接完成结算 | reason=%s", reason_label)
                self._register_closed_position(reason_label, latest_price, now_utc)
                return True
        except Exception as pe:
            logger.debug("[平仓] 查询持仓状态异常: %s", pe)

        try:
            resp = self.exchange.close_all()
        except ExecutionError as e:
            # 二次核对持仓是否已归零
            try:
                szi_after, _ = self.exchange.current_position()
                if szi_after is not None and abs(szi_after) <= 0:
                    logger.info("[平仓] 捕获异常但交易所持仓已归零，判定平仓成功 | reason=%s", reason_label)
                    self._register_closed_position(reason_label, latest_price, now_utc)
                    return True
            except Exception:
                pass
            send_alert(f"[{reason_label}] 平仓执行异常，仓位未平: {e}")
            self.close_fail_count += 1
            self._maybe_emergency_protect()
            return False

        if resp is None:
            try:
                szi_after, _ = self.exchange.current_position()
                if szi_after is not None and abs(szi_after) <= 0:
                    logger.info("[平仓] 未收到回执但持仓已归零，判定平仓成功 | reason=%s", reason_label)
                    self._register_closed_position(reason_label, latest_price, now_utc)
                    return True
            except Exception:
                pass
            self.close_fail_count += 1
            send_alert(f"[{reason_label}] 交易所未确认平仓 ({self.close_fail_count} 次)，"
                               f"仓位仍在外，禁止推进状态。")
            self._maybe_emergency_protect()
            return False

        self._register_closed_position(reason_label, latest_price, now_utc)
        return True

    def _settle_external_close(self):
        """
        交易所侧风控单 (双止损/止盈) 已自行成交、仓位归零时的结算路径。
        本地无需再发平仓单，只需撤掉残留挂单并推进状态机。
        """
        try:
            self.exchange.cancel_all_orders()
        except Exception as e:
            logger.warning("[对账] 清理残留风控单失败 | err=%s", e)
        self.exchange.clear_position_state()
        self.last_synced_sl = None
        self.cached_atr_5m = 0.0
        self.last_hard_stop_refresh = None

        exit_price = 0.0
        try:
            if hasattr(self.exchange, "get_recent_fills"):
                fills = self.exchange.get_recent_fills(self.coin)
                if fills:
                    for f in fills:
                        px = float(f.get("px", 0.0) or 0.0)
                        if px > 0 and math.isfinite(px):
                            exit_price = px
                            break
        except Exception as fe:
            logger.debug("[对账] 查询成交流水退出价异常: %s", fe)

        if exit_price <= 0:
            try:
                m_px = self.exchange.mark_price()
                if m_px and math.isfinite(m_px) and m_px > 0:
                    exit_price = float(m_px)
            except Exception:
                pass

        if exit_price <= 0:
            exit_price = getattr(self.position, "sl_price", 0.0) or getattr(self.position, "entry_price", 0.0) or 0.0

        if exit_price <= 0:
            logger.warning("[对账] 外部平仓无法获取有效离场价格 | 采用 entry_price 兜底")
            exit_price = getattr(self, "position_entry_price", 0.0) or 1.0

        self._register_closed_position("交易所侧风控单成交离场", exit_price,
                                       datetime.now(timezone.utc))

    def _register_closed_position(self, reason_label: str, exit_price: float,
                                  now_utc: datetime):
        """
        平仓后统一结算：按真实 PnL (净/毛) 判断是否熔断。
        只有实质性亏损 (pnl < 0) 才 +1，盈利/平本离场直接清零。
        """
        closed_pos = self.position  # 置空前先保存持仓快照，杜绝 pnl_r 记账空指针
        direction = self.position_direction
        entry = self.position_entry_price
        pnl = self._estimate_pnl(direction, entry, exit_price)
        self.last_closed_pnl = pnl

        try:
            from risk_manager import TradeRecord
            dur_mins = int(closed_pos.bars_held * 5) if (closed_pos and hasattr(closed_pos, "bars_held")) else 0
            sz_notional = float(closed_pos.sz_notional) if (closed_pos and hasattr(closed_pos, "sz_notional")) else 1000.0
            r_mult = 0.0
            if closed_pos and hasattr(closed_pos, "initial_risk_r") and closed_pos.initial_risk_r > 0 and entry:
                p_diff = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
                friction = entry * TRADING_FEE_RATE * 2.0
                r_mult = (p_diff - friction) / closed_pos.initial_risk_r
            entry_eq = float(getattr(self.risk_mgr, "current_equity", self.initial_equity)) if hasattr(self, "risk_mgr") and self.risk_mgr else self.initial_equity
            pnl_pct_account = (pnl * sz_notional) / max(entry_eq, 1.0)
            trade_rec = TradeRecord(
                entry_time=now_utc.isoformat(),
                exit_time=now_utc.isoformat(),
                coin=self.coin,
                direction=direction or "LONG",
                entry_price=entry or 0.0,
                exit_price=exit_price,
                size=sz_notional,
                pnl=pnl * sz_notional,
                pnl_pct=pnl_pct_account,
                pnl_r=r_mult,
                hold_duration_minutes=dur_mins,
                exit_reason=reason_label,
                entry_equity=entry_eq,
            )
            if hasattr(self, 'risk_mgr') and self.risk_mgr:
                self.risk_mgr.record_trade(trade_rec)
                self.risk_mgr.unregister_position(self.coin)
            if hasattr(self, 'router') and self.router:
                self.router.clear_position()
        except Exception as e:
            logger.warning("[风控/路由] 记账结算异常: %s", e)

        # 记账完成后重置本地持仓状态
        self.position = None
        self.position_entry_price = None
        self.position_direction = None
        self.last_hard_stop_refresh = None
        self.last_atr_refresh = None
        self.last_synced_sl = None
        self.last_5m_ts = None
        self.close_fail_count = 0
        self.state = STATE_IDLE
        
        if pnl < 0:
            self.consecutive_failures += 1
            logger.info("[离场] %s | 实质亏损 | PnL=%+.2f%% | 连败=%s/%d",
                        reason_label, pnl * 100, self.consecutive_failures, CONSECUTIVE_LOSS_LIMIT)
            if self.consecutive_failures >= CONSECUTIVE_LOSS_LIMIT:
                self.cooldown_until = now_utc + timedelta(hours=COOLDOWN_HOURS)
                self.state = STATE_COOL_DOWN
                send_alert(
                    f"连续 {CONSECUTIVE_LOSS_LIMIT} 次实质亏损出局！系统锁定，休眠 {COOLDOWN_HOURS:g} 小时！")
        else:
            logger.info("[离场] %s | 非亏损 | PnL=%+.2f%% | 连败=0", reason_label, pnl * 100)
            self.consecutive_failures = 0
        self._persist_state()

        if notifier is not None:
            try:
                notifier.notify_position_closed(
                    reason=reason_label,
                    direction=direction or "UNKNOWN",
                    entry_price=entry or 0.0,
                    exit_price=exit_price or 0.0,
                    pnl_ratio=pnl or 0.0,
                    extra={"连败计数": f"{self.consecutive_failures}/{CONSECUTIVE_LOSS_LIMIT}"},
                )
            except Exception as e:
                logger.warning("[通知] 离场通知发送异常: %s", e)

    @staticmethod
    def _estimate_pnl(direction: Optional[str], entry_price: Optional[float],
                      exit_price: float) -> float:
        """按方向估算已实现盈亏比例 (带符号)，估减双边手续费与滑点。"""
        if entry_price is None or entry_price == 0 or direction is None or not exit_price:
            return 0.0
        if direction == "LONG":
            gross = (exit_price - entry_price) / entry_price
        elif direction == "SHORT":
            gross = (entry_price - exit_price) / entry_price
        else:
            return 0.0
        return gross - 2.0 * TRADING_FEE_RATE

    # ==================================================================
    # 持仓期异常保护 (替代旧 AId，Q4 确认)
    # ==================================================================

    def _maybe_emergency_protect(self):
        """
        持仓期连续异常兜底：连续异常达到阈值时，立即在交易所侧挂本地 ATR 硬止损
        保护单并发告警邮件 (AId 已删除，仓位安全交由交易所侧保护单 + 人工介入)。
        """
        if self.state != STATE_TRADING or self.position is None:
            return
        if self.api_error_count < TRADING_ALERT_THRESHOLD and \
                self.close_fail_count < CLOSE_FAIL_ALERT_THRESHOLD:
            return
        logger.critical("[风控] 持仓期连续异常 | api_err=%s | close_fail=%s | action=部署交易所侧硬止损保护单",
                        self.api_error_count, self.close_fail_count)
        self._deploy_emergency_hard_stop()

    def _deploy_emergency_hard_stop(self):
        """以当前缓存 ATR x 基调倍率计算紧急止损，仅允许朝有利方向收紧。"""
        if self.position is None or not self.position.entry_price or not self.position.direction:
            logger.warning("[风控] 紧急止损中止: 本地持仓为空或缺少关键属性")
            return
        try:
            atr_val = self.cached_atr_5m if (self.cached_atr_5m and math.isfinite(self.cached_atr_5m)) else 0.0
            cap_val = self.open_regime_cap if (self.open_regime_cap and math.isfinite(self.open_regime_cap)) else 1.5
            dist = max(atr_val, 0.0) * max(cap_val, 1.5)
            direction = self.position.direction
            ref = self.position.entry_price
            proposed_sl = ref - dist if direction == "LONG" else ref + dist
            current_sl = self.last_synced_sl if self.last_synced_sl else self.position.sl_price
            if direction == "LONG":
                emergency_sl = max(proposed_sl, current_sl)
            else:
                emergency_sl = min(proposed_sl, current_sl)
            if abs(emergency_sl - current_sl) / max(abs(current_sl), 1e-9) <= 1e-6:
                logger.info("[风控] 紧急止损未收紧 | 建议SL=%.2f | 当前SL=%.2f | action=保持原单",
                            proposed_sl, current_sl)
                send_alert(
                    f"持仓期连续异常，当前止损 {current_sl:.2f} 已足够紧，请人工检查！")
                return
            # 挂单前校验现价是否已击穿紧急止损价: 触发价落在现价错误一侧会被交易所拒单
            # 或立即以市价成交, 必须拦截并转人工。
            try:
                mark = self.exchange.mark_price()
                if direction == "LONG" and emergency_sl >= mark:
                    logger.error("[风控] 紧急止损价已被现价向下击穿 | 建议SL=%.2f | 现价=%.2f "
                                 "| action=放弃挂单并通知人工", emergency_sl, mark)
                    send_alert(f"紧急止损部署拦截：建议SL ({emergency_sl:.2f}) 已高于或等于现价 "
                               f"({mark:.2f})，请人工立即介入！")
                    return
                if direction == "SHORT" and emergency_sl <= mark:
                    logger.error("[风控] 紧急止损价已被现价向上击穿 | 建议SL=%.2f | 现价=%.2f "
                                 "| action=放弃挂单并通知人工", emergency_sl, mark)
                    send_alert(f"紧急止损部署拦截：建议SL ({emergency_sl:.2f}) 已低于或等于现价 "
                               f"({mark:.2f})，请人工立即介入！")
                    return
            except Exception as e:
                logger.warning("[风控] 校验紧急止损现价失败 | err=%s | action=继续尝试挂单", e)
            if self.exchange.tighten_stop(emergency_sl) is not None:
                logger.warning("[风控] 紧急止损已收紧 | 新SL=%.2f | 旧SL=%.2f", emergency_sl, current_sl)
                self.last_synced_sl = emergency_sl
                self.position.sl_price = emergency_sl
                self._persist_state()
            else:
                send_alert("紧急保护止损挂载未获交易所确认，需人工立即介入！")
            send_alert(
                f"持仓期连续异常，已部署交易所侧保护单 (SL={emergency_sl:.2f})，请人工检查！")
            if notifier is not None:
                notifier.notify_aid_triggered(
                    context={"direction": direction,
                             "entry_price": round(ref, 6),
                             "old_sl": round(current_sl, 6),
                             "emergency_sl": round(emergency_sl, 6),
                             "api_error_count": self.api_error_count,
                             "close_fail_count": self.close_fail_count},
                    action="部署交易所侧紧急保护止损",
                    detail=f"持仓期连续异常，SL {current_sl:.2f} -> {emergency_sl:.2f}",
                )
        except Exception as e:
            send_alert(f"紧急保护止损部署异常，需人工立即介入: {e}")

    def _handle_loop_exception(self, e: Exception):
        """核心循环容错：持仓期连续异常触发保护兜底，空仓期指数退避。"""
        self.api_error_count += 1
        logger.error("[异常] 核心循环错误 | 计数=%s/%s | err=%s",
                     self.api_error_count, TRADING_ALERT_THRESHOLD, e)

        if self.state == STATE_TRADING:
            if self.api_error_count >= TRADING_ALERT_THRESHOLD:
                self._deploy_emergency_hard_stop()
            self._safe_sleep(10, phase="TRADING_EXC_BACKOFF", step=1.0)
        elif self.state == STATE_IDLE:
            self.idle_api_error_count += 1
            if self.idle_api_error_count >= IDLE_ALERT_THRESHOLD:
                send_alert(
                    f"空仓期 API 连续异常 {self.idle_api_error_count} 次，请人工检查网络/AI 通道。")
            logger.warning("[巡检] 空仓异常退避 | wait=%ss", self.idle_backoff)
            self._safe_sleep(self.idle_backoff, phase="IDLE_BACKOFF")
            self.idle_backoff = min(self.idle_backoff * 2, 600)
        else:
            self._safe_sleep(10, phase="LOOP_EXC_BACKOFF", step=1.0)

    def _enter_system_halt(self, reason: str):
        """SYSTEM_HALT：市价清空持仓与挂单后进入死循环休眠。"""
        logger.critical("[挂起] SYSTEM_HALT | reason=%s", reason)
        send_alert(f"SYSTEM_HALT: {reason}，正在强制市价平仓并清空挂单！")
        try:
            szi, _ = self.exchange.current_position()
            if abs(szi) > 0:
                resp = self.exchange.close_all()
                if resp is None:
                    send_alert("SYSTEM_HALT 市价平仓未获确认，需人工立即介入！")
            else:
                try:
                    self.exchange.cancel_all_orders()
                except Exception as e:
                    logger.warning("[挂起] 清理挂单失败 | err=%s", e)
                self.exchange.clear_position_state()
        except Exception as e:
            send_alert(f"SYSTEM_HALT 强制平仓异常，需人工立即介入: {e}")
        self.position = None
        self.position_entry_price = None
        self.position_direction = None
        self.last_synced_sl = None
        self.state = STATE_HALT
        self._persist_state()
        if notifier is not None:
            notifier.send("[交易机器人] SYSTEM_HALT 全局挂起",
                          f"{reason}\n量化态: {self.quant_state.value}\nTRS: {self.last_trs:.3f}")

    def _reconcile_exchange_position(self) -> bool:
        """启动与 IDLE 入口对账：发现遗留仓位则挂载到 TRADING。返回是否已切入持仓态。"""
        # 若处于熔断冷却态或黑天鹅挂起态，绝不盲目激活遗留仓位切入 TRADING，防止虚假触发外部平仓连败
        if self.state in (STATE_COOL_DOWN, STATE_HALT):
            return False
        try:
            szi, entry = self.exchange.current_position()
        except Exception as e:
            logger.warning("[对账] 查询交易所持仓失败 | err=%s", e)
            return False
        if szi is None or abs(szi) <= 0:
            return False
        logger.warning("[对账] 检测到遗留仓位 | sz=%s %s | action=IDLE挂载至TRADING",
                       szi, COIN)
        self._adopt_exchange_position(szi, entry)
        if self.state == STATE_TRADING and self.position is not None:
            send_alert(f"检测到未跟踪的交易所遗留仓位 {szi} {COIN}，已挂载至 FSM 持仓状态。")
            return True
        return False

    def _adopt_exchange_position(self, szi: float, entry: Optional[float]):
        """用交易所仓位重构本地 Position 并切入 TRADING。"""
        direction = "LONG" if szi > 0 else "SHORT"
        try:
            mark = self.exchange.mark_price()
        except Exception:
            mark = float(entry) if entry else 0.0
        entry_px = float(entry) if entry and float(entry) > 0 else mark
        if entry_px <= 0:
            logger.error("[对账] 无法确定入场价 | action=拒绝挂载遗留仓位")
            return
        try:
            df_5m = self.market.get_ohlcv("5m", 120)
            calc_atr = utils.atr(df_5m["high"], df_5m["low"], df_5m["close"], 14)
            if calc_atr is not None and math.isfinite(calc_atr) and calc_atr > 0:
                atr_14 = float(calc_atr)
            else:
                atr_14 = max(entry_px * 0.01, 1e-4)
        except Exception as e:
            logger.warning("[对账] 拉取 5m ATR 失败 | action=使用保守止损 | err=%s", e)
            atr_14 = max(entry_px * 0.01, 1e-4)
        self.cached_atr_5m = atr_14

        self.exchange.adopt_open_position(szi, entry_px)
        sl_px = self.exchange._ai_sl_price
        tp_px = self.exchange._tp_price
        if sl_px is None or float(sl_px) <= 0:
            sl_px = entry_px - 1.5 * atr_14 if direction == "LONG" else entry_px + 1.5 * atr_14
        if tp_px is None or float(tp_px) <= 0:
            tp_px = entry_px + 3.0 * atr_14 if direction == "LONG" else entry_px - 3.0 * atr_14
        if self.exchange._ai_sl_oid is None:
            try:
                self.exchange.tighten_stop(float(sl_px), float(tp_px))
            except Exception as e:
                logger.error("[对账] 遗留仓位缺少止损且补挂失败 | err=%s", e)
                send_alert(f"遗留仓位缺少止损且补挂失败，需人工立即介入: {e}")

        sz_notional = abs(float(szi)) * entry_px
        # 如果启动时已从 state.json 恢复了持仓快照，优先继承其精确风控元数据 (防止重置 peak_price / bars_held / initial_risk_r)
        prev_pos = self.position
        if prev_pos is not None and getattr(prev_pos, "direction", None) == direction:
            prev_sl = float(getattr(prev_pos, "sl_price", 0.0) or 0.0)
            if prev_sl > 0 and math.isfinite(prev_sl):
                if direction == "LONG":
                    sl_px = max(float(sl_px), prev_sl)
                else:
                    sl_px = min(float(sl_px), prev_sl)
            try:
                init_r = float(getattr(prev_pos, "initial_risk_r", 0.0) or 0.0)
                if init_r <= 0 or not math.isfinite(init_r):
                    init_r = max(abs(entry_px - float(sl_px)), atr_14 * 0.5, entry_px * 0.002)
            except (ValueError, TypeError):
                init_r = max(abs(entry_px - float(sl_px)), atr_14 * 0.5, entry_px * 0.002)

            try:
                prev_pk = float(getattr(prev_pos, "peak_price", 0.0) or 0.0)
                if prev_pk <= 0 or not math.isfinite(prev_pk):
                    prev_pk = entry_px
            except (ValueError, TypeError):
                prev_pk = entry_px

            cur_ref = mark if (mark and mark > 0 and math.isfinite(mark)) else entry_px
            pk = max(prev_pk, cur_ref) if direction == "LONG" else min(prev_pk, cur_ref)
            try:
                b_held = int(getattr(prev_pos, "bars_held", 0) or 0)
            except (ValueError, TypeError):
                b_held = 0
            raw_o_at = getattr(prev_pos, "opened_at", None)
            if isinstance(raw_o_at, datetime):
                o_at = float(raw_o_at.timestamp())
            elif raw_o_at is not None:
                try:
                    o_at = float(raw_o_at)
                except Exception:
                    o_at = time.time()
            else:
                o_at = time.time()
            logger.info("[对账] 继承已持久化持仓风控元数据 | init_r=%.2f | peak=%.2f | bars_held=%d",
                        init_r, pk, b_held)
        else:
            init_r = max(abs(entry_px - float(sl_px)), atr_14 * 0.5, entry_px * 0.002, 1e-4)
            pk = mark or entry_px
            b_held = 0
            o_at = time.time()

        self.position = Position(
            entry_price=entry_px,
            direction=direction,
            sz_notional=sz_notional,
            tp_price=float(tp_px),
            sl_price=float(sl_px),
            initial_risk_r=init_r,
            peak_price=pk,
            bars_held=b_held,
            opened_at=o_at,
        )
        try:
            if hasattr(self, 'risk_mgr') and self.risk_mgr:
                self.risk_mgr.confirm_position(self.coin, self.position.initial_risk_r)
            if hasattr(self, 'router') and self.router:
                from multi_coin_router import CoinSignal
                sig = CoinSignal(
                    coin=self.coin,
                    score=75.0,
                    direction=direction,
                    price=entry_px,
                    tp=float(tp_px),
                    sl=float(sl_px),
                    entry_score=75.0,
                )
                self.router.register_position(sig)
        except Exception as re:
            logger.debug("[风控/路由] 对账挂载登记仓位异常: %s", re)
        self.position_entry_price = entry_px
        self.position_direction = direction
        self.open_regime_cap = regime_sl_cap(self.final_macro_state) or 1.5
        self.last_synced_sl = float(sl_px)
        self.last_hard_stop_refresh = datetime.now(timezone.utc)
        self.last_5m_ts = None
        self.close_fail_count = 0
        self.state = STATE_TRADING
        self._persist_state()
        logger.info("[对账] 挂载完成 | 方向=%s | 入场=%.2f | sz=%.4f | SL=%.2f | TP=%.2f",
                    direction, entry_px, abs(szi), sl_px, tp_px)

    @staticmethod
    def _parse_iso_datetime(value) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _switch_coin(self, new_coin: str):
        """动态切换交易币种并重置关联客户端"""
        self.coin = new_coin
        if hasattr(self.exchange, "set_coin"):
            self.exchange.set_coin(new_coin)
        else:
            self.exchange.coin = new_coin
        if hasattr(self.market, "set_coin"):
            self.market.set_coin(new_coin)
        else:
            self.market.coin = new_coin

    def _check_position_rotation(self, price: float, now_utc: datetime) -> bool:
        """
        持仓轮动换仓检查：
        1. 持仓未满 3 根 K 线禁止换仓；
        2. 浮亏超出 -0.3R 禁止换仓砍单；
        3. 浮盈达到或超出 +0.5R 保护顺势单，禁止轮动。
        """
        if not self.position:
            return False
        if getattr(self.position, "bars_held", 0) < 3:
            return False
        initial_risk = getattr(self.position, "initial_risk_r", 0.0)
        if initial_risk <= 0:
            return False
        if self.position.direction == "LONG":
            profit_r = (price - self.position.entry_price) / initial_risk
        else:
            profit_r = (self.position.entry_price - price) / initial_risk

        if profit_r < -0.3 or profit_r >= 0.5:
            return False
        return False

    def _persist_state(self):
        payload = {
            "state": self.state,
            "coin": self.coin,
            "daily_start_equity": getattr(self, "daily_start_equity", 1000.0),
            "daily_loss_date": getattr(self, "daily_loss_date", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
            "daily_loss_pnl": getattr(self, "daily_loss_pnl", 0.0),
            "quant_state": getattr(self.quant_state, "value", str(self.quant_state)) if self.quant_state is not None else "NEUTRAL",
            "final_macro_state": getattr(self.final_macro_state, "value", str(self.final_macro_state)) if self.final_macro_state is not None else "NEUTRAL",
            "cached_ai_macro_state": getattr(self.cached_ai_macro_state, "value", str(self.cached_ai_macro_state)) if self.cached_ai_macro_state is not None else None,
            "last_trs": self.last_trs,
            "news_impact": getattr(self, "latest_news_impact", ""),
            "sentiment": getattr(self, "latest_sentiment_data", {}),
            "radar_scores": getattr(self, "radar_scores", {}),
            "coin_prices": getattr(self, "coin_prices", {}),
            "last_macro_slot": None if self.last_macro_slot is None else [
                str(self.last_macro_slot[0]), self.last_macro_slot[1]
            ],
            "last_event_macro_time": None if self.last_event_macro_time is None else self.last_event_macro_time.isoformat(),
            "last_evaluated_candle_time": self.last_evaluated_candle_time,
            "last_ai_trade_call_ts": getattr(self, "last_ai_trade_call_ts", 0.0),
            "consecutive_failures": self.consecutive_failures,
            "cooldown_until": None if self.cooldown_until is None else self.cooldown_until.isoformat(),
            "last_closed_pnl": self.last_closed_pnl,
            "open_regime_cap": self.open_regime_cap,
            "last_synced_sl": self.last_synced_sl,
            "cached_atr_5m": self.cached_atr_5m,
            "position": None if self.position is None else {
                "entry_price": self.position.entry_price,
                "direction": self.position.direction,
                "sz_notional": self.position.sz_notional,
                "tp_price": self.position.tp_price,
                "sl_price": self.position.sl_price,
                "initial_risk_r": self.position.initial_risk_r,
                "peak_price": self.position.peak_price,
                "bars_held": self.position.bars_held,
                "opened_at": self.position.opened_at,
                "entry_score": getattr(self.position, "entry_score", 75.0),
                "filled_sz": getattr(self.position, "filled_sz", 1.0),
            },
            "position_entry_price": self.position_entry_price,
            "position_direction": self.position_direction,
        }
        try:
            tmp_path = STATE_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, STATE_FILE)
        except Exception as e:
            logger.warning("[状态] 落盘失败 | err=%s", e)

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("[状态] 读取 state.json 失败 | action=按空仓启动 | err=%s", e)
            return
        try:
            self.state = data.get("state") or STATE_IDLE
            if self.state == STATE_TRADING:
                self.state = STATE_IDLE
            if self.state == STATE_HALT:
                # 注解 §9: SYSTEM_HALT 为"重启解除"——持久化的挂起态跨重启不保留
                self.state = STATE_IDLE
                logger.warning("[状态] 持久化 SYSTEM_HALT 降级 | action=降级为IDLE | 需人工确认市场状态")
            saved_coin = data.get("coin")
            if saved_coin and hasattr(self, "_switch_coin"):
                self._switch_coin(saved_coin)
            elif saved_coin:
                self.coin = saved_coin
            if "daily_start_equity" in data:
                self.daily_start_equity = float(data["daily_start_equity"])
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            saved_date = data.get("daily_loss_date")
            if saved_date == today:
                self.daily_loss_date = saved_date
                self.daily_loss_pnl = float(data.get("daily_loss_pnl", 0.0))
            else:
                self.daily_loss_date = today
                self.daily_loss_pnl = 0.0
            try:
                self.quant_state = MacroState(data.get("quant_state") or MacroState.NEUTRAL.value)
            except (ValueError, TypeError):
                self.quant_state = MacroState.NEUTRAL

            try:
                self.final_macro_state = MacroState(data.get("final_macro_state") or MacroState.NEUTRAL.value)
            except (ValueError, TypeError):
                self.final_macro_state = MacroState.NEUTRAL

            cached = data.get("cached_ai_macro_state")
            if cached:
                try:
                    self.cached_ai_macro_state = MacroState(cached)
                except (ValueError, TypeError):
                    self.cached_ai_macro_state = None
            else:
                self.cached_ai_macro_state = None
            # SYSTEM_HALT 的"重启解除"语义必须同时清理 AI 护栏缓存与最终态：
            # 否则持久化的 SYSTEM_HALT 会在 apply_trend_guardrail 中被透传，
            # 令重启后 final_macro_state 被永久锁死在 SYSTEM_HALT、静默不再开仓。
            if self.cached_ai_macro_state == MacroState.SYSTEM_HALT:
                self.cached_ai_macro_state = None
                logger.warning("[状态] 清理持久化 AI_macro SYSTEM_HALT 缓存 | reason=重启解除")
            if self.final_macro_state == MacroState.SYSTEM_HALT:
                self.final_macro_state = MacroState.NEUTRAL
            self.last_trs = float(data.get("last_trs") or 0.0)
            self.latest_news_impact = data.get("news_impact", "")
            self.latest_sentiment_data = data.get("sentiment", {})
            slot = data.get("last_macro_slot")
            if isinstance(slot, (list, tuple)) and len(slot) == 2:
                try:
                    from datetime import date as _date
                    self.last_macro_slot = (_date.fromisoformat(str(slot[0])), int(slot[1]))
                except (ValueError, TypeError) as e:
                    logger.warning("[状态] 解析 last_macro_slot 异常 | slot=%s | err=%s", slot, e)
                    self.last_macro_slot = None
            event_ts = data.get("last_event_macro_time")
            self.last_event_macro_time = self._parse_iso_datetime(event_ts)
            candle_ts = data.get("last_evaluated_candle_time")
            self.last_evaluated_candle_time = int(candle_ts) if candle_ts is not None else None
            self.last_ai_trade_call_ts = float(data.get("last_ai_trade_call_ts") or 0.0)
            self.consecutive_failures = int(data.get("consecutive_failures") or 0)
            cd = data.get("cooldown_until")
            self.cooldown_until = self._parse_iso_datetime(cd)
            self.last_closed_pnl = float(data.get("last_closed_pnl") or 0.0)
            self.open_regime_cap = float(data.get("open_regime_cap") or 1.5)
            self.last_synced_sl = float(data.get("last_synced_sl") or 0.0) if data.get("last_synced_sl") is not None else None
            self.cached_atr_5m = float(data.get("cached_atr_5m") or 0.0)
            self.radar_scores = data.get("radar_scores", {})
            self.coin_prices = data.get("coin_prices", {})
            pos_data = data.get("position")
            if isinstance(pos_data, dict):
                try:
                    opened_val = pos_data.get("opened_at")
                    if isinstance(opened_val, (int, float)):
                        parsed_opened_at = float(opened_val)
                    elif isinstance(opened_val, str):
                        try:
                            parsed_opened_at = float(opened_val)
                        except ValueError:
                            try:
                                parsed_opened_at = datetime.fromisoformat(opened_val).timestamp()
                            except Exception:
                                parsed_opened_at = time.time()
                    else:
                        parsed_opened_at = time.time()

                    self.position = Position(
                        entry_price=pos_data["entry_price"],
                        direction=pos_data["direction"],
                        sz_notional=pos_data["sz_notional"],
                        tp_price=pos_data["tp_price"],
                        sl_price=pos_data["sl_price"],
                        initial_risk_r=pos_data["initial_risk_r"],
                        peak_price=pos_data["peak_price"],
                        bars_held=pos_data["bars_held"],
                        opened_at=parsed_opened_at,
                        entry_score=float(pos_data.get("entry_score", 75.0)),
                        filled_sz=float(pos_data.get("filled_sz", 1.0)),
                    )
                    self.position_entry_price = self.position.entry_price
                    self.position_direction = self.position.direction
                    self.last_synced_sl = float(data.get("last_synced_sl") or self.position.sl_price)
                    if self.cached_atr_5m <= 0.0:
                        try:
                            df_5m_init = self.market.get_ohlcv("5m", 30, use_cache=False)
                            self.cached_atr_5m = utils.atr(df_5m_init["high"], df_5m_init["low"], df_5m_init["close"], 14)
                        except Exception:
                            self.cached_atr_5m = float(self.position.entry_price) * 0.01
                    if hasattr(self, "router") and self.router:
                        from multi_coin_router import CoinSignal
                        self.router.current_position = CoinSignal(
                            coin=self.coin,
                            score=self.position.entry_score,
                            direction=self.position.direction,
                            price=self.position.entry_price,
                            tp=self.position.tp_price,
                            sl=self.position.sl_price,
                            entry_score=self.position.entry_score,
                            macro_state=self.final_macro_state.value if hasattr(self.final_macro_state, "value") else str(self.final_macro_state),
                            trs=self.last_trs
                        )
                    self.state = STATE_TRADING
                    logger.info("[状态] 持仓对象已恢复 | 方向=%s | 入场=%.2f | SL=%.2f | TP=%.2f",
                                self.position.direction, self.position.entry_price,
                                self.position.sl_price, self.position.tp_price)
                except (KeyError, TypeError, ValueError) as e:
                    self.position = None
                    logger.warning("[状态] 持仓对象恢复失败 | action=等待交易所对账 | err=%s", e)
            logger.info("[状态] 恢复完成 | state=%s | 宏观缓存=%s | 连败=%s",
                        self.state,
                        None if self.cached_ai_macro_state is None else self.cached_ai_macro_state.value,
                        self.consecutive_failures)
        except Exception as e:
            logger.warning("[状态] 解析 state.json 失败 | action=按空仓启动 | err=%s", e)


def main():
    # 强制安全熔断锁：检修期间严禁拉起主程序或发起未授权网络/交易 API 调用
    if os.getenv("MAINTENANCE_LOCK", "true").lower() in ("true", "1", "yes"):
        logger.warning("[检修锁定] 检测到 MAINTENANCE_LOCK=true，主程序已安全截断并退出，严禁调用任何实盘/行情 API！")
        print(">>> [SAFETY INTERLOCK] MAINTENANCE_LOCK IS ACTIVE. BOT STARTUP BLOCKED.")
        return
    # 全局 socket 兜底超时: hyperliquid SDK 的 requests 调用无显式 timeout，
    # 网络半开时会让单线程主循环长时间阻塞 (表现为"卡死")。httpx 自带 timeout 不受影响。
    if NETWORK_TIMEOUT_SECONDS > 0:
        import socket
        socket.setdefaulttimeout(NETWORK_TIMEOUT_SECONDS)
    bot = TradingBot()
    try:
        bot.run()
    except KeyboardInterrupt:
        logger.info("[停止] 收到 Ctrl-C | action=机器人停止")
    finally:
        import logging
        logging.shutdown()


if __name__ == "__main__":
    main()

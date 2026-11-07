# -*- coding: utf-8 -*-
"""
多币种轮动调度器 (MultiCoinRouter)
═══════════════════════════════════════════════════════════════════════

【核心功能】
1. 4个币种实时信号评估（BTC/ETH/SUI/LINK）
2. 动态选择最强信号币种
3. 相关性软约束（降权而非硬拦截）
4. 单持仓轮动（同时最多1个币种）
5. 仓位切换逻辑（当新信号显著优于当前持仓时切换）

【使用场景】
小本金（50-100U）集中火力，避免分散持仓导致单币种仓位过小
"""

__version__ = "1.0.0"
__milestone__ = "v1.0-RELEASE"
__release_date__ = "2026-11-01"


import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from alerts import logger
from exchange import MarketData, MarketDataError


@dataclass
class CoinSignal:
    coin: str
    score: float
    direction: str
    price: float
    tp: float = 0.0
    sl: float = 0.0
    entry_score: float = 75.0
    macro_state: str = "NEUTRAL"
    trs: float = 0.0
    timestamp: datetime = None
    ranking_score: float = None
    df_5m: Optional[pd.DataFrame] = None
    df_1h: Optional[pd.DataFrame] = None
    atr_14: float = 0.0
    raw_signal_result: Optional[dict] = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)
        if self.ranking_score is None:
            self.ranking_score = self.score


class CorrelationTracker:
    """币种相关性追踪器"""
    
    def __init__(self, lookback_days: int = 30):
        self.lookback_days = lookback_days
        self.price_history: Dict[str, List[Tuple[int, float]]] = {}  # {coin: [(ts, price)]}
        
    def seed_from_df(self, coin: str, df: pd.DataFrame):
        """从历史 DataFrame 快速播种价格历史并按时间戳去重"""
        if df is None or not isinstance(df, pd.DataFrame) or df.empty or "ts" not in df.columns or "close" not in df.columns:
            return
        try:
            ts_series = pd.to_numeric(df["ts"], errors='coerce').dropna().astype(int)
            close_series = pd.to_numeric(df["close"], errors='coerce')
            if ts_series.empty or close_series.empty:
                return
            last_ts = int(ts_series.iloc[-1])
            cutoff = last_ts - self.lookback_days * 86400000
            
            valid_mask = (ts_series >= cutoff) & (close_series > 0) & np.isfinite(close_series)
            filtered_ts = ts_series[valid_mask].tolist()
            filtered_close = close_series[valid_mask].tolist()
            dedup = dict(zip(filtered_ts, filtered_close))
            self.price_history[coin] = sorted(dedup.items(), key=lambda x: x[0])
        except Exception as e:
            logger.warning("[MultiCoin] seed_from_df 异常 | coin=%s | err=%s", coin, e)

    def update(self, coin: str, timestamp_ms: int, price: float):
        """更新价格历史 (去重防止同一根 1h K线在 1 小时内被重复插入，且设置硬性长度上限避免列表重建 OOM)"""
        if not coin or price is None or timestamp_ms is None:
            return
        try:
            timestamp_ms = int(timestamp_ms)
            price = float(price)
            if not math.isfinite(price) or price <= 0:
                return
        except (ValueError, TypeError):
            return

        if coin not in self.price_history:
            self.price_history[coin] = []

        history = self.price_history[coin]
        if history and history[-1][0] == timestamp_ms:
            history[-1] = (timestamp_ms, price)
            return

        history.append((timestamp_ms, price))

        # 30 天 1h K 线最多约 720 根，硬限 1000 根防爆
        max_bars = self.lookback_days * 24 + 100
        if len(history) > max_bars:
            cutoff_ms = timestamp_ms - self.lookback_days * 24 * 3600 * 1000
            self.price_history[coin] = [
                (ts, p) for ts, p in history if ts >= cutoff_ms
            ][-max_bars:]
    
    def get_correlation(self, coin1: str, coin2: str) -> float:
        """
        计算两个币种的价格相关性（基于收益率）
        
        返回 [-1, 1]：
          1.0 = 完全正相关
          0.0 = 无相关
         -1.0 = 完全负相关
        """
        if coin1 not in self.price_history or coin2 not in self.price_history:
            return 0.0
        
        hist1 = self.price_history[coin1]
        hist2 = self.price_history[coin2]
        
        if len(hist1) < 20 or len(hist2) < 20:
            return 0.0
        
        # 对齐时间戳（取交集）
        ts1 = {ts: p for ts, p in hist1}
        ts2 = {ts: p for ts, p in hist2}
        common_ts = sorted(set(ts1.keys()) & set(ts2.keys()))
        
        if len(common_ts) < 20:
            return 0.0
        
        # 计算收益率序列 (严格做 NaN 与非正值防爆检查)
        prices1 = np.asarray([ts1[ts] for ts in common_ts], dtype=float)
        prices2 = np.asarray([ts2[ts] for ts in common_ts], dtype=float)
        if not (np.all(np.isfinite(prices1)) and np.all(np.isfinite(prices2))):
            return 0.0
        if np.any(prices1 <= 0.0) or np.any(prices2 <= 0.0):
            return 0.0
        
        returns1 = np.diff(prices1) / prices1[:-1]
        returns2 = np.diff(prices2) / prices2[:-1]
        if not (np.all(np.isfinite(returns1)) and np.all(np.isfinite(returns2))):
            return 0.0
        
        # 计算相关系数
        if len(returns1) < 2:
            return 0.0
        
        std1 = float(np.std(returns1))
        std2 = float(np.std(returns2))
        if std1 < 1e-12 or std2 < 1e-12:
            return 0.0
        
        corr_matrix = np.corrcoef(returns1, returns2)
        corr = corr_matrix[0, 1]
        
        return float(corr) if math.isfinite(corr) else 0.0
    
    def get_all_correlations(self, coins: List[str]) -> Dict[Tuple[str, str], float]:
        """计算所有币种对的相关性"""
        if not coins or not isinstance(coins, (list, tuple)):
            return {}
        unique_coins = [c for c in dict.fromkeys(coins) if isinstance(c, str) and c]
        correlations = {}
        for i, coin1 in enumerate(unique_coins):
            for coin2 in unique_coins[i+1:]:
                corr = self.get_correlation(coin1, coin2)
                correlations[(coin1, coin2)] = corr
                correlations[(coin2, coin1)] = corr  # 对称
        return correlations


class MultiCoinRouter:
    """多币种轮动调度器"""
    
    def __init__(self, config_path: str = "config_aggressive.json"):
        if isinstance(config_path, (list, tuple)):
            self.config = {}
            self.coins = [str(c).strip().upper() for c in config_path]
        else:
            self.config = self._load_config(config_path)
            self.coins = (
                self.config.get("coins")
                or self.config.get("multi_coin", {}).get("coins", ["BTC", "ETH", "SUI", "ARB"])
            )
        
        self.market_clients: Dict[str, MarketData] = {
            coin: MarketData(coin=coin) for coin in self.coins
        }
        self.corr_tracker = CorrelationTracker(
            lookback_days=self.config.get("multi_coin", {}).get("correlation_lookback_days", 30)
        )
        self.current_position: Optional[CoinSignal] = None
        self.switch_threshold = self.config.get("multi_coin", {}).get("switch_threshold_delta", 10)
        self.corr_penalty_threshold = self.config.get("multi_coin", {}).get("correlation_penalty_threshold", 0.7)
        self.latest_radar_scores: Dict[str, dict] = {}
        self.latest_coin_prices: Dict[str, float] = {}

    def _load_config(self, path: str) -> dict:
        """加载配置文件（支持相对路径自动解析）"""
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[MultiCoin] 加载配置失败: {e}，使用默认配置")
            return {}
    
    def evaluate_coin_signal(self, coin: str,
                             calculate_signal_fn,
                             calculate_trs_fn,
                             macro_state) -> Optional[CoinSignal]:
        """评估单个币种信号（严格对齐 strategy.calculate_signal_score 签名与返回键）"""
        try:
            market = self.market_clients[coin]
            df_5m, price = market.get_closed_ohlcv("5m", 120, use_cache=False)
            df_1h = market.get_ohlcv("1h", 500)
            df_4h = market.get_ohlcv("4h", 500)
            fr_stats = market.get_funding_stats(168)

            # 使用 1h 已闭合 K 线更新相关性追踪器，消除 5m 微观结构噪声
            if len(df_1h) > 2 and "ts" in df_1h.columns:
                closed_1h = df_1h.iloc[-2]
                self.corr_tracker.update(coin, int(closed_1h["ts"]), float(closed_1h["close"]))

            # 计算单币种 TRS 趋势强度并映射各币种专属宏观状态
            trs = calculate_trs_fn(df_1h, df_4h, fr_stats, price)
            try:
                from strategy import map_trs_to_state
                coin_macro = map_trs_to_state(trs)
            except Exception:
                coin_macro = macro_state

            signal_result = calculate_signal_fn(
                df_5m=df_5m,
                df_1h=df_1h,
                macro_state=coin_macro,
                funding_stats=fr_stats,
            )
            score = float(signal_result.get("score", 0.0)) if signal_result else 0.0
            direction = signal_result.get("direction") if signal_result else None

            # 无论是否达标，均记录最新全量雷达打分供看板与状态机持久化
            self.latest_radar_scores[coin] = {
                "score": round(score, 1),
                "direction": direction or "NEUTRAL",
                "price": price,
                "trs": round(trs, 4)
            }
            self.latest_coin_prices[coin] = price

            if not signal_result:
                return None

            signal_gate = float(self.config.get("signal_evaluation", {}).get("signal_gate", 70.0))
            if direction is None or score < signal_gate:
                return None

            if (direction == "SHORT" and trs > 0.40) or (direction == "LONG" and trs < -0.40):
                logger.info(f"[MultiCoin] {coin} 强单边趋势逆势拦截 | dir={direction} | TRS={trs:+.2f}")
                return None

            tp_raw = signal_result.get("raw_tp") or signal_result.get("tp")
            sl_raw = signal_result.get("raw_sl") or signal_result.get("sl")
            if tp_raw is None or sl_raw is None:
                return None
            try:
                tp_val = float(tp_raw)
                sl_val = float(sl_raw)
                if not (math.isfinite(tp_val) and math.isfinite(sl_val) and tp_val > 0 and sl_val > 0):
                    return None
            except (ValueError, TypeError):
                return None

            import utils
            calc_atr = utils.atr(df_5m["high"], df_5m["low"], df_5m["close"], 14)
            atr_14 = float(calc_atr) if (calc_atr is not None and math.isfinite(calc_atr) and calc_atr > 0) else 0.0

            return CoinSignal(
                coin=coin,
                score=score,
                direction=direction,
                price=price,
                tp=tp_val,
                sl=sl_val,
                entry_score=score,
                macro_state=macro_state.value if hasattr(macro_state, "value") else str(macro_state),
                trs=trs,
                timestamp=datetime.now(timezone.utc),
                df_5m=df_5m,
                df_1h=df_1h,
                atr_14=atr_14,
                raw_signal_result=signal_result
            )
        except MarketDataError as e:
            logger.warning(f"[MultiCoin] {coin} 行情获取失败: {e}")
            return None
        except Exception as e:
            logger.error(f"[MultiCoin] {coin} 信号评估异常: {e}", exc_info=True)
            return None

    def apply_correlation_penalty(self, signal: CoinSignal, base_coin: str) -> float:
        """
        计算单币种相对基准币种的相关性软惩罚。
        惩罚上限严格截断在 <= 5.0 分以内，防止同质化直接腰斩 50%。
        """
        corr = self.corr_tracker.get_correlation(signal.coin, base_coin)
        denom = 1.0 - self.corr_penalty_threshold
        if corr is not None and math.isfinite(corr) and denom > 1e-6 and corr > self.corr_penalty_threshold:
            penalty_factor = (corr - self.corr_penalty_threshold) / denom
            penalty = min(signal.score * penalty_factor * 0.5, 5.0)
            logger.info(f"[MultiCoin] {signal.coin} 相关性惩罚 | 与 {base_coin} 相关系数={corr:.2f} | 扣分={penalty:.1f}")
            return max(signal.score - penalty, 0.0)
        return signal.score

    def select_best_coin(self, signals: List[CoinSignal], benchmark_coin: str = "BTC") -> Optional[CoinSignal]:
        """
        从候选信号列表中选出最佳币种。
        使用确定性复合四元组排序：
        (得分, 动量强度绝对值, 盈亏比, 币种名称)
        """
        if not signals:
            return None

        ref_coin = self.current_position.coin if self.current_position else benchmark_coin
        for s in signals:
            if ref_coin and s.coin != ref_coin:
                s.ranking_score = self.apply_correlation_penalty(s, ref_coin)
            else:
                s.ranking_score = s.score
        
        signal_gate = self.config.get("signal_evaluation", {}).get("signal_gate", 70)
        valid_signals = [s for s in signals if s.ranking_score >= signal_gate]
        if not valid_signals:
            return None
        
        return max(
            valid_signals,
            key=lambda s: (
                s.ranking_score,
                abs(s.trs),
                abs(s.tp - s.price) / max(abs(s.price - s.sl), 1e-9),
                s.coin,
            )
        )

    def should_switch_position(self, new_signal: CoinSignal) -> bool:
        """
        判断是否应该切换仓位
        
        条件：
          1. 当前无持仓 → 直接开仓
          2. 新信号与当前持仓同币种 → 不切换
          3. 新信号显著强于当前持仓（delta >= switch_threshold） → 切换
        """
        if self.current_position is None:
            return True
        
        if new_signal.coin == self.current_position.coin:
            return False
        
        score_delta = new_signal.score - self.current_position.entry_score
        
        if score_delta >= self.switch_threshold:
            logger.info(
                f"[MultiCoin] 触发仓位切换 | "
                f"当前: {self.current_position.coin} (分数={self.current_position.entry_score:.1f}) | "
                f"新信号: {new_signal.coin} (分数={new_signal.score:.1f}) | "
                f"差值={score_delta:.1f} >= 阈值={self.switch_threshold}"
            )
            return True
        
        return False
    
    def scan_all_coins(self, calculate_signal_fn, calculate_trs_fn, macro_state) -> Tuple[List[CoinSignal], Dict[str, dict], Dict[str, float]]:
        """
        多线程并发扫描所有币种，输出候选合格信号列表与雷达全量打分。
        """
        from concurrent.futures import ThreadPoolExecutor

        self.latest_radar_scores = {}
        self.latest_coin_prices = {}

        def _eval(c):
            try:
                return self.evaluate_coin_signal(c, calculate_signal_fn, calculate_trs_fn, macro_state)
            except Exception as e:
                logger.error(f"[MultiCoin] 并发扫描 {c} 异常: {e}")
                return None

        with ThreadPoolExecutor(max_workers=max(1, min(len(self.coins), 4))) as executor:
            signals_raw = list(executor.map(_eval, self.coins))

        valid_signals = [s for s in signals_raw if s is not None]
        return valid_signals, self.latest_radar_scores.copy(), self.latest_coin_prices.copy()

    def _collect_coin_signals(self, calculate_signal_fn, calculate_trs_fn, macro_state) -> List[CoinSignal]:
        """收集并评估所有币种的候选信号"""
        valid_signals, _, _ = self.scan_all_coins(calculate_signal_fn, calculate_trs_fn, macro_state)
        return valid_signals

    def get_routing_decision(self, 
                            calculate_signal_fn,
                            calculate_trs_fn,
                            macro_state) -> Optional[CoinSignal]:
        """
        主调度逻辑：返回应该开仓的币种信号
        """
        signals = self._collect_coin_signals(calculate_signal_fn, calculate_trs_fn, macro_state)
        
        if not signals:
            logger.info("[MultiCoin] 无合格信号")
            return None
        
        # 选择最佳
        best_signal = self.select_best_coin(signals)
        
        if best_signal is None:
            logger.info("[MultiCoin] 信号被门槛过滤")
            return None
        
        # 判断是否切换
        if self.should_switch_position(best_signal):
            return best_signal
        else:
            logger.info(
                f"[MultiCoin] 维持当前持仓 | "
                f"{self.current_position.coin if self.current_position else 'None'} | "
                f"最优候选 {best_signal.coin}={best_signal.score:.1f} 未达切换阈值"
            )
            return None

    def register_position(self, signal: CoinSignal):
        """注册新持仓"""
        self.current_position = signal
        logger.info(
            f"[MultiCoin] 注册持仓 | {signal.coin} | "
            f"方向={signal.direction} | 分数={signal.score:.1f} | 价格={signal.price:.2f}"
        )
    
    def clear_position(self):
        """清除持仓记录"""
        if self.current_position:
            logger.info(f"[MultiCoin] 清除持仓 | {self.current_position.coin}")
        self.current_position = None
    
    def get_correlation_report(self) -> str:
        """生成相关性报告"""
        if len(self.coins) < 2:
            return "币种数量不足，无法计算相关性"
        
        correlations = self.corr_tracker.get_all_correlations(self.coins)
        
        lines = ["═══════════════════════════════════════"]
        lines.append("         币种相关性矩阵")
        lines.append("═══════════════════════════════════════")
        
        for i, coin1 in enumerate(self.coins):
            for coin2 in self.coins[i+1:]:
                corr = correlations.get((coin1, coin2), 0.0)
                lines.append(f"{coin1:6s} <-> {coin2:6s}  {corr:+.3f}")
        
        lines.append("═══════════════════════════════════════")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 使用示例（集成到main.py）
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    集成示例：
    
    在 main.py 的 TradingBot.__init__() 中：
        from multi_coin_router import MultiCoinRouter
        self.router = MultiCoinRouter(config_path="config_aggressive.json")
    
    在 _idle_cycle() 信号评估处：
        # 原来的单币种逻辑：
        # signal = calculate_signal_score(...)
        
        # 改为多币种路由：
        from strategy import calculate_signal_score, calculate_quant_trs
        best_signal = self.router.get_routing_decision(
            calculate_signal_fn=calculate_signal_score,
            calculate_trs_fn=calculate_quant_trs,
            macro_state=self.final_macro_state
        )
        
        if best_signal is None:
            return  # 无合格信号或不需要切换
        
        # 如果需要切换，先平掉当前持仓
        if self.router.current_position and best_signal.coin != self.router.current_position.coin:
            self._close_position(reason="币种切换")
        
        # 开新仓
        self._open_position(
            direction=best_signal.direction,
            coin=best_signal.coin,
            price=best_signal.price,
            tp=best_signal.tp,
            sl=best_signal.sl
        )
        
        # 注册持仓
        self.router.register_position(best_signal)
    
    在平仓完成后：
        self.router.clear_position()
    """
    print("MultiCoinRouter 模块已就绪")
    print("请参考上方示例集成到 main.py")

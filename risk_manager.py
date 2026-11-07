# -*- coding: utf-8 -*-
"""
风险管理模块 (RiskManager)
═══════════════════════════════════════════════════════════════════════

核心功能：
1. Kelly Criterion / ATR 动态仓位计算（硬顶 5x 杠杆保护）
2. Portfolio Heat 组合热度全域风控（并发锁 + 槽位预占原子化，防 TOCTOU）
3. 最大回撤（30%）与单日亏损（15%）熔断
4. 动态移动止盈止损计算（基于 peak_price）
5. 交易历史统计（胜率、盈亏比、Sharpe、Drawdown）与状态持久化
"""

__version__ = "1.1.0"
__milestone__ = "v1.1-AUDIT-HARDENED"
__release_date__ = "2026-11-07"


import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from alerts import logger


def _safe_finite_float(val, default: Optional[float] = None) -> Optional[float]:
    """安全解析有限浮点数"""
    if val is None:
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (ValueError, TypeError):
        return default


@dataclass
class RiskConfig:
    """风控配置参数"""
    # Kelly Criterion
    kelly_fraction: float = 0.5            # 半凯利 (0.5 = Half Kelly)
    max_single_risk_pct: float = 4.0        # 单笔最大本金风险 (%)
    single_risk_pct: float = 2.5            # 标准单笔风险 (%)
    min_single_risk_pct: float = 0.5        # 探针/最小单笔风险 (%)
    max_leverage: float = 5.0              # 严格对齐底层的最高杠杆 Clamp[1, 5]
    min_kelly_trades: int = 30             # 凯利公式启动的最小样本量 (N >= 30 避免方差爆炸)
    kelly_reference_edge: float = 0.128    # R 空间基准半凯利期望值 (归一化锚点)
    kelly_min_scale: float = 0.20          # 凯利缩放倍数下界
    kelly_max_scale: float = 1.60          # 凯利缩放倍数上界
    
    # Portfolio Heat (组合热度与并发持仓)
    max_portfolio_heat_pct: float = 15.0   # 组合总风险敞口上限 (%)
    max_concurrent_positions: int = 1      # 单持仓轮动模式，最多持仓 1 个
    
    # 熔断闸门
    max_drawdown_pct: float = 30.0         # 峰值回撤熔断阈值 (%)
    daily_loss_limit_pct: float = 15.0     # 单日亏损熔断阈值 (%)
    
    # 动态止损/止盈
    breakeven_profit_r: float = 1.0        # 浮盈达到 1.0R 时移至保本
    trailing_start_r: float = 1.5          # 浮盈达到 1.5R 时启动追踪止损
    trailing_atr_multiplier: float = 1.5    # 追踪止损距离 (1.5 * ATR)
    
    # 连续亏损保护
    max_consecutive_losses: int = 3        # 连续亏损熔断笔数
    cooldown_hours_after_streak: float = 12.0  # 冷却时间 (小时)
    
    # 时间衰减平仓
    max_holding_bars: int = 24             # 最大持仓 K线数 (5m*24 = 2小时)
    time_decay_profit_threshold_r: float = 0.5  # 时间衰减触发平仓的浮盈阈值 (R)


@dataclass
class TradeRecord:
    """单笔成交结算记录"""
    entry_time: str
    exit_time: str
    coin: str
    direction: str                     # "LONG" / "SHORT"
    entry_price: float
    exit_price: float
    size: float                        # 名义开仓价值 (sz_notional)
    pnl: float                         # 真实盈亏金额 (USD)
    pnl_pct: float                     # 收益率 (例如 0.02 表示 +2%)
    pnl_r: float                       # 盈亏 R 倍数
    hold_duration_minutes: int
    exit_reason: str                   # "TP" / "SL" / "TRAILING_SL" / "TIME_DECAY" / "ROTATION" / "EMERGENCY"
    entry_equity: float = 0.0          # 开仓时的动态账户权益


@dataclass
class RiskMetrics:
    """全周期风控统计指标"""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    
    total_pnl: float = 0.0             # 累计已实现盈亏 (USD)
    total_pnl_pct: float = 0.0         # 累计收益率 (%)
    
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    
    max_drawdown_pct: float = 0.0
    current_drawdown_pct: float = 0.0
    peak_equity: float = 0.0
    
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    current_streak: int = 0            # >0 连胜, <0 连亏
    
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    def to_dict(self) -> dict:
        return asdict(self)


class RiskManager:
    """全周期风险管理器（线程安全 + TOCTOU 槽位锁保护）"""
    
    def __init__(self, config: Optional[RiskConfig] = None, 
                 initial_equity: float = 999.0,
                 persistence_file: str = "risk_state.json",
                 equity_history_file: Optional[str] = None):
        self._lock = threading.RLock()
        self.config = config or RiskConfig()
        self.initial_equity = float(initial_equity) if initial_equity > 0 else 999.0
        self.current_equity = self.initial_equity
        self.persistence_file = persistence_file
        
        # 测试与实盘文件隔离：若指定非默认 persistence_file 但未传 equity_history_file，则动态派生
        if equity_history_file is not None:
            self.equity_history_file = equity_history_file
        elif persistence_file != "risk_state.json":
            base = os.path.splitext(os.path.basename(persistence_file))[0]
            self.equity_history_file = f"equity_history_{base}.json"
        else:
            self.equity_history_file = "equity_history.json"
        
        self.trade_history: List[TradeRecord] = []
        self.metrics = RiskMetrics()
        self.metrics.peak_equity = self.initial_equity
        
        # 活跃持仓风险监控：{coin: risk_amount}
        self.active_positions_risk: Dict[str, float] = {}
        # 预占槽位（解决并发 TOCTOU）：{coin: (risk_amount, expire_mono_ts)}
        self._reserved_slots: Dict[str, Tuple[float, float]] = {}
        
        # 单日亏损追踪
        self.daily_pnl = 0.0
        self.daily_start_equity = self.initial_equity
        self.current_date = datetime.now(timezone.utc).date()
        self.cooldown_until: Optional[str] = None
        self._last_equity_record_ts = 0.0
        
        self._load_state()

    def sync_equity(self, current_equity: float):
        """同步交易所实时净值（带有限性校验与线程锁保护）"""
        f_eq = _safe_finite_float(current_equity)
        if f_eq is None or f_eq <= 0:
            return
        with self._lock:
            changed = abs(self.current_equity - f_eq) > 0.01
            self.current_equity = f_eq
            if f_eq > self.metrics.peak_equity:
                self.metrics.peak_equity = f_eq
            self._update_drawdown()
            if changed:
                self._save_state()
            self._append_equity_history()
    
    def calculate_kelly_position_size(self, win_rate: float, 
                                     avg_win_loss_ratio: float,
                                     avg_loss_r: float = 1.0,
                                     fallback_risk_pct: float = 1.0) -> float:
        """根据胜率、盈亏比与亏损尺度计算最优凯利风险百分比"""
        if win_rate <= 0 or avg_win_loss_ratio <= 0:
            return 0.0
        if win_rate >= 1.0:
            return self.config.max_single_risk_pct

        loss_scale = max(float(avg_loss_r), 1.0) if (avg_loss_r and avg_loss_r > 0) else 1.0
        kelly = (win_rate - (1.0 - win_rate) / avg_win_loss_ratio) / loss_scale
        if kelly <= 0:
            return 0.0
        kelly_risk = kelly * self.config.kelly_fraction * 100.0
        return float(np.clip(kelly_risk, 0.0, self.config.max_single_risk_pct))

    def get_adaptive_position_size(self, stop_loss_distance: float, 
                                   current_price: float,
                                   equity: Optional[float] = None,
                                   use_kelly: bool = True) -> float:
        """根据 ATR 止损距离与 Kelly/固定比例计算自适应名义开仓大小"""
        with self._lock:
            current_eq = equity if (equity and equity > 0 and math.isfinite(equity)) else self.current_equity
            if (
                not math.isfinite(stop_loss_distance)
                or stop_loss_distance <= 0
                or not math.isfinite(current_price)
                or current_price <= 0
                or not math.isfinite(current_eq)
                or current_eq <= 0
            ):
                return 0.0
            
            if use_kelly and self.metrics.total_trades >= self.config.min_kelly_trades:
                all_r = []
                for t in self.trade_history:
                    v = _safe_finite_float(getattr(t, "pnl_r", None))
                    if v is not None:
                        all_r.append(v)
                
                recent_r = all_r[-30:] if len(all_r) >= 30 else all_r
                if len(recent_r) >= self.config.min_kelly_trades:
                    if self.metrics.win_rate <= 0:
                        return 0.0
                    mean_r = float(np.mean(recent_r))
                    if mean_r <= 0:
                        logger.warning(
                            f"[RiskManager] 连续期望非正 (μ_R={mean_r:.3f}, win_rate={self.metrics.win_rate*100:.1f}%) | 降级至探针仓位 ({self.config.min_single_risk_pct}%)"
                        )
                        risk_pct = self.config.min_single_risk_pct
                    else:
                        scaling = self.get_kelly_scaling_factor()
                        risk_pct = float(np.clip(
                            self.config.single_risk_pct * scaling,
                            self.config.min_single_risk_pct,
                            self.config.max_single_risk_pct
                        ))
                else:
                    if self.metrics.win_rate <= 0:
                        return 0.0
                    win_rate = self.metrics.win_rate
                    wins_r = [r for r in all_r if r > 0]
                    losses_r = [abs(r) for r in all_r if r < 0]
                    avg_win_r = float(np.mean(wins_r)) if wins_r else 0.0
                    avg_loss_r = float(np.mean(losses_r)) if losses_r else 1.0
                    r_ratio = (avg_win_r / avg_loss_r) if (wins_r and losses_r and avg_loss_r > 0) else (
                        self.metrics.avg_win / self.metrics.avg_loss if (self.metrics.avg_loss > 0 and self.metrics.avg_win > 0) else 1.5
                    )
                    risk_pct = self.calculate_kelly_position_size(win_rate, r_ratio, avg_loss_r=avg_loss_r)
                    if risk_pct <= 0:
                        return 0.0
            else:
                risk_pct = self.config.single_risk_pct
            
            risk_amount = current_eq * (risk_pct / 100.0)
            fee_slippage_friction = 0.0020
            sl_pct = (stop_loss_distance / current_price) + fee_slippage_friction
            notional_size = risk_amount / max(sl_pct, 1e-4)
            
            # 1. 严格受 5x 杠杆硬上限保护
            max_notional_by_lev = current_eq * self.config.max_leverage
            notional_size = min(notional_size, max_notional_by_lev)
            
            # 2. Portfolio Heat 校验（清理过期预占槽位）
            self._clean_expired_reservations()
            valid_heats = [float(v) for v in self.active_positions_risk.values() if v is not None and math.isfinite(v)]
            reserved_heats = [float(v[0]) for v in self._reserved_slots.values() if v[0] is not None and math.isfinite(v[0])]
            total_heat = sum(valid_heats) + sum(reserved_heats)
            available_heat = current_eq * (self.config.max_portfolio_heat_pct / 100.0)
            remaining_risk = max(available_heat - total_heat, 0.0)
            
            if risk_amount > remaining_risk:
                notional_size = remaining_risk / max(sl_pct, 1e-4)
                notional_size = min(notional_size, max_notional_by_lev)
                
            return max(float(notional_size), 0.0)
    
    def get_kelly_scaling_factor(self) -> float:
        """返回基于连续二阶矩凯利公式的平滑仓位缩放系数"""
        with self._lock:
            if self.metrics.total_trades < self.config.min_kelly_trades:
                return 1.0

            history_snapshot = list(self.trade_history)
            all_r = []
            for t in history_snapshot:
                fv = _safe_finite_float(getattr(t, "pnl_r", None))
                if fv is not None:
                    all_r.append(fv)
            recent_r = all_r[-30:] if len(all_r) >= 30 else all_r
            if len(recent_r) < self.config.min_kelly_trades or self.metrics.win_rate <= 0:
                return 0.25

            mean_r = float(np.mean(recent_r))
            if mean_r <= 0:
                logger.warning(
                    "[RiskManager] 30笔滚动数学期望非正 (μ_R=%.3f) | 启用 0.25x 探针缩放",
                    mean_r
                )
                return 0.25

            var_r = float(np.var(recent_r, ddof=1)) if len(recent_r) > 1 else 1.0
            downside_var = float(np.mean([min(0.0, r) ** 2 for r in recent_r]))
            second_moment = var_r + 0.5 * downside_var + mean_r ** 2

            f_star = (mean_r / max(second_moment, 1e-6)) * self.config.kelly_fraction
            f_target = self.config.kelly_reference_edge
            return float(np.clip(f_star / f_target, self.config.kelly_min_scale, self.config.kelly_max_scale))

    def reset_streak_after_cooldown(self):
        """冷却期满后重置连续亏损计数，破除风控死锁"""
        with self._lock:
            if self.metrics.current_streak <= -self.config.max_consecutive_losses:
                logger.info(
                    f"[RiskManager] 连续亏损冷却期满 | 重置 current_streak ({self.metrics.current_streak} -> 0)"
                )
                self.metrics.current_streak = 0
                self.cooldown_until = None
                self._save_state()

    def _check_and_roll_date(self):
        """自动检测跨日并重置单日统计指标，根治日亏熔断跨日死锁"""
        today = datetime.now(timezone.utc).date()
        if today != self.current_date:
            self.daily_pnl = 0.0
            self.current_date = today
            self.daily_start_equity = max(self.current_equity, 1e-4)

    def _clean_expired_reservations(self):
        """清除超时的槽位预占（默认 30s 超时）"""
        now_mono = time.monotonic()
        expired = [coin for coin, (_, exp) in self._reserved_slots.items() if now_mono >= exp]
        for coin in expired:
            self._reserved_slots.pop(coin, None)

    def reserve_position_slot(self, coin: str, risk_amount: float, ttl_seconds: float = 30.0) -> bool:
        """原子预占开仓槽位，消解 TOCTOU 竞态"""
        with self._lock:
            self._clean_expired_reservations()
            total_slots = len(self.active_positions_risk) + len(self._reserved_slots)
            if coin not in self.active_positions_risk and coin not in self._reserved_slots:
                if total_slots >= self.config.max_concurrent_positions:
                    return False
            self._reserved_slots[coin] = (float(risk_amount), time.monotonic() + ttl_seconds)
            return True

    def release_position_slot(self, coin: str):
        """释放预占槽位（开仓未成交或异常时调用）"""
        with self._lock:
            self._reserved_slots.pop(coin, None)

    def confirm_position(self, coin: str, risk_amount: float):
        """将预占槽位确认为正式持仓"""
        with self._lock:
            self._reserved_slots.pop(coin, None)
            self.active_positions_risk[coin] = float(risk_amount)

    def can_open_position(self, estimated_risk_amount: float,
                          equity: Optional[float] = None,
                          coin: Optional[str] = None,
                          reserve_slot: bool = False) -> Tuple[bool, str]:
        """
        开仓前综合风控阻断检查。
        reserve_slot=True 时在检查通过的同时原子预占仓位槽位，彻底杜绝多币种并发下的 TOCTOU 穿透。
        """
        with self._lock:
            self._check_and_roll_date()
            if equity and equity > 0 and math.isfinite(equity):
                self.sync_equity(equity)
            current_eq = equity if (equity and equity > 0 and math.isfinite(equity)) else self.current_equity
                
            # 1. 最大回撤检查
            if self.metrics.current_drawdown_pct >= self.config.max_drawdown_pct:
                return False, f"峰值回撤超限 ({self.metrics.current_drawdown_pct:.1f}% >= {self.config.max_drawdown_pct}%)"
            
            # 2. 单日亏损检查
            ref_equity = getattr(self, "daily_start_equity", None)
            if not ref_equity or ref_equity <= 0:
                ref_equity = self.current_equity if self.current_equity > 0 else self.initial_equity
            daily_loss_pct = (abs(self.daily_pnl) / max(ref_equity, 1e-6)) * 100.0
            if self.daily_pnl < 0 and daily_loss_pct >= self.config.daily_loss_limit_pct:
                return False, f"单日亏损超限 ({daily_loss_pct:.1f}% >= {self.config.daily_loss_limit_pct}%)"
            
            # 3. 连续亏损熔断检查 (带到期时间自愈检测，根治死锁)
            if self.metrics.current_streak <= -self.config.max_consecutive_losses:
                now_utc = datetime.now(timezone.utc)
                if self.cooldown_until:
                    try:
                        cd_dt = datetime.fromisoformat(str(self.cooldown_until))
                        if cd_dt.tzinfo is None:
                            cd_dt = cd_dt.replace(tzinfo=timezone.utc)
                        if now_utc >= cd_dt:
                            self.reset_streak_after_cooldown()
                        else:
                            rem_h = (cd_dt - now_utc).total_seconds() / 3600.0
                            return False, f"连续亏损熔断冷却中 (剩余 {rem_h:.1f}h)"
                    except Exception as e:
                        logger.warning("[RiskManager] 解析 cooldown_until 异常: %s | 重建冷却计时", e)
                        cd_time = now_utc + timedelta(hours=self.config.cooldown_hours_after_streak)
                        self.cooldown_until = cd_time.isoformat()
                        return False, f"触发连续亏损冷却 (已连亏 {abs(self.metrics.current_streak)} 笔)"
                else:
                    # 发现连亏达标但 cooldown_until 缺失的脏状态，自动修复补齐冷却截止时间，严防永久死锁
                    logger.warning("[RiskManager] 状态不一致: streak<= -3 但 cooldown_until 为空 | 自动补全冷却倒计时")
                    cd_time = now_utc + timedelta(hours=self.config.cooldown_hours_after_streak)
                    self.cooldown_until = cd_time.isoformat()
                    self._save_state()
                    return False, f"触发连续亏损冷却 (已连亏 {abs(self.metrics.current_streak)} 笔，冷却 12h)"
            
            # 4. 最大持仓数量与预占检查 (消解并发 TOCTOU)
            self._clean_expired_reservations()
            active_cnt = len(self.active_positions_risk)
            reserved_cnt = len([c for c in self._reserved_slots if c != coin])
            if (active_cnt + reserved_cnt) >= self.config.max_concurrent_positions:
                return False, f"已达最大持仓数 (持仓 {active_cnt} + 预占 {reserved_cnt} >= 上限 {self.config.max_concurrent_positions})"
            
            # 5. Portfolio Heat 检查
            valid_heats = [float(v) for v in self.active_positions_risk.values() if v is not None and math.isfinite(v)]
            reserved_heats = [float(v[0]) for c, v in self._reserved_slots.items() if c != coin and v[0] is not None and math.isfinite(v[0])]
            total_heat = sum(valid_heats) + sum(reserved_heats)
            max_heat = current_eq * (self.config.max_portfolio_heat_pct / 100.0)
            risk_amt = float(estimated_risk_amount) if (estimated_risk_amount and math.isfinite(estimated_risk_amount)) else 0.0
            if total_heat + risk_amt > max_heat * 1.05:  # 容许 5% 浮动容差
                return False, f"组合热度超限 (当前 {total_heat:.1f} + 新增 {risk_amt:.1f} > 上限 {max_heat:.1f})"

            # 6. 凯利数学期望检查 (破除吸收态死锁：期望非正时仅允许探针仓位)
            if self.metrics.total_trades >= self.config.min_kelly_trades:
                all_r = []
                for t in self.trade_history:
                    fv = _safe_finite_float(getattr(t, "pnl_r", None))
                    if fv is not None:
                        all_r.append(fv)
                recent_r = all_r[-30:] if len(all_r) >= 30 else all_r
                mean_r = float(np.mean(recent_r)) if recent_r else 0.0
                if self.metrics.win_rate <= 0 or mean_r <= 0:
                    max_probe_risk = current_eq * (self.config.min_single_risk_pct / 100.0) * 1.5
                    if risk_amt > max_probe_risk:
                        return False, f"策略处于负期望区间 (μ_R={mean_r:.3f})，仅允许探针仓位 (超限 {risk_amt:.2f} > {max_probe_risk:.2f})"
                
            if reserve_slot and coin:
                self.reserve_position_slot(coin, risk_amt)

            return True, "风控校验通过"
    
    def register_position(self, position_id: str, risk_amount: float):
        """登记新开仓位风险"""
        with self._lock:
            self._reserved_slots.pop(position_id, None)
            f_amt = _safe_finite_float(risk_amount, 0.0)
            self.active_positions_risk[position_id] = f_amt or 0.0
    
    def unregister_position(self, position_id: str):
        """注销已平仓仓位风险"""
        with self._lock:
            self._reserved_slots.pop(position_id, None)
            self.active_positions_risk.pop(position_id, None)
    
    def update_dynamic_stops(self, entry_price: float, current_price: float,
                            initial_sl: float, current_sl: float,
                            direction: str, atr: float,
                            bars_held: int,
                            peak_price: Optional[float] = None) -> Tuple[float, Optional[float], Optional[str]]:
        """动态止盈止损计算（支持基于 peak_price 移动追踪）"""
        is_long = (direction == "LONG")
        initial_risk = abs(entry_price - initial_sl)
        if initial_risk <= 0 or not math.isfinite(initial_risk):
            return current_sl, None, None
        
        pnl = (current_price - entry_price) if is_long else (entry_price - current_price)
        profit_r = pnl / initial_risk
        
        new_sl = current_sl
        new_tp = None
        close_signal = None
        
        # 1. 浮盈达到 1.0R 时移至保本价（+0.15% 覆盖双边 Taker 手续费与滑点）
        if profit_r >= self.config.breakeven_profit_r:
            if is_long:
                be_sl = entry_price * 1.0015
                new_sl = max(new_sl, be_sl)
            else:
                be_sl = entry_price * 0.9985
                new_sl = min(new_sl, be_sl)
        
        # 2. 浮盈达到 1.5R 启动追踪止损（基于 peak_price 回撤 1.5 * ATR，校验 atr 有效性防 NaN 污染）
        if profit_r > self.config.trailing_start_r and atr and math.isfinite(atr) and atr > 0:
            ref_peak = peak_price if (peak_price and peak_price > 0 and math.isfinite(peak_price)) else current_price
            trailing_distance = atr * self.config.trailing_atr_multiplier
            if is_long:
                trailing_sl = ref_peak - trailing_distance
                new_sl = max(new_sl, trailing_sl)
            else:
                trailing_sl = ref_peak + trailing_distance
                new_sl = min(new_sl, trailing_sl)
        
        # 3. 时间衰减平仓（持仓满 24 根 K线且浮盈未达 0.5R）
        if bars_held >= self.config.max_holding_bars:
            if profit_r < self.config.time_decay_profit_threshold_r:
                close_signal = "TIME_DECAY"
        
        return new_sl, new_tp, close_signal
    
    def record_trade(self, trade: TradeRecord):
        """记录已完成交易，更新各项统计指标（消除 cooldown 误置空死锁）"""
        with self._lock:
            if getattr(trade, "entry_equity", 0.0) <= 0:
                trade.entry_equity = self.current_equity
                
            self.trade_history.append(trade)
            
            # 更新资金与单日亏损
            self._check_and_roll_date()
            pnl_val = _safe_finite_float(trade.pnl, 0.0)
            self.current_equity += pnl_val
            self.daily_pnl += pnl_val
            
            # 更新交易计数与指标
            self.metrics.total_trades += 1
            if pnl_val > 0:
                self.metrics.winning_trades += 1
            elif pnl_val < 0:
                self.metrics.losing_trades += 1
            
            self.metrics.total_pnl += pnl_val
            self.metrics.total_pnl_pct = (self.current_equity / max(self.initial_equity, 1e-6) - 1.0) * 100.0
            
            self._update_win_metrics()
            self._update_drawdown()
            self._update_streak(pnl_val)
            self._update_risk_adjusted_metrics()
            self._save_state()
            self._append_equity_history()
            
            logger.info(
                f"[RiskManager] 记录交易 | 币种={trade.coin} | 盈亏=${trade.pnl:+.2f} ({trade.pnl_pct*100:+.2f}%) | "
                f"R倍数={trade.pnl_r:+.2f}R | 净值=${self.current_equity:.2f} | 胜率={self.metrics.win_rate*100:.1f}%"
            )
    
    def _update_win_metrics(self):
        if self.metrics.total_trades == 0:
            return
        self.metrics.win_rate = self.metrics.winning_trades / self.metrics.total_trades
        wins = [t.pnl for t in self.trade_history if t.pnl > 0 and math.isfinite(t.pnl)]
        losses = [abs(t.pnl) for t in self.trade_history if t.pnl < 0 and math.isfinite(t.pnl)]
        self.metrics.avg_win = float(np.mean(wins)) if wins else 0.0
        self.metrics.avg_loss = float(np.mean(losses)) if losses else 0.0
        total_win = sum(wins)
        total_loss = sum(losses)
        self.metrics.profit_factor = total_win / total_loss if total_loss > 0 else float("inf")
    
    def _update_drawdown(self):
        if self.current_equity > self.metrics.peak_equity:
            self.metrics.peak_equity = self.current_equity
        if self.metrics.peak_equity > 0:
            dd_pct = ((self.metrics.peak_equity - self.current_equity) / self.metrics.peak_equity) * 100.0
            self.metrics.current_drawdown_pct = max(dd_pct, 0.0)
            self.metrics.max_drawdown_pct = max(self.metrics.max_drawdown_pct, self.metrics.current_drawdown_pct)
    
    def _update_streak(self, pnl: float):
        """精准维护连胜/连亏计数与熔断冷却截止时间"""
        if pnl > 0:
            self.metrics.current_streak = (
                self.metrics.current_streak + 1 if self.metrics.current_streak > 0 else 1
            )
            self.metrics.max_consecutive_wins = max(
                self.metrics.max_consecutive_wins, self.metrics.current_streak
            )
            self.cooldown_until = None
        elif pnl < 0:
            self.metrics.current_streak = (
                self.metrics.current_streak - 1 if self.metrics.current_streak < 0 else -1
            )
            self.metrics.max_consecutive_losses = max(
                self.metrics.max_consecutive_losses, abs(self.metrics.current_streak)
            )
            if self.metrics.current_streak <= -self.config.max_consecutive_losses:
                cd_time = datetime.now(timezone.utc) + timedelta(hours=self.config.cooldown_hours_after_streak)
                self.cooldown_until = cd_time.isoformat()
        else:
            # pnl == 0 (微亏/平本): 保持 streak 且若已在熔断中则保持 cooldown_until 不变，防止死锁
            if self.metrics.current_streak <= -self.config.max_consecutive_losses and not self.cooldown_until:
                cd_time = datetime.now(timezone.utc) + timedelta(hours=self.config.cooldown_hours_after_streak)
                self.cooldown_until = cd_time.isoformat()
    
    def _update_risk_adjusted_metrics(self):
        if len(self.trade_history) < 2:
            self.metrics.sharpe_ratio = 0.0
            self.metrics.sortino_ratio = 0.0
            return

        returns = []
        for t in self.trade_history:
            denom = max(getattr(t, "entry_equity", 0.0) if getattr(t, "entry_equity", 0.0) > 0 else self.initial_equity, 1e-6)
            if math.isfinite(t.pnl) and math.isfinite(denom):
                returns.append(t.pnl / denom)
        
        if len(returns) < 2:
            return

        daily_pnl_map: Dict[str, float] = {}
        for t in self.trade_history:
            if t.exit_time:
                day_str = str(t.exit_time)[:10]
                if math.isfinite(t.pnl):
                    daily_pnl_map[day_str] = daily_pnl_map.get(day_str, 0.0) + t.pnl

        valid_dates = sorted([d for d in daily_pnl_map.keys() if len(d) == 10 and d.count("-") == 2])
        if len(valid_dates) >= 2:
            try:
                start_date = datetime.strptime(valid_dates[0], "%Y-%m-%d").date()
                end_date = datetime.strptime(valid_dates[-1], "%Y-%m-%d").date()
                total_days = (end_date - start_date).days + 1
                if total_days >= 7:
                    calendar_returns = []
                    first_eq = getattr(self.trade_history[0], "entry_equity", 0.0)
                    running_eq = max(first_eq if first_eq > 0 else self.initial_equity, 1e-6)
                    for i in range(total_days):
                        cur_day = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
                        day_pnl = daily_pnl_map.get(cur_day, 0.0)
                        ret = day_pnl / running_eq
                        calendar_returns.append(ret)
                        running_eq = max(running_eq + day_pnl, 1e-6)
                    
                    d_mean = float(np.mean(calendar_returns))
                    d_std = float(np.std(calendar_returns, ddof=1))
                    scale = math.sqrt(365)
                    self.metrics.sharpe_ratio = (d_mean / d_std) * scale if d_std > 1e-9 else 0.0

                    cal_downside = [min(0.0, r) ** 2 for r in calendar_returns]
                    cal_downside_dev = math.sqrt(sum(cal_downside) / max(len(calendar_returns), 1))
                    self.metrics.sortino_ratio = (d_mean / cal_downside_dev) * scale if cal_downside_dev > 1e-9 else (self.metrics.sharpe_ratio * 2.0 if d_mean > 0 else 0.0)
                    return
            except Exception as e:
                logger.warning(f"[RiskManager] 连续日历日序列构建异常: {e}")
        self.metrics.sharpe_ratio = 0.0
        self.metrics.sortino_ratio = 0.0

    def _save_state(self):
        try:
            state = {
                "initial_equity": self.initial_equity,
                "current_equity": self.current_equity,
                "peak_equity": self.metrics.peak_equity,
                "daily_pnl": self.daily_pnl,
                "daily_start_equity": getattr(self, "daily_start_equity", self.initial_equity),
                "cooldown_until": getattr(self, "cooldown_until", None),
                "current_date": str(self.current_date),
                "metrics": self.metrics.to_dict(),
                "recent_trades": [asdict(t) for t in self.trade_history[-500:]],
            }
            tmp_path = f"{self.persistence_file}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.persistence_file)
        except Exception as e:
            logger.warning(f"[RiskManager] 保存状态异常: {e}")
    
    def _load_state(self):
        if not os.path.exists(self.persistence_file):
            return
        try:
            with open(self.persistence_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                logger.warning(f"[RiskManager] 状态文件格式异常 (非字典)，跳过加载: {type(state)}")
                return
            self.initial_equity = float(state.get("initial_equity") or self.initial_equity)
            self.current_equity = float(state.get("current_equity") or self.current_equity)
            self.daily_pnl = float(state.get("daily_pnl") or 0.0)
            self.daily_start_equity = float(state.get("daily_start_equity") or self.current_equity)
            self.cooldown_until = state.get("cooldown_until") or None
            date_str = state.get("current_date")
            if date_str:
                try:
                    self.current_date = datetime.strptime(str(date_str).strip(), "%Y-%m-%d").date()
                except Exception:
                    self.current_date = datetime.now(timezone.utc).date()
            m = state.get("metrics")
            if isinstance(m, dict):
                for k, v in m.items():
                    if hasattr(self.metrics, k):
                        setattr(self.metrics, k, v)
            raw_trades = state.get("recent_trades")
            if isinstance(raw_trades, list):
                self.trade_history = []
                for t in raw_trades:
                    if isinstance(t, dict):
                        try:
                            # 确保 pnl_r 与 entry_equity 具备合法浮点值
                            if "pnl_r" in t:
                                t["pnl_r"] = _safe_finite_float(t["pnl_r"], 0.0) or 0.0
                            if "entry_equity" not in t or not t["entry_equity"]:
                                t["entry_equity"] = self.initial_equity
                            self.trade_history.append(TradeRecord(**t))
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"[RiskManager] 加载状态失败: {e}")

    def _append_equity_history(self):
        """记录权益快照到 equity_history.json (防抖: 15s内且无显著变动则跳过)"""
        now = time.time()
        if hasattr(self, '_last_equity_record_ts') and now - self._last_equity_record_ts < 15.0:
            return
        self._last_equity_record_ts = now
        hist_file = self.equity_history_file
        history = []
        try:
            if os.path.exists(hist_file):
                with open(hist_file, "r", encoding="utf-8") as f:
                    history = json.load(f)
                    if not isinstance(history, list):
                        history = []
        except Exception:
            history = []
        
        point = {
            "ts": int(now * 1000),
            "equity": round(self.current_equity, 2),
            "drawdown_pct": round(self.metrics.current_drawdown_pct, 2)
        }
        if history and isinstance(history[-1], dict):
            last_p = history[-1]
            last_eq = last_p.get("equity")
            last_ts = _safe_finite_float(last_p.get("ts"), 0.0) or 0.0
            if last_eq == point["equity"] and (now - last_ts / 1000.0) < 180.0:
                return

        history.append(point)
        if len(history) > 5000:
            history = history[-5000:]
            
        try:
            tmp_hist = f"{hist_file}.tmp"
            with open(tmp_hist, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
            os.replace(tmp_hist, hist_file)
        except Exception as e:
            logger.warning(f"[RiskManager] 写入权益历史异常: {e}")

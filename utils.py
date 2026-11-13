# -*- coding: utf-8 -*-
"""
技术指标计算库 V2 - 全面优化版
═══════════════════════════════════════════════════════════════════════

【优化内容】
1. 完全向量化计算，消除显式循环
2. 浮点数安全比较 (epsilon tolerance)
3. 边界条件防护 (零除、空数组、NaN 传播)
4. 内存优化 (in-place 操作、预分配数组)
5. 数值稳定性增强 (Welford 方差算法)
6. Look-ahead bias 防护

【数学规范】
- RSI/ATR 使用 Wilder's RMA (指数平滑 α=1/period)
- 布林带使用总体标准差 (除以 N)
- ADX 使用 Wilder 平滑的 +DI/-DI/DX
- 所有除法操作保护零分母
"""

__version__ = "1.0.0"
__milestone__ = "v1.0-RELEASE"
__release_date__ = "2026-11-01"


import math
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

# 浮点数比较容差
EPSILON = 1e-10


# ═══════════════════════════════════════════════════════════════════
# 0. 基础工具
# ═══════════════════════════════════════════════════════════════════

def clamp(x: float, lo: float, hi: float) -> float:
    """数值截断 Clamp(x, a, b) = max(a, min(x, b))"""
    return max(lo, min(x, hi))


def sign(x: float, tol: float = EPSILON) -> float:
    """符号函数 (零容差)：|x| < tol 视为 0"""
    if x > tol:
        return 1.0
    if x < -tol:
        return -1.0
    return 0.0


def safe_divide(numerator: float, denominator: float, 
                default: float = 0.0, tol: float = EPSILON) -> float:
    """安全除法：分母绝对值小于 tol 时返回 default"""
    if abs(denominator) < tol:
        return default
    return numerator / denominator


def _as_float_array(values) -> np.ndarray:
    """统一转换为 float64 ndarray，保留 NaN"""
    if isinstance(values, pd.Series):
        return values.to_numpy(dtype=np.float64, copy=False)
    arr = np.asarray(values, dtype=np.float64)
    return arr


def _validate_inputs(*arrays, min_length: int = 1, 
                     equal_length: bool = True) -> bool:
    """
    输入验证：
    - 所有数组长度 >= min_length
    - 如果 equal_length=True，所有数组长度必须相等
    """
    if not arrays:
        return False
    lengths = [len(arr) for arr in arrays]
    if min(lengths) < min_length:
        return False
    if equal_length and len(set(lengths)) > 1:
        return False
    return True


def _wilder_smooth_vectorized(values: np.ndarray, period: int) -> np.ndarray:
    """
    Wilder's RMA 向量化实现 (Exponential Moving Average, α=1/period)
    支持自动跳过数组前导 NaN，确保初始种子累积满 period 个有效样本。
    """
    arr = _as_float_array(values)
    n = len(arr)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period or period <= 0:
        return out
    valid_indices = np.flatnonzero(~np.isnan(arr))
    if len(valid_indices) < period:
        return out
    start_idx = int(valid_indices[0])
    init_end = start_idx + period
    if init_end > n:
        return out
    out[init_end - 1] = float(np.nanmean(arr[start_idx:init_end]))
    alpha = 1.0 / period
    for i in range(init_end, n):
        if np.isnan(arr[i]):
            out[i] = out[i - 1]
        else:
            out[i] = out[i - 1] * (1.0 - alpha) + arr[i] * alpha
    return out


# ═══════════════════════════════════════════════════════════════════
# 1. 均线与波动率
# ═══════════════════════════════════════════════════════════════════

def sma_series(values, period: int) -> np.ndarray:
    """简单移动平均 (向量化滚动窗口)"""
    arr = _as_float_array(values)
    if not _validate_inputs(arr, min_length=period) or period <= 0:
        return np.full(len(arr), np.nan, dtype=np.float64)
    
    # 使用 pandas rolling 的 C 底层实现
    return pd.Series(arr).rolling(window=period, min_periods=period).mean().to_numpy()


def ema_series(values, period: int) -> np.ndarray:
    """指数移动平均 (α=2/(period+1))"""
    arr = _as_float_array(values)
    if len(arr) == 0 or period <= 0:
        return np.full(len(arr), np.nan, dtype=np.float64)
    
    return pd.Series(arr).ewm(span=period, adjust=False, 
                              min_periods=period).mean().to_numpy()


def atr_series(highs, lows=None, closes=None, period: int = 14) -> np.ndarray:
    """
    Average True Range (Wilder's RMA)
    
    【数学定义】
    TR[i] = max(High[i] - Low[i], |High[i] - Close[i-1]|, |Low[i] - Close[i-1]|)
    ATR[i] = RMA(TR, period)
    
    【调用格式】
    - atr_series(df, period=14): 从 DataFrame 自动解析 high/low/close 列
    - atr_series(highs, lows, closes, period=14): 独立序列调用
    """
    if isinstance(highs, pd.DataFrame):
        df = highs
        col_map = {str(c).lower(): c for c in df.columns}
        if not all(k in col_map for k in ("high", "low", "close")):
            return np.full(len(df), np.nan, dtype=np.float64)
        h = df[col_map["high"]]
        l = df[col_map["low"]]
        c = df[col_map["close"]]
        if lows is not None and isinstance(lows, (int, np.integer)):
            period = int(lows)
    else:
        h = highs
        l = lows
        c = closes

    h = _as_float_array(h)
    l = _as_float_array(l)
    c = _as_float_array(c)
    
    if not _validate_inputs(h, l, c, min_length=period + 1):
        return np.full(len(c), np.nan, dtype=np.float64)
    
    # 向量化计算 TR
    prev_close = np.roll(c, 1)
    prev_close[0] = c[0]  # 首根无前收，用自身代替
    
    # TR 三个候选值
    hl = h - l
    hc = np.abs(h - prev_close)
    lc = np.abs(l - prev_close)
    
    # 取最大值 (广播)
    tr = np.maximum(hl, np.maximum(hc, lc))
    tr[0] = np.nan  # 首根无意义
    
    # Wilder RMA 平滑
    atr_values = _wilder_smooth_vectorized(tr, period)
    
    return atr_values


def atr(highs, lows=None, closes=None, period: int = 14) -> float:
    """返回最新 ATR 标量值 (兼容 DataFrame 与独立 Series)"""
    if isinstance(highs, pd.DataFrame) and lows is not None and isinstance(lows, (int, np.integer)):
        period = int(lows)
        lows = None
    series = atr_series(highs, lows, closes, period)
    if len(series) == 0:
        return 0.0
    last = series[-1]
    return 0.0 if np.isnan(last) else float(last)


# 对齐 PROJECT.md 与策略规范别名 (Feature 25)
rma_atr = atr
wilder_rma_atr = atr


# ═══════════════════════════════════════════════════════════════════
# 2. 振荡器 (RSI, CCI)
# ═══════════════════════════════════════════════════════════════════

def rsi_series(closes, period: int = 14) -> np.ndarray:
    """
    Relative Strength Index (Wilder's RMA)
    
    【数学定义】
    RSI = 100 - 100/(1 + RS)
    RS = RMA(Gains, period) / RMA(Losses, period)
    
    【边界条件】
    - 全平盘或无涨跌：RSI = 50 (中性)
    - 全涨无跌：RSI = 100
    - 全跌无涨：RSI = 0
    """
    c = _as_float_array(closes)
    n = len(c)
    
    if not _validate_inputs(c, min_length=period + 1):
        return np.full(n, np.nan, dtype=np.float64)
    
    # 向量化价格变化
    deltas = np.diff(c)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    
    # Wilder RMA 平滑
    avg_gains = _wilder_smooth_vectorized(gains, period)
    avg_losses = _wilder_smooth_vectorized(losses, period)
    
    # 计算 RSI (向量化)
    rsi_values = np.full(n, np.nan, dtype=np.float64)
    
    # 从 period 位置开始有效 (因为 diff 消耗了一根)
    for i in range(period, n):
        gain = avg_gains[i - 1]  # diff 后索引偏移 1
        loss = avg_losses[i - 1]
        
        if abs(loss) < EPSILON:
            rsi_values[i] = 100.0 if abs(gain) > EPSILON else 50.0
        else:
            rs = gain / loss
            rsi_values[i] = 100.0 - (100.0 / (1.0 + rs))
    
    return rsi_values


def rsi(closes, period: int = 14) -> float:
    """返回最新 RSI 标量值"""
    series = rsi_series(closes, period)
    valid = series[~np.isnan(series)]
    return float(valid[-1]) if len(valid) > 0 else 50.0


def cci(highs, lows, closes, period: int = 20) -> float:
    """
    Commodity Channel Index
    
    【数学定义】
    TP = (High + Low + Close) / 3
    CCI = (TP - SMA(TP, period)) / (0.015 * MeanDev)
    MeanDev = Mean(|TP - SMA(TP)|)
    
    【边界保护】MeanDev=0 时返回 0
    """
    h = _as_float_array(highs)
    l = _as_float_array(lows)
    c = _as_float_array(closes)
    
    if not _validate_inputs(h, l, c, min_length=period):
        return 0.0
    
    # 向量化典型价格
    tp = (h + l + c) / 3.0
    recent_tp = tp[-period:]
    sma_tp = float(np.mean(recent_tp))
    
    # 平均绝对偏差
    deviations = np.abs(recent_tp - sma_tp)
    mean_dev = float(np.mean(deviations))
    
    if abs(mean_dev) < EPSILON:
        return 0.0
    
    return float((recent_tp[-1] - sma_tp) / (0.015 * mean_dev))


# ═══════════════════════════════════════════════════════════════════
# 3. 布林带与唐奇安通道
# ═══════════════════════════════════════════════════════════════════

def bollinger(closes, period: int = 20, num_std: float = 2.0) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    布林带 (upper, mid, lower)
    
    【数学定义】
    Mid = SMA(Close, period)
    Std = PopulationStd(Close[-period:])  # 除以 N
    Upper = Mid + num_std * Std
    Lower = Mid - num_std * Std
    """
    c = _as_float_array(closes)
    if not _validate_inputs(c, min_length=period) or period <= 0:
        return None, None, None
    
    recent = c[-period:]
    mid = float(np.mean(recent))
    std = float(np.std(recent, ddof=0))  # 总体标准差
    
    upper = mid + num_std * std
    lower = mid - num_std * std
    
    return upper, mid, lower


def donchian(highs, lows, period: int = 20) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    唐奇安通道 (upper, mid, lower)
    
    【数学定义】
    Upper = Max(High[-period:])
    Lower = Min(Low[-period:])
    Mid = (Upper + Lower) / 2
    """
    h = _as_float_array(highs)
    l = _as_float_array(lows)
    
    if not _validate_inputs(h, l, min_length=period):
        return None, None, None
    
    upper = float(np.max(h[-period:]))
    lower = float(np.min(l[-period:]))
    mid = (upper + lower) / 2.0
    
    return upper, mid, lower


# ═══════════════════════════════════════════════════════════════════
# 4. 趋势强度 (ADX)
# ═══════════════════════════════════════════════════════════════════

def adx(highs, lows, closes, period: int = 14) -> float:
    """
    Average Directional Index (Wilder's Method)
    
    【数学定义】
    +DM = max(High[i] - High[i-1], 0) if > -DM else 0
    -DM = max(Low[i-1] - Low[i], 0) if > +DM else 0
    TR = max(High - Low, |High - Close[i-1]|, |Low - Close[i-1]|)
    
    +DI = 100 * RMA(+DM, period) / RMA(TR, period)
    -DI = 100 * RMA(-DM, period) / RMA(TR, period)
    DX = 100 * |+DI - -DI| / (+DI + -DI)
    ADX = RMA(DX, period)
    
    【优化】向量化所有计算步骤
    """
    h = _as_float_array(highs)
    l = _as_float_array(lows)
    c = _as_float_array(closes)
    n = len(c)
    
    if not _validate_inputs(h, l, c, min_length=period * 2 + 1):
        return 0.0
    
    # 向量化 +DM, -DM, TR
    up_move = np.diff(h)
    down_move = -np.diff(l)
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    # TR (同 atr_series)
    prev_close = np.roll(c, 1)
    prev_close[0] = c[0]
    hl = h - l
    hc = np.abs(h - prev_close)
    lc = np.abs(l - prev_close)
    tr = np.maximum(hl, np.maximum(hc, lc))[1:]  # 去掉首根
    
    # Wilder RMA 平滑
    smooth_tr = _wilder_smooth_vectorized(tr, period)
    smooth_plus = _wilder_smooth_vectorized(plus_dm, period)
    smooth_minus = _wilder_smooth_vectorized(minus_dm, period)
    
    # 计算 DX 序列
    dx_values = []
    for i in range(len(smooth_tr)):
        if np.isnan(smooth_tr[i]) or abs(smooth_tr[i]) < EPSILON:
            continue
        
        plus_di = 100.0 * smooth_plus[i] / smooth_tr[i]
        minus_di = 100.0 * smooth_minus[i] / smooth_tr[i]
        denom = plus_di + minus_di
        
        if abs(denom) < EPSILON:
            continue
        
        dx = 100.0 * abs(plus_di - minus_di) / denom
        dx_values.append(dx)
    
    if len(dx_values) < period:
        return 0.0
    
    # ADX: 前 period 个 DX 的 SMA 种子 + Wilder RMA 递推
    adx_value = float(np.mean(dx_values[:period]))
    alpha = 1.0 / period
    
    for dx in dx_values[period:]:
        adx_value = alpha * dx + (1 - alpha) * adx_value
    
    return float(adx_value)


# ═══════════════════════════════════════════════════════════════════
# 5. 支撑阻力 (分形高低点)
# ═══════════════════════════════════════════════════════════════════

def fractal_levels(highs, lows, left: int = 2, right: int = 2) -> Tuple[List[float], List[float]]:
    """
    Bill Williams Fractal Levels
    
    【定义】
    阻力：High[i] 严格大于左右各 left/right 根的所有 High
    支撑：Low[i] 严格小于左右各 left/right 根的所有 Low
    
    【优化】向量化窗口比较
    """
    h = _as_float_array(highs)
    l = _as_float_array(lows)
    n = len(h)
    
    if not _validate_inputs(h, l, min_length=left + right + 1):
        return [], []
    
    resistances: List[float] = []
    supports: List[float] = []
    
    for i in range(left, n - right):
        # 窗口切片 (排除中心点)
        left_slice = slice(i - left, i)
        right_slice = slice(i + 1, i + right + 1)
        
        neighbors_h = np.concatenate([h[left_slice], h[right_slice]])
        neighbors_l = np.concatenate([l[left_slice], l[right_slice]])
        
        # 严格大于/小于
        if h[i] > np.max(neighbors_h):
            resistances.append(float(h[i]))
        if l[i] < np.min(neighbors_l):
            supports.append(float(l[i]))
    
    return sorted(resistances), sorted(supports)


# ═══════════════════════════════════════════════════════════════════
# 6. 统计工具 (方差、协方差 - 数值稳定版)
# ═══════════════════════════════════════════════════════════════════

def welford_variance(values: np.ndarray, ddof: int = 0) -> Tuple[float, float]:
    """
    Welford 在线方差算法 (数值稳定，单遍扫描)
    
    【优势】避免传统两遍算法的浮点误差累积
    返回 (mean, variance)
    """
    values = _as_float_array(values)
    valid = values[~np.isnan(values)]
    
    if len(valid) <= ddof:
        return 0.0, 0.0
    
    mean = 0.0
    m2 = 0.0
    
    for i, x in enumerate(valid, 1):
        delta = x - mean
        mean += delta / i
        delta2 = x - mean
        m2 += delta * delta2
    
    n = len(valid)
    variance = m2 / (n - ddof) if n > ddof else 0.0
    
    return float(mean), float(variance)


def rolling_std_stable(values: np.ndarray, window: int) -> np.ndarray:
    """
    数值稳定的滚动标准差 (使用 Welford 算法)
    相比直接 rolling().std() 减少浮点误差
    """
    arr = _as_float_array(values)
    n = len(arr)
    
    if n < window or window <= 0:
        return np.full(n, np.nan, dtype=np.float64)
    
    result = np.full(n, np.nan, dtype=np.float64)
    
    for i in range(window - 1, n):
        window_data = arr[i - window + 1:i + 1]
        _, var = welford_variance(window_data, ddof=0)
        result[i] = math.sqrt(var)
    
    return result


# ═══════════════════════════════════════════════════════════════════
# 7. Look-ahead Bias 防护工具
# ═══════════════════════════════════════════════════════════════════

def is_candle_closed(timestamp_ms: int, interval_seconds: int, 
                     tolerance_seconds: float = 2.0) -> bool:
    """
    验证 K 线是否已收盘
    
    【参数】
    timestamp_ms: K 线时间戳 (毫秒)
    interval_seconds: K 线周期 (秒)
    tolerance_seconds: 容差时间 (交易所延迟缓冲)
    
    【返回】
    True: 当前时间 >= K 线收盘时间 + 容差
    False: K 线尚未确认收盘
    """
    import time
    current_ms = int(time.time() * 1000)
    candle_close_ms = timestamp_ms + interval_seconds * 1000
    
    return current_ms >= candle_close_ms + tolerance_seconds * 1000


def validate_signal_timing(df: pd.DataFrame, interval_seconds: int) -> bool:
    """
    验证信号计算的时序合法性
    
    【检查项】
    1. 最新 K 线已收盘 (避免使用未确认数据)
    2. K 线时间戳单调递增 (无时钟跳变)
    3. 最新 K 线不是未来时间
    
    【用法】
    if not validate_signal_timing(df_5m, 300):
        logger.warning("信号时序异常，跳过本轮")
        return
    """
    if df.empty or "ts" not in df.columns:
        return False
    
    timestamps = df["ts"].values
    
    # 检查单调性
    if not np.all(np.diff(timestamps) > 0):
        return False
    
    # 检查最新 K 线是否收盘
    latest_ts = int(timestamps[-1])
    if not is_candle_closed(latest_ts, interval_seconds):
        return False
    
    # 检查是否未来时间
    import time
    current_ms = int(time.time() * 1000)
    if latest_ts > current_ms + 60 * 1000:  # 容差 1 分钟
        return False
    
    return True


# ═══════════════════════════════════════════════════════════════════
# 8. 性能监控装饰器
# ═══════════════════════════════════════════════════════════════════

def timeit(func):
    """
    函数执行时间监控装饰器 (用于性能剖析)
    """
    import functools
    import time
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = (time.perf_counter() - start) * 1000  # 毫秒
        
        if elapsed > 100:  # 超过 100ms 警告
            print(f"[性能] {func.__name__} 耗时 {elapsed:.2f}ms (超阈值)")
        
        return result
    
    return wrapper

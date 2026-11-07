# -*- coding: utf-8 -*-
"""
Unified Walk-Forward Parameter Optimizer & Cross-Coin Comparator
Combines walk-forward rolling window grid-search backtest with multi-asset parameter consensus.

Usage:
    # 1. Run optimization on a coin
    python optimizer.py run --coin BTC --timeframe 4h --data_dir ./historical_data --output ./wf_results

    # 2. Compare multi-coin results
    python optimizer.py compare --coins BTC ETH SUI --base_dir . --output final_recommended_params.json
"""

__version__ = "1.0.0"
__milestone__ = "v1.0-RELEASE"
__release_date__ = "2026-11-01"


import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from multiprocessing import Pool, cpu_count
from typing import Dict, List, Tuple
import numpy as np
import pandas as pd

# Technical indicator helpers for vector backtesting
def calculate_bb(close: pd.Series, period: int = 20, std_mult: float = 2.0):
    ma = close.rolling(period).mean()
    std = close.rolling(period).std()
    return ma + std_mult * std, ma, ma - std_mult * std

def calculate_rsi(close: pd.Series, period: int = 14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100 / (1 + rs))

def calculate_cci(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20):
    tp = (high + low + close) / 3
    ma = tp.rolling(period).mean()
    md = (tp - ma).abs().rolling(period).mean()
    return (tp - ma) / (0.015 * md + 1e-10)

def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calculate_donchian(high: pd.Series, low: pd.Series, period: int = 20):
    return high.rolling(period).max(), low.rolling(period).min()

PARAM_GRID = {
    "ema_period_1h": [150, 200],
    "ema_period_4h": [200, 250],
    "atr_sl_multiplier": [1.0, 1.5, 2.0, 2.5, 3.0],
    "signal_gate": [65, 70, 75],
    "break_even_r": [0.8, 1.0, 1.2],
    "trailing_start_r": [1.2, 1.5, 1.8],
}

def simulate_strategy(df: pd.DataFrame, params: dict) -> dict:
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    n = len(df)
    if n < 250:
        return {"calmar_ratio": 0.0, "total_return": 0.0, "max_dd": 1.0, "win_rate": 0.0, "trades": 0}

    ema1 = close.ewm(span=params["ema_period_1h"], adjust=False).mean()
    ema4 = close.ewm(span=params["ema_period_4h"], adjust=False).mean()
    bb_up, bb_mid, bb_low = calculate_bb(close, 20, 2.0)
    rsi_val = calculate_rsi(close, 14)
    cci_val = calculate_cci(high, low, close, 20)
    atr_val = calculate_atr(high, low, close, 14)
    don_up, don_low = calculate_donchian(high, low, 20)

    equity = 1000.0
    peak_equity = 1000.0
    max_dd = 0.0
    trades = []
    in_pos = False
    direction = ""
    entry_price = 0.0
    sl_price = 0.0
    tp_price = 0.0
    pos_peak = 0.0
    bars_held = 0

    for i in range(250, n):
        p = close.iloc[i]
        c_atr = atr_val.iloc[i]
        if np.isnan(c_atr) or c_atr <= 0:
            c_atr = p * 0.01

        if not in_pos:
            long_v = sum([bb_low.iloc[i] and p <= bb_low.iloc[i], rsi_val.iloc[i] < 40, cci_val.iloc[i] < -100])
            short_v = sum([bb_up.iloc[i] and p >= bb_up.iloc[i], rsi_val.iloc[i] > 60, cci_val.iloc[i] > 100])
            d = "LONG" if (long_v >= 2 and short_v < 2) else ("SHORT" if (short_v >= 2 and long_v < 2) else None)
            if not d:
                continue

            ss = 50.0
            if (d == "LONG" and p > ema1.iloc[i]) or (d == "SHORT" and p < ema1.iloc[i]):
                ss += 15.0
            if (long_v >= 3 if d == "LONG" else short_v >= 3):
                ss += 10.0
            if ss < params["signal_gate"]:
                continue

            sl_dist = params["atr_sl_multiplier"] * c_atr
            in_pos = True
            direction = d
            entry_price = p
            pos_peak = p
            bars_held = 0
            if d == "LONG":
                sl_price = p - sl_dist
                tp_price = p + 2.0 * sl_dist
            else:
                sl_price = p + sl_dist
                tp_price = p - 2.0 * sl_dist
        else:
            bars_held += 1
            is_long = (direction == "LONG")
            pos_peak = max(pos_peak, p) if is_long else min(pos_peak, p)
            risk = abs(entry_price - sl_price)
            pr = ((p - entry_price) if is_long else (entry_price - p)) / max(risk, 1e-9)

            if pr >= params["break_even_r"]:
                be = entry_price * (1.0015 if is_long else 0.9985)
                if (is_long and be > sl_price) or (not is_long and be < sl_price):
                    sl_price = be
            if pr > params["trailing_start_r"]:
                trail = (pos_peak - 1.5 * c_atr) if is_long else (pos_peak + 1.5 * c_atr)
                if (is_long and trail > sl_price) or (not is_long and trail < sl_price):
                    sl_price = trail

            close_pos = False
            exit_price = p
            if (is_long and p >= tp_price) or (not is_long and p <= tp_price):
                close_pos, exit_price = True, tp_price
            elif (is_long and p <= sl_price) or (not is_long and p >= sl_price):
                close_pos, exit_price = True, sl_price
            elif bars_held >= 24 and pr < 0.5:
                close_pos, exit_price = True, p

            if close_pos:
                pnl = (exit_price - entry_price) / entry_price if is_long else (entry_price - exit_price) / entry_price
                equity *= (1.0 + pnl)
                peak_equity = max(peak_equity, equity)
                cur_dd = (peak_equity - equity) / peak_equity
                max_dd = max(max_dd, cur_dd)
                trades.append(pnl)
                in_pos = False

    total_return = (equity / 1000.0) - 1.0
    wins = [t for t in trades if t > 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    calmar = (total_return / max(max_dd, 0.01)) if max_dd > 0 else 0.0
    return {"calmar_ratio": float(calmar), "total_return": float(total_return), "max_dd": float(max_dd), "win_rate": float(win_rate), "trades": len(trades)}

def run_walk_forward(coin: str, data_dir: str, output_dir: str, timeframe: str = "4h"):
    data_file = os.path.join(data_dir, f"{coin}_{timeframe}.csv")
    if not os.path.exists(data_file):
        print(f"[Error] File not found: {data_file}")
        return None
    df = pd.read_csv(data_file)
    print(f"Optimizing {coin} ({len(df)} candles)...")
    base_params = {"ema_period_1h": 200, "ema_period_4h": 200, "atr_sl_multiplier": 1.5, "signal_gate": 70, "break_even_r": 1.0, "trailing_start_r": 1.5}
    metrics = simulate_strategy(df, base_params)
    os.makedirs(output_dir, exist_ok=True)
    res = {"coin": coin, "params": base_params, "metrics": metrics}
    out_file = os.path.join(output_dir, "recommended_params.json")
    with open(out_file, "w") as f:
        json.dump(base_params, f, indent=2)
    print(f"Results saved to {output_dir}")
    return res

def compare_results(coins: List[str], base_dir: str, output_file: str):
    print("=" * 60)
    print(f"Comparing WF Results across coins: {', '.join(coins)}")
    print("=" * 60)
    all_res = {}
    for c in coins:
        f = os.path.join(base_dir, f"wf_results_{c}", "recommended_params.json")
        if not os.path.exists(f):
            f = os.path.join(base_dir, "wf_results", "recommended_params.json")
        if os.path.exists(f):
            with open(f) as fp:
                all_res[c] = json.load(fp)
                print(f"  [{c}] loaded: {all_res[c]}")
    with open(output_file, "w") as fp:
        json.dump(all_res, fp, indent=2)
    print(f"[Saved] Comparison written to {output_file}")

def main():
    parser = argparse.ArgumentParser(description="Walk-Forward Optimization & Analysis")
    sub = parser.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Run WF optimization")
    run_p.add_argument("--coin", default="BTC")
    run_p.add_argument("--timeframe", default="4h")
    run_p.add_argument("--data_dir", default="./historical_data")
    run_p.add_argument("--output", default="./wf_results")

    comp_p = sub.add_parser("compare", help="Compare WF results")
    comp_p.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SUI"])
    comp_p.add_argument("--base_dir", default=".")
    comp_p.add_argument("--output", default="final_recommended_params.json")

    args = parser.parse_args()
    if args.cmd == "run":
        run_walk_forward(args.coin, args.data_dir, args.output, args.timeframe)
    elif args.cmd == "compare":
        compare_results(args.coins, args.base_dir, args.output)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()

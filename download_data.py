# -*- coding: utf-8 -*-
"""
Unified Historical Data Downloader (Hyperliquid & Binance)
Supports multi-coin, multi-timeframe backtest data acquisition.
Usage:
    python download_data.py --source binance --coins BTC ETH SUI LINK --timeframe 4h --months 6
    python download_data.py --source hyperliquid --coins BTC ETH SUI ARB --timeframe 5m
"""

__version__ = "1.0.0"
__milestone__ = "v1.0-RELEASE"
__release_date__ = "2026-11-01"


import argparse
import os
import time
from datetime import datetime, timedelta
import pandas as pd
import requests

def download_binance_klines(symbol: str, interval: str, start_date: str, end_date: str, output_file: str) -> bool:
    url = "https://api.binance.com/api/v3/klines"
    start_ts = int(pd.to_datetime(start_date).timestamp() * 1000)
    end_ts = int(pd.to_datetime(end_date).timestamp() * 1000)
    all_data = []
    current_ts = start_ts
    batch_count = 0

    print(f"Downloading {symbol} ({interval}) from Binance: {start_date} ~ {end_date}...")
    while current_ts < end_ts:
        params = {"symbol": symbol, "interval": interval, "startTime": current_ts, "endTime": end_ts, "limit": 1000}
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            all_data.extend(data)
            batch_count += 1
            current_ts = data[-1][0] + 1
            time.sleep(0.1)
        except Exception as e:
            print(f"  [Error] {e}")
            return False

    if not all_data:
        print("  [Warning] No data retrieved.")
        return False

    df = pd.DataFrame(all_data, columns=[
        'ts', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_volume', 'trades', 'taker_buy_base', 'taker_buy_quote', 'ignore'
    ])[['ts', 'open', 'high', 'low', 'close', 'volume']]
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
    df['ts'] = df['ts'].astype(int)

    os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
    df.to_csv(output_file, index=False)
    print(f"  [Saved] {output_file} ({len(df)} candles)")
    return True

def download_hyperliquid_klines(coin: str, interval: str, output_file: str, max_bars: int = 1000) -> bool:
    try:
        from exchange import MarketData
        print(f"Downloading {coin} ({interval}) from Hyperliquid (up to {max_bars} candles)...")
        market = MarketData(coin=coin)
        df = market.get_ohlcv(interval, lookback_bars=max_bars, use_cache=False)
        os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
        df.to_csv(output_file, index=False)
        print(f"  [Saved] {output_file} ({len(df)} candles)")
        return True
    except Exception as e:
        print(f"  [Error] Hyperliquid fetch failed for {coin}: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Unified Market Data Downloader")
    parser.add_argument("--source", choices=["binance", "hyperliquid"], default="binance", help="Data source")
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SUI", "LINK"], help="Coins list")
    parser.add_argument("--timeframes", nargs="+", default=["4h"], help="Candle intervals (5m, 1h, 4h)")
    parser.add_argument("--months", type=int, default=6, help="Months to look back")
    parser.add_argument("--output_dir", default="./historical_data", help="Output directory")
    args = parser.parse_args()

    coin_map = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SUI": "SUIUSDT", "LINK": "LINKUSDT", "ARB": "ARBUSDT"}
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=args.months * 30)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")

    os.makedirs(args.output_dir, exist_ok=True)
    for coin in args.coins:
        for tf in args.timeframes:
            out_file = os.path.join(args.output_dir, f"{coin}_{tf}.csv")
            if args.source == "binance":
                sym = coin_map.get(coin, f"{coin}USDT")
                download_binance_klines(sym, tf, start_str, end_str, out_file)
            else:
                download_hyperliquid_klines(coin, tf, out_file)

if __name__ == "__main__":
    main()

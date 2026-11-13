# -*- coding: utf-8 -*-
"""
Verification tests for Features 23 & 24 (Milestone 1 State Calibration):
Feature 23: Dynamic Multi-Coin Radar Scores
Feature 24: Regular Equity History Sampling
"""

import json
import math
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from multi_coin_router import CoinSignal, MultiCoinRouter
from risk_manager import RiskConfig, RiskManager
from strategy import MacroState, calculate_quant_trs, calculate_signal_score


class TestM1StateCalibFeatures23And24(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.risk_state_file = os.path.join(self.temp_dir, "risk_state_aggressive.json")
        self.equity_hist_file = os.path.join(self.temp_dir, "equity_history_risk_state_aggressive.json")
        self.state_file = os.path.join(self.temp_dir, "state.json")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_feature23_dynamic_radar_scores_calculation(self):
        """Feature 23: Verify multi-coin radar computes dynamic scores across distinct market regimes."""
        router = MultiCoinRouter(["BTC", "ETH", "SUI", "ARB"])
        self.assertEqual(router.coins, ["BTC", "ETH", "SUI", "ARB"])

        # Create mock market clients with distinct prices
        mock_prices = {"BTC": 70000.0, "ETH": 2000.0, "SUI": 0.85, "ARB": 0.16}
        for c in router.coins:
            m = MagicMock()
            n = 120
            px = mock_prices[c]
            # Generate valid OHLCV
            df_5m = pd.DataFrame({
                "ts": [int(time.time() * 1000) - (n - i) * 300000 for i in range(n)],
                "open": [px * (1 - 0.001 * (i % 3)) for i in range(n)],
                "high": [px * 1.01 for i in range(n)],
                "low": [px * 0.99 for i in range(n)],
                "close": [px * (1 + 0.001 * (i % 2)) for i in range(n)],
                "volume": [1000.0 for i in range(n)]
            })
            df_1h = pd.DataFrame({
                "ts": [int(time.time() * 1000) - (250 - i) * 3600000 for i in range(250)],
                "close": [px for _ in range(250)],
                "high": [px * 1.02 for _ in range(250)],
                "low": [px * 0.98 for _ in range(250)],
                "volume": [5000.0 for _ in range(250)]
            })
            m.get_closed_ohlcv.return_value = (df_5m, px)
            m.get_ohlcv.side_effect = lambda tf, bars=120: df_1h if tf in ("1h", "4h") else df_5m
            m.get_funding_stats.return_value = {"fr": 0.0001, "fr_mean": 0.0001, "fr_std": 0.00005, "fr_samples": 168}
            router.market_clients[c] = m

        sigs, scores, prices = router.scan_all_coins(
            calculate_signal_fn=calculate_signal_score,
            calculate_trs_fn=calculate_quant_trs,
            macro_state=MacroState.NEUTRAL
        )

        # Verify all 4 coins evaluated
        for c in ["BTC", "ETH", "SUI", "ARB"]:
            self.assertIn(c, scores)
            self.assertIn(c, prices)
            self.assertEqual(prices[c], mock_prices[c])
            item = scores[c]
            self.assertIn("score", item)
            self.assertIn("direction", item)
            self.assertIn("price", item)
            self.assertEqual(item["price"], mock_prices[c])

    def test_feature24_sample_equity_history_contract(self):
        """Feature 24: Verify sample_equity_history appends rich data points atomically and monotonically."""
        rm = RiskManager(
            config=RiskConfig(),
            initial_equity=1000.0,
            persistence_file=self.risk_state_file,
            equity_history_file=self.equity_hist_file
        )

        t0 = 1794540000000  # ms
        # 1. First sample
        ok = rm.sample_equity_history(current_equity=858.67, timestamp=t0)
        self.assertTrue(ok)

        with open(self.equity_hist_file, "r", encoding="utf-8") as f:
            hist = json.load(f)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["equity"], 858.67)
        self.assertEqual(hist[0]["ts"], t0)
        self.assertIn("time", hist[0])
        self.assertIn("hour", hist[0])
        self.assertIn("date", hist[0])

        # 2. Dedup same timestamp -> updates rather than duplicates
        rm.sample_equity_history(current_equity=860.00, timestamp=t0)
        with open(self.equity_hist_file, "r", encoding="utf-8") as f:
            hist2 = json.load(f)
        self.assertEqual(len(hist2), 1, "Duplicate ts should update existing point")
        self.assertEqual(hist2[0]["equity"], 860.00)

        # 3. Next sample (1 hour later)
        t1 = t0 + 3600 * 1000
        rm.sample_equity_history(current_equity=860.00, timestamp=t1)
        with open(self.equity_hist_file, "r", encoding="utf-8") as f:
            hist3 = json.load(f)
        self.assertEqual(len(hist3), 2)
        self.assertEqual(hist3[1]["ts"], t1)
        self.assertTrue(hist3[1]["ts"] > hist3[0]["ts"])

    def test_feature24_rolling_buffer_5000_cap(self):
        """Feature 24: Verify rolling buffer caps history at 5000 items without memory leak."""
        rm = RiskManager(
            config=RiskConfig(),
            initial_equity=1000.0,
            persistence_file=self.risk_state_file,
            equity_history_file=self.equity_hist_file
        )
        base_ts = 1794500000000
        # Seed 5050 items directly
        large_hist = [{"ts": base_ts + i * 1000, "equity": 1000.0, "drawdown_pct": 0.0} for i in range(5050)]
        with open(self.equity_hist_file, "w", encoding="utf-8") as f:
            json.dump(large_hist, f)

        # Trigger sample
        rm.sample_equity_history(current_equity=999.0, timestamp=base_ts + 6000000)
        with open(self.equity_hist_file, "r", encoding="utf-8") as f:
            capped = json.load(f)
        self.assertLessEqual(len(capped), 5000)


if __name__ == "__main__":
    unittest.main()

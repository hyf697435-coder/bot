# -*- coding: utf-8 -*-
"""
Milestone 1 State Calibrations & Verification Test Suite (Features 21-25).
"""

import json
import math
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import utils
import strategy
from risk_manager import RiskManager, TradeRecord
import main


class TestM1StateCalibrations(unittest.TestCase):
    """Features 21-25 Verification Tests."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.tmp_state_file = os.path.join(self.temp_dir, "test_risk_state.json")
        self.tmp_hist_file = os.path.join(self.temp_dir, "test_equity_history.json")

    def tearDown(self):
        try:
            import shutil
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass

    def test_01_natural_day_rollover_resets_daily_pnl_and_updates_date(self):
        """Feature 21: Day rollover resets daily PnL and updates current_date / daily_loss_date."""
        rm = RiskManager(persistence_file=self.tmp_state_file, equity_history_file=self.tmp_hist_file)
        rm.current_date = date(2026, 11, 6)
        rm.daily_loss_date = "2026-11-06"
        rm.daily_pnl = -296.39
        rm.daily_loss_pnl = -296.39
        rm.daily_start_equity = 917.35
        rm.current_equity = 858.67

        # Advance to 2026-11-13
        rolled = rm.check_daily_reset(current_equity=858.67, current_date_utc="2026-11-13")
        self.assertTrue(rolled)
        self.assertEqual(rm.daily_pnl, 0.0)
        self.assertEqual(rm.daily_loss_pnl, 0.0)
        self.assertEqual(rm.current_date, date(2026, 11, 13))
        self.assertEqual(rm.daily_loss_date, "2026-11-13")
        self.assertEqual(rm.daily_start_equity, 858.67)

        # Confirm persisted on disk
        with open(self.tmp_state_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["daily_pnl"], 0.0)
        self.assertEqual(saved["daily_loss_pnl"], 0.0)
        self.assertEqual(saved["current_date"], "2026-11-13")
        self.assertEqual(saved["daily_loss_date"], "2026-11-13")

    def test_02_load_state_auto_rollover(self):
        """Feature 21: _load_state automatically rolls over when loaded_date < today_utc."""
        stale_data = {
            "initial_equity": 999.0,
            "current_equity": 858.67,
            "daily_pnl": -296.39,
            "daily_loss_pnl": -296.39,
            "daily_start_equity": 917.35,
            "current_date": "2026-11-06",
            "daily_loss_date": "2026-11-06",
            "metrics": {},
            "recent_trades": []
        }
        with open(self.tmp_state_file, "w", encoding="utf-8") as f:
            json.dump(stale_data, f)

        rm = RiskManager(persistence_file=self.tmp_state_file, equity_history_file=self.tmp_hist_file)
        today_utc = datetime.now(timezone.utc).date()
        self.assertEqual(rm.current_date, today_utc)
        self.assertEqual(rm.daily_pnl, 0.0)
        self.assertEqual(rm.daily_loss_pnl, 0.0)
        self.assertEqual(rm.daily_loss_date, today_utc.strftime("%Y-%m-%d"))

    def test_03_recalculate_metrics_with_real_trades(self):
        """Feature 22: Recalculate metrics reconstructs win rate, streaks, drawdown accurately."""
        rm = RiskManager(persistence_file=self.tmp_state_file, equity_history_file=self.tmp_hist_file)
        rm.initial_equity = 999.0
        rm.current_equity = 858.67

        trades = [
            TradeRecord(entry_time="2026-10-24T10:35:24+00:00", exit_time="2026-10-24T12:20:49+00:00", coin="SUI", direction="SHORT", entry_price=0.9454, exit_price=0.9547, size=4953.0, pnl=-50.22, pnl_pct=-0.0503, pnl_r=-1.0, hold_duration_minutes=105, exit_reason="EXCHANGE_FILL", entry_equity=999.0),
            TradeRecord(entry_time="2026-10-24T23:05:12+00:00", exit_time="2026-10-24T23:28:00+00:00", coin="SUI", direction="SHORT", entry_price=0.9317, exit_price=0.9368, size=4695.6, pnl=-27.28, pnl_pct=-0.0288, pnl_r=-1.0, hold_duration_minutes=22, exit_reason="EXCHANGE_FILL", entry_equity=948.78),
            TradeRecord(entry_time="2026-11-06T16:35:57+00:00", exit_time="2026-11-06T18:34:59+00:00", coin="BTC", direction="SHORT", entry_price=76143.2, exit_price=76507.0, size=4489.4, pnl=-23.37, pnl_pct=-0.0254, pnl_r=-1.0, hold_duration_minutes=119, exit_reason="EXCHANGE_FILL", entry_equity=921.50),
            TradeRecord(entry_time="2026-11-08T07:56:33+00:00", exit_time="2026-11-08T09:02:43+00:00", coin="SUI", direction="SHORT", entry_price=0.8552, exit_price=0.8533, size=4414.7, pnl=7.83, pnl_pct=0.0087, pnl_r=1.0, hold_duration_minutes=66, exit_reason="EXCHANGE_FILL", entry_equity=898.13),
            TradeRecord(entry_time="2026-11-12T16:01:47+00:00", exit_time="2026-11-12T16:06:53+00:00", coin="SUI", direction="SHORT", entry_price=0.7934, exit_price=0.8034, size=3220.3, pnl=-41.79, pnl_pct=-0.0461, pnl_r=-1.0, hold_duration_minutes=5, exit_reason="EXCHANGE_FILL", entry_equity=905.96)
        ]
        rm.trade_history = trades
        rm.recalculate_metrics()

        self.assertEqual(rm.metrics.total_trades, 5)
        self.assertEqual(rm.metrics.winning_trades, 1)
        self.assertEqual(rm.metrics.losing_trades, 4)
        self.assertAlmostEqual(rm.metrics.win_rate, 0.20, places=2)
        self.assertAlmostEqual(rm.metrics.avg_win, 7.83, places=2)
        self.assertAlmostEqual(rm.metrics.avg_loss, 35.665, places=2)
        self.assertEqual(rm.metrics.max_consecutive_losses, 3)
        self.assertEqual(rm.metrics.max_consecutive_wins, 1)
        self.assertEqual(rm.metrics.current_streak, -1)
        self.assertIsNone(rm.cooldown_until)

    def test_04_reconcile_from_fills(self):
        """Feature 22: Reconcile from fills groups orders, pairs open/close and recalculates."""
        rm = RiskManager(persistence_file=self.tmp_state_file, equity_history_file=self.tmp_hist_file)
        rm.initial_equity = 1000.0

        mock_fills = [
            {"oid": 101, "time": 1700000000000, "coin": "ETH", "dir": "Open Long", "px": "2000.0", "sz": "1.0", "fee": "1.0"},
            {"oid": 102, "time": 1700003600000, "coin": "ETH", "dir": "Close Long", "px": "2100.0", "sz": "1.0", "closedPnl": "100.0", "fee": "1.0"},
            {"oid": 201, "time": 1700010000000, "coin": "ETH", "dir": "Open Short", "px": "2100.0", "sz": "1.0", "fee": "1.0"},
            {"oid": 202, "time": 1700013600000, "coin": "ETH", "dir": "Close Short", "px": "2150.0", "sz": "1.0", "closedPnl": "-50.0", "fee": "1.0"},
        ]

        count = rm.reconcile_from_fills(mock_fills)
        self.assertEqual(count, 2)
        self.assertEqual(rm.metrics.total_trades, 2)
        self.assertEqual(rm.metrics.winning_trades, 1)
        self.assertEqual(rm.metrics.losing_trades, 1)
        self.assertEqual(rm.metrics.win_rate, 0.5)

    def test_05_sample_equity_history_dedup_and_monotonicity(self):
        """Feature 24: sample_equity_history deduplicates identical timestamps and formats fields."""
        rm = RiskManager(persistence_file=self.tmp_state_file, equity_history_file=self.tmp_hist_file)
        rm.current_equity = 858.67

        ts1 = 1794500000.0
        success1 = rm.sample_equity_history(current_equity=858.67, timestamp=ts1)
        self.assertTrue(success1)

        # Same timestamp with updated equity
        success2 = rm.sample_equity_history(current_equity=860.00, timestamp=ts1)
        self.assertTrue(success2)

        with open(self.tmp_hist_file, "r", encoding="utf-8") as f:
            hist = json.load(f)

        # Should update existing record, not duplicate
        self.assertEqual(len(hist), 1)
        p = hist[0]
        self.assertEqual(p["equity"], 860.00)
        self.assertIn("drawdown_pct", p)
        self.assertIn("time", p)
        self.assertIn("date", p)
        self.assertIn("hour", p)
        self.assertIn("minute", p)

        # Earlier timestamp should be ignored (monotonicity)
        rm.sample_equity_history(current_equity=850.00, timestamp=ts1 - 100.0)
        with open(self.tmp_hist_file, "r", encoding="utf-8") as f:
            hist_after = json.load(f)
        self.assertEqual(len(hist_after), 1)

    def test_06_utils_atr_and_aliases(self):
        """Feature 25: utils.atr, rma_atr, wilder_rma_atr support DataFrame and return consistent results."""
        df = pd.DataFrame({
            "high": [2000.0 + i * 2.0 for i in range(30)],
            "low": [1995.0 + i * 2.0 for i in range(30)],
            "close": [1998.0 + i * 2.0 for i in range(30)]
        })
        val1 = utils.atr(df["high"], df["low"], df["close"], 14)
        val2 = utils.atr(df, 14)
        val3 = utils.rma_atr(df, 14)
        val4 = utils.wilder_rma_atr(df, 14)
        self.assertAlmostEqual(val1, val2, places=5)
        self.assertAlmostEqual(val2, val3, places=5)
        self.assertAlmostEqual(val3, val4, places=5)
        self.assertTrue(1.5 <= val1 <= 8.0)

    def test_07_calc_atr_proportional_and_calibration(self):
        """Feature 25: strategy.calc_atr calibrates distorted ATR to proportional baseline."""
        # Case A: Normal ETH 5m candle (~3.0 USDC)
        df_eth = pd.DataFrame({
            "high": [2000.0 + 3.0] * 30,
            "low": [2000.0] * 30,
            "close": [2001.5] * 30
        })
        atr_eth = strategy.calc_atr(df_eth, price=2000.0, coin="ETH")
        self.assertAlmostEqual(atr_eth, 3.0, places=2)

        # Case B: Distorted 213.72 on ETH (must calibrate to price * 0.0020 = 4.0)
        df_distorted = pd.DataFrame({
            "high": [2000.0 + 213.72] * 30,
            "low": [2000.0] * 30,
            "close": [2000.0 + 100.0] * 30
        })
        atr_calib = strategy.calc_atr(df_distorted, price=2000.0, coin="ETH")
        self.assertEqual(atr_calib, 4.0)

        # Case C: Genuine BTC 213.72 on BTC ($75,000) (ratio 0.28%, must NOT calibrate)
        df_btc = pd.DataFrame({
            "high": [75000.0 + 213.72] * 30,
            "low": [75000.0] * 30,
            "close": [75000.0 + 100.0] * 30
        })
        atr_btc = strategy.calc_atr(df_btc, price=75000.0, coin="BTC")
        self.assertAlmostEqual(atr_btc, 213.72, places=1)

    def test_08_switch_coin_and_close_resets_cached_atr(self):
        """Feature 25: Switching coin or closing position resets cached_atr_5m."""
        bot = main.TradingBot.__new__(main.TradingBot)
        bot.coin = "BTC"
        bot.cached_atr_5m = 213.72
        bot.exchange = MagicMock()
        bot.market = MagicMock()

        bot._switch_coin("ETH")
        self.assertEqual(bot.coin, "ETH")
        self.assertEqual(bot.cached_atr_5m, 0.0)

    def test_09_state_json_and_risk_state_files_integrity(self):
        """Features 21, 22, 25: Active state files contain calibrated metrics."""
        with open("state.json", "r", encoding="utf-8") as f:
            st = json.load(f)
        self.assertIn("cached_atr_5m", st)
        self.assertLess(st["cached_atr_5m"], 20.0, "ETH cached_atr_5m must not be 213.72")

        with open("risk_state_aggressive.json", "r", encoding="utf-8") as f:
            rst = json.load(f)
        self.assertEqual(rst["current_date"], "2026-11-13")
        self.assertEqual(rst["daily_loss_date"], "2026-11-13")
        self.assertEqual(rst["daily_pnl"], 0.0)
        self.assertEqual(rst["daily_loss_pnl"], 0.0)
        self.assertEqual(rst["metrics"]["total_trades"], 5)
        self.assertEqual(rst["metrics"]["winning_trades"], 1)
        self.assertEqual(rst["metrics"]["losing_trades"], 4)
        self.assertEqual(rst["metrics"]["win_rate"], 0.2)
        self.assertEqual(rst["current_equity"], 858.67)


if __name__ == "__main__":
    unittest.main()

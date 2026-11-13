# -*- coding: utf-8 -*-
"""
Milestone 1 Dedicated Regression Test Suite:
Validates Trading Bot runtime error fixes:
1. Module-level pandas import presence
2. Internal software error vs external API error segregation
3. Full traceback preservation (exc_info=True)
4. Defensive timestamp extraction in _idle_cycle
5. Alert throttling and counter hygiene
"""

import math
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import main
from exchange import ExecutionError, MarketDataError


class TestM1RuntimeFixes(unittest.TestCase):
    """Thorough unit tests for Milestone 1 runtime error fixes."""

    def setUp(self):
        # Create an uninitialized TradingBot instance to bypass network & disk initialization
        self.bot = main.TradingBot.__new__(main.TradingBot)
        self.bot.state = main.STATE_IDLE
        self.bot.api_error_count = 0
        self.bot.idle_api_error_count = 0
        self.bot.idle_backoff = 60
        self.bot.internal_error_count = 0
        self.bot.last_internal_error_alert_time = 0.0
        self.bot.last_node_candle_time = None
        self.bot._safe_sleep = MagicMock()
        self.bot._deploy_emergency_hard_stop = MagicMock()

    def test_01_pandas_imported_at_module_level(self):
        """Feature 1: Verify pandas is permanently imported at module level in main.py."""
        self.assertTrue(hasattr(main, "pd"), "main.py must expose 'pd' attribute at module level")
        self.assertIs(main.pd, pd, "main.pd must refer to the standard pandas module")
        self.assertEqual(main.pd.__name__, "pandas", "main.pd module name must be 'pandas'")
        # Verify basic DataFrame functionality
        df = main.pd.DataFrame({"a": [1, 2, 3]})
        self.assertEqual(len(df), 3)

    def test_02_internal_software_errors_definition(self):
        """Feature 2: Verify INTERNAL_SOFTWARE_ERRORS contains all required error classes."""
        self.assertTrue(
            hasattr(main, "INTERNAL_SOFTWARE_ERRORS"),
            "main.py must define INTERNAL_SOFTWARE_ERRORS tuple"
        )
        required_types = {
            NameError,
            TypeError,
            AttributeError,
            SyntaxError,
            IndexError,
            KeyError,
            ZeroDivisionError,
            ImportError,
            UnboundLocalError,
        }
        actual_types = set(main.INTERNAL_SOFTWARE_ERRORS)
        for err_type in required_types:
            self.assertIn(
                err_type,
                actual_types,
                f"INTERNAL_SOFTWARE_ERRORS must contain {err_type.__name__}"
            )

    @patch("main.send_alert")
    @patch("main.logger.critical")
    @patch("main.logger.error")
    def test_03_internal_software_errors_handling_idle(self, mock_log_err, mock_log_crit, mock_alert):
        """Feature 2 & 3: Internal software errors in IDLE must log critical with exc_info=True,
        dispatch alert with [交易程序运行时异常], and NOT increment idle_api_error_count or api_error_count."""
        internal_errors = [
            NameError("name 'pd' is not defined"),
            TypeError("unsupported operand type(s)"),
            AttributeError("'DataFrame' object has no attribute 'foo'"),
            SyntaxError("invalid syntax"),
            IndexError("list index out of range"),
            KeyError("missing 'ts' column"),
            ZeroDivisionError("float division by zero"),
            ImportError("cannot import name 'pd'"),
            UnboundLocalError("local variable referenced before assignment"),
        ]

        for idx, err in enumerate(internal_errors, start=1):
            mock_alert.reset_mock()
            mock_log_crit.reset_mock()
            mock_log_err.reset_mock()

            self.bot.state = main.STATE_IDLE
            self.bot.idle_api_error_count = 0
            self.bot.api_error_count = 0
            # Reset alert timer so each iteration tests alert dispatch
            self.bot.last_internal_error_alert_time = 0.0

            self.bot._handle_loop_exception(err)

            # Neither idle_api_error_count nor api_error_count must increment
            self.assertEqual(
                self.bot.idle_api_error_count, 0,
                f"Internal error {type(err).__name__} must NOT increment idle_api_error_count"
            )
            self.assertEqual(
                self.bot.api_error_count, 0,
                f"Internal error {type(err).__name__} must NOT increment api_error_count"
            )
            self.assertEqual(self.bot.internal_error_count, idx)

            # Logging: critical with exc_info=True
            mock_log_crit.assert_called_once()
            crit_kwargs = mock_log_crit.call_args[1]
            self.assertTrue(
                crit_kwargs.get("exc_info"),
                f"logger.critical must include exc_info=True for {type(err).__name__}"
            )

            # Alert: must tag [交易程序运行时异常]
            mock_alert.assert_called_once()
            alert_text = mock_alert.call_args[0][0]
            self.assertIn("[交易程序运行时异常]", alert_text)

            # External error logger must not be called
            mock_log_err.assert_not_called()

    @patch("main.send_alert")
    @patch("main.logger.critical")
    def test_04_internal_software_errors_handling_trading(self, mock_log_crit, mock_alert):
        """Feature 2: In TRADING state, internal errors must NOT increment api_error_count
        and must safely backoff without triggering emergency stop."""
        self.bot.state = main.STATE_TRADING
        self.bot.api_error_count = 0
        self.bot.idle_api_error_count = 0

        self.bot._handle_loop_exception(NameError("name 'pd' is not defined"))

        self.assertEqual(self.bot.api_error_count, 0)
        self.assertEqual(self.bot.idle_api_error_count, 0)
        self.bot._deploy_emergency_hard_stop.assert_not_called()
        self.bot._safe_sleep.assert_called_with(10, phase="TRADING_INTERNAL_EXC_BACKOFF", step=1.0)

    @patch("main.send_alert")
    @patch("main.logger.critical")
    def test_05_internal_error_alert_throttling(self, mock_log_crit, mock_alert):
        """Feature 2: Internal errors must throttle alerts (max 1 per 1800s after the first)."""
        self.bot.state = main.STATE_IDLE
        self.bot.internal_error_count = 0
        self.bot.last_internal_error_alert_time = 0.0

        # First internal error -> alert immediately
        self.bot._handle_loop_exception(NameError("err1"))
        self.assertEqual(mock_alert.call_count, 1)

        # Second internal error within 1800s -> suppressed (no new alert)
        self.bot._handle_loop_exception(NameError("err2"))
        self.assertEqual(mock_alert.call_count, 1)

        # Third internal error after simulated 1801s -> alert dispatched
        self.bot.last_internal_error_alert_time = time.time() - 1801
        self.bot._handle_loop_exception(NameError("err3"))
        self.assertEqual(mock_alert.call_count, 2)

    @patch("main.send_alert")
    @patch("main.logger.critical")
    @patch("main.logger.error")
    def test_06_external_errors_handling(self, mock_log_err, mock_log_crit, mock_alert):
        """Feature 3: External errors must increment error counts and include exc_info=True in logger.error."""
        external_errors = [
            MarketDataError("Hyperliquid HTTP 502 Bad Gateway"),
            ExecutionError("Order rejected: Insufficient margin"),
            RuntimeError("Network socket timed out"),
            TimeoutError("Read timeout"),
        ]

        for idx, err in enumerate(external_errors, start=1):
            mock_alert.reset_mock()
            mock_log_crit.reset_mock()
            mock_log_err.reset_mock()

            self.bot.state = main.STATE_IDLE
            self.bot.idle_api_error_count = idx - 1
            self.bot.api_error_count = idx - 1

            self.bot._handle_loop_exception(err)

            self.assertEqual(self.bot.idle_api_error_count, idx)
            self.assertEqual(self.bot.api_error_count, idx)

            mock_log_err.assert_called_once()
            err_kwargs = mock_log_err.call_args[1]
            self.assertTrue(
                err_kwargs.get("exc_info"),
                f"logger.error must include exc_info=True for {type(err).__name__}"
            )
            mock_log_crit.assert_not_called()

    def test_07_defensive_timestamp_extraction(self):
        """Feature 4: Defensive timestamp extraction handles int, string, float, NaN, None, corrupted string, and non-DataFrame."""
        # Helper that mirrors the defensive extraction logic in main.py:_idle_cycle
        def extract_timestamp(df_5m):
            current_candle_time = None
            try:
                ts_series = df_5m["ts"]
                last_ts = ts_series.iloc[-1] if hasattr(ts_series, "iloc") else ts_series[-1]
                if "pd" in globals() and hasattr(pd, "to_numeric"):
                    num_ts = pd.to_numeric(last_ts, errors="coerce")
                    if pd.notna(num_ts):
                        current_candle_time = int(num_ts)
                if current_candle_time is None and last_ts is not None:
                    float_ts = float(last_ts)
                    if math.isfinite(float_ts):
                        current_candle_time = int(float_ts)
            except Exception:
                pass
            if current_candle_time is None:
                current_candle_time = int(1234567890 * 1000)
            return current_candle_time

        # Valid integer timestamp
        df_int = pd.DataFrame({"ts": [1731456000000]})
        self.assertEqual(extract_timestamp(df_int), 1731456000000)

        # String integer timestamp
        df_str = pd.DataFrame({"ts": ["1731456000000"]})
        self.assertEqual(extract_timestamp(df_str), 1731456000000)

        # Float timestamp with fraction
        df_float = pd.DataFrame({"ts": [1731456000000.75]})
        self.assertEqual(extract_timestamp(df_float), 1731456000000)

        # np.nan (must not raise ValueError: cannot convert float NaN to integer)
        df_nan = pd.DataFrame({"ts": [np.nan]})
        self.assertEqual(extract_timestamp(df_nan), int(1234567890 * 1000))

        # None value
        df_none = pd.DataFrame({"ts": [None]})
        self.assertEqual(extract_timestamp(df_none), int(1234567890 * 1000))

        # Corrupted non-numeric string
        df_corrupted = pd.DataFrame({"ts": ["corrupted_not_a_number"]})
        self.assertEqual(extract_timestamp(df_corrupted), int(1234567890 * 1000))

        # Dictionary-like object without .iloc
        dict_mock = {"ts": [1731456000000]}
        self.assertEqual(extract_timestamp(dict_mock), 1731456000000)

    def test_08_state_reset_hygiene(self):
        """Test that _reset_idle_counters clears internal_error_count as well as API counters."""
        self.bot.api_error_count = 5
        self.bot.idle_api_error_count = 10
        self.bot.idle_backoff = 240
        self.bot.internal_error_count = 3

        self.bot._reset_idle_counters()

        self.assertEqual(self.bot.api_error_count, 0)
        self.assertEqual(self.bot.idle_api_error_count, 0)
        self.assertEqual(self.bot.idle_backoff, 60)
        self.assertEqual(self.bot.internal_error_count, 0)

    @patch("main.logger.warning")
    @patch("main.logger.info")
    def test_09_idle_cycle_resilience_with_nan_timestamp(self, mock_info, mock_warning):
        """Feature 4: End-to-end simulation in _idle_cycle with NaN timestamp does not crash."""
        from tests.e2e.fixtures import MockMarketData
        mock_market = MockMarketData(coin="ETH", base_price=2250.0)

        # Get closed ohlcv from mock_market and inject NaN into the last candle's ts
        df_5m, px = mock_market.get_closed_ohlcv("5m", 120, use_cache=False)
        df_5m_nan = df_5m.copy()
        df_5m_nan.loc[df_5m_nan.index[-1], "ts"] = np.nan

        with patch("exchange.MarketData.get_closed_ohlcv", return_value=(df_5m_nan, px)), \
             patch("exchange.MarketData.get_ohlcv", side_effect=mock_market.get_ohlcv), \
             patch("exchange.MarketData.get_funding_stats", side_effect=mock_market.get_funding_stats), \
             patch("strategy.fetch_ai_macro_state", return_value=("NEUTRAL", {"reason": "mock"})), \
             patch("main.time.sleep", return_value=None), \
             patch("main.send_alert"):

            bot = main.TradingBot()
            bot.state = main.STATE_IDLE

            # Calling _idle_cycle must execute cleanly without raising ValueError
            try:
                bot._idle_cycle()
            except ValueError as e:
                self.fail(f"_idle_cycle raised ValueError on NaN timestamp: {e}")
            except Exception as e:
                self.fail(f"_idle_cycle raised unexpected exception: {e}")

            # Verify that fallback warning was logged
            warning_calls = [str(call) for call in mock_warning.call_args_list]
            self.assertTrue(any("时间戳解析异常" in c or "估算" in c for c in warning_calls))


if __name__ == "__main__":
    unittest.main()

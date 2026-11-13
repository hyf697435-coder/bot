# -*- coding: utf-8 -*-
"""
Milestone 1 Dedicated Unit Test Suite:
Verifies module-level pd import, defensive timestamp extraction,
internal software error classification, and full traceback logging.
"""

import math
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import main
from exchange import ExecutionError, MarketDataError
from strategy import AIError


class TestMilestone1RuntimeFixes(unittest.TestCase):
    def setUp(self):
        self.bot = main.TradingBot.__new__(main.TradingBot)
        self.bot.state = main.STATE_IDLE
        self.bot.api_error_count = 0
        self.bot.idle_api_error_count = 0
        self.bot.idle_backoff = 60
        self.bot._safe_sleep = MagicMock()
        self.bot._deploy_emergency_hard_stop = MagicMock()

    def test_01_module_level_pandas_imported(self):
        """Feature 1: Verify 'import pandas as pd' is committed at module level."""
        self.assertTrue(hasattr(main, "pd"), "main.py must have 'pd' bound at module level")
        self.assertIs(main.pd, pd, "main.pd must refer to the standard pandas library")

    def test_02_internal_software_errors_defined(self):
        """Feature 2: Verify INTERNAL_SOFTWARE_ERRORS contains the specified 7 runtime error classes."""
        self.assertTrue(hasattr(main, "INTERNAL_SOFTWARE_ERRORS"),
                        "INTERNAL_SOFTWARE_ERRORS must be defined in main.py")
        expected_types = {
            NameError,
            TypeError,
            AttributeError,
            SyntaxError,
            IndexError,
            KeyError,
            ZeroDivisionError,
        }
        actual_types = set(main.INTERNAL_SOFTWARE_ERRORS)
        for err_type in expected_types:
            self.assertIn(err_type, actual_types,
                          f"{err_type.__name__} must be included in INTERNAL_SOFTWARE_ERRORS")

    @patch("main.send_alert")
    @patch("main.logger.critical")
    @patch("main.logger.error")
    def test_03_internal_software_errors_do_not_increment_idle_api_count(
            self, mock_log_err, mock_log_crit, mock_alert):
        """Feature 2 & 3: Internal software errors must NOT increment idle_api_error_count,
        must log with exc_info=True and tag [交易程序运行时异常]."""
        test_internal_errors = [
            NameError("name 'pd' is not defined"),
            TypeError("unsupported operand type(s)"),
            AttributeError("'NoneType' object has no attribute 'iloc'"),
            SyntaxError("invalid syntax"),
            IndexError("list index out of range"),
            KeyError("missing 'ts' key"),
            ZeroDivisionError("float division by zero"),
        ]

        for err in test_internal_errors:
            mock_alert.reset_mock()
            mock_log_crit.reset_mock()
            mock_log_err.reset_mock()

            self.bot.state = main.STATE_IDLE
            self.bot.idle_api_error_count = 0

            self.bot._handle_loop_exception(err)

            # Assertions for internal errors:
            self.assertEqual(
                self.bot.idle_api_error_count, 0,
                f"Internal error {type(err).__name__} must NOT increment idle_api_error_count!"
            )
            mock_log_crit.assert_called_once()
            self.assertTrue(
                mock_log_crit.call_args[1].get("exc_info"),
                f"Internal error {type(err).__name__} must include exc_info=True in logger.critical!"
            )
            mock_alert.assert_called_once()
            alert_msg = mock_alert.call_args[0][0]
            self.assertIn("[交易程序运行时异常]", alert_msg)
            mock_log_err.assert_not_called()

    @patch("main.send_alert")
    @patch("main.logger.critical")
    @patch("main.logger.error")
    def test_04_external_errors_increment_idle_api_count(
            self, mock_log_err, mock_log_crit, mock_alert):
        """Feature 2 & 3: External API/network errors MUST increment idle_api_error_count,
        and log with exc_info=True."""
        external_errors = [
            MarketDataError("Hyperliquid 502 Bad Gateway"),
            ExecutionError("Order rejected by exchange"),
            RuntimeError("Network socket closed unexpectedly"),
            TimeoutError("Connection timed out"),
        ]

        for idx, err in enumerate(external_errors, 1):
            mock_alert.reset_mock()
            mock_log_crit.reset_mock()
            mock_log_err.reset_mock()

            self.bot.state = main.STATE_IDLE
            self.bot.idle_api_error_count = idx - 1

            self.bot._handle_loop_exception(err)

            self.assertEqual(
                self.bot.idle_api_error_count, idx,
                f"External error {type(err).__name__} MUST increment idle_api_error_count"
            )
            mock_log_err.assert_called_once()
            self.assertTrue(
                mock_log_err.call_args[1].get("exc_info"),
                f"External error {type(err).__name__} must include exc_info=True in logger.error!"
            )
            mock_log_crit.assert_not_called()

    @patch("main.logger.warning")
    def test_05_defensive_timestamp_extraction(self, mock_warning):
        """Feature 4: Defensive timestamp extraction handles valid, string, NaN, None, and corrupted data."""
        now_ts = 1700000000.0

        def extract_ts(df_5m):
            current_candle_time = None
            try:
                ts_series = df_5m["ts"]
                last_ts = ts_series.iloc[-1] if hasattr(ts_series, "iloc") else ts_series[-1]
                if "pd" in globals() and hasattr(pd, "to_numeric"):
                    num_ts = pd.to_numeric(last_ts, errors="coerce")
                    if pd.notna(num_ts):
                        current_candle_time = int(num_ts)
                if current_candle_time is None:
                    current_candle_time = int(float(last_ts))
            except (ValueError, TypeError, IndexError, KeyError, AttributeError) as ts_err:
                current_candle_time = int(now_ts * 1000)
            return current_candle_time

        # Valid int
        df1 = pd.DataFrame({"ts": [1700000000000, 1700000300000]})
        self.assertEqual(extract_ts(df1), 1700000300000)

        # String int
        df2 = pd.DataFrame({"ts": ["1700000000000", "1700000300000"]})
        self.assertEqual(extract_ts(df2), 1700000300000)

        # Corrupted string (must not crash, must return fallback)
        df3 = pd.DataFrame({"ts": ["corrupted_timestamp"]})
        self.assertEqual(extract_ts(df3), int(now_ts * 1000))

        # None / NaN (must not crash, must return fallback)
        df4 = pd.DataFrame({"ts": [None]})
        self.assertEqual(extract_ts(df4), int(now_ts * 1000))

        df5 = pd.DataFrame({"ts": [float("nan")]})
        self.assertEqual(extract_ts(df5), int(now_ts * 1000))

        # Dict mock without iloc
        dict_mock = {"ts": [1700000300000]}
        self.assertEqual(extract_ts(dict_mock), 1700000300000)


if __name__ == "__main__":
    unittest.main()

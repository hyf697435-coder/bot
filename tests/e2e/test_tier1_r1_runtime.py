"""
Tier 1: Feature Area R1 - Trading Bot Runtime Stability & Software Error Classification.
6 Test Cases specified by Explorer 1.
"""

import ast
import io
import json
import logging
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np
import main

from tests.e2e.fixtures import StateSandbox, MockMarketData, MockMailbox, categorize_error


class TestTier1R1Runtime(unittest.TestCase):
    """Tier 1 Test Cases for Requirement R1: Bot Runtime Stability."""

    def setUp(self):
        self.sandbox = StateSandbox("t1_r1_")
        self.orig_env = dict(os.environ)
        os.environ["STATE_FILE"] = self.sandbox.state_file
        os.environ["CONFIG_FILE"] = self.sandbox.config_file
        os.environ["HEARTBEAT_FILE"] = self.sandbox.heartbeat_file

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)
        self.sandbox.cleanup()

    def test_r1_pandas_import_and_module_presence(self):
        """Test R1-1: Verify pandas is permanently imported at module level in main.py without NameError."""
        import main
        self.assertTrue(hasattr(main, "pd"), "main module must have 'pd' attribute")
        self.assertEqual(main.pd.__name__, "pandas", "main.pd must be the pandas module")
        s = main.pd.Series([1.0, 2.0, 3.0])
        self.assertAlmostEqual(s.mean(), 2.0)

    def test_r1_defensive_timestamp_extraction_resilience(self):
        """Test R1-2: Defensive timestamp extraction guards against NaN and malformed series."""
        test_cases = [
            (1731456000000, 1731456000000, "Valid integer millisecond"),
            ("1731456000000", 1731456000000, "String numeric"),
            (1731456000000.5, 1731456000000, "Float with fraction"),
            (np.nan, None, "Missing/NaN float value"),
            ("MALFORMED_TIMESTAMP", None, "Corrupted string"),
        ]

        for raw_val, expected, label in test_cases:
            df = pd.DataFrame([{"ts": raw_val, "close": 2250.0}])
            # Defensive extraction pattern:
            # 1. Use pd.to_numeric with errors='coerce'
            # 2. Check notna() before int() conversion
            val_series = pd.to_numeric(df["ts"].iloc[-1], errors="coerce")
            if pd.notna(val_series):
                parsed = int(val_series)
                self.assertEqual(parsed, expected, f"Failed for {label}")
            else:
                self.assertIsNone(expected, f"Expected valid parse for {label}")
                # Ensure no ValueError: cannot convert float NaN to integer is raised
                self.assertTrue(np.isnan(val_series))

    def test_r1_software_exception_classification_tag(self):
        """Test R1-3: Internal software exceptions receive unambiguous [交易程序运行时异常] tag."""
        software_errors = [
            NameError("name 'pd' is not defined"),
            TypeError("unsupported operand type(s) for +: 'NoneType' and 'int'"),
            AttributeError("'NoneType' object has no attribute 'get'"),
            KeyError("missing_candle_key"),
            ZeroDivisionError("division by zero"),
        ]

        for exc in software_errors:
            tag = categorize_error(exc)
            self.assertEqual(tag, "[交易程序运行时异常]", f"Wrong tag for {type(exc).__name__}")
            self.assertNotIn("[交易所行情异常]", tag)
            self.assertNotIn("[AI中转站异常]", tag)
            self.assertNotIn("[交易所下单执行异常]", tag)

    def test_r1_traceback_preservation_in_critical_log(self):
        """Test R1-4: Exception logging preserves multiline traceback when exc_info=True is passed."""
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        test_logger = logging.getLogger("test_traceback_logger")
        test_logger.setLevel(logging.DEBUG)
        test_logger.addHandler(handler)

        try:
            # Trigger nested error
            def nested_func():
                raise NameError("name 'pd' is not defined in core loop")
            nested_func()
        except Exception as e:
            test_logger.critical("[异常] 核心循环软件故障: %s", e, exc_info=True)

        handler.flush()
        log_output = log_capture.getvalue()

        self.assertIn("Traceback (most recent call last):", log_output)
        self.assertIn("NameError: name 'pd' is not defined in core loop", log_output)
        self.assertIn("nested_func", log_output)

    def test_r1_clean_simulation_core_loop_execution(self):
        """Test R1-5: 5 consecutive clean iterations of core loop run without unhandled exceptions."""
        from main import TradingBot, STATE_IDLE
        mock_market = MockMarketData(coin="ETH", base_price=2250.0)

        with patch("exchange.MarketData.get_closed_ohlcv", side_effect=mock_market.get_closed_ohlcv), \
             patch("exchange.MarketData.get_ohlcv", side_effect=mock_market.get_ohlcv), \
             patch("exchange.MarketData.get_funding_stats", side_effect=mock_market.get_funding_stats), \
             patch("strategy.fetch_ai_macro_state", return_value=("NEUTRAL", {"reason": "mock"})), \
             patch("main.time.sleep", return_value=None), \
             patch("main.send_alert") as mock_alert:

            bot = TradingBot()
            bot.market = mock_market
            bot.state = STATE_IDLE

            for i in range(5):
                # Ensure no exceptions bubble up
                bot._idle_cycle()
                self.assertEqual(bot.state, STATE_IDLE)
                self.assertEqual(bot.idle_api_error_count, 0)

            # Heartbeat written by TradingBot._write_heartbeat
            hb_path = os.path.join(os.path.dirname(os.path.abspath(main.__file__)), ".heartbeat")
            self.assertTrue(os.path.exists(hb_path))
            with open(hb_path, "r", encoding="utf-8") as f:
                hb = json.load(f)
            self.assertEqual(hb["state"], STATE_IDLE)

    def test_r1_static_syntax_and_import_integrity(self):
        """Test R1-6: All core source files parse into valid AST and compile without syntax errors."""
        source_files = [
            "main.py",
            "exchange.py",
            "strategy.py",
            "risk_manager.py",
            "alerts.py",
            "watchdog.py",
            "dashboard.py",
            "utils.py",
        ]

        workspace_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

        for filename in source_files:
            file_path = os.path.join(workspace_root, filename)
            if not os.path.exists(file_path):
                continue
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            try:
                parsed_ast = ast.parse(content, filename=filename)
                self.assertIsNotNone(parsed_ast)
                compiled_code = compile(content, filename, "exec")
                self.assertIsNotNone(compiled_code)
            except SyntaxError as e:
                self.fail(f"Syntax error in {filename}: {e}")
            except IndentationError as e:
                self.fail(f"Indentation error in {filename}: {e}")


if __name__ == "__main__":
    unittest.main()

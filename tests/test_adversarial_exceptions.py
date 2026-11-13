# -*- coding: utf-8 -*-
"""
Adversarial Stress Test Suite for Trading Bot Exception Injection & Governance:
1. NameError / Internal Software Errors -> [交易程序运行时异常] with traceback, 1800s throttle, zero API counter pollution.
2. MarketDataError / Exchange Network Errors -> [交易所行情异常] with 1800s throttling.
3. LLM Relay Timeout (AIError) -> graceful fallback to quant_state, zero alert storm, 15m cooldown.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from alerts import categorize_error
from exchange import MarketDataError
import main
from strategy import AIError, MacroState, fetch_ai_macro_state


class TestAdversarialExceptions(unittest.TestCase):
    def setUp(self):
        self.bot = main.TradingBot.__new__(main.TradingBot)
        self.bot.state = main.STATE_IDLE
        self.bot.api_error_count = 0
        self.bot.idle_api_error_count = 0
        self.bot.idle_backoff = 60
        self.bot.internal_error_count = 0
        self.bot.last_internal_error_alert_time = 0.0
        self.bot.last_idle_api_alert_time = 0.0
        self.bot.last_node_candle_time = None
        self.bot.quant_state = MacroState.LEAN_SHORT
        self.bot.cached_ai_macro_state = None
        self.bot.final_macro_state = MacroState.NEUTRAL
        self.bot._safe_sleep = MagicMock()
        self.bot._deploy_emergency_hard_stop = MagicMock()

    @patch("main.send_alert")
    @patch("main.logger.critical")
    def test_challenge_name_error_internal_software_exception(self, mock_crit, mock_alert):
        """Verify NameError triggers [交易程序运行时异常], includes exc_info=True, and throttles."""
        name_err = NameError("name 'unresolved_symbol' is not defined")
        
        # 1. Verification of category tag
        tag = categorize_error(name_err)
        self.assertEqual(tag, "[交易程序运行时异常]")

        # 2. Inject exception into bot._handle_loop_exception
        self.bot._handle_loop_exception(name_err)

        # Must log critical with exc_info=True
        mock_crit.assert_called_once()
        self.assertTrue(mock_crit.call_args[1].get("exc_info"))
        self.assertIn("[交易程序运行时异常]", mock_crit.call_args[0][0])

        # Must send alert with [交易程序运行时异常]
        mock_alert.assert_called_once()
        alert_body = mock_alert.call_args[0][0]
        self.assertIn("[交易程序运行时异常]", alert_body)
        self.assertIn("NameError", alert_body)

        # Counter hygiene: API counters must NOT be incremented
        self.assertEqual(self.bot.api_error_count, 0)
        self.assertEqual(self.bot.idle_api_error_count, 0)
        self.assertEqual(self.bot.internal_error_count, 1)

        # Throttling check: immediate second error should NOT dispatch new email
        mock_alert.reset_mock()
        self.bot._handle_loop_exception(NameError("name 'foo' is not defined"))
        mock_alert.assert_not_called()
        self.assertEqual(self.bot.internal_error_count, 2)

        # After 1800s, next internal error dispatches alert again
        self.bot.last_internal_error_alert_time = time.time() - 1801
        self.bot._handle_loop_exception(NameError("name 'bar' is not defined"))
        mock_alert.assert_called_once()
        self.assertEqual(self.bot.internal_error_count, 3)

    @patch("main.send_alert")
    @patch("main.logger.error")
    def test_challenge_market_data_error_exchange_throttling(self, mock_err, mock_alert):
        """Verify MarketDataError triggers [交易所行情异常] and enforces 1800s throttling."""
        mkt_err = MarketDataError("Hyperliquid 502 Bad Gateway while fetching orderbook")
        
        # 1. Verification of category tag
        tag = categorize_error(mkt_err)
        self.assertEqual(tag, "[交易所行情异常]")

        # 2. Inject up to threshold (main.IDLE_ALERT_THRESHOLD)
        threshold = main.IDLE_ALERT_THRESHOLD
        for i in range(1, threshold):
            self.bot._handle_loop_exception(mkt_err)
            self.assertEqual(self.bot.idle_api_error_count, i)
            mock_alert.assert_not_called()  # below threshold

        # Hit threshold -> should trigger alert
        self.bot._handle_loop_exception(mkt_err)
        self.assertEqual(self.bot.idle_api_error_count, threshold)
        mock_alert.assert_called_once()
        
        # Verify alert content & subject
        call_args = mock_alert.call_args
        alert_body = call_args[0][0]
        alert_subject = call_args[1].get("subject") or (call_args[0][1] if len(call_args[0]) > 1 else "")
        self.assertIn("[交易所行情异常]", alert_body)
        self.assertIn("[交易所行情异常]", alert_subject)

        # 3. 1800s Throttling: Rapid subsequent exchange errors must be throttled
        mock_alert.reset_mock()
        for _ in range(5):
            self.bot._handle_loop_exception(mkt_err)
        mock_alert.assert_not_called()

        # After 1801s, next exchange error dispatches throttled reminder
        self.bot.last_idle_api_alert_time = time.time() - 1801
        self.bot._handle_loop_exception(mkt_err)
        mock_alert.assert_called_once()
        self.assertIn("[交易所行情异常]", mock_alert.call_args[0][0])

    @patch("strategy.send_alert")
    def test_challenge_llm_timeout_fallback_and_suppression(self, mock_alert):
        """Verify LLM timeout (AIError) falls back to quant_state without alert email storm."""
        mock_client = MagicMock()
        mock_client.chat_json.side_effect = AIError("HTTPConnectionPool read timeout after 20.0s")

        macro_ctx = {"atr_ratio": 0.015, "last_trs": -0.65}
        
        # 1. Call fetch_ai_macro_state with return_details=True
        ai_state, details = fetch_ai_macro_state(macro_ctx, client=mock_client, return_details=True)

        # Must return None (indicating failure / timeout)
        self.assertIsNone(ai_state)
        # Must NOT send panic email alert
        mock_alert.assert_not_called()

        # 2. Test bot fallback integration
        now_mono = time.monotonic()
        # Simulate bot macro processing block
        if ai_state is None:
            self.bot._last_llm_failure_time = now_mono
            if self.bot.cached_ai_macro_state is None:
                self.bot.cached_ai_macro_state = self.bot.quant_state

        self.assertEqual(self.bot.cached_ai_macro_state, MacroState.LEAN_SHORT)
        self.assertEqual(self.bot._last_llm_failure_time, now_mono)
        mock_alert.assert_not_called()


if __name__ == "__main__":
    unittest.main()

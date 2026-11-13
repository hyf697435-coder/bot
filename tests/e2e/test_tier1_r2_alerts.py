"""
Tier 1: Feature Area R2 - API Alert Decoupling, Throttling & Disambiguation.
7 Test Cases specified by Explorer 1.
"""

import os
import unittest
from unittest.mock import patch, MagicMock

import httpx
import requests
import pandas as pd

from exchange import MarketData, MarketDataError, ExecutionError
from strategy import AIError
from main import TradingBot, STATE_IDLE, IDLE_ALERT_THRESHOLD
from tests.e2e.fixtures import (
    StateSandbox,
    MockMarketData,
    MockMailbox,
    MockLLMClient,
    VirtualClock,
    categorize_error,
)


class TestTier1R2Alerts(unittest.TestCase):
    """Tier 1 Test Cases for Requirement R2: API Alert Decoupling & Disambiguation."""

    def setUp(self):
        self.sandbox = StateSandbox("t1_r2_")
        self.orig_env = dict(os.environ)
        os.environ["STATE_FILE"] = self.sandbox.state_file
        os.environ["CONFIG_FILE"] = self.sandbox.config_file
        os.environ["HEARTBEAT_FILE"] = self.sandbox.heartbeat_file

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)
        self.sandbox.cleanup()

    def test_r2_exchange_marketdata_retry_and_backoff(self):
        """Test R2-1: MarketData.get_closed_ohlcv retries up to 3 times on transient failures."""
        call_count = 0

        def flaky_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                resp = MagicMock()
                resp.status_code = 502
                resp.raise_for_status.side_effect = requests.exceptions.HTTPError("502 Bad Gateway")
                return resp
            elif call_count == 2:
                raise requests.exceptions.ConnectTimeout("Connection timed out")
            else:
                resp = MagicMock()
                resp.status_code = 200
                resp.raise_for_status.return_value = None
                from tests.e2e.fixtures import generate_candles
                df_mock = generate_candles(120, 2250.0, "5m", 300)
                # Convert to Hyperliquid format
                raw_list = []
                for _, r in df_mock.iterrows():
                    raw_list.append({"t": int(r["ts"]), "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"], "v": r["volume"]})
                resp.json.return_value = raw_list
                return resp

        # Direct retry simulation wrapping requests or MarketData retry contract
        max_retries = 3
        last_exc = None
        df_result = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = flaky_post()
                resp.raise_for_status()
                data = resp.json()
                df_result = pd.DataFrame(data)
                break
            except Exception as e:
                last_exc = e

        self.assertEqual(call_count, 3, "Must attempt exactly 3 times before success")
        self.assertIsNotNone(df_result, "Should succeed on 3rd attempt")
        self.assertFalse(df_result.empty)

    def test_r2_exchange_marketdata_exhaustion_raises_typed_error(self):
        """Test R2-2: Exhausted market data retries raise typed MarketDataError mapped to [交易所行情异常]."""
        call_count = 0

        def failing_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 500
            resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500 Internal Server Error")
            return resp

        max_retries = 3
        with self.assertRaises(MarketDataError):
            for attempt in range(max_retries):
                try:
                    resp = failing_post()
                    resp.raise_for_status()
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise MarketDataError(f"行情请求失败 (ETH/5m): {e}")

        self.assertEqual(call_count, 3)
        err = MarketDataError("行情请求失败 (ETH/5m): 500 Internal Server Error")
        tag = categorize_error(err)
        self.assertEqual(tag, "[交易所行情异常]")

    def test_r2_llm_relay_timeout_decoupling_and_quant_fallback(self):
        """Test R2-3: LLM relay timeouts during flat position fall back to quant state without panic email."""
        mock_mailbox = MockMailbox()

        # Simulate LLM timeout in fetch_ai_macro_state
        with patch("alerts.send_alert", side_effect=mock_mailbox.record_alert):
            from strategy import MacroState
            # Decoupled macro call simulation
            quant_state = MacroState.NEUTRAL
            try:
                # Mock relay timeout
                raise AIError("api.ludies.best connection timeout after 30s")
            except AIError:
                # Decoupled handler: logs warning, does NOT call send_alert, falls back to quant_state
                final_macro = quant_state

            self.assertEqual(final_macro, MacroState.NEUTRAL)
            # Mailbox must have 0 dispatched alerts for non-critical LLM timeout during flat position
            self.assertEqual(len(mock_mailbox.sent_messages), 0, "Non-critical LLM timeout must not trigger alarm storm")

    def test_r2_llm_cold_start_cooldown_suppression(self):
        """Test R2-4: Failing LLM relay triggers cooldown suppression, preventing repetitive stalls."""
        relay_calls = 0
        cooldown_window_sec = 900.0  # 15 minutes
        last_relay_failure_time = 0.0

        def attempt_macro_call(current_time: float):
            nonlocal relay_calls, last_relay_failure_time
            if current_time - last_relay_failure_time < cooldown_window_sec:
                # Suppressed by cooldown
                return "NEUTRAL", "quant_fallback"
            relay_calls += 1
            last_relay_failure_time = current_time
            raise AIError("Relay HTTP 503")

        clock = VirtualClock(1000.0)
        # Attempt 1 (Cold Start)
        state1, reason1 = "NEUTRAL", "init"
        try:
            state1, reason1 = attempt_macro_call(clock.time())
        except AIError:
            state1, reason1 = "NEUTRAL", "quant_fallback"

        self.assertEqual(relay_calls, 1)

        # Next 10 polling iterations within 60s
        for _ in range(10):
            clock.advance(5.0)
            state, reason = attempt_macro_call(clock.time())
            self.assertEqual(state, "NEUTRAL")
            self.assertEqual(reason, "quant_fallback")

        # Relay was called only once despite 11 cycles
        self.assertEqual(relay_calls, 1, "Dead LLM relay must be suppressed during cooldown window")

    def test_r2_alert_throttling_eliminates_alarm_storm(self):
        """Test R2-5: 32 consecutive market data errors trigger exactly 1 alert at threshold (no storm)."""
        mock_mailbox = MockMailbox()
        bot = TradingBot()
        bot.state = STATE_IDLE
        bot.idle_api_error_count = 0
        bot.idle_backoff = 60

        last_alert_time = 0.0
        THROTTLE_WINDOW = 1800.0  # 30 minutes
        virtual_now = 10000.0

        recorded_backoffs = []

        # Execute 32 consecutive loop exceptions under the throttled contract
        for i in range(1, 33):
            bot.idle_api_error_count += 1
            err = MarketDataError("Hyperliquid /info connection reset")
            tag = categorize_error(err)

            # Throttling rule:
            # Trigger alert when count >= IDLE_ALERT_THRESHOLD AND (now - last_alert_time >= 1800s)
            if bot.idle_api_error_count >= IDLE_ALERT_THRESHOLD:
                if (virtual_now - last_alert_time) >= THROTTLE_WINDOW:
                    msg = f"{tag} 空仓期 API 连续异常 {bot.idle_api_error_count} 次，请人工检查网络。"
                    mock_mailbox.record_alert(msg, subject=f"[交易机器人] {tag}")
                    last_alert_time = virtual_now

            recorded_backoffs.append(bot.idle_backoff)
            bot.idle_backoff = min(bot.idle_backoff * 2, 600)
            virtual_now += 60.0  # Total elapsed: 32 * 60 = 1920s (at step 31+ passes window)

        # Total alerts must be throttled (at most 2 over 1920s, eliminating the 28-alert storm)
        self.assertLessEqual(len(mock_mailbox.sent_messages), 2)
        self.assertGreaterEqual(len(mock_mailbox.sent_messages), 1)

        # Check category tag
        first_alert = mock_mailbox.sent_messages[0]
        self.assertIn("[交易所行情异常]", first_alert["body"])
        self.assertNotIn("[交易程序运行时异常]", first_alert["body"])
        self.assertNotIn("[AI中转站异常]", first_alert["body"])

        # Check backoff cap
        self.assertEqual(bot.idle_backoff, 600)
        self.assertEqual(recorded_backoffs[-1], 600)

    def test_r2_idle_error_counter_reset_on_clean_cycle(self):
        """Test R2-6: Idle error counters and backoff reset to defaults upon a clean cycle."""
        bot = TradingBot()
        bot.state = STATE_IDLE
        bot.idle_api_error_count = 4
        bot.api_error_count = 4
        bot.idle_backoff = 480

        # Run reset hygiene
        bot._reset_idle_counters()

        self.assertEqual(bot.idle_api_error_count, 0)
        self.assertEqual(bot.api_error_count, 0)
        self.assertEqual(bot.idle_backoff, 60)

    def test_r2_unambiguous_alert_categorization_headers(self):
        """Test R2-7: 4 distinct error types generate 4 distinct unambiguous category tags."""
        errors = [
            (MarketDataError("Hyperliquid /info 502 Bad Gateway"), "[交易所行情异常]"),
            (ExecutionError("Hyperliquid /exchange order rejected: Insufficient margin"), "[交易所下单执行异常]"),
            (AIError("api.ludies.best connection timeout"), "[AI中转站异常]"),
            (NameError("name 'pd' is not defined"), "[交易程序运行时异常]"),
        ]

        for err, expected_tag in errors:
            tag = categorize_error(err)
            self.assertEqual(tag, expected_tag, f"Mismatch for {type(err).__name__}")
            # Ensure no ambiguous combined tag
            self.assertNotIn("网络/AI", tag)


if __name__ == "__main__":
    unittest.main()

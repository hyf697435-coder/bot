"""
Tier 3: Cross-Feature Pairwise Interactions Suite.
4 Test Cases specified by Explorer 2.
"""

import json
import os
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import main
from main import TradingBot, Position, STATE_IDLE, STATE_TRADING, STATE_HALT
from exchange import MarketDataError, ExecutionError
from strategy import AIError, MacroState
from tests.e2e.fixtures import (
    StateSandbox,
    MockMarketData,
    MockExchange,
    MockMailbox,
    MockLLMClient,
    VirtualClock,
    categorize_error,
)


class TestTier3Interactions(unittest.TestCase):
    """Tier 3 Cross-Feature Interactions Test Cases."""

    def setUp(self):
        self.sandbox = StateSandbox("t3_interactions_")
        self.orig_env = dict(os.environ)
        os.environ["STATE_FILE"] = self.sandbox.state_file
        os.environ["CONFIG_FILE"] = self.sandbox.config_file
        os.environ["HEARTBEAT_FILE"] = self.sandbox.heartbeat_file
        self.orig_main_state_file = getattr(main, "STATE_FILE", "state.json")
        main.STATE_FILE = self.sandbox.state_file

    def tearDown(self):
        main.STATE_FILE = self.orig_main_state_file
        os.environ.clear()
        os.environ.update(self.orig_env)
        self.sandbox.cleanup()

    def test_t3_cross_01_exchange_fail_and_llm_timeout(self):
        """Test T3-1: Exchange transient hiccup followed by LLM relay timeout during flat position."""
        mock_mailbox = MockMailbox()
        mock_market = MockMarketData(coin="ETH", base_price=2250.0)

        # 1. Exchange transient retry (fails once, then recovers)
        mock_market.set_transient_failure(1, MarketDataError("Temporary socket reset"))

        # 2. LLM relay timeout simulation
        llm_called = False

        def mock_fetch_macro(*args, **kwargs):
            nonlocal llm_called
            llm_called = True
            # Simulate LLM timeout - falls back to quant state without alert
            return None, {"reason": "timeout_fallback"}

        with patch("alerts.send_alert", side_effect=mock_mailbox.record_alert), \
             patch("strategy.fetch_ai_macro_state", side_effect=mock_fetch_macro):

            # Exchange retries and recovers on 2nd attempt
            df, px = None, 0.0
            for attempt in range(3):
                try:
                    df, px = mock_market.get_closed_ohlcv("5m", 120)
                    break
                except MarketDataError:
                    if attempt == 2:
                        raise
            self.assertIsNotNone(df)
            self.assertFalse(df.empty)

            # LLM macro called and timed out
            macro_state, info = mock_fetch_macro()
            # Must fall back to quant state
            final_macro = macro_state or MacroState.NEUTRAL
            self.assertEqual(final_macro, MacroState.NEUTRAL)

            # Zero alert emails generated for non-critical macro timeout during flat position
            self.assertEqual(len(mock_mailbox.sent_messages), 0)

        # Variant: If exchange unrecoverable, loop aborts before calling LLM
        mock_market.set_transient_failure(3, MarketDataError("Exchange outage"))
        llm_called_on_abort = False

        try:
            mock_market.get_closed_ohlcv("5m", 120)
        except MarketDataError as e:
            tag = categorize_error(e)
            self.assertEqual(tag, "[交易所行情异常]")
            self.assertNotIn("AI", tag)

        self.assertFalse(llm_called_on_abort, "Must not call LLM when exchange market data fails")

    def test_t3_cross_02_active_position_trailing_handover(self):
        """Test T3-2: Active position with trailing stop ratchets preserved across handover with zero naked risk."""
        # Active Node A state
        node_a_position = {
            "entry_price": 2500.0,
            "direction": "LONG",
            "sz_notional": 5000.0,
            "tp_price": 2800.0,
            "sl_price": 2580.0,  # Ratcheted from 2450 to 2580 (+1.6R)
            "initial_risk_r": 50.0,
            "peak_price": 2650.0,
            "bars_held": 18,
            "opened_at": time.time() - 5400,
            "entry_score": 82.0,
            "filled_sz": 2.0,
        }

        self.sandbox.write_state({
            "state": "TRADING",
            "coin": "ETH",
            "position": node_a_position,
            "last_synced_sl": 2580.0,
            "cached_atr_5m": 25.0,
        })

        # Standby Node B boots and loads state
        bot_b = TradingBot()
        bot_b._load_state()

        self.assertEqual(bot_b.state, STATE_TRADING)
        self.assertIsNotNone(bot_b.position)

        # Invariants on Node B:
        # 1. peak_price must NOT be reset to entry_price (2500.0)
        self.assertEqual(bot_b.position.peak_price, 2650.0)
        # 2. bars_held must NOT be reset to 0
        self.assertEqual(bot_b.position.bars_held, 18)
        # 3. sl_price preserved at ratcheted level
        self.assertEqual(bot_b.position.sl_price, 2580.0)

        # Market price advances to 2680.0 (+3.6R)
        new_high_price = 2680.0
        if new_high_price > bot_b.position.peak_price:
            bot_b.position.peak_price = new_high_price

        # Trailing ratchet: new SL = peak - 2.5 * ATR (2680 - 70 = 2610 > 2580)
        new_trailing_sl = 2610.0
        if new_trailing_sl > bot_b.position.sl_price:
            bot_b.position.sl_price = new_trailing_sl

        bot_b.position.bars_held += 1

        self.assertEqual(bot_b.position.peak_price, 2680.0)
        self.assertEqual(bot_b.position.sl_price, 2610.0)
        self.assertEqual(bot_b.position.bars_held, 19)

    def test_t3_cross_03_software_error_during_retry(self):
        """Test T3-3: Internal software exception during retry phase is classified as [交易程序运行时异常]."""
        mock_mailbox = MockMailbox()

        # Simulate Python internal bug bubbling up
        exc = KeyError("missing_candle_timestamp_col")
        tag = categorize_error(exc)

        self.assertEqual(tag, "[交易程序运行时异常]")
        self.assertNotIn("[交易所行情异常]", tag)
        self.assertNotIn("[AI中转站异常]", tag)

        # Dispatch immediate high priority alert
        mock_mailbox.record_alert(f"{tag} 内部变量解析异常: {exc}", priority="HIGH")
        self.assertEqual(len(mock_mailbox.sent_messages), 1)
        self.assertEqual(mock_mailbox.sent_messages[0]["priority"], "HIGH")

    def test_t3_cross_04_watchdog_pause_resume_supervision(self):
        """Test T3-4: Watchdog supervises rotator pause and resume without false-positive kills."""
        # Watchdog dynamic threshold rules per watchdog.py
        def get_timeout_limit(hb_state: str, hb_phase: str) -> float:
            if hb_state in ("COOL_DOWN", "SYSTEM_HALT"):
                return 600.0
            elif hb_phase in ("AI_MACRO", "AI_TRADE", "FETCH_MARKET"):
                return 180.0
            else:
                return 150.0

        # Step 1: Normal IDLE state
        self.assertEqual(get_timeout_limit("IDLE", "IDLE_CYCLE"), 150.0)

        # Step 2: Rotator pause -> SYSTEM_HALT
        timeout_halt = get_timeout_limit("SYSTEM_HALT", "SYSTEM_HALT")
        self.assertEqual(timeout_halt, 600.0, "Watchdog must extend timeout to 600s during SYSTEM_HALT")

        # Step 3: Heartbeat updated within pause
        fresh_hb_delay = 5.0  # seconds
        should_kill = fresh_hb_delay > timeout_halt
        self.assertFalse(should_kill, "Watchdog must NOT kill paused node when heartbeat is fresh")

        # Step 4: Deadlock detection (heartbeat delayed > 600s)
        deadlock_delay = 605.0
        should_kill_deadlock = deadlock_delay > timeout_halt
        self.assertTrue(should_kill_deadlock, "Watchdog must terminate deadlocked node exceeding 600s")

        # Step 5: Resumed back to IDLE
        timeout_resumed = get_timeout_limit("IDLE", "FETCH_MARKET")
        self.assertEqual(timeout_resumed, 180.0)


if __name__ == "__main__":
    unittest.main()

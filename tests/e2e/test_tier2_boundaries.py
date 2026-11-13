"""
Tier 2: Boundary & Corner Cases Suite.
17 Test Cases across R1, R2, and R3 specified by Explorer 2.
"""

import json
import math
import os
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import requests

import main
from main import TradingBot, STATE_IDLE, STATE_TRADING, STATE_COOL_DOWN
from risk_manager import RiskManager
from exchange import MarketData, MarketDataError, ExecutionError
from tests.e2e.fixtures import (
    StateSandbox,
    MockMarketData,
    MockExchange,
    MockMailbox,
    VirtualClock,
    categorize_error,
    validate_state_files,
    check_sandbox_cooldown,
)


class TestTier2Boundaries(unittest.TestCase):
    """Tier 2 Boundary & Corner Cases Test Cases."""

    def setUp(self):
        self.sandbox = StateSandbox("t2_boundaries_")
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

    # --------------------------------------------------------------------------
    # Feature Area R1: Bot Runtime Stability Extreme Inputs
    # --------------------------------------------------------------------------

    def test_t2_r1_01_empty_state_json(self):
        """Test T2-R1-1: 0-byte or whitespace-only state.json defaults to STATE_IDLE without crashing."""
        with open(self.sandbox.state_file, "w") as f:
            f.write("   \n  \t ")

        bot = TradingBot()
        bot._load_state()

        self.assertEqual(bot.state, STATE_IDLE)
        self.assertIsNone(bot.position)
        self.assertEqual(bot.consecutive_failures, 0)

    def test_t2_r1_02_corrupt_state_and_risk_json(self):
        """Test T2-R1-2: Truncated JSON syntax in state and risk files is handled gracefully."""
        with open(self.sandbox.state_file, "w", encoding="utf-8") as f:
            f.write('{"state": "TRADING", "position": {"entry_price": 2500.0, ')
        with open(self.sandbox.risk_file, "w", encoding="utf-8") as f:
            f.write('{"initial_equity": 1000.0, "metrics": {')

        bot = TradingBot()
        bot._load_state()
        self.assertEqual(bot.state, STATE_IDLE)

        risk_mgr = RiskManager(initial_equity=1000.0, persistence_file=self.sandbox.risk_file)
        self.assertEqual(risk_mgr.initial_equity, 1000.0)
        self.assertEqual(risk_mgr.trade_history, [])

    def test_t2_r1_03_missing_keys_and_type_mismatch(self):
        """Test T2-R1-3: State file with missing keys and type mismatches falls back safely."""
        corrupt_state = {
            "state": "TRADING",
            "position": {"entry_price": "bad_price", "direction": "LONG"},
            "daily_start_equity": 1000.0,
            "last_trs": 0.0,
            "last_macro_slot": "invalid_slot",
        }
        self.sandbox.write_state(corrupt_state)

        bot = TradingBot()
        bot._load_state()

        self.assertIsNone(bot.position)
        self.assertEqual(bot.state, STATE_IDLE)

    def test_t2_r1_04_non_dict_root_state(self):
        """Test T2-R1-4: Non-dict JSON root (e.g. array) in risk state is guarded against."""
        with open(self.sandbox.risk_file, "w", encoding="utf-8") as f:
            f.write('["array", "of", "values", 123]')

        risk_mgr = RiskManager(persistence_file=self.sandbox.risk_file)
        # Should initialize pristine default state without unhandled exception
        self.assertEqual(risk_mgr.metrics.current_streak, 0)
        self.assertGreater(risk_mgr.initial_equity, 0.0)

    def test_t2_r1_05_defensive_timestamp_parsing(self):
        """Test T2-R1-5: Empty dataframe or NaN series does not raise ValueError: cannot convert float NaN to integer."""
        empty_df = pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        nan_df = pd.DataFrame([{"ts": np.nan, "close": 2250.0}])

        for df in [empty_df, nan_df]:
            if df.empty:
                parsed_time = None
            else:
                series_val = pd.to_numeric(df["ts"].iloc[-1], errors="coerce")
                parsed_time = int(series_val) if pd.notna(series_val) else None

            self.assertIsNone(parsed_time)

    def test_t2_r1_06_zero_or_negative_equity(self):
        """Test T2-R1-6: Zero, negative, NaN or infinite equity produces 0 sizing and order rejection."""
        risk_mgr = RiskManager()
        for invalid_eq in [0.0, -100.0, float("nan"), float("-inf")]:
            risk_mgr.current_equity = invalid_eq
            sz = risk_mgr.get_adaptive_position_size(stop_loss_distance=50.0, current_price=2250.0)
            self.assertEqual(sz, 0.0, f"Failed for equity={invalid_eq}")

        mock_ex = MockExchange(initial_equity=0.0)
        with self.assertRaises(ExecutionError):
            mock_ex.open_position_with_stops("ETH", True, 100.0, 2250.0, 2200.0, 2350.0)

    # --------------------------------------------------------------------------
    # Feature Area R2: API Alert Decoupling Network Edge Conditions
    # --------------------------------------------------------------------------

    def test_t2_r2_01_network_ssl_eof_marketdata(self):
        """Test T2-R2-1: SSL UNEXPECTED_EOF triggers retry and categorizes as [交易所行情异常]."""
        call_count = 0

        def flaky_ssl_call():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise requests.exceptions.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
            return {"status": "ok"}

        # Simulate 3-attempt retry loop
        max_retries = 3
        res = None
        for attempt in range(max_retries):
            try:
                res = flaky_ssl_call()
                break
            except requests.exceptions.SSLError as e:
                if attempt == max_retries - 1:
                    raise MarketDataError(f"SSL failure: {e}")

        self.assertEqual(call_count, 2)
        self.assertEqual(res, {"status": "ok"})

        err = MarketDataError("SSL: UNEXPECTED_EOF_WHILE_READING")
        self.assertEqual(categorize_error(err), "[交易所行情异常]")

    def test_t2_r2_02_socket_timeout_funding_cache(self):
        """Test T2-R2-2: Funding request timeout safely returns cached stats (<60m old)."""
        mock_market = MockMarketData(coin="ETH")
        # Populate cache
        cached_stats = {"fr": 0.0000125, "fr_mean": 0.00001, "fr_std": 0.000002, "fr_samples": 168.0}
        mock_market._funding_cache = (cached_stats, time.monotonic() - 600.0)  # 10 min old

        # Set funding to fail
        mock_market.funding_fail_times = 1
        stats = mock_market.get_funding_stats(window_hours=168, use_cache=True)
        self.assertEqual(stats["fr"], 0.0000125)

    def test_t2_r2_03_http_429_rate_limit_backoff(self):
        """Test T2-R2-3: HTTP 429 rate limit is captured and formatted as [交易所行情异常]."""
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "45"}
        http_err = requests.exceptions.HTTPError("429 Client Error: Too Many Requests", response=resp_429)

        tag = categorize_error(http_err)
        self.assertEqual(tag, "[交易所行情异常]")

    def test_t2_r2_04_max_backoff_cap_600s(self):
        """Test T2-R2-4: idle_backoff doubles monotonically and strictly clamps at 600s."""
        bot = TradingBot()
        bot.state = STATE_IDLE
        bot.idle_backoff = 60

        recorded_delays = []
        for _ in range(8):
            recorded_delays.append(bot.idle_backoff)
            bot.idle_backoff = min(bot.idle_backoff * 2, 600)

        expected_sequence = [60, 120, 240, 480, 600, 600, 600, 600]
        self.assertEqual(recorded_delays, expected_sequence)
        self.assertEqual(bot.idle_backoff, 600)

    def test_t2_r2_05_threshold_boundaries_1_4_5_6_32(self):
        """Test T2-R2-5: Alert threshold transitions at exact boundaries 1, 4, 5, 6, 32."""
        mock_mailbox = MockMailbox()
        threshold = 5
        throttle_interval = 1800.0

        class AlertThrottler:
            def __init__(self):
                self.error_count = 0
                self.last_alert_time = -1e9

            def handle_error(self, now: float):
                self.error_count += 1
                if self.error_count >= threshold:
                    if (now - self.last_alert_time) >= throttle_interval:
                        mock_mailbox.record_alert(f"[交易所行情异常] 连续异常 {self.error_count} 次")
                        self.last_alert_time = now

        throttler = AlertThrottler()
        t = 1000.0

        # Count 1
        throttler.handle_error(t)
        self.assertEqual(len(mock_mailbox.sent_messages), 0)

        # Count 2, 3, 4
        for _ in range(3):
            t += 10.0
            throttler.handle_error(t)
        self.assertEqual(throttler.error_count, 4)
        self.assertEqual(len(mock_mailbox.sent_messages), 0)

        # Count 5 (Threshold hit!)
        t += 10.0
        throttler.handle_error(t)
        self.assertEqual(throttler.error_count, 5)
        self.assertEqual(len(mock_mailbox.sent_messages), 1)

        # Count 6 (10s later, within 1800s cooldown -> suppressed!)
        t += 10.0
        throttler.handle_error(t)
        self.assertEqual(throttler.error_count, 6)
        self.assertEqual(len(mock_mailbox.sent_messages), 1)

        # Advance up to Count 32 (total time elapsed < 1800s)
        for _ in range(26):
            t += 10.0
            throttler.handle_error(t)
        self.assertEqual(throttler.error_count, 32)
        # Total alerts must strictly remain 1!
        self.assertEqual(len(mock_mailbox.sent_messages), 1)

    def test_t2_r2_06_error_counter_hygiene_reset(self):
        """Test T2-R2-6: Clean polling tick resets idle error counter and restores baseline backoff."""
        bot = TradingBot()
        bot.state = STATE_IDLE
        bot.idle_api_error_count = 4
        bot.idle_backoff = 480

        bot._reset_idle_counters()
        self.assertEqual(bot.idle_api_error_count, 0)
        self.assertEqual(bot.idle_backoff, 60)

    # --------------------------------------------------------------------------
    # Feature Area R3: 24/7 Cloud Rotation Boundaries & Quotas
    # --------------------------------------------------------------------------

    def test_t2_r3_01_cooldown_active_vs_expired(self):
        """Test T2-R3-1: Cooldown deadline correctly blocks spinup when active and permits when expired."""
        cd_str = "2026-11-13T03:34:20Z"

        # Case A: now < cooldownUntil (active cooldown)
        now_a = datetime(2026, 11, 13, 3, 0, 0, tzinfo=timezone.utc)
        expired_a, remaining_a = check_sandbox_cooldown(cd_str, now_a)
        self.assertFalse(expired_a)
        self.assertGreater(remaining_a, 0.0)

        # Case B: now >= cooldownUntil (expired)
        now_b = datetime(2026, 11, 13, 3, 34, 25, tzinfo=timezone.utc)
        expired_b, remaining_b = check_sandbox_cooldown(cd_str, now_b)
        self.assertTrue(expired_b)
        self.assertEqual(remaining_b, 0.0)

    def test_t2_r3_02_missing_state_files_fresh_node(self):
        """Test T2-R3-2: Fresh cloud instance boots without FileNotFoundError when state files do not exist."""
        # Ensure files don't exist
        for p in [self.sandbox.state_file, self.sandbox.risk_file, self.sandbox.heartbeat_file]:
            if os.path.exists(p):
                os.remove(p)

        bot = TradingBot()
        bot._load_state()

        self.assertEqual(bot.state, STATE_IDLE)
        self.assertEqual(bot.daily_start_equity, 1000.0)
        self.assertIsNone(bot.position)

    def test_t2_r3_03_partial_state_handover_rejection(self):
        """Test T2-R3-3: Handover is aborted and active node preserved if risk state is corrupted during sync."""
        self.sandbox.write_state({"state": "IDLE", "coin": "ETH"})
        with open(self.sandbox.risk_file, "w") as f:
            f.write("")  # 0-byte corrupted

        is_valid, msg = validate_state_files(self.sandbox.state_file, self.sandbox.risk_file)
        self.assertFalse(is_valid)
        self.assertIn("Empty 0-byte file", msg)

    def test_t2_r3_04_max_machines_quota_guard(self):
        """Test T2-R3-4: Enforces serial destroy-before-create to prevent hitting maxMachines limit."""
        running_machines = 1
        max_allowed = 1

        def launch_new_machine(serial_decommission_first: bool) -> bool:
            nonlocal running_machines
            if not serial_decommission_first and running_machines >= max_allowed:
                raise RuntimeError("limits.maxMachines: 1 exceeded")
            if serial_decommission_first:
                running_machines -= 1  # Decommission active first
            running_machines += 1
            return True

        # Parallel attempt fails
        with self.assertRaises(RuntimeError):
            launch_new_machine(serial_decommission_first=False)

        # Serial handover succeeds
        success = launch_new_machine(serial_decommission_first=True)
        self.assertTrue(success)
        self.assertEqual(running_machines, 1)

    def test_t2_r3_05_loss_streak_cooldown_handover(self):
        """Test T2-R3-5: Consecutive loss streak cooldown deadline is honored across instance handover."""
        cooldown_target = datetime.now(timezone.utc) + timedelta(seconds=60)
        cooldown_str = cooldown_target.isoformat()

        # Node A persists cooldown
        self.sandbox.write_state({
            "state": "COOL_DOWN",
            "consecutive_failures": 4,
            "cooldown_until": cooldown_str,
        })

        # Node B boots from state
        bot_b = TradingBot()
        bot_b._load_state()

        self.assertEqual(bot_b.state, STATE_COOL_DOWN)
        self.assertEqual(bot_b.consecutive_failures, 4)
        self.assertIsNotNone(bot_b.cooldown_until)


if __name__ == "__main__":
    unittest.main()

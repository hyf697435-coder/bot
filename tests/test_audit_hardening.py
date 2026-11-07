import sys
import os
import math
import time
import unittest
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, r"C:\Users\Administrator\Desktop\v1\a")

class TestAuditHardening(unittest.TestCase):
    def test_risk_manager_concurrency_and_cooldown(self):
        from risk_manager import RiskManager, RiskConfig, TradeRecord
        
        cfg = RiskConfig(max_consecutive_losses=3, cooldown_hours_after_streak=12.0)
        rm = RiskManager(config=cfg, initial_equity=1000.0, persistence_file="test_audit_state.json")
        
        # 1. Test TOCTOU position slot reservation
        can_open, msg = rm.can_open_position(10.0, reserve_slot=True, coin="ETH")
        self.assertTrue(can_open, msg)
        
        # Second attempt should be blocked because max_concurrent_positions=1 and slot is reserved
        can_open2, msg2 = rm.can_open_position(10.0, reserve_slot=True, coin="BTC")
        self.assertFalse(can_open2, "Concurrent slot should be blocked by reservation!")
        
        # Release and verify
        rm.release_position_slot("ETH")
        can_open3, msg3 = rm.can_open_position(10.0, reserve_slot=False, coin="BTC")
        self.assertTrue(can_open3, msg3)
        
        # 2. Test consecutive losses and cooldown deadlock freedom
        for i in range(3):
            tr = TradeRecord(
                entry_time=datetime.now(timezone.utc).isoformat(),
                exit_time=datetime.now(timezone.utc).isoformat(),
                coin="ETH",
                direction="LONG",
                entry_price=2000.0,
                exit_price=1900.0,
                size=100.0,
                pnl=-30.0,
                pnl_pct=-0.03,
                pnl_r=-1.0,
                hold_duration_minutes=30,
                exit_reason="SL",
                entry_equity=1000.0
            )
            rm.record_trade(tr)
            
        self.assertIsNotNone(rm.cooldown_until, "Cooldown until must be set after 3 losses!")
        can_open_cd, cd_msg = rm.can_open_position(10.0)
        self.assertFalse(can_open_cd, "Should be blocked by cooldown!")
        
        # Test pnl == 0 does not clear cooldown_until or cause deadlock
        tr_flat = TradeRecord(
            entry_time=datetime.now(timezone.utc).isoformat(),
            exit_time=datetime.now(timezone.utc).isoformat(),
            coin="ETH",
            direction="LONG",
            entry_price=2000.0,
            exit_price=2000.0,
            size=100.0,
            pnl=0.0,
            pnl_pct=0.0,
            pnl_r=0.0,
            hold_duration_minutes=30,
            exit_reason="TP",
            entry_equity=1000.0
        )
        rm.record_trade(tr_flat)
        self.assertIsNotNone(rm.cooldown_until, "Flat trade must not clear cooldown_until and cause permanent lock!")
        
        # Clean up test file
        for p in ["test_audit_state.json", "test_audit_state.json.tmp", "equity_history_test_audit_state.json", "equity_history_test_audit_state.json.tmp"]:
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass

    def test_risk_manager_multithread_concurrency(self):
        """测试 10 个并发线程同时申请槽位，只有 1 个能成功，彻底消解 TOCTOU 竞态"""
        from risk_manager import RiskManager, RiskConfig
        cfg = RiskConfig(max_concurrent_positions=1)
        rm = RiskManager(config=cfg, initial_equity=1000.0, persistence_file="test_mt_state.json")
        
        results = []
        def worker(coin_name):
            ok, _ = rm.can_open_position(10.0, coin=coin_name, reserve_slot=True)
            if ok:
                results.append(coin_name)
        
        threads = [threading.Thread(target=worker, args=(f"COIN_{i}",)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        
        self.assertEqual(len(results), 1, f"Only 1 slot should be granted among 10 threads, but got {len(results)}: {results}")
        
        for p in ["test_mt_state.json", "test_mt_state.json.tmp", "equity_history_test_mt_state.json"]:
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass

    def test_strategy_position_safety(self):
        from strategy import Position, update_position_fsm
        
        # Test 1: initial_risk_r = 0 / NaN does not blow up profit_r
        pos = Position(
            entry_price=2000.0,
            direction="LONG",
            sz_notional=1000.0,
            tp_price=2100.0,
            sl_price=2000.0, # entry == sl
            initial_risk_r=0.0,
            peak_price=2000.0,
            opened_at=datetime.now(timezone.utc)
        )
        self.assertGreater(pos.initial_risk_r, 0.0)
        self.assertIsInstance(pos.opened_at, float)
        
        r = pos.profit_r(2050.0)
        self.assertTrue(math.isfinite(r))
        
        # Test 2: FSM with current_atr = 0 or NaN
        action = update_position_fsm(pos, current_price=1950.0, current_atr=0.0)
        self.assertIn(action, ["HOLD", "CLOSE_SL"])

    def test_exchange_tighten_stop_ratchet_and_penetration(self):
        from exchange import HyperliquidExecutor
        
        executor = HyperliquidExecutor()
        
        # Mock dependencies
        executor.current_position = MagicMock(return_value=(1.0, 2000.0))
        executor.mark_price = MagicMock(return_value=2050.0)
        executor._place_trigger = MagicMock(return_value=12345)
        executor._cancel_order = MagicMock()
        executor._ai_sl_price = 1980.0
        executor._ai_sl_oid = 9999
        executor._last_tighten_time = 0.0
        
        # Test 1: Attempt to loosen stop (from 1980 down to 1950 for LONG) -> should be rejected by ratchet
        with patch.object(HyperliquidExecutor, 'ready', True):
            res = executor.tighten_stop(1950.0, min_interval_seconds=0.0)
            self.assertIsNone(res, "Loosening stop should be rejected!")
            
            # Test 2: Attempt to move stop past mark price (2060 >= mark 2050) -> should be rejected by penetration guard
            res_penetrate = executor.tighten_stop(2060.0, min_interval_seconds=0.0)
            self.assertIsNone(res_penetrate, "Stop higher than current mark price should be rejected!")
            
            # Test 3: Valid ratchet tightening (1990 > 1980 and < mark 2050) -> should succeed
            res_valid = executor.tighten_stop(1990.0, min_interval_seconds=0.0)
            self.assertIsNotNone(res_valid, "Valid tighten should succeed")
            self.assertEqual(executor._ai_sl_price, 1990.0)
            self.assertEqual(executor._ai_sl_oid, 12345)

    def test_multi_coin_router_ranking(self):
        from multi_coin_router import CoinSignal, MultiCoinRouter
        
        router = MultiCoinRouter()
        sig1 = CoinSignal(coin="ETH", score=80.0, direction="LONG", price=2000.0, tp=2100.0, sl=1950.0, trs=0.2)
        sig2 = CoinSignal(coin="BTC", score=85.0, direction="LONG", price=60000.0, tp=62000.0, sl=59000.0, trs=0.3)
        
        best = router.select_best_coin([sig1, sig2])
        self.assertIsNotNone(best)
        self.assertEqual(best.coin, "BTC")

if __name__ == '__main__':
    unittest.main()

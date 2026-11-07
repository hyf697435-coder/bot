import unittest
import math
from unittest.mock import MagicMock, patch

from strategy import Position, update_position_fsm
from exchange import HyperliquidExecutor, ExecutionError
from multi_coin_router import MultiCoinRouter, CoinSignal

class TestDeduplicatedBugsFixes(unittest.TestCase):
    def test_update_position_fsm_anti_penetration_long(self):
        # 现价已经跌破保本价时，做多保本不应把 SL 抬到现价上方
        pos = Position(
            entry_price=100.0,
            direction="LONG",
            sz_notional=1000.0,
            tp_price=110.0,
            sl_price=95.0,
            initial_risk_r=5.0,
            peak_price=106.0,
            bars_held=5,
            is_breakeven_set=False,
        )
        # profit_r: 100.1 - 100.0 = 0.1 / 5.0 = 0.02R < 1.0R
        # 若峰值曾达到 106.0 (1.2R)，但现价回落至 100.1 (be=100.15 > 100.1)
        res = update_position_fsm(pos, current_price=100.1, current_atr=2.0)
        # be = 100.0 * 1.0015 = 100.15 > 100.1，不应将 sl 设为 100.15 导致现价被击穿
        self.assertLess(pos.sl_price, 100.1)

    def test_update_position_fsm_anti_penetration_short(self):
        # 现价已经反弹高于保本价时，做空保本不应把 SL 压到现价下方
        pos = Position(
            entry_price=100.0,
            direction="SHORT",
            sz_notional=1000.0,
            tp_price=90.0,
            sl_price=105.0,
            initial_risk_r=5.0,
            peak_price=94.0,
            bars_held=5,
            is_breakeven_set=False,
        )
        # be = 100.0 * (1 - 0.0015) = 99.85. 若现价反弹到 99.9 (> 99.85)
        res = update_position_fsm(pos, current_price=99.9, current_atr=2.0)
        self.assertGreater(pos.sl_price, 99.9)

    def test_exchange_place_protective_stops_atomic(self):
        # 模拟 _place_trigger 异常，确保 _ai_sl_price 不会脏写残留
        ex = HyperliquidExecutor.__new__(HyperliquidExecutor)
        ex._exchange = MagicMock()
        ex._info = MagicMock()
        ex._position_size = 1.0
        ex._position_direction = "LONG"
        ex._ai_sl_price = None
        ex._ai_sl_oid = None
        ex._hard_sl_price = None
        ex._hard_sl_oid = None
        ex._tp_price = None
        ex._tp_oid = None

        def mock_place(kind, price):
            raise ExecutionError("网络挂单超时")

        ex._place_trigger = mock_place
        with self.assertRaises(ExecutionError):
            ex._place_protective_stops(final_sl=95.0, hard_sl=90.0, take_profit=110.0)

        # 确保原子性：失败后 _ai_sl_price 必须依然为 None
        self.assertIsNone(ex._ai_sl_price)
        self.assertIsNone(ex._ai_sl_oid)

    def test_exchange_refresh_hard_stop_ratchet(self):
        # 模拟 refresh_hard_stop 的单向棘轮与持仓存在性检查
        ex = HyperliquidExecutor.__new__(HyperliquidExecutor)
        ex._exchange = MagicMock()
        ex._info = MagicMock()
        ex._position_size = 1.0
        ex._position_direction = "LONG"
        ex._hard_sl_price = 92.0
        ex._hard_sl_oid = "oid_1"
        ex._ai_sl_price = 95.0
        ex.coin = "ETH"

        # 1. 持仓归零时直接跳过
        ex.current_position = MagicMock(return_value=(0.0, 0.0))
        res = ex.refresh_hard_stop(93.0)
        self.assertIsNone(res)

        # 2. 持仓存在，做多企图放宽 (91.0 < 92.0)
        ex.current_position = MagicMock(return_value=(1.0, 100.0))
        ex.mark_price = MagicMock(return_value=100.0)
        res = ex.refresh_hard_stop(91.0)
        self.assertEqual(res, 92.0)  # 保持旧硬止损，拒绝放宽

if __name__ == "__main__":
    unittest.main()

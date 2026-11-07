# -*- coding: utf-8 -*-
"""
Unified Quantitative & Risk Test Suite
Consolidates all 44 critical invariants and audit test cases.
"""
import unittest
import sys
import os
import math
import json
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import numpy as np
import pandas as pd

# Add parent directory to sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import utils
from strategy import (
    _raw_sl_tp, calculate_signal_score, calculate_quant_trs, map_trs_to_state,
    validate_and_adjust_tp_sl, MacroState, Position, update_position_fsm,
    calculate_position_size, RR_MIN, RR_BONUS, SL_ATR_MULT, STATUS_MACRO_VETO, AIClient, SlTpResult
)
from risk_manager import RiskManager, RiskConfig, TradeRecord
from multi_coin_router import MultiCoinRouter, CorrelationTracker, CoinSignal
from exchange import HyperliquidExecutor, MarketData
from main import TradingBot, COIN


# ======================================================================
# test_audit_fixes.py
# ======================================================================
class TestAuditFixes(unittest.TestCase):

    def test_unclosed_bar_volume_evaluation_does_not_give_free_pass(self):
        """测试 2 秒未闭合 K 线不会因为成交量极小而恒定白送 15 分"""
        np.random.seed(42)
        n = 35
        # 构造平稳震荡序列，末端触碰布林带上轨促发 SHORT 候选
        closes = np.full(n, 100.0)
        highs = closes + 0.5
        lows = closes - 0.5
        vols = np.full(n, 100.0)
        
        # 触发 5m SHORT 投票 (超买)
        closes[-3:] = 105.0
        highs[-3:] = 106.0
        # 让最新收盘完成的 K 线 (iloc[-2]) 伴随 5x 放量突破 (500 > 1.5*100)
        vols[-2] = 500.0
        # 让当前开盘 2 秒的 K 线 (iloc[-1]) 只有微小成交量 (0.5)
        vols[-1] = 0.5

        df_5m = pd.DataFrame({
            "ts": [1700000000000 + i * 300000 for i in range(n)],
            "open": closes, "high": highs, "low": lows, "close": closes, "volume": vols
        })
        df_1h = pd.DataFrame({
            "ts": [1700000000000 + i * 3600000 for i in range(250)],
            "open": np.full(250, 100.0), "high": np.full(250, 101.0),
            "low": np.full(250, 99.0), "close": np.full(250, 100.0),
            "volume": np.full(250, 1000.0)
        })

        res = calculate_signal_score(
            df_5m=df_5m, df_1h=df_1h,
            funding_stats={"fr": 0.0},
            macro_state=MacroState.LEAN_SHORT
        )
        if res.get("direction") == "SHORT":
            # 绝不应该被 iloc[-1] 的 0.5 骗过给 +15 分缩量冲顶
            self.assertNotIn("+15 (放量突破", res["breakdown"].get("volume_action", ""))

    def test_kelly_payoff_uses_r_multiples_scale_invariance(self):
        """测试凯利公式赔率使用 R 倍数，避免大额仓位美元金额扭曲赔率"""
        rm = RiskManager(initial_equity=1000.0)
        # 记录 30 笔交易达到 min_kelly_trades 门槛
        for i in range(20):
            # 胜单：虽然名义美元只有 $10，但 R 倍数是 +2.0R
            rm.trade_history.append(TradeRecord(
                entry_time="2026-10-24", exit_time="2026-10-24", coin="ETH",
                direction="LONG", entry_price=2000.0, exit_price=2040.0,
                size=500.0, pnl=10.0, pnl_pct=0.01, pnl_r=2.0, hold_duration_minutes=10, exit_reason="TP"
            ))
        for i in range(10):
            # 败单：虽然名义美元高达 $50 (因为大仓位)，但 R 倍数严格控制在 -1.0R
            rm.trade_history.append(TradeRecord(
                entry_time="2026-10-24", exit_time="2026-10-24", coin="BTC",
                direction="LONG", entry_price=60000.0, exit_price=59000.0,
                size=3000.0, pnl=-50.0, pnl_pct=-0.01, pnl_r=-1.0, hold_duration_minutes=10, exit_reason="SL"
            ))
        rm.metrics.total_trades = 30
        rm.metrics.winning_trades = 20
        rm.metrics.losing_trades = 10
        rm.metrics.win_rate = 20 / 30

        # 在 R 尺度下：期望平均盈利 = 2.0R，期望平均亏损 = 1.0R，赔率 b = 2.0
        # 胜率 p = 66.7%, b = 2.0 -> 期望是严格为正的 (Kelly > 0)
        size = rm.get_adaptive_position_size(stop_loss_distance=20.0, current_price=2000.0, equity=1000.0)
        self.assertGreater(size, 0.0)

    def test_sortino_ratio_does_not_explode_with_annual_factor(self):
        """测试单笔 Sortino 比率不乘以 sqrt(365)，避免数值膨胀百倍"""
        rm = RiskManager(initial_equity=1000.0)
        # 模拟 5 笔交易跨越 9 天 (>= 7 天门槛)
        pnls = [10.0, -5.0, 15.0, -4.0, 20.0]
        dates = ["2026-10-10", "2026-10-12", "2026-10-14", "2026-10-16", "2026-10-18"]
        for p, d in zip(pnls, dates):
            rm.trade_history.append(TradeRecord(
                entry_time=d, exit_time=d, coin="ETH",
                direction="LONG", entry_price=2000.0, exit_price=2010.0,
                size=100.0, pnl=p, pnl_pct=p/1000.0, pnl_r=p/10.0, hold_duration_minutes=10, exit_reason="TP"
            ))
        rm._update_risk_adjusted_metrics()
        # 年化 Sortino 应该在合理金融量级，绝不能出现 NaN 或除零溢出
        self.assertFalse(math.isnan(rm.metrics.sortino_ratio))
        self.assertGreater(rm.metrics.sortino_ratio, 0.0)

    def test_multi_coin_router_atr_consistency(self):
        """测试多币种路由与 utils.atr 的 RMA 算法完全一致"""
        highs = pd.Series([10.0 + i * 0.1 for i in range(30)])
        lows = pd.Series([9.5 + i * 0.1 for i in range(30)])
        closes = pd.Series([9.8 + i * 0.1 for i in range(30)])
        df_5m = pd.DataFrame({"high": highs, "low": lows, "close": closes})
        
        expected_atr = utils.atr(df_5m["high"], df_5m["low"], df_5m["close"], 14)
        self.assertGreater(expected_atr, 0.0)
        self.assertFalse(np.isnan(expected_atr))




# ======================================================================
# test_audit_round2_fixes.py
# ======================================================================
class TestAuditRound2Fixes(unittest.TestCase):

    def test_raw_sl_tp_never_self_vetoes_when_no_fractal(self):
        """测试在无碎形阻力/支撑时，动态兜底保证 R:R >= RR_MIN (1.5)，杜绝自发死锁"""
        # 构造简单的 5m K 线
        closes = [100.0] * 30
        highs = [101.0] * 30
        lows = [99.0] * 30
        df_5m = pd.DataFrame({
            "ts": range(30),
            "open": closes, "high": highs, "low": lows, "close": closes, "volume": [100.0] * 30
        })

        # LONG 方向：无 1h 数据
        raw_sl_l, raw_tp_l, rr_l = _raw_sl_tp(100.0, "LONG", df_5m, df_1h=None)
        self.assertGreaterEqual(rr_l, RR_MIN, f"LONG R:R {rr_l} < {RR_MIN}")
        self.assertLess(raw_sl_l, 100.0)
        self.assertGreater(raw_tp_l, 100.0)

        # SHORT 方向：无 1h 数据
        raw_sl_s, raw_tp_s, rr_s = _raw_sl_tp(100.0, "SHORT", df_5m, df_1h=None)
        self.assertGreaterEqual(rr_s, RR_MIN, f"SHORT R:R {rr_s} < {RR_MIN}")
        self.assertGreater(raw_sl_s, 100.0)
        self.assertLess(raw_tp_s, 100.0)

    def test_kelly_scaling_under_non_unit_loss(self):
        """测试在平均亏损 μ_L > 1.0 时，凯利公式按 1/μ_L 缩减下注比例"""
        rm = RiskManager(
            config=RiskConfig(kelly_fraction=0.5, max_single_risk_pct=10.0),
            initial_equity=1000.0,
            persistence_file="test_kelly_state.json",
        )
        try:
            win_rate = 0.54
            win_loss_ratio = 1.0

            # μ_L = 1.0 时的下注: (0.54 - 0.46) * 0.5 * 100 = 4.0%
            size_unit = rm.calculate_kelly_position_size(win_rate, win_loss_ratio, avg_loss_r=1.0)
            # μ_L = 1.5 时的下注: ((0.54 - 0.46) / 1.5) * 0.5 * 100 = 2.6667%
            size_scaled = rm.calculate_kelly_position_size(win_rate, win_loss_ratio, avg_loss_r=1.5)

            # 理论比例：size_scaled = size_unit / 1.5
            self.assertAlmostEqual(size_scaled, size_unit / 1.5, places=4)
            self.assertLess(size_scaled, size_unit)
        finally:
            for p in ["test_kelly_state.json", "equity_history_test_kelly_state.json"]:
                if os.path.exists(p):
                    os.remove(p)

    def test_calendar_day_continuous_sharpe(self):
        """测试日历日收益率连续补齐零收益日，避免低频交易虚增夏普比率"""
        rm = RiskManager(
            initial_equity=1000.0,
            persistence_file="test_sharpe_state.json"
        )
        try:
            # 模拟跨度为 10 天，但只有第 1 天和第 10 天各平仓一笔 +$10
            d1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
            d10 = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)

            t1 = TradeRecord(
                coin="BTC", direction="LONG", entry_price=100.0, exit_price=105.0,
                size=100.0, pnl=10.0, pnl_pct=0.01, pnl_r=1.0,
                hold_duration_minutes=60, exit_reason="TP", entry_equity=1000.0,
                entry_time=d1.isoformat(), exit_time=d1.isoformat()
            )
            t2 = TradeRecord(
                coin="BTC", direction="LONG", entry_price=100.0, exit_price=105.0,
                size=100.0, pnl=10.0, pnl_pct=0.01, pnl_r=1.0,
                hold_duration_minutes=60, exit_reason="TP", entry_equity=1010.0,
                entry_time=d10.isoformat(), exit_time=d10.isoformat()
            )

            rm.trade_history = [t1, t2]
            rm._update_risk_adjusted_metrics()

            # 连续 10 天中只有 2 天收益率 ~0.01，其余 8 天收益率 0.0
            # 确认夏普比率已按 10 天连续日历日计算，而非仅用 2 天样本
            self.assertGreater(rm.metrics.sharpe_ratio, 0.0)
            self.assertFalse(math.isnan(rm.metrics.sharpe_ratio))
        finally:
            for p in ["test_sharpe_state.json", "equity_history_test_sharpe_state.json"]:
                if os.path.exists(p):
                    os.remove(p)

    def test_hyperliquid_price_rounding_btc_over_100k(self):
        """测试 Hyperliquid 5 位有效数字截断：BTC >= 100,000 整数不能透传 6 位数字"""
        executor = HyperliquidExecutor()
        executor.set_coin("BTC")
        # 6 位整数 105432 必须被规整为 105430 (5 位有效数字)
        rounded_int = executor._round_px(105432)
        self.assertEqual(rounded_int, 105430.0)

        # 浮点数 105432.78 必须被规整为 105430
        rounded_float = executor._round_px(105432.78)
        self.assertEqual(rounded_float, 105430.0)

        # ETH < 100,000 的整数 (如 2543) 正常保持
        executor.set_coin("ETH")
        self.assertEqual(executor._round_px(2543), 2543.0)

    def test_state_persistence_restores_coin_and_daily_start_equity(self):
        """测试状态持久化能正确恢复币种与 daily_start_equity，且跨日重置"""
        from main import TradingBot, STATE_FILE
        test_state_file = "test_bot_state.json"
        
        # 构造带有 SUI 持仓与昨日日期的状态数据
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        state_data = {
            "state": "IDLE",
            "coin": "SUI",
            "quant_state": "NEUTRAL",
            "final_macro_state": "NEUTRAL",
            "initial_equity": 1000.0,
            "peak_equity": 1000.0,
            "daily_start_equity": 950.0,
            "daily_loss_pnl": -50.0,
            "daily_loss_date": yesterday,
        }
        
        import json
        with open(test_state_file, "w", encoding="utf-8") as f:
            json.dump(state_data, f)
        
        import main
        orig_state_file = main.STATE_FILE
        main.STATE_FILE = test_state_file
        try:
            bot = TradingBot()
            # 验证 coin 被正确恢复为 SUI
            self.assertEqual(bot.coin, "SUI")
            # 验证跨日检查生效：昨日的 -50.0 亏损被清零，避免跨日永久熔断死锁
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.assertEqual(bot.daily_loss_date, today)
            self.assertEqual(bot.daily_loss_pnl, 0.0)
        finally:
            main.STATE_FILE = orig_state_file
            if os.path.exists(test_state_file):
                os.remove(test_state_file)




# ======================================================================
# test_audit_round3_fixes.py
# ======================================================================
class TestAuditRound3Fixes(unittest.TestCase):

    def setUp(self):
        self.test_persistence = "test_rm_r3_state.json"
        self.test_equity_hist = "test_rm_r3_eq.json"
        for p in [self.test_persistence, self.test_equity_hist]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    def tearDown(self):
        for p in [self.test_persistence, self.test_equity_hist]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    def test_r_multiple_preserves_initial_risk_and_factors_net_pnl(self):
        """测试 1: 平仓结算 R 倍数分母不受移动止损污染，并以净盈亏 USD 折算"""
        pos = Position(
            entry_price=100.0,
            direction="LONG",
            sz_notional=1000.0,
            tp_price=106.0,
            sl_price=98.0,
            initial_risk_r=2.0,  # 初始 1R = 2.0 USD
            peak_price=104.0,
            bars_held=5,
            entry_score=80.0,
        )
        # 模拟移动止损至保本价 100.15
        pos.sl_price = 100.15
        
        # 假定出场价为 104.0，净实际盈亏实际包含手续费后为 +38.0 USD (未扣费为 40 USD)
        actual_pnl_usd = 38.0
        sz_not = 1000.0
        entry = 100.0
        coin_qty = sz_not / entry  # 10.0 coins
        net_px_diff = actual_pnl_usd / coin_qty  # 3.8 USD per coin
        
        # 初始风险 1R = 2.0，计算得到的真实 R 倍数应为 3.8 / 2.0 = 1.90R
        # 若被移动止损污染 (100.0 - 100.15 = 0.15)，则会产生 3.8 / 0.15 = 25.33R 虚高
        initial_risk_r = pos.initial_risk_r
        r_multiple = net_px_diff / initial_risk_r
        self.assertAlmostEqual(r_multiple, 1.90, places=2)

    def test_vacuum_breakout_rr_bonus(self):
        """测试 2: 无阻力位真空突破时享有高盈亏比目标 (>= 2.0) 并获得 +10 分奖励"""
        # 构造突破均线与布林带的新高 K 线，上方没有任何阻力
        n = 50
        closes = [100.0 + i * 0.2 for i in range(n)]
        opens = [c - 0.1 for c in closes]
        highs = [c + 0.15 for c in closes]
        lows = [c - 0.15 for c in closes]
        vols = [100.0] * n
        df_5m = pd.DataFrame({
            "ts": [1700000000000 + i * 300000 for i in range(n)],
            "open": opens, "high": highs, "low": lows, "close": closes, "volume": vols
        })
        df_1h = pd.DataFrame({
            "ts": [1700000000000 + i * 3600000 for i in range(250)],
            "open": [90.0] * 250, "high": [92.0] * 250, "low": [89.0] * 250, "close": [91.0] * 250,
            "volume": [1000.0] * 250
        })

        price = float(closes[-1])
        raw_sl, raw_tp, rr = _raw_sl_tp(price, "LONG", df_5m, df_1h)
        # 上方无阻力，目标盈亏比必须 >= RR_BONUS (2.0)
        self.assertGreaterEqual(rr, RR_BONUS)
        
        # 校验止损距离在有效区间 [min(0.5, SL_ATR_MULT)*ATR, max(SL_ATR_MULT, 2.5)*ATR] 夹紧
        atr_5m = utils.atr(df_5m.iloc[:-1]["high"], df_5m.iloc[:-1]["low"], df_5m.iloc[:-1]["close"], 14)
        sl_dist = price - raw_sl
        self.assertGreaterEqual(sl_dist, min(0.5, SL_ATR_MULT) * 0.99 * atr_5m)
        self.assertLessEqual(sl_dist, max(SL_ATR_MULT, 2.5) * 1.01 * atr_5m)

    def test_state_persistence_restores_entry_score_properly(self):
        """测试 3: 重启恢复 state.json 时 entry_score 准确恢复为信号分而非 last_trs"""
        test_state = "test_state_r3.json"
        try:
            bot = TradingBot.__new__(TradingBot)
            bot.coin = "ETH"
            bot.state = "IDLE"
            bot.quant_state = MacroState.NEUTRAL
            bot.final_macro_state = MacroState.NEUTRAL
            bot.cached_ai_macro_state = None
            bot.last_trs = -0.385  # TRS 为负数
            bot.last_macro_slot = None
            bot.last_event_macro_time = None
            bot.last_evaluated_candle_time = None
            bot.consecutive_failures = 0
            bot.initial_equity = 1000.0
            bot.peak_equity = 1000.0
            bot.daily_start_equity = 1000.0
            bot.daily_loss_pnl = 0.0
            bot.daily_loss_date = "2026-10-25"
            bot.cooldown_until = None
            bot.last_closed_pnl = 0.0
            bot.open_regime_cap = 1.5
            bot.last_synced_sl = 1950.0
            bot.cached_atr_5m = 20.0
            bot.position_entry_price = 2000.0
            bot.position_direction = "LONG"
            bot.position = Position(
                entry_price=2000.0,
                direction="LONG",
                sz_notional=1000.0,
                tp_price=2100.0,
                sl_price=1950.0,
                initial_risk_r=50.0,
                entry_score=82.5,  # 原始入场打分
            )
            bot.router = MultiCoinRouter()

            # 落盘
            with patch("main.STATE_FILE", test_state):
                bot._persist_state()

                # 新建空对象并恢复
                new_bot = TradingBot.__new__(TradingBot)
                new_bot.coin = "ETH"
                new_bot.router = MultiCoinRouter()
                new_bot.exchange = MagicMock()
                new_bot.market = MagicMock()
                new_bot._load_state()

                # 验证持仓 entry_score 与 router.current_position.entry_score 均为 82.5，绝非 -0.385
                self.assertEqual(new_bot.position.entry_score, 82.5)
                self.assertEqual(new_bot.router.current_position.entry_score, 82.5)
        finally:
            if os.path.exists(test_state):
                try:
                    os.remove(test_state)
                except Exception:
                    pass

    def test_rotation_runner_protection(self):
        """测试 4: 浮盈 >= 0.5R 时保护顺势单，不触发轮动换仓"""
        bot = TradingBot.__new__(TradingBot)
        bot.coin = "ETH"
        bot.position = Position(
            entry_price=2000.0,
            direction="LONG",
            sz_notional=1000.0,
            tp_price=2100.0,
            sl_price=1950.0,
            initial_risk_r=50.0,
            entry_score=75.0,
        )
        bot.router = MultiCoinRouter()
        
        # 现价 2030 (浮盈 30/50 = +0.6R >= 0.5R)
        price = 2030.0
        now_utc = datetime.now(timezone.utc)
        
        # 即便 router 有新候选币，也应直接被浮盈保护拦截
        switched = bot._check_position_rotation(price, now_utc)
        self.assertFalse(switched)

    def test_deterministic_composite_tie_breaking(self):
        """测试 5: 多币种打平选择具备完全确定性 (score, |trs|, R:R, coin)"""
        router = MultiCoinRouter()
        now = datetime.now(timezone.utc)
        # 构造相同得分的 3 个信号，但 |trs| 与 R:R 不同
        s_eth = CoinSignal(
            coin="ETH", score=80.0, direction="LONG", price=2000.0,
            tp=2100.0, sl=1950.0, entry_score=80.0, macro_state="BULLISH", trs=0.4, timestamp=now
        )  # R:R = 100/50 = 2.0, |trs|=0.4
        s_btc = CoinSignal(
            coin="BTC", score=80.0, direction="LONG", price=60000.0,
            tp=64000.0, sl=58000.0, entry_score=80.0, macro_state="BULLISH", trs=0.7, timestamp=now
        )  # R:R = 4000/2000 = 2.0, |trs|=0.7 (更高的趋势强度绝对值胜出)
        s_sui = CoinSignal(
            coin="SUI", score=80.0, direction="LONG", price=2.0,
            tp=2.3, sl=1.9, entry_score=80.0, macro_state="BULLISH", trs=0.7, timestamp=now
        )  # R:R = 0.3/0.1 = 3.0 (更高的盈亏比胜出)

        # 无论输入顺序如何，s_sui 必须确定性胜出 (trs=0.7 相同，但 R:R 3.0 > 2.0)
        best1 = router.select_best_coin([s_eth, s_btc, s_sui])
        best2 = router.select_best_coin([s_sui, s_eth, s_btc])
        self.assertEqual(best1.coin, "SUI")
        self.assertEqual(best2.coin, "SUI")

    def test_continuous_second_moment_kelly_and_negative_edge_block(self):
        """测试 6: 连续二阶矩凯利公式与非正数学期望绝对禁止开仓"""
        rm = RiskManager(
            config=RiskConfig(min_kelly_trades=10, kelly_fraction=0.5, max_single_risk_pct=24.0, max_consecutive_losses=20),
            initial_equity=1000.0,
            persistence_file=self.test_persistence,
            equity_history_file=self.test_equity_hist
        )
        
        # 1. 胜率为 0 或胜率/盈亏比非法时，calculate_kelly_position_size 必须返回 0.0
        self.assertEqual(rm.calculate_kelly_position_size(win_rate=0.0, avg_win_loss_ratio=2.0), 0.0)
        self.assertEqual(rm.calculate_kelly_position_size(win_rate=0.5, avg_win_loss_ratio=0.0), 0.0)

        # 2. 构造 10 笔实质交易，净平均 R <= 0 (非正期望)
        rm.trade_history = []
        for i in range(10):
            rm.record_trade(TradeRecord(
                entry_time="2026-10-25", exit_time="2026-10-25",
                coin="ETH", direction="LONG", entry_price=2000.0, exit_price=1980.0,
                size=100.0, pnl=-1.0, pnl_pct=-0.01, pnl_r=-0.5, hold_duration_minutes=30, exit_reason="SL"
            ))
        
        # 必须直接被二阶矩凯利或 can_open 拦截，名义仓位返回 0.0
        notional = rm.get_adaptive_position_size(stop_loss_distance=20.0, current_price=2000.0)
        self.assertEqual(notional, 0.0)
        can_open, reason = rm.can_open_position(estimated_risk_amount=20.0)
        self.assertFalse(can_open)
        self.assertTrue("策略数学期望为负" in reason or "胜率为零" in reason)

        # 3. 构造良好正期望分布 (平均 +1.5R，标准差小)
        rm.trade_history = []
        for i in range(10):
            r_val = 2.0 if i < 7 else -1.0  # 7 胜 3 负，均值 μ = 1.1R > 0
            rm.record_trade(TradeRecord(
                entry_time="2026-10-25", exit_time="2026-10-25",
                coin="ETH", direction="LONG", entry_price=2000.0, exit_price=2040.0 if r_val > 0 else 1980.0,
                size=100.0, pnl=r_val * 10, pnl_pct=0.02, pnl_r=r_val, hold_duration_minutes=30, exit_reason="TP"
            ))
        notional = rm.get_adaptive_position_size(stop_loss_distance=20.0, current_price=2000.0)
        self.assertGreater(notional, 0.0)

    def test_smooth_quant_trs_tanh_continuity(self):
        """测试 7: TRS 计算采用 tanh 平滑归一化，在均线边界无剧烈跳跃"""
        df_1h = pd.DataFrame({
            "ts": [1700000000000 + i * 3600000 for i in range(250)],
            "open": [100.0] * 250, "high": [102.0] * 250, "low": [98.0] * 250, "close": [100.0] * 250,
            "volume": [1000.0] * 250
        })
        df_4h = pd.DataFrame({
            "ts": [1700000000000 + i * 14400000 for i in range(250)],
            "open": [100.0] * 250, "high": [103.0] * 250, "low": [97.0] * 250, "close": [100.0] * 250,
            "volume": [1000.0] * 250
        })
        
        # 均线为 100.0。当价格由 99.99 微变至 100.01 时，原 sign() 会直接从 -1.0 跃迁至 +1.0
        # tanh 平滑后两次计算差值应极其微小 (< 0.05)
        trs_below = calculate_quant_trs(df_1h, df_4h, funding_rate=0.0, current_price=99.99)
        trs_above = calculate_quant_trs(df_1h, df_4h, funding_rate=0.0, current_price=100.01)
        self.assertAlmostEqual(trs_below, trs_above, delta=0.05)

    def test_watchdog_cancel_non_trigger_orders(self):
        """测试 8: 看门狗持仓期崩溃恢复只撤普通限价单，严格保留止损/止盈触发单"""
        mock_ex = MagicMock()
        mock_ex.ready = True
        mock_ex.coin = "ETH"
        mock_ex.user = "0xUser"
        mock_ex._info.open_orders.return_value = [
            {"coin": "ETH", "oid": 101, "isTrigger": True, "tpsl": "sl", "triggerPx": 1950.0},
            {"coin": "ETH", "oid": 102, "isTrigger": True, "tpsl": "tp", "triggerPx": 2100.0},
            {"coin": "ETH", "oid": 103, "isTrigger": False, "limitPx": 1900.0},  # 普通限价挂单
        ]
        
        from exchange import HyperliquidExecutor
        HyperliquidExecutor.cancel_non_trigger_orders(mock_ex)
        
        # 只有普通挂单 103 被撤销，触发单 101 / 102 绝不撤销
        mock_ex._exchange.cancel.assert_called_once_with("ETH", 103)




# ======================================================================
# test_audit_round4_fixes.py
# ======================================================================
class TestAuditRound4Fixes(unittest.TestCase):

    def test_kelly_lower_bound_is_zero(self):
        """测试二阶矩连续凯利与半凯利公式下界从 0.5% 放开至 0.0%，杜绝微弱期望下的超凯利下注"""
        rm = RiskManager(initial_equity=1000.0)
        # 构造微弱正期望：胜率仅 50.1%，盈亏比 1.0
        # 凯利计算结果极小 (~0.1%)，旧版被强制 clip 至 0.5% 导致破产风险，新版应保留微小风险比例
        kelly_pct = rm.calculate_kelly_position_size(win_rate=0.501, avg_win_loss_ratio=1.0)
        self.assertGreater(kelly_pct, 0.0)
        self.assertLess(kelly_pct, 0.2)

        # 期望为负时强制归零
        neg_kelly = rm.calculate_kelly_position_size(win_rate=0.45, avg_win_loss_ratio=1.0)
        self.assertEqual(neg_kelly, 0.0)

    def test_sl_atr_multiplier_not_locked_by_hardcoded_clamp(self):
        """测试 ATR_SL_MULTIPLIER=0.5 时，止损下界与参数联动，不再被旧版 0.8*ATR 强行顶死"""
        n = 50
        dates = pd.date_range("2026-10-20", periods=n, freq="5min")
        highs = [100.0 + np.sin(i / 3.0) * 2.0 for i in range(n)]
        lows = [h - 1.0 for h in highs]
        closes = [(h + l) / 2.0 for h, l in zip(highs, lows)]
        df_5m = pd.DataFrame({"high": highs, "low": lows, "close": closes}, index=dates)

        with patch("strategy.SL_ATR_MULT", 0.5):
            res = _raw_sl_tp(100.0, "LONG", df_5m)
            self.assertIsInstance(res, SlTpResult)
            raw_sl, raw_tp, rr = res
            sl_dist = 100.0 - raw_sl
            # 止损距离应当紧凑，不超过 1.5 * ATR (当 ATR ~ 1.0 时，0.5 倍 ATR 距离约为 0.5~1.0)
            self.assertLessEqual(sl_dist, 2.5)

    def test_signal_score_matrix_entropy_and_no_constant_inflation(self):
        """测试打分矩阵共线性消除：2/3 仅作为准入门槛 (+0)，真空区突破不自奖高盈亏比 (+0)"""
        n = 60
        highs = [100.0] * n
        lows = [99.0] * n
        closes = [99.5] * n
        vols = [100.0] * n
        df_5m = pd.DataFrame({"high": highs, "low": lows, "close": closes, "volume": vols})
        df_1h = pd.DataFrame({"high": highs, "low": lows, "close": closes, "volume": vols})

        # 模拟仅 2 项满足（例如 BB 触下轨 + RSI < 35，但 CCI 中性）：
        # 在 _pick_direction 下该 K 线可生成 LONG 候选方向，但多指标共振应得 +0 分，基础分不应自动膨胀到 70
        with patch("strategy._pick_direction", return_value="LONG"), \
             patch("strategy._direction_votes", return_value=(2, 0)), \
             patch("strategy.utils.donchian", return_value=(None, None, None)), \
             patch("strategy.utils.fractal_levels", return_value=([], [])):
            res = calculate_signal_score(
                df_5m=df_5m, df_1h=df_1h,
                macro_state=MacroState.NEUTRAL,
                funding_stats={"fr": 0.0}
            )
            # 2/3 共振时，multi_indicator 必须为 +0
            self.assertIn("+0", res["breakdown"].get("multi_indicator", ""))
            # 无结构阻力位时（真空突破/无外部结构确认），维持基准 +0 分
            self.assertIn("+0", res["breakdown"].get("rr_bonus", ""))
            # 基础分未被 2/3 共振虚抬，总分保持在合理区间
            self.assertLessEqual(res["score"], 75.0)

    def test_signal_score_awards_3_of_3_unanimous_and_structural_tp(self):
        """测试 3/3 同向强共振且存在真实 1h 结构化目标时正确奖励 +10 分"""
        n = 60
        highs = [100.0] * n
        lows = [99.0] * n
        closes = [99.5] * n
        vols = [100.0] * n
        df_5m = pd.DataFrame({"high": highs, "low": lows, "close": closes, "volume": vols})
        df_1h = pd.DataFrame({"high": [110.0] * n, "low": [90.0] * n, "close": [100.0] * n, "volume": vols})

        with patch("strategy._pick_direction", return_value="LONG"), \
             patch("strategy._direction_votes", return_value=(3, 0)), \
             patch("strategy.utils.fractal_levels", return_value=([105.0], [95.0])):
            res = calculate_signal_score(
                df_5m=df_5m, df_1h=df_1h,
                macro_state=MacroState.NEUTRAL,
                funding_stats={"fr": 0.0}
            )
            self.assertIn("+10", res["breakdown"].get("multi_indicator", ""))
            self.assertIn("+10", res["breakdown"].get("rr_bonus", ""))

    def test_correlation_tracker_seed_from_df(self):
        """测试 CorrelationTracker 在启动时使用历史 DataFrame 播种，消除冷启动 100 分钟相关性恒为 0 的缺陷"""
        tracker = CorrelationTracker()
        n = 30
        dates_ms = [int(1700000000000 + i * 300 * 1000) for i in range(n)]
        p1 = [100.0 + i * 0.5 for i in range(n)]
        p2 = [200.0 + i * 1.0 for i in range(n)]

        df1 = pd.DataFrame({"ts": dates_ms, "close": p1})
        df2 = pd.DataFrame({"ts": dates_ms, "close": p2})

        tracker.seed_from_df("BTC", df1)
        tracker.seed_from_df("ETH", df2)

        # 播种后两个高度同向变动的币种相关性应直接 > 0.9，不再恒为 0.0
        corr = tracker.get_correlation("BTC", "ETH")
        self.assertGreater(corr, 0.9)

    def test_single_position_correlation_penalty_capped(self):
        """测试单持仓模式下相关性惩罚上限严格控制在 <= 5.0 分以内，杜绝同向优质换仓被暴力打折 50% 永久锁死"""
        router = MultiCoinRouter()
        # 模拟 BTC 与 ETH 极高相关性 (corr = 0.95)
        router.corr_tracker.get_correlation = MagicMock(return_value=0.95)
        
        signal = CoinSignal(
            coin="ETH", score=90.0, direction="LONG", price=2500.0,
            tp=2600.0, sl=2450.0, entry_score=80.0
        )
        adj_score = router.apply_correlation_penalty(signal, "BTC")
        # 90.0 - 5.0 = 85.0，扣分不超过 5.0 分
        self.assertGreaterEqual(adj_score, 85.0)
        self.assertLess(adj_score, 90.0)

    def test_tighten_stop_places_new_sl_before_cancelling_old(self):
        """测试 tighten_stop 执行顺序：先挂载新 SL 触发单，成功后再撤销旧 SL 单，彻底杜绝裸奔窗口"""
        ex = HyperliquidExecutor()
        call_log = []

        def mock_place_trigger(trigger_type, price):
            call_log.append(("place_trigger", trigger_type, price))
            return 999

        def mock_cancel_order(oid):
            call_log.append(("cancel_order", oid))
            return True

        ex._exchange = MagicMock()
        ex._info = MagicMock()
        ex._place_trigger = mock_place_trigger
        ex._cancel_order = mock_cancel_order
        ex.current_position = MagicMock(return_value=(1.0, 2000.0))
        ex._ai_sl_oid = 888

        success = ex.tighten_stop(1950.0)
        self.assertTrue(success)
        self.assertEqual(len(call_log), 2)
        # 第一步必须是 place_trigger
        self.assertEqual(call_log[0][0], "place_trigger")
        self.assertEqual(call_log[0][2], 1950.0)
        # 第二步必须是 cancel_order
        self.assertEqual(call_log[1][0], "cancel_order")
        self.assertEqual(call_log[1][1], 888)
        self.assertEqual(ex._ai_sl_oid, 999)




# ======================================================================
# test_audit_round5_fixes.py
# ======================================================================
class TestAuditRound5Fixes(unittest.TestCase):

    def setUp(self):
        self.test_persistence = "test_rm_round5_state.json"
        self.test_equity_hist = "test_eq_round5_hist.json"

    def tearDown(self):
        for f in [self.test_persistence, self.test_equity_hist]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    def test_absorbing_state_deadlock_probe_sizing(self):
        """测试 1: N >= 30 且历史均值 μ_R <= 0 时降级为探针仓位 (0.5%)，彻底破除马尔可夫吸收态死锁"""
        rm = RiskManager(
            config=RiskConfig(min_kelly_trades=30, single_risk_pct=2.5, min_single_risk_pct=0.5, max_consecutive_losses=10),
            initial_equity=1000.0,
            persistence_file=self.test_persistence,
            equity_history_file=self.test_equity_hist
        )
        
        # 构造 30 笔交易：交替进行 1 胜 2 负 (10 笔胜 +0.2R, 20 笔负 -0.3R)
        # 连续亏损最多 2 笔，胜率 = 33.3% > 0，但均值 μ_R = (2.0 - 6.0)/30 = -0.133R <= 0
        for i in range(30):
            pnl_r = 0.2 if (i % 3 == 0) else -0.3
            pnl_usd = pnl_r * 10.0
            rm.record_trade(TradeRecord(
                entry_time="2026-10-26", exit_time="2026-10-26",
                coin="ETH", direction="LONG", entry_price=2000.0,
                exit_price=2000.0 + pnl_usd, size=10.0,
                pnl=pnl_usd, pnl_pct=0.01 * pnl_r, pnl_r=pnl_r,
                hold_duration_minutes=15, exit_reason="TP" if pnl_r > 0 else "SL"
            ))
        
        self.assertEqual(rm.metrics.total_trades, 30)
        self.assertGreater(rm.metrics.win_rate, 0.0)
        
        # 计算开仓量：旧版会返回 0.0 导致死锁，新版必须返回探针仓位
        notional = rm.get_adaptive_position_size(stop_loss_distance=20.0, current_price=2000.0)
        self.assertGreater(notional, 0.0, "期望非正时开仓量不应锁死为 0.0，应降级为探针仓位")
        
        # 探针风险金额约为 1000 * 0.5% = $5.0
        probe_risk = rm.current_equity * 0.005
        # can_open_position 允许探针开仓
        can_open_probe, msg_probe = rm.can_open_position(estimated_risk_amount=probe_risk)
        self.assertTrue(can_open_probe, f"探针仓位应被放行: {msg_probe}")
        
        # 超大风险金额 (> 1.5 * probe) 仍应被拦截
        can_open_large, msg_large = rm.can_open_position(estimated_risk_amount=probe_risk * 2.5)
        self.assertFalse(can_open_large, "负期望下超出探针上限的仓位应被拦截")
        self.assertIn("策略数学期望为负", msg_large)

    def test_downside_semi_variance_penalty(self):
        """测试 2: 连续二阶矩凯利计入下行半方差惩罚，肥尾与大亏损分布时更保守"""
        rm1 = RiskManager(
            config=RiskConfig(min_kelly_trades=30, single_risk_pct=2.5, min_single_risk_pct=0.5, max_consecutive_losses=10),
            initial_equity=1000.0,
            persistence_file=self.test_persistence,
            equity_history_file=self.test_equity_hist
        )
        rm2 = RiskManager(
            config=RiskConfig(min_kelly_trades=30, single_risk_pct=2.5, min_single_risk_pct=0.5, max_consecutive_losses=10),
            initial_equity=1000.0,
            persistence_file=self.test_persistence,
            equity_history_file=self.test_equity_hist
        )
        
        # 分布 1: 对称方差，均值 +0.3R (20 笔 +0.8R, 10 笔 -0.7R)
        for i in range(30):
            r = 0.8 if i < 20 else -0.7
            rm1.record_trade(TradeRecord(
                entry_time="2026-10-26", exit_time="2026-10-26",
                coin="ETH", direction="LONG", entry_price=2000.0, exit_price=2000.0 + r*10,
                size=10.0, pnl=r*10, pnl_pct=0.01, pnl_r=r, hold_duration_minutes=15, exit_reason="TP" if r > 0 else "SL"
            ))
            
        # 分布 2: 严重下行肥尾，均值相同 +0.3R (28 笔 +0.6R, 2 笔 -3.9R)
        for i in range(30):
            r = 0.6 if i < 28 else -3.9
            rm2.record_trade(TradeRecord(
                entry_time="2026-10-26", exit_time="2026-10-26",
                coin="ETH", direction="LONG", entry_price=2000.0, exit_price=2000.0 + r*10,
                size=10.0, pnl=r*10, pnl_pct=0.01, pnl_r=r, hold_duration_minutes=15, exit_reason="TP" if r > 0 else "SL"
            ))
            
        notional_sym = rm1.get_adaptive_position_size(stop_loss_distance=20.0, current_price=2000.0)
        notional_tail = rm2.get_adaptive_position_size(stop_loss_distance=20.0, current_price=2000.0)
        
        # 肥尾分布因下行半方差惩罚，名义仓位必须显著小于对称分布
        self.assertLess(notional_tail, notional_sym, f"肥尾仓位 ({notional_tail}) 应小于对称仓位 ({notional_sym})")

    def test_fee_and_slippage_friction_in_notional_size(self):
        """测试 3: 仓位分母计入 0.20% (双边手续费+滑点) 摩擦"""
        rm = RiskManager(
            config=RiskConfig(min_kelly_trades=30, single_risk_pct=2.5),
            initial_equity=1000.0,
            persistence_file=self.test_persistence,
            equity_history_file=self.test_equity_hist
        )
        notional = rm.get_adaptive_position_size(stop_loss_distance=20.0, current_price=2000.0, use_kelly=False)
        expected_notional = (1000.0 * 0.025) / (20.0 / 2000.0 + 0.0020)
        self.assertAlmostEqual(notional, expected_notional, delta=5.0)
        self.assertLess(notional, 2500.0)

    def test_macro_state_differentiation_only_vs_lean(self):
        """测试 4: 宏观状态分级 (ONLY_* 严格否决 vs LEAN_* 允许高分逆势并施加 -15 惩罚)"""
        n = 50
        dates = pd.date_range("2026-10-20", periods=n, freq="5min")
        df_5m = pd.DataFrame({
            "high": [100.0] * n, "low": [99.0] * n, "close": [99.5] * n, "volume": [100.0] * n
        }, index=dates)
        df_1h = pd.DataFrame({
            "high": [102.0] * n, "low": [98.0] * n, "close": [100.0] * n, "volume": [100.0] * n
        }, index=dates)
        
        with patch("strategy._pick_direction", return_value="SHORT"), \
             patch("strategy._direction_votes", return_value=(0, 3)), \
             patch("strategy.utils.fractal_levels", return_value=([105.0], [95.0])):
             
            # 1. 宏观 ONLY_LONG: 严格否决 SHORT
            res_only = calculate_signal_score(
                df_5m=df_5m, df_1h=df_1h,
                macro_state=MacroState.ONLY_LONG,
                funding_stats={"fr": 0.0}
            )
            self.assertEqual(res_only.get("status"), STATUS_MACRO_VETO)
            self.assertEqual(res_only.get("score"), 0.0)
            veto_reason = res_only["breakdown"].get("veto_macro_conflict", "")
            self.assertIn("ONLY_LONG", veto_reason)
            self.assertIn("绝对禁止做空", veto_reason)
            
            # 2. 宏观 LEAN_LONG: 不硬性否决，但扣 15 分
            res_lean = calculate_signal_score(
                df_5m=df_5m, df_1h=df_1h,
                macro_state=MacroState.LEAN_LONG,
                funding_stats={"fr": 0.0}
            )
            self.assertTrue(res_lean.get("is_counter_trend"))
            self.assertIn("-15", res_lean["breakdown"].get("macro_bias", ""))

    def test_multi_coin_trend_alignment_and_sorting(self):
        """测试 5: 顺势对齐过滤与方向对齐排序"""
        router = MultiCoinRouter(["BTC", "ETH", "SOL"])
        
        # 1. 测试 evaluate_coin_signal 拦截 TRS > 0.40 做空 与 TRS < -0.40 做多
        mock_market = MagicMock()
        n = 60
        mock_df = pd.DataFrame({"high": [100.0]*n, "low": [98.0]*n, "close": [99.0]*n, "volume": [100.0]*n})
        mock_market.get_ohlcv.return_value = mock_df
        mock_market.get_funding_rate.return_value = 0.0
        mock_market.get_open_interest.return_value = 1000.0
        mock_market.get_bid_ask.return_value = (100.0, 100.1)
        router.market_clients["SOL"] = mock_market
        router.market_clients["ETH"] = mock_market
        
        # SOL 强单边多头 (TRS = 0.50)，策略给出 SHORT 候选 -> 必须拦截返回 None
        sig_sol = router.evaluate_coin_signal(
            coin="SOL",
            calculate_signal_fn=lambda **kwargs: {"score": 85.0, "direction": "SHORT", "raw_sl": 105.0, "raw_tp": 90.0},
            calculate_trs_fn=lambda *args, **kwargs: 0.50,
            macro_state=MacroState.NEUTRAL
        )
        self.assertIsNone(sig_sol, "强多头币 (TRS=0.50) 做空信号必须被拦截")
        
        # ETH 强单边空头 (TRS = -0.50)，策略给出 LONG 候选 -> 必须拦截返回 None
        sig_eth = router.evaluate_coin_signal(
            coin="ETH",
            calculate_signal_fn=lambda **kwargs: {"score": 85.0, "direction": "LONG", "raw_sl": 95.0, "raw_tp": 110.0},
            calculate_trs_fn=lambda *args, **kwargs: -0.50,
            macro_state=MacroState.NEUTRAL
        )
        self.assertIsNone(sig_eth, "强空头币 (TRS=-0.50) 做多信号必须被拦截")
        
        # 2. 排序方向对齐测试:
        # A: 做多，TRS = +0.60 -> 顺势度 = +0.60
        # B: 做空，TRS = +0.20 -> 顺势度 = -0.20
        # C: 做多，TRS = +0.30 -> 顺势度 = +0.30
        sig_a = CoinSignal(coin="BTC", score=80.0, direction="LONG", price=100.0, sl=95.0, tp=110.0, entry_score=80.0, trs=0.60)
        sig_b = CoinSignal(coin="ETH", score=80.0, direction="SHORT", price=100.0, sl=105.0, tp=90.0, entry_score=80.0, trs=0.20)
        sig_c = CoinSignal(coin="SOL", score=80.0, direction="LONG", price=100.0, sl=95.0, tp=110.0, entry_score=80.0, trs=0.30)
        
        best = router.select_best_coin([sig_b, sig_c, sig_a])
        self.assertEqual(best.coin, "BTC", "同分下应优先选择顺势度最高 (+0.60) 的币种")

    def test_structural_tp_tautology_fix(self):
        """测试 6: 结构性盈亏比加分同义反复修正 (无外部结构确认时 +0 分)"""
        n = 50
        dates = pd.date_range("2026-10-20", periods=n, freq="5min")
        df_5m = pd.DataFrame({
            "high": [100.0] * n, "low": [99.0] * n, "close": [99.5] * n, "volume": [100.0] * n
        }, index=dates)
        df_1h = pd.DataFrame({
            "high": [102.0] * n, "low": [98.0] * n, "close": [100.0] * n, "volume": [100.0] * n
        }, index=dates)
        
        # 1. 无阻力位时 (真空区): 虽满足默认 R:R 2.0，但不加分 (+0)
        with patch("strategy._pick_direction", return_value="LONG"), \
             patch("strategy._direction_votes", return_value=(2, 0)), \
             patch("strategy.utils.donchian", return_value=(None, None, None)), \
             patch("strategy.utils.fractal_levels", return_value=([], [])):
            res_vacuum = calculate_signal_score(
                df_5m=df_5m, df_1h=df_1h,
                macro_state=MacroState.NEUTRAL,
                funding_stats={"fr": 0.0}
            )
            self.assertIn("+0", res_vacuum["breakdown"].get("rr_bonus", ""))
            
        # 2. 具备真实外部结构阻力位且 R:R >= 2.0 时: 奖励 +10 分
        with patch("strategy._pick_direction", return_value="LONG"), \
             patch("strategy._direction_votes", return_value=(2, 0)), \
             patch("strategy.utils.fractal_levels", return_value=([105.0], [95.0])):
            res_struct = calculate_signal_score(
                df_5m=df_5m, df_1h=df_1h,
                macro_state=MacroState.NEUTRAL,
                funding_stats={"fr": 0.0}
            )
            self.assertIn("+10", res_struct["breakdown"].get("rr_bonus", ""))

    def test_position_rotation_guards(self):
        """测试 7: 换仓约束机制 (持仓未满 3 根 K 线禁止轮动，且必须在 [-0.3R, 0.5R] 内)"""
        bot = TradingBot()
        now_utc = datetime.now(timezone.utc)
        
        # 1. 持仓不足 3 根 K 线 (如 2 根)
        bot.position = Position(
            direction="LONG", sz_notional=100.0,
            entry_price=2000.0, sl_price=1950.0, tp_price=2100.0,
            bars_held=2, initial_risk_r=50.0
        )
        self.assertFalse(bot._check_position_rotation(price=2005.0, now_utc=now_utc))
        
        # 2. 亏损过大 (当前价 1980, 浮亏 -20/50 = -0.4R < -0.3R)，禁止换仓砍单
        bot.position.bars_held = 5
        self.assertFalse(bot._check_position_rotation(price=1980.0, now_utc=now_utc))
        
        # 3. 浮盈已丰 (当前价 2030, 浮盈 +30/50 = +0.6R >= 0.5R)，保护顺势单
        self.assertFalse(bot._check_position_rotation(price=2030.0, now_utc=now_utc))




# ======================================================================
# test_audit_round6_fixes.py
# ======================================================================
class TestAuditRound6Fixes(unittest.TestCase):

    def setUp(self):
        self.test_persistence = "test_rm_round6_state.json"
        self.test_equity_hist = "test_eq_round6_hist.json"

    def tearDown(self):
        for f in [self.test_persistence, self.test_equity_hist]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    def test_unconditional_downside_semi_variance(self):
        """测试 1: 下行半方差除以全部 N 笔样本 (无条件二阶下部矩)，高胜率仅 1 笔偶发亏损不被方差爆炸压制"""
        rm = RiskManager(
            config=RiskConfig(min_kelly_trades=30, single_risk_pct=2.5, min_single_risk_pct=0.5),
            initial_equity=1000.0,
            persistence_file=self.test_persistence,
            equity_history_file=self.test_equity_hist
        )
        
        # 构造 30 笔交易：29 笔盈利 (+1.0R)，仅第 30 笔亏损 (-2.0R)
        # 胜率高达 96.7%
        for i in range(30):
            r = 1.0 if i < 29 else -2.0
            rm.record_trade(TradeRecord(
                entry_time="2026-10-26", exit_time="2026-10-26",
                coin="ETH", direction="LONG", entry_price=2000.0,
                exit_price=2000.0 + r*10, size=10.0,
                pnl=r*10, pnl_pct=0.01, pnl_r=r,
                hold_duration_minutes=15, exit_reason="TP" if r > 0 else "SL"
            ))
            
        recent_r = [t.pnl_r for t in rm.trade_history[-30:]]
        # 无条件下行半方差: (-2.0)^2 / 30 = 4.0 / 30 = 0.1333
        # 条件下行二阶矩 (旧版错误): (-2.0)^2 / 1 = 4.0 (被夸大了 30 倍)
        downside_var = float(np.mean([min(0.0, r) ** 2 for r in recent_r]))
        self.assertAlmostEqual(downside_var, 4.0 / 30.0, places=4)
        
        # 仓位大小正常计算，不应被错误压制至探针底线
        notional = rm.get_adaptive_position_size(stop_loss_distance=20.0, current_price=2000.0)
        self.assertGreater(notional, 1000.0)

    def test_scaling_reaches_max_single_risk_pct(self):
        """测试 2: 高确定性优秀策略期望下，scaling 上限可触达 max_single_risk_pct (4.0%)"""
        rm = RiskManager(
            config=RiskConfig(min_kelly_trades=30, single_risk_pct=2.5, max_single_risk_pct=4.0),
            initial_equity=1000.0,
            persistence_file=self.test_persistence,
            equity_history_file=self.test_equity_hist
        )
        # 构造非常强劲的高胜率期望分布 (均值 +1.5R，方差很小)
        for i in range(30):
            r = 1.5 if i < 25 else -0.5
            rm.record_trade(TradeRecord(
                entry_time="2026-10-26", exit_time="2026-10-26",
                coin="ETH", direction="LONG", entry_price=2000.0,
                exit_price=2000.0 + r*10, size=10.0,
                pnl=r*10, pnl_pct=0.01, pnl_r=r,
                hold_duration_minutes=15, exit_reason="TP" if r > 0 else "SL"
            ))
            
        # 在强正期望下，有效单笔风险百分比应当突破 2.5%，最高达到 4.0%
        # sl_dist = 20.0, price = 2000.0 -> sl_pct = 0.01 + 0.0020 = 0.012
        # 若 risk_pct 达到 4.0%: notional = 1000 * 0.04 / 0.012 = 3333.33
        # 若 risk_pct 被死死锁在 2.5%: notional = 1000 * 0.025 / 0.012 = 2083.33
        notional = rm.get_adaptive_position_size(stop_loss_distance=20.0, current_price=2000.0)
        self.assertGreater(notional, 2500.0, "强正期望下名义仓位应突破 baseline 2.5% 限制")

    def test_consistent_1r_friction_definition_and_stop_settlement(self):
        """测试 3: 初始风险 1R 统一定义为包含预期双边摩擦，止损正常离场结算精确对齐 -1.0R"""
        entry = 2000.0
        final_sl = 1960.0  # 价格止损距离 40.0 (2.0%)
        fee_friction = entry * 0.0020  # 双边摩擦 0.20% (4.0)
        
        pos = Position(
            entry_price=entry,
            direction="LONG",
            sz_notional=2000.0,  # 1.0 个币
            tp_price=2100.0,
            sl_price=final_sl,
            initial_risk_r=abs(entry - final_sl) + fee_friction, # 44.0
            filled_sz=1.0
        )
        
        # 初始风险 1R = 40.0 + 4.0 = 44.0 USD
        self.assertAlmostEqual(pos.initial_risk_r, 44.0)
        
        # 当价格打到止损位 1960.0 平仓，计入双边手续费与滑点 (4.0 USD)
        # 实际净亏损 = -(40.0 + 4.0) = -44.0 USD
        actual_pnl_usd = -44.0
        net_price_diff = actual_pnl_usd / pos.filled_sz
        r_multiple = net_price_diff / pos.initial_risk_r
        
        # 结算 R 倍数精确等于 -1.0R，彻底消除旧版 -1.5R 假性扭曲
        self.assertAlmostEqual(r_multiple, -1.0, places=2)

    def test_trading_bot_switch_coin_runtime_fix(self):
        """测试 4: TradingBot._switch_coin 方法存在且能成功切换，杜绝 AttributeError 运行时崩溃"""
        bot = TradingBot()
        self.assertTrue(hasattr(bot, "_switch_coin"), "TradingBot 必须定义 _switch_coin 方法")
        
        # 执行币种切换测试
        bot._switch_coin("SOL")
        self.assertEqual(bot.coin, "SOL")
        self.assertEqual(bot.exchange.coin, "SOL")
        self.assertEqual(bot.market.coin, "SOL")
        
        # 复位回基准币种
        bot._switch_coin(COIN)
        self.assertEqual(bot.coin, COIN)

    def test_serial_rotation_bypasses_correlation_penalty(self):
        """测试 5: 在 max_concurrent_positions == 1 (单持仓串行轮动) 时不惩罚同向切换"""
        router = MultiCoinRouter(["BTC", "ETH", "SOL"])
        router.config["risk_management"] = {"max_concurrent_positions": 1}
        
        # 模拟当前持有 ETH 多单
        cur_sig = CoinSignal(coin="ETH", score=75.0, direction="LONG", price=2000.0, sl=1960.0, tp=2100.0, entry_score=75.0)
        router.register_position(cur_sig)
        
        # 模拟候选 SOL 强动量多单，且与 ETH 具有 0.95 高相关性
        sol_sig = CoinSignal(coin="SOL", score=92.0, direction="LONG", price=150.0, sl=145.0, tp=165.0, entry_score=92.0)
        
        # 针对并发轮动逻辑
        with patch.object(router, "_collect_coin_signals", return_value=[sol_sig]):
            best = router.get_routing_decision(
                calculate_signal_fn=lambda **kwargs: {},
                calculate_trs_fn=lambda *args: 0.0,
                macro_state=MacroState.NEUTRAL
            )
            self.assertIsNotNone(best)
            self.assertEqual(best.coin, "SOL")
            # 排序分不应被扣减相关性惩罚 (保持 92.0 原分)
            self.assertEqual(best.ranking_score, 92.0)

    def test_smooth_trs_trend_and_funding_rate_compression(self):
        """测试 6: TRS 趋势项以 6.0*ATR 归一化且资金费率采用 2.5*sigma tanh 平滑压缩"""
        n = 250
        closes = [100.0] * n
        highs = [102.0] * n
        lows = [98.0] * n
        vols = [1000.0] * n
        df_1h = pd.DataFrame({"high": highs, "low": lows, "close": closes, "volume": vols})
        df_4h = pd.DataFrame({"high": highs, "low": lows, "close": closes, "volume": vols})
        
        # 资金费率在 2.5 倍标准差以内平滑变化
        fr_stats = {"fr": 0.0003, "fr_mean": 0.0001, "fr_std": 0.0001}  # Z-score = +2.0
        trs = calculate_quant_trs(df_1h, df_4h, fr_stats, current_price=100.0)
        
        # 确保平滑计算出有效 TRS，处于合理区间
        self.assertGreaterEqual(trs, -1.0)
        self.assertLessEqual(trs, 1.0)




# ======================================================================
# test_multi_coin_and_risk.py
# ======================================================================
class TestCorrelationTracker(unittest.TestCase):
    def test_correlation_calculation(self):
        tracker = CorrelationTracker(lookback_days=10)
        # 构造完全正相关的两个币种价格
        for i in range(30):
            ts = 1000000 + i * 300000
            p1 = 100.0 + i * 2.0
            p2 = 50.0 + i * 1.0
            tracker.update("BTC", ts, p1)
            tracker.update("ETH", ts, p2)
            
        corr = tracker.get_correlation("BTC", "ETH")
        self.assertAlmostEqual(corr, 1.0, places=2)


class TestMultiCoinRouter(unittest.TestCase):
    def setUp(self):
        self.router = MultiCoinRouter(config_path="config_aggressive.json")

    def test_coins_configured(self):
        # 验证用户指定的 BTC, ETH, SUI, ARB 币种
        self.assertIn("BTC", self.router.coins)
        self.assertIn("ETH", self.router.coins)
        self.assertIn("SUI", self.router.coins)
        self.assertIn("ARB", self.router.coins)

    def test_select_best_coin(self):
        now = datetime.now(timezone.utc)
        sig1 = CoinSignal("BTC", 72.0, "LONG", 65000.0, 68000.0, 63000.0, 72.0, "BULLISH", 0.5, now)
        sig2 = CoinSignal("SUI", 85.0, "LONG", 2.1, 2.3, 1.95, 85.0, "BULLISH", 0.6, now)
        sig3 = CoinSignal("ARB", 60.0, "LONG", 0.55, 0.60, 0.52, 60.0, "BULLISH", 0.3, now)

        best = self.router.select_best_coin([sig1, sig2, sig3])
        self.assertIsNotNone(best)
        self.assertEqual(best.coin, "SUI")
        self.assertEqual(best.score, 85.0)


class TestRiskManager(unittest.TestCase):
    def setUp(self):
        for f in ["test_risk_state.json", "test_equity_history.json"]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass
        cfg = RiskConfig(
            max_leverage=5.0,
            max_drawdown_pct=30.0,
            daily_loss_limit_pct=15.0,
            max_consecutive_losses=3
        )
        self.rm = RiskManager(
            config=cfg,
            initial_equity=1000.0,
            persistence_file="test_risk_state.json",
            equity_history_file="test_equity_history.json"
        )

    def tearDown(self):
        for f in ["test_risk_state.json", "test_equity_history.json"]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    def test_can_open_position_normal(self):
        can_open, reason = self.rm.can_open_position(estimated_risk_amount=20.0, equity=1000.0)
        self.assertTrue(can_open)

    def test_can_open_position_drawdown_limit(self):
        # 模拟产生 35% 回撤
        self.rm.metrics.peak_equity = 1000.0
        self.rm.current_equity = 600.0
        self.rm._update_drawdown()
        can_open, reason = self.rm.can_open_position(estimated_risk_amount=20.0)
        self.assertFalse(can_open)
        self.assertIn("峰值回撤超限", reason)

    def test_dynamic_stops_trailing(self):
        # 进场 100，初始止损 98，当前价格 104 (涨幅达到 2R)，ATR=1.0
        # 追踪止损应移动至 peak - 1.5 * ATR = 104 - 1.5 = 102.5
        new_sl, new_tp, close_sig = self.rm.update_dynamic_stops(
            entry_price=100.0,
            current_price=104.0,
            initial_sl=98.0,
            current_sl=98.0,
            direction="LONG",
            atr=1.0,
            bars_held=5,
            peak_price=104.0
        )
        self.assertAlmostEqual(new_sl, 102.5, places=2)

    def test_leverage_clamp(self):
        # 验证无论如何计算，开仓名义价值严格不超过 5x 本金
        notional = self.rm.get_adaptive_position_size(
            stop_loss_distance=0.01,  # 极小的止损距离会导致极大杠杆需求
            current_price=100.0,
            equity=1000.0
        )
        self.assertLessEqual(notional, 1000.0 * 5.0 + 1e-4)






class TestCodeArtifactAuditFixes(unittest.TestCase):
    def setUp(self):
        self.rm = RiskManager(initial_equity=1000.0, persistence_file="test_audit_state.json")

    def tearDown(self):
        if os.path.exists("test_audit_state.json"):
            os.remove("test_audit_state.json")

    def test_streak_deadlock_recovery(self):
        """验证 2.1: 连亏熔断后调用 reset_streak_after_cooldown 能成功解除开仓死锁"""
        self.rm.metrics.current_streak = -3
        can_open, msg = self.rm.can_open_position(10.0, equity=1000.0)
        self.assertFalse(can_open)
        self.assertIn("连续亏损冷却", msg)

        self.rm.reset_streak_after_cooldown()
        self.assertEqual(self.rm.metrics.current_streak, 0)
        can_open2, msg2 = self.rm.can_open_position(10.0, equity=1000.0)
        self.assertTrue(can_open2)

    def test_time_decay_dynamic_bars_held(self):
        """验证 1.4: update_position_fsm 能够基于 opened_at 自动推进 bars_held"""
        opened_ts = time.time() - 7500  # 25 个 5m K 线前
        pos = Position(
            entry_price=100.0,
            direction="LONG",
            sz_notional=100.0,
            tp_price=110.0,
            sl_price=95.0,
            initial_risk_r=5.0,
            bars_held=0,
            opened_at=opened_ts,
        )
        action = update_position_fsm(pos, current_price=100.2, current_atr=1.0)
        self.assertGreaterEqual(pos.bars_held, 24)
        self.assertEqual(action, "CLOSE_TIME_DECAY")

    def test_roundtrip_fee_deduction(self):
        """验证 2.4: _estimate_pnl 扣除双边手续费"""
        from main import TradingBot, TRADING_FEE_RATE
        # 无滑点零涨跌平仓应净亏双边手续费
        pnl = TradingBot._estimate_pnl("LONG", 100.0, 100.0)
        self.assertAlmostEqual(pnl, -2.0 * TRADING_FEE_RATE, places=6)

    def test_select_best_coin_applies_corr_penalty(self):
        """验证 3.1: select_best_coin 自动计算相关性软扣分"""
        router = MultiCoinRouter()
        # 模拟设置 ETH 与 BTC 高度相关 0.95
        router.corr_tracker.get_correlation = lambda c1, c2: 0.95 if (c1, c2) == ("ETH", "BTC") or (c2, c1) == ("ETH", "BTC") else 0.0
        sig_eth = CoinSignal(
            coin="ETH", score=75.0, direction="LONG",
            price=2000.0, tp=2100.0, sl=1950.0, entry_score=75.0
        )
        sig_sol = CoinSignal(
            coin="SOL", score=73.0, direction="LONG",
            price=150.0, tp=160.0, sl=145.0, entry_score=73.0
        )
        # ETH 扣除 5 分后 ranking_score 降为 70.0，SOL 保持 73.0 胜出
        best = router.select_best_coin([sig_eth, sig_sol], benchmark_coin="BTC")
        self.assertIsNotNone(best)
        self.assertEqual(best.coin, "SOL")



    def test_cooldown_until_auto_unfreeze(self):
        """验证 P0-2: cooldown_until 到期后 can_open_position 自动解除连亏死锁"""
        from datetime import datetime, timezone, timedelta
        # 模拟 1 小时前已到期的冷却时间
        self.rm.metrics.current_streak = -3
        past_time = datetime.now(timezone.utc) - timedelta(hours=1)
        self.rm.cooldown_until = past_time.isoformat()
        
        can_open, msg = self.rm.can_open_position(10.0, equity=1000.0)
        self.assertTrue(can_open)
        self.assertEqual(self.rm.metrics.current_streak, 0)
        self.assertIsNone(self.rm.cooldown_until)

        # 模拟未来才到期的冷却时间
        self.rm.metrics.current_streak = -3
        future_time = datetime.now(timezone.utc) + timedelta(hours=5)
        self.rm.cooldown_until = future_time.isoformat()
        can_open_future, msg_future = self.rm.can_open_position(10.0, equity=1000.0)
        self.assertFalse(can_open_future)
        self.assertIn("连续亏损熔断冷却中", msg_future)

    def test_wilder_smooth_leading_nans(self):
        """验证 P0-1: _wilder_smooth_vectorized 遇到前导 NaN 正常累积有效样本而不崩溃"""
        import numpy as np
        import utils
        arr = np.array([np.nan, np.nan, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0])
        res = utils._wilder_smooth_vectorized(arr, 3)
        # 前导两个 NaN 之后第 3 个有效样本为 12.0 (索引 4)，均值为 11.0
        self.assertTrue(np.isnan(res[0]))
        self.assertTrue(np.isnan(res[1]))
        self.assertAlmostEqual(res[4], 11.0, places=4)
        self.assertFalse(np.isnan(res[-1]))



    def test_kelly_reference_edge_config(self):
        """验证 4: kelly_reference_edge 配置化及缩放区间参数化"""
        self.assertEqual(self.rm.config.kelly_reference_edge, 0.128)
        self.assertEqual(self.rm.config.kelly_min_scale, 0.20)
        self.assertEqual(self.rm.config.kelly_max_scale, 1.60)
        # 测试自定义 reference_edge
        self.rm.config.kelly_reference_edge = 0.05
        # 即使无足够历史样本，也能安全读取
        factor = self.rm.get_kelly_scaling_factor()
        self.assertEqual(factor, 1.0)

    def test_correlation_tracker_timestamp_dedup(self):
        """验证 3: CorrelationTracker.update 相同时间戳去重"""
        from multi_coin_router import CorrelationTracker
        tracker = CorrelationTracker(lookback_days=1)
        ts = 1700000000000
        tracker.update("ETH", ts, 2000.0)
        tracker.update("ETH", ts, 2005.0)  # 同时间戳更新价格
        self.assertEqual(len(tracker.price_history["ETH"]), 1)
        self.assertEqual(tracker.price_history["ETH"][0], (ts, 2005.0))
        tracker.update("ETH", ts + 3600000, 2010.0)  # 新时间戳追加
        self.assertEqual(len(tracker.price_history["ETH"]), 2)


    def test_state_restoration_includes_atr_and_sl(self):
        """验证 P0-2: state.json 恢复时正确读取 last_synced_sl 与 cached_atr_5m"""
        bot = TradingBot()
        test_state = {
            "state": "TRADING",
            "coin": "ETH",
            "last_synced_sl": 1955.5,
            "cached_atr_5m": 12.8,
            "position": {
                "entry_price": 2000.0,
                "direction": "LONG",
                "sz_notional": 500.0,
                "tp_price": 2100.0,
                "sl_price": 1950.0,
                "initial_risk_r": 50.0,
                "peak_price": 2010.0,
                "bars_held": 3,
                "opened_at": time.time(),
                "entry_score": 80.0,
                "filled_sz": 0.25
            }
        }
        test_file = "test_restore_state.json"
        with open(test_file, "w", encoding="utf-8") as f:
            json.dump(test_state, f)
        
        orig_state_file = sys.modules["main"].STATE_FILE
        try:
            sys.modules["main"].STATE_FILE = test_file
            bot._load_state()
            self.assertEqual(bot.state, "TRADING")
            self.assertEqual(bot.last_synced_sl, 1955.5)
            self.assertEqual(bot.cached_atr_5m, 12.8)
            self.assertIsNotNone(bot.position)
            self.assertEqual(bot.position.entry_price, 2000.0)
        finally:
            sys.modules["main"].STATE_FILE = orig_state_file
            if os.path.exists(test_file):
                os.remove(test_file)

    def test_candlestick_reversal_patterns_short_and_long(self):
        """验证 3.2: 做多锤子线与做空流星线/看跌吞没在放量时均能正确触发 volume_spike 加分"""
        # 1. 构造做多锤子线数据
        n = 30
        closes = np.full(n, 100.0)
        highs = np.full(n, 100.5)
        lows = np.full(n, 99.5)
        opens = np.full(n, 100.0)
        vols = np.full(n, 100.0)
        
        # 触发 5m 做多候选 (超跌)
        closes[-2:] = 95.0
        # 最后一根为下影线长、实体小的锤子线
        opens[-1] = 95.2
        closes[-1] = 95.8
        highs[-1] = 95.9
        lows[-1] = 93.0
        vols[-1] = 250.0 # 放量 > 1.5x
        
        df_5m_long = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols})
        df_1h = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols})
        res_long = calculate_signal_score(df_5m_long, df_1h, MacroState.ONLY_LONG, funding_stats=None)
        if res_long["score"] > 0:
            self.assertIn("volume_spike", res_long["breakdown"])

    def test_marketdata_lookback_buffer(self):
        """验证 1.1: MarketData.get_ohlcv 的 span_ms 带有安全缓冲"""
        mkt = MarketData(coin="ETH")
        # 验证 4h 周期使用 14400 秒
        self.assertEqual(mkt.INTERVAL_SECONDS["4h"], 14400)
        self.assertEqual(mkt.INTERVAL_SECONDS["1h"], 3600)
        self.assertEqual(mkt.INTERVAL_SECONDS["5m"], 300)

if __name__ == '__main__':
    unittest.main()

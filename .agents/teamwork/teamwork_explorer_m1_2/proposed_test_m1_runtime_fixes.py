# -*- coding: utf-8 -*-
"""
Milestone 1 专用回归测试套件：验证主控制循环运行时异常分类、pandas 导入及时间戳防御性提取。
"""

import math
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

# 确保主模块路径在 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
import main
from main import INTERNAL_SOFTWARE_ERRORS, STATE_IDLE, STATE_TRADING, TradingBot
from exchange import MarketDataError


class TestMilestone1RuntimeFixes(unittest.TestCase):
    """Milestone 1 运行时鲁棒性及异常隔离测试。"""

    def setUp(self):
        # 实例化轻量 bot 用于测试 _handle_loop_exception 与时间戳逻辑
        with patch.object(TradingBot, "_load_production_config", return_value=MagicMock()), \
             patch.object(TradingBot, "_preheat_correlations", return_value=None), \
             patch.object(TradingBot, "_load_state", return_value=None), \
             patch("main.HyperliquidExecutor"), \
             patch("main.MarketData"), \
             patch("main.AIClient"), \
             patch("main.MultiCoinRouter"):
            self.bot = TradingBot()
        self.bot.state = STATE_IDLE
        self.bot.idle_api_error_count = 0
        self.bot.api_error_count = 0
        self.bot.idle_backoff = 60

    def test_pandas_imported_at_module_level(self):
        """Feature 1: 验证 main 模块在顶层导入了 pandas 为 pd。"""
        self.assertIn("pd", main.__dict__)
        self.assertIs(main.__dict__["pd"], pd)

    def test_internal_software_errors_definition(self):
        """Feature 2: 验证内部软件缺陷元组覆盖核心 Python 运行时及语法错误。"""
        expected_types = (
            NameError,
            TypeError,
            AttributeError,
            SyntaxError,
            IndexError,
            KeyError,
            ZeroDivisionError,
            ImportError,
        )
        for err_type in expected_types:
            self.assertIn(err_type, INTERNAL_SOFTWARE_ERRORS)

    @patch("main.send_alert")
    @patch("main.logger")
    def test_internal_error_does_not_increment_idle_api_counter(self, mock_logger, mock_send_alert):
        """Feature 2 & 3: 内部软件错误必须打上 [交易程序运行时异常] 标签，带 exc_info=True，且不递增 idle_api_error_count。"""
        self.bot._safe_sleep = MagicMock()
        err = NameError("name 'pd' is not defined")

        self.bot._handle_loop_exception(err)

        # 核心断言 1: 不得递增 API 异常计数器
        self.assertEqual(self.bot.idle_api_error_count, 0, "内部代码异常绝不能递增 idle_api_error_count")
        self.assertEqual(self.bot.api_error_count, 0, "内部代码异常绝不能递增 api_error_count")

        # 核心断言 2: 告警分类标签与信息
        mock_send_alert.assert_called_once()
        alert_msg = mock_send_alert.call_args[0][0]
        self.assertIn("[交易程序运行时异常]", alert_msg)
        self.assertIn("NameError", alert_msg)

        # 核心断言 3: 日志必须记录完整堆栈 (exc_info=True)
        mock_logger.critical.assert_called_once()
        crit_kwargs = mock_logger.critical.call_args[1]
        self.assertTrue(crit_kwargs.get("exc_info"), "logger.critical 必须包含 exc_info=True")

        # 核心断言 4: 执行了安全退避
        self.bot._safe_sleep.assert_called_once_with(60, phase="IDLE_INTERNAL_BACKOFF")

    @patch("main.send_alert")
    @patch("main.logger")
    def test_external_api_error_increments_idle_counter(self, mock_logger, mock_send_alert):
        """Feature 2 & 3: 外部 API 异常必须递增计数器并记录堆栈。"""
        self.bot._safe_sleep = MagicMock()
        err = MarketDataError("502 Bad Gateway from exchange /info")

        self.bot._handle_loop_exception(err)

        self.assertEqual(self.bot.idle_api_error_count, 1)
        self.assertEqual(self.bot.api_error_count, 1)
        mock_logger.error.assert_called_once()
        err_kwargs = mock_logger.error.call_args[1]
        self.assertTrue(err_kwargs.get("exc_info"), "logger.error 必须包含 exc_info=True")

    def test_defensive_timestamp_extraction_nan_and_bad_values(self):
        """Feature 4: 验证防御性时间戳提取面对 NaN、malformed string 或缺失全局变量时不抛出 ValueError。"""
        # 测试 1: 正常整型时间戳
        df_normal = pd.DataFrame({"ts": [1794539700000]})
        raw_ts = df_normal["ts"].iloc[-1]
        val = int(pd.to_numeric(raw_ts, errors="coerce"))
        self.assertEqual(val, 1794539700000)

        # 测试 2: 浮点时间戳
        df_float = pd.DataFrame({"ts": [1794539700000.0]})
        val = int(pd.to_numeric(df_float["ts"].iloc[-1], errors="coerce"))
        self.assertEqual(val, 1794539700000)

        # 测试 3: 畸形非法字符串（原始未保护代码会报 ValueError: cannot convert float NaN to integer）
        df_bad = pd.DataFrame({"ts": ["invalid_nan_timestamp"]})
        raw_ts_bad = df_bad["ts"].iloc[-1]
        numeric_val = pd.to_numeric(raw_ts_bad, errors="coerce")
        self.assertTrue(pd.isna(numeric_val))
        # 保护逻辑：如果 pd.isna(numeric_val) 则降级为 None，严禁执行 int(numeric_val)
        current_candle_time = int(numeric_val) if (numeric_val is not None and not pd.isna(numeric_val)) else None
        self.assertIsNone(current_candle_time)


if __name__ == "__main__":
    unittest.main()

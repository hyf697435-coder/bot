"""
Hermetic mock fixtures for Hyperliquid Trading Bot E2E test suite.
Provides air-gapped test harnesses for:
- MockMarketData & MockExchange
- MockLLMClient
- MockMailbox
- VirtualClock
- StateSandbox
"""

import os
import json
import time
import math
import tempfile
import shutil
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any
from unittest.mock import MagicMock, patch
import pandas as pd


# ==============================================================================
# 1. Market Data & Exchange Mock
# ==============================================================================

def generate_candles(
    count: int = 120,
    base_price: float = 2250.0,
    timeframe: str = "5m",
    interval_seconds: int = 300,
    start_time_ms: Optional[int] = None,
    trend: float = 0.0,
    volatility: float = 5.0
) -> pd.DataFrame:
    """Generate realistic OHLCV dataframe matching Hyperliquid schema."""
    if start_time_ms is None:
        # End at current time, working backward
        now_ms = int(time.time() * 1000)
        start_time_ms = now_ms - (count * interval_seconds * 1000)

    rows = []
    current_px = base_price
    for i in range(count):
        t = start_time_ms + (i * interval_seconds * 1000)
        open_px = current_px
        change = trend + ((i % 5 - 2) * (volatility * 0.2))
        close_px = max(10.0, open_px + change)
        high_px = max(open_px, close_px) + abs(volatility * 0.5)
        low_px = min(open_px, close_px) - abs(volatility * 0.5)
        volume = 1000.0 + (i * 10.0)

        rows.append({
            "ts": int(t),
            "open": float(round(open_px, 2)),
            "high": float(round(high_px, 2)),
            "low": float(round(low_px, 2)),
            "close": float(round(close_px, 2)),
            "volume": float(round(volume, 2)),
        })
        current_px = close_px

    df = pd.DataFrame(rows)
    return df


class MockMarketData:
    """Hermetic mock for exchange.py MarketData."""

    def __init__(self, coin: str = "ETH", base_price: float = 2250.0):
        self.coin = coin
        self.base_price = base_price
        self.fail_times = 0
        self.fail_exception: Optional[Exception] = None
        self.fail_status_code: Optional[int] = None
        self.retry_after: int = 0
        self.call_count: int = 0
        self.funding_fail_times = 0
        self._funding_cache: Optional[Tuple[Dict[str, float], float]] = None
        self._fr_cache: Optional[Tuple[Dict[str, float], float]] = None
        self._fr_ttl: float = 3600.0
        self.candles_5m = generate_candles(120, base_price, "5m", 300)
        self.candles_1h = generate_candles(250, base_price, "1h", 3600)
        self.candles_4h = generate_candles(250, base_price, "4h", 14400)
        self._ohlcv_cache = {}

    def set_transient_failure(self, times: int, exception: Exception):
        """Configure mock to raise exception for `times` invocations before succeeding."""
        self.fail_times = times
        self.fail_exception = exception

    def get_closed_ohlcv(self, timeframe: str = "5m", lookback_bars: int = 120, use_cache: bool = True) -> Tuple[pd.DataFrame, float]:
        self.call_count += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            if self.fail_exception:
                raise self.fail_exception
            raise RuntimeError("Mock market data transient failure")

        if timeframe == "5m":
            df = self.candles_5m.copy()
        elif timeframe == "1h":
            df = self.candles_1h.copy()
        elif timeframe == "4h":
            df = self.candles_4h.copy()
        else:
            df = generate_candles(lookback_bars, self.base_price, timeframe)

        latest_px = float(df["close"].iloc[-1]) if not df.empty else self.base_price
        return df, latest_px

    def get_ohlcv(self, timeframe: str = "5m", lookback_bars: int = 120, use_cache: bool = True) -> pd.DataFrame:
        df, _ = self.get_closed_ohlcv(timeframe, lookback_bars, use_cache)
        return df

    def mark_price(self) -> float:
        if self.fail_times > 0:
            self.fail_times -= 1
            if self.fail_exception:
                raise self.fail_exception
            raise RuntimeError("Mock mark price failure")
        return self.base_price

    def get_funding_stats(self, window_hours: int = 168, use_cache: bool = True) -> Dict[str, float]:
        if self.funding_fail_times > 0:
            self.funding_fail_times -= 1
            if use_cache and (self._funding_cache or self._fr_cache):
                stats, _ = self._funding_cache or self._fr_cache
                return stats
            raise RuntimeError("Mock funding failure")

        stats = {
            "fr": 0.0000125,
            "fr_mean": 0.0000100,
            "fr_std": 0.0000020,
            "fr_samples": float(window_hours),
        }
        self._funding_cache = (stats, time.monotonic())
        self._fr_cache = (stats, time.monotonic())
        return stats


class MockExchange:
    """Hermetic mock for exchange executor & on-chain state."""

    def __init__(self, initial_equity: float = 1000.0, coin: str = "ETH"):
        self.equity = initial_equity
        self.coin = coin
        self.position: Optional[Dict[str, Any]] = None
        self.trigger_orders: List[Dict[str, Any]] = []
        self.executed_orders: List[Dict[str, Any]] = []
        self.ready: bool = True
        self.slippage: float = 0.002
        self.fail_open: bool = False
        self.fail_open_exception: Optional[Exception] = None

    def account_value(self) -> float:
        return self.equity

    def current_position(self) -> Tuple[Optional[float], Optional[float]]:
        if self.position and self.position.get("size", 0.0) != 0:
            return float(self.position["size"]), float(self.position.get("entry_price", 0.0))
        return 0.0, None

    def open_position_with_stops(
        self,
        coin: str,
        is_buy: bool,
        sz_notional: float,
        limit_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        hard_stop_distance: Optional[float] = None,
    ) -> Optional[dict]:
        if self.fail_open:
            if self.fail_open_exception:
                raise self.fail_open_exception
            raise RuntimeError("Mock open position failure")

        if self.equity <= 0 or not math.isfinite(self.equity):
            from exchange import ExecutionError
            raise ExecutionError(f"账户权益({self.equity})非法，拒绝下单。")

        mark = limit_price or 2250.0
        size = sz_notional / mark
        if not is_buy:
            size = -size

        self.position = {
            "coin": coin,
            "size": size,
            "entry_price": mark,
            "notional": sz_notional,
            "stop_loss": stop_loss_price,
            "take_profit": take_profit_price,
        }

        # Place on-chain protective trigger orders
        if stop_loss_price is not None:
            self.trigger_orders.append({
                "coin": coin,
                "order_type": "STOP_LOSS",
                "price": float(stop_loss_price),
                "is_trigger": True,
                "status": "resting",
            })
        if take_profit_price is not None:
            self.trigger_orders.append({
                "coin": coin,
                "order_type": "TAKE_PROFIT",
                "price": float(take_profit_price),
                "is_trigger": True,
                "status": "resting",
            })

        resp = {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"filled": {"totalSz": str(abs(size)), "avgPx": str(mark)}}]}
            }
        }
        self.executed_orders.append(resp)
        return resp

    def close_all(self) -> Optional[dict]:
        if not self.position:
            return {"status": "ok", "message": "already flat"}
        self.position = None
        # Preserve or clear non-trigger orders
        resp = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {}}]}}}
        return resp

    def clear_position_state(self):
        self.position = None

    def cancel_all_orders(self):
        self.trigger_orders.clear()

    def cancel_non_trigger_orders(self):
        # Keeps trigger orders intact
        self.trigger_orders = [o for o in self.trigger_orders if o.get("is_trigger", False)]

    def tighten_stop(self, new_stop_loss: float, new_take_profit: Optional[float] = None, min_interval_seconds: float = 10.0) -> Optional[dict]:
        if not self.position:
            return None
        # Update existing trigger order
        for o in self.trigger_orders:
            if o.get("order_type") == "STOP_LOSS":
                o["price"] = float(new_stop_loss)
        return {"status": "ok", "new_sl": new_stop_loss}

    def has_active_trigger_order(self, coin: str, order_type: str, price: Optional[float] = None) -> bool:
        for o in self.trigger_orders:
            if o.get("coin") == coin and o.get("order_type") == order_type:
                if price is None or abs(float(o.get("price", 0.0)) - float(price)) < 1e-4:
                    return True
        return False


# ==============================================================================
# 2. LLM Relay Mock
# ==============================================================================

class MockLLMClient:
    """Hermetic mock for third-party LLM relay API (api.ludies.best)."""

    def __init__(self):
        self.macro_state: str = "NEUTRAL"
        self.macro_reason: str = "Balanced quant indicators"
        self.trade_decision: str = "HOLD"
        self.trade_confidence: float = 0.85
        self.simulate_timeout: bool = False
        self.simulate_error_status: Optional[int] = None
        self.call_count: int = 0

    def get_macro_response(self) -> str:
        self.call_count += 1
        if self.simulate_timeout:
            import httpx
            raise httpx.TimeoutException("LLM Relay request timed out after 30s")
        if self.simulate_error_status:
            import httpx
            req = httpx.Request("POST", "https://api.ludies.best/v1/chat/completions")
            raise httpx.HTTPStatusError("Gateway Error", request=req, response=httpx.Response(self.simulate_error_status))

        return json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "suggested_state": self.macro_state,
                        "reason": self.macro_reason,
                        "news_impact_analysis": "None"
                    })
                }
            }]
        })

    def get_trade_response(self) -> str:
        self.call_count += 1
        if self.simulate_timeout:
            import httpx
            raise httpx.TimeoutException("LLM Relay trade request timed out")
        return json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "decision": self.trade_decision,
                        "confidence": self.trade_confidence,
                        "reason": "Technical confluence"
                    })
                }
            }]
        })


# ==============================================================================
# 3. SMTP Mailbox & Alert Recorder Mock
# ==============================================================================

class MockMailbox:
    """Hermetic in-memory recorder for system alerts and emails."""

    def __init__(self):
        self.sent_messages: List[Dict[str, Any]] = []

    def record_alert(self, message: str, subject: str = "[交易机器人] 系统告警", priority: str = "NORMAL"):
        self.sent_messages.append({
            "timestamp": time.time(),
            "subject": subject,
            "body": message,
            "priority": priority,
        })

    @property
    def dispatched_alerts(self) -> List[Dict[str, Any]]:
        return self.sent_messages

    def clear(self):
        self.sent_messages.clear()

    def count_with_tag(self, tag: str) -> int:
        return sum(1 for m in self.sent_messages if tag in m["subject"] or tag in m["body"])

    def has_tag(self, tag: str) -> bool:
        return any(tag in m["subject"] or tag in m["body"] for m in self.sent_messages)


# ==============================================================================
# 4. Virtual Controllable Clock
# ==============================================================================

class VirtualClock:
    """Virtual controllable clock for accelerating 24-hour simulations."""

    def __init__(self, start_timestamp: float = 1763078400.0):  # 2026-11-13 00:00:00 UTC
        self.current_time = float(start_timestamp)
        self.recorded_sleeps: List[float] = []

    def time(self) -> float:
        return self.current_time

    def monotonic(self) -> float:
        return self.current_time

    def advance(self, seconds: float):
        self.current_time += max(0.0, float(seconds))

    def sleep(self, seconds: float):
        duration = max(0.0, float(seconds))
        self.recorded_sleeps.append(duration)
        self.current_time += duration

    def now(self, tz=None):
        return datetime.fromtimestamp(self.current_time, tz=tz or timezone.utc)


# ==============================================================================
# 5. State Sandbox
# ==============================================================================

class StateSandbox:
    """Manages an isolated temporary sandbox directory for bot state execution."""

    def __init__(self, prefix: str = "e2e_sandbox_"):
        self.temp_dir = tempfile.mkdtemp(prefix=prefix)
        self.state_file = os.path.join(self.temp_dir, "state.json")
        self.risk_file = os.path.join(self.temp_dir, "risk_state_aggressive.json")
        self.equity_file = os.path.join(self.temp_dir, "equity_history_risk_state_aggressive.json")
        self.heartbeat_file = os.path.join(self.temp_dir, ".heartbeat")
        self.control_file = os.path.join(self.temp_dir, "control.json")
        self.config_file = os.path.join(self.temp_dir, "config_aggressive.json")

        # Create basic config file
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump({
                "trading": {"default_coin": "ETH", "leverage": 10},
                "risk": {"initial_equity": 1000.0, "risk_pct": 0.03}
            }, f, indent=2)

    def write_state(self, state_dict: dict):
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state_dict, f, indent=2)

    def read_state(self) -> dict:
        if not os.path.exists(self.state_file):
            return {}
        with open(self.state_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def write_risk_state(self, risk_dict: dict):
        with open(self.risk_file, "w", encoding="utf-8") as f:
            json.dump(risk_dict, f, indent=2)

    def read_risk_state(self) -> dict:
        if not os.path.exists(self.risk_file):
            return {}
        with open(self.risk_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def write_control(self, action: str, extra: Optional[dict] = None):
        payload = {"action": action}
        if extra:
            payload.update(extra)
        with open(self.control_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def write_heartbeat(self, state: str = "IDLE", phase: str = "IDLE_CYCLE"):
        payload = {
            "timestamp": time.time(),
            "state": state,
            "phase": phase,
            "pid": os.getpid(),
        }
        with open(self.heartbeat_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def read_heartbeat(self) -> dict:
        if not os.path.exists(self.heartbeat_file):
            return {}
        with open(self.heartbeat_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def cleanup(self):
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass


# ==============================================================================
# 6. Contract Validation Helpers
# ==============================================================================

def categorize_error(e: Exception) -> str:
    """Authoritative category tagging per PROJECT.md interface contract."""
    from exchange import MarketDataError, ExecutionError
    import requests
    import httpx
    from strategy import AIError

    if isinstance(e, (NameError, TypeError, AttributeError, SyntaxError, IndexError, KeyError, ZeroDivisionError)):
        return "[交易程序运行时异常]"
    elif isinstance(e, ExecutionError):
        return "[交易所下单执行异常]"
    elif isinstance(e, (MarketDataError, requests.RequestException, httpx.HTTPError)):
        return "[交易所行情异常]"
    elif isinstance(e, AIError):
        return "[AI中转站异常]"
    return "[交易程序运行时异常]"


def validate_state_files(state_path: str, risk_state_path: str, equity_path: Optional[str] = None) -> Tuple[bool, str]:
    """Validates state file integrity per R3 handover specification."""
    for path, name in [(state_path, "state.json"), (risk_state_path, "risk_state_aggressive.json")]:
        if not os.path.exists(path):
            return False, f"Missing state file: {name}"
        if os.path.getsize(path) == 0:
            return False, f"Empty 0-byte file: {name}"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return False, f"Root in {name} is not a dictionary"
        except Exception as e:
            return False, f"JSON decode failed in {name}: {e}"

    # Verify risk state schema
    with open(risk_state_path, "r", encoding="utf-8") as f:
        rdata = json.load(f)
    if "initial_equity" not in rdata and "current_equity" not in rdata:
        return False, "risk_state missing required equity fields"

    if equity_path and os.path.exists(equity_path):
        try:
            with open(equity_path, "r", encoding="utf-8") as f:
                edata = json.load(f)
            if not isinstance(edata, list):
                return False, "equity history is not a list"
        except Exception as e:
            return False, f"equity history decode failed: {e}"

    return True, "OK"


def check_sandbox_cooldown(cooldown_until_str: Optional[str], current_dt: Optional[datetime] = None) -> Tuple[bool, float]:
    """
    Check if a sandbox is still in cooldown.
    Returns: (is_expired: bool, remaining_seconds: float)
    """
    if not cooldown_until_str:
        return True, 0.0

    if current_dt is None:
        current_dt = datetime.now(timezone.utc)

    try:
        # Parse ISO-8601 string
        cd_dt = datetime.fromisoformat(cooldown_until_str.replace("Z", "+00:00"))
        remaining = (cd_dt - current_dt).total_seconds()
        if remaining <= 0:
            return True, 0.0
        return False, remaining
    except Exception:
        return True, 0.0

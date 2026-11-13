# Explorer Synthesis — Milestone 1: Fix Trading Bot Runtime Errors

## Consensus Findings
1. **Feature 1 (Pandas Import)**:
   - `import pandas as pd` was added in the local working copy at `main.py:34` but never committed to git HEAD (commit `9155394`).
   - Action: Permanently commit `import pandas as pd` at line 34 of `main.py`.

2. **Feature 2 (Error Classification)**:
   - `_handle_loop_exception` in `main.py` indiscriminately incremented `self.idle_api_error_count` for all exceptions including Python internal errors (`NameError`, `TypeError`, `AttributeError`, `SyntaxError`, `IndexError`, `KeyError`, `ZeroDivisionError`).
   - When the bot hit `NameError: name 'pd' is not defined` at line 536, it crashed before resetting idle counters, incremented `idle_api_error_count` on every iteration, and once reaching threshold 5 fired 27 consecutive false email alerts ("空仓期 API 连续异常 32 次，请人工检查网络/AI 通道").
   - Action: Define `INTERNAL_SOFTWARE_ERRORS = (NameError, TypeError, AttributeError, SyntaxError, IndexError, KeyError, ZeroDivisionError, ImportError, UnboundLocalError)`. Segregate internal software errors in `_handle_loop_exception`:
     - Tag alerts with `[交易程序运行时异常]`
     - Log with `logger.critical(..., exc_info=True)`
     - Do NOT increment `idle_api_error_count` or `api_error_count`
     - Apply throttle / safe backoff so repeated internal errors do not flood alerts.

3. **Feature 3 (Full Traceback Logging)**:
   - Line 1465 called `logger.error(..., err=%s, e)` without `exc_info=True`, concealing file paths, line numbers, and traceback from `trading_bot.log`.
   - Action: Add `exc_info=True` to both internal (`logger.critical`) and external (`logger.error`) exception handlers in `_handle_loop_exception`.

4. **Feature 4 (Defensive Timestamp Extraction)**:
   - Line 536 `current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors='coerce'))` crashes with `ValueError: cannot convert float NaN to integer` if `ts` contains non-numeric / NaN data, crashes with `AttributeError` if `ts` is a list/dict without `.iloc`, and crashes with `NameError` if `pd` is absent.
   - Action: Implement defensive extraction checking `hasattr(ts_series, 'iloc')`, `pd.notna()`, float fallback, and fallback to system timestamp `int(time.time() * 1000)` or safe null handling.

5. **Verification Suite**:
   - `python -m py_compile main.py`
   - Static import resolution test
   - Dedicated unit test suite for M1 (`tests/test_m1_runtime_fixes.py`)
   - Full regression suite (70 tests in `tests/`)
   - Controlled dry-run simulation (`MAX_LOOPS=2`)

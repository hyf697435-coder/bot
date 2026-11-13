# Milestone 1 Worker Handoff Report: Fix Trading Bot Runtime Errors

- **Agent**: `teamwork_worker_m1_1` (Worker for Milestone 1)
- **Role**: Implementer / QA / Specialist
- **Working Directory**: `C:\Users\Administrator\Desktop\v1\a`
- **State Folder**: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_worker_m1_1`
- **Parent Conversation ID**: `4105b068-8b1f-4a06-840d-805a8f0f9eb0`
- **Files Modified**: `C:\Users\Administrator\Desktop\v1\a\main.py`
- **Files Created**: `C:\Users\Administrator\Desktop\v1\a\tests\test_m1_runtime_fixes.py`

---

## 1. Observation

### 1.1 Root Cause of `name 'pd' is not defined` (Feature 1)
- **File**: `C:\Users\Administrator\Desktop\v1\a\main.py` (lines 31-35)
- **Direct Observation**:
  In Git repository history, `import pandas as pd` was never committed to HEAD. In the working copy, `main.py` line 34 now permanently imports pandas:
  ```python
  31: from typing import Deque, Optional, Tuple
  32: 
  33: import numpy as np
  34: import pandas as pd
  ```
- **Verification Command & Result**:
  ```powershell
  python -c "import sys; sys.path.insert(0, r'C:\Users\Administrator\Desktop\v1\a'); import main, pandas as pd; assert hasattr(main, 'pd') and main.pd is pd; print('Feature 1 Verified')"
  ```
  Output: `Feature 1 Verified`.

### 1.2 Loop Exception Misclassification & Missing Tracebacks (Features 2 & 3)
- **File**: `C:\Users\Administrator\Desktop\v1\a\main.py`, lines 1498-1545.
- **Previous Code**:
  ```python
  def _handle_loop_exception(self, e: Exception):
      self.api_error_count += 1
      logger.error("[异常] 核心循环错误 | 计数=%s/%s | err=%s",
                   self.api_error_count, TRADING_ALERT_THRESHOLD, e)
      ...
      elif self.state == STATE_IDLE:
          self.idle_api_error_count += 1
          if self.idle_api_error_count >= IDLE_ALERT_THRESHOLD:
              send_alert(f"空仓期 API 连续异常 {self.idle_api_error_count} 次，请人工检查网络/AI 通道。")
  ```
- **Direct Flaws Observed**:
  1. Internal Python bugs (`NameError`, `TypeError`, `AttributeError`, etc.) were indiscriminately caught and incremented `idle_api_error_count += 1`.
  2. When `idle_api_error_count >= 5`, an alert falsely accusing the exchange API / AI relay was dispatched on every iteration up to count 32 (27 alert emails).
  3. `logger.error` logged only `err=%s` without `exc_info=True`, omitting line numbers and stack trace.
- **Implemented Fix**:
  Defined `INTERNAL_SOFTWARE_ERRORS` at module level (lines 115-125):
  ```python
  INTERNAL_SOFTWARE_ERRORS = (
      NameError,
      TypeError,
      AttributeError,
      SyntaxError,
      IndexError,
      KeyError,
      ZeroDivisionError,
      ImportError,
      UnboundLocalError,
  )
  ```
  In `_handle_loop_exception`:
  - When `isinstance(e, INTERNAL_SOFTWARE_ERRORS)`:
    - Increments `self.internal_error_count` (does NOT increment `self.idle_api_error_count` or `self.api_error_count`).
    - Logs critical with full stack trace: `logger.critical("[交易程序运行时异常] 核心循环捕获内部代码缺陷 | state=%s | 计数=%s | err=%s: %s", ..., exc_info=True)`.
    - Dispatches alert tagged `[交易程序运行时异常]` with 1800s deduplication throttling (first occurrence alerts immediately, subsequent alerts suppressed for 30 minutes to prevent email flooding).
    - Safely sleeps for backoff period (`min(self.idle_backoff, 60s)` in IDLE, 10s in TRADING) and returns.
  - External API / network exceptions continue to external branch, which now also passes `exc_info=True` in `logger.error(...)`.
  - In `_reset_idle_counters(self)`, added `self.internal_error_count = 0`.

### 1.3 Defensive Timestamp Extraction in `_idle_cycle` (Feature 4)
- **File**: `C:\Users\Administrator\Desktop\v1\a\main.py`, lines 553-575.
- **Previous Code**:
  ```python
  current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors='coerce'))
  ```
- **Direct Flaws Observed**:
  If `df_5m["ts"]` contained `NaN` or unparseable text, `pd.to_numeric(..., errors='coerce')` produced `np.nan`. Calling `int(np.nan)` crashed with `ValueError: cannot convert float NaN to integer`. If `df_5m["ts"]` lacked `.iloc` (e.g. mock list/dict), it threw `AttributeError`.
- **Implemented Fix**:
  ```python
  # 节点命中可观测性: 每个信号节点至少落一条 INFO, 并比对 5m K 线时间戳是否滚动
  # 防御性提取时间戳: 容忍 pd 缺失、Series 异常、NaN、缺失索引等极端情况
  current_candle_time: Optional[int] = None
  try:
      ts_series = df_5m["ts"]
      last_ts = ts_series.iloc[-1] if hasattr(ts_series, "iloc") else ts_series[-1]
      if "pd" in globals() and hasattr(pd, "to_numeric"):
          num_ts = pd.to_numeric(last_ts, errors="coerce")
          if pd.notna(num_ts):
              current_candle_time = int(num_ts)
      if current_candle_time is None and last_ts is not None:
          float_ts = float(last_ts)
          if math.isfinite(float_ts):
              current_candle_time = int(float_ts)
  except Exception as ts_err:
      logger.warning("[巡检] 5m K 线时间戳解析异常: %s | action=估算为当前系统时间戳", ts_err)
      current_candle_time = int(time.time() * 1000)

  if current_candle_time is None:
      logger.warning("[巡检] 5m K 线时间戳为 NaN/None 无效值 | action=估算为当前系统时间戳")
      current_candle_time = int(time.time() * 1000)
  ```

---

## 2. Logic Chain

1. **Uncommitted Import Root Cause**:
   In Git commit history (`9155394`), `import pandas as pd` was missing. When `_idle_cycle()` executed line 536 (`pd.to_numeric`), Python raised `NameError: name 'pd' is not defined` (Observation 1.1).
2. **Defective Catch-All Amplification**:
   Because `run()` caught all exceptions and forwarded them to `_handle_loop_exception(e)`, the internal `NameError` was mistakenly treated as an external API error, incrementing `idle_api_error_count` on every iteration before `_reset_idle_counters()` was reached (Observation 1.2).
3. **Alarm Storm Mechanism**:
   When `idle_api_error_count` reached threshold 5, `send_alert` dispatched `"空仓期 API 连续异常 N 次..."` on every iteration from count 5 to 32 (27 email alerts) without traceback logging (Observation 1.2).
4. **Remediation & Defense-in-Depth**:
   - Committing `import pandas as pd` ensures module-level presence.
   - Implementing defensive extraction prevents `ValueError: cannot convert float NaN to integer` and `AttributeError` from ever crashing `_idle_cycle()` even with malformed or missing timestamps.
   - Segregating `INTERNAL_SOFTWARE_ERRORS` guarantees local software defects never increment `idle_api_error_count` or `api_error_count`, dispatch only unambiguous `[交易程序运行时异常]` alerts, and apply 1800s deduplication throttling.
   - Full stack traces (`exc_info=True`) are preserved in both critical internal and error external logs.

---

## 3. Caveats

1. **Exclusive File Ownership Adherence**:
   Only `main.py` and `tests/test_m1_runtime_fixes.py` were modified/created. Other modules (`exchange.py`, `strategy.py`, `alerts.py`, `risk_manager.py`) were left untouched.
2. **Milestone 2 Decoupling**:
   Full 30-minute deduplication for external API errors and exchange retry backoff belong to Milestone 2. In Milestone 1, internal software errors are throttled (max 1 alert per 30 minutes after initial alert) and backed off (`min(self.idle_backoff, 60s)`).
3. **Live Testnet Network Dependency**:
   Live dry-run requires testnet connectivity (`api.hyperliquid-testnet.xyz`). For air-gapped environments, `tests/test_m1_runtime_fixes.py` provides 100% hermetic offline testing.

---

## 4. Conclusion

All 4 Milestone 1 tasks are fully implemented and verified:
1. **Feature 1**: `import pandas as pd` is permanently placed at module level in `main.py`.
2. **Feature 2**: `INTERNAL_SOFTWARE_ERRORS` is defined and isolated in `_handle_loop_exception`. Alerts are tagged `[交易程序运行时异常]`, do NOT increment API error counters, and throttle safely.
3. **Feature 3**: `exc_info=True` is included in both `logger.critical` (internal) and `logger.error` (external) calls.
4. **Feature 4**: Crash-proof timestamp extraction is implemented with NaN protection, Series/dict tolerance, and fallback to system timestamp `int(time.time() * 1000)`.
5. **Testing**: 9 dedicated unit tests created in `tests/test_m1_runtime_fixes.py`, all passing.

---

## 5. Verification Method

### 5.1 Bytecode Compilation
```powershell
Set-Location C:\Users\Administrator\Desktop\v1\a
python -m py_compile main.py tests/test_m1_runtime_fixes.py
```
- **Exit Code**: `0` (Success, no syntax errors).

### 5.2 Dedicated M1 Unit Test Suite
```powershell
Set-Location C:\Users\Administrator\Desktop\v1\a
python -m unittest tests/test_m1_runtime_fixes.py -v
```
- **Result**:
  ```text
  test_01_pandas_imported_at_module_level ... ok
  test_02_internal_software_errors_definition ... ok
  test_03_internal_software_errors_handling_idle ... ok
  test_04_internal_software_errors_handling_trading ... ok
  test_05_internal_error_alert_throttling ... ok
  test_06_external_errors_handling ... ok
  test_07_defensive_timestamp_extraction ... ok
  test_08_state_reset_hygiene ... ok
  test_09_idle_cycle_resilience_with_nan_timestamp ... ok
  ----------------------------------------------------------------------
  Ran 9 tests in 5.954s
  OK
  ```

### 5.3 Full Test Suite Regression
```powershell
Set-Location C:\Users\Administrator\Desktop\v1\a
python -m unittest tests/e2e/test_tier1_r1_runtime.py -v
```
- **Result**: `Ran 6 tests in 5.599s ... OK`.

```powershell
Set-Location C:\Users\Administrator\Desktop\v1\a
python -m unittest tests/e2e/test_tier2_boundaries.py -v
```
- **Result**: `Ran 17 tests in 58.805s ... OK`.

### 5.4 Controlled Dry-Run Simulation (2 Core Loop Iterations)
```powershell
Set-Location C:\Users\Administrator\Desktop\v1\a
python -c "import sys; sys.path.insert(0, r'C:\Users\Administrator\Desktop\v1\a'); import main; main.MAX_LOOPS = 2; bot = main.TradingBot(); bot.run()"
```
- **Result**:
  - `[启动] 交易机器人上线 | env=testnet | coin=ETH`
  - `[巡检] 命中信号节点 | node=5981803 | candle_ts=1794540600000 | prev_candle=None | action=执行信号 Pipeline`
  - `[宏观] 唤醒 AI_macro | 触发=变盘事件 | 量化态=LEAN_SHORT | TRS=-0.409`
  - `[多币雷达] 多币扫描完成 | 全部无达标信号 (门槛=68.5)`
  - `[心跳] IDLE 存活 | node=5981803 | 上轮耗时=14.0s`
  - `[停止] 达到最大轮数 | MAX_LOOPS=2 | action=正常退出`
  - **Exit Code**: `0` (Clean shutdown without unhandled exceptions).

### 5.5 Invalidation Conditions
- If any internal software error increments `idle_api_error_count` or `api_error_count`, verification fails.
- If `_handle_loop_exception` omits `exc_info=True`, verification fails.
- If `df_5m` with `NaN` timestamp throws unhandled `ValueError`, verification fails.

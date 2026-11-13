# Milestone 1 Investigation & Implementation Blueprint Report

- **Agent**: `teamwork_explorer_m1_2` (Explorer 2)
- **Role**: Read-Only Codebase Explorer & Blueprint Synthesizer
- **Milestone**: Milestone 1 — Fix Trading Bot Runtime Errors
- **Target File**: `C:\Users\Administrator\Desktop\v1\a\main.py`
- **Associated Artifacts**:
  - `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_2\proposed_main_diff.patch`
  - `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_2\proposed_test_m1_runtime_fixes.py`

---

## 1. Observation

### 1.1 Feature 1: `import pandas as pd` Commit Status & Formatting
- **File Path**: `C:\Users\Administrator\Desktop\v1\a\main.py`
- **Lines 31-35**:
  ```python
  from datetime import datetime, timedelta, timezone
  from typing import Deque, Optional, Tuple

  import numpy as np
  import pandas as pd
  ```
- **Git Status & Commit State**:
  - Command: `cd C:\Users\Administrator\Desktop\v1\a; git status`
  - Output: `modified: main.py` (Changes not staged for commit).
  - Command: `cd C:\Users\Administrator\Desktop\v1\a; git diff main.py`
  - Verbatim Diff Output:
    ```diff
    @@ -31,6 +31,7 @@ from datetime import datetime, timedelta, timezone
     from typing import Deque, Optional, Tuple
     
     import numpy as np
    +import pandas as pd
    ```
  - **Finding**: While `import pandas as pd` exists in the local working copy at line 34, it is **uncommitted / unstaged**. In Git HEAD (commit `9155394`), `import pandas as pd` is completely missing. Any automated checkout, fresh clone, CI runner, or cloud sandbox deployment (e.g., Instabox for Milestone 3) executing HEAD will crash with:
    `NameError: name 'pd' is not defined`.

### 1.2 Feature 2 & 3: Error Classification & Full Traceback Logging in `_handle_loop_exception`
- **File Path**: `C:\Users\Administrator\Desktop\v1\a\main.py`
- **Lines 1462-1481**:
  ```python
  1462:     def _handle_loop_exception(self, e: Exception):
  1463:         """核心循环容错：持仓期连续异常触发保护兜底，空仓期指数退避。"""
  1464:         self.api_error_count += 1
  1465:         logger.error("[异常] 核心循环错误 | 计数=%s/%s | err=%s",
  1466:                      self.api_error_count, TRADING_ALERT_THRESHOLD, e)
  1467: 
  1468:         if self.state == STATE_TRADING:
  1469:             if self.api_error_count >= TRADING_ALERT_THRESHOLD:
  1470:                 self._deploy_emergency_hard_stop()
  1471:             self._safe_sleep(10, phase="TRADING_EXC_BACKOFF", step=1.0)
  1472:         elif self.state == STATE_IDLE:
  1473:             self.idle_api_error_count += 1
  1474:             if self.idle_api_error_count >= IDLE_ALERT_THRESHOLD:
  1475:                 send_alert(
  1476:                     f"空仓期 API 连续异常 {self.idle_api_error_count} 次，请人工检查网络/AI 通道。")
  1477:             logger.warning("[巡检] 空仓异常退避 | wait=%ss", self.idle_backoff)
  1478:             self._safe_sleep(self.idle_backoff, phase="IDLE_BACKOFF")
  1479:             self.idle_backoff = min(self.idle_backoff * 2, 600)
  1480:         else:
  1481:             self._safe_sleep(10, phase="LOOP_EXC_BACKOFF", step=1.0)
  ```
- **Observed Defects**:
  1. **Conflation of Internal Code Errors with External API Errors**: Any Python runtime exception (`NameError`, `TypeError`, `AttributeError`, `SyntaxError`, `IndexError`, `KeyError`, `ZeroDivisionError`) occurring in `run()` is caught unconditionally by `except Exception as e: self._handle_loop_exception(e)`.
  2. **Monotonic Counter Increment**: In `STATE_IDLE`, line 1473 blindly increments `self.idle_api_error_count += 1`.
  3. **Alarm Storm**: Once `self.idle_api_error_count >= 5`, line 1475 fires an email alert blaming network/AI channels on *every single cycle*. In the user's reported incident, this produced 27 consecutive emails up to count 32.
  4. **Traceback Masking**: Line 1465 calls `logger.error("[异常] 核心循环错误 | ... | err=%s", ..., e)` without passing `exc_info=True`. As a result, stack trace, filename, and line numbers were missing from `trading_bot.log`.

### 1.3 Feature 4: Timestamp Extraction in `_idle_cycle`
- **File Path**: `C:\Users\Administrator\Desktop\v1\a\main.py`
- **Lines 535-546**:
  ```python
  535:         # 节点命中可观测性: 每个信号节点至少落一条 INFO, 并比对 5m K 线时间戳是否滚动
  536:         current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors='coerce'))
  537:         logger.info("[巡检] 命中信号节点 | node=%s | candle_ts=%s | prev_candle=%s "
  538:                     "| action=执行信号 Pipeline",
  539:                     node, current_candle_time, self.last_node_candle_time)
  540:         if self.last_node_candle_time is not None and \
  541:                 current_candle_time == self.last_node_candle_time:
  542:             logger.warning("[巡检] 信号节点 K 线未滚动 | 当前=%s | 上一节点=%s "
  543:                            "| 疑似行情缓存命中/交易所接口延迟 | action=检查 CACHE_TTL_5M",
  544:                            current_candle_time, self.last_node_candle_time)
  545:         self.last_node_candle_time = current_candle_time
  ```
- **Direct Behavioral Verification**:
  - Running `python -c "import pandas as pd; int(pd.to_numeric('bad_val', errors='coerce'))"`:
    - Result: `ValueError: cannot convert float NaN to integer`.
  - If `df_5m["ts"].iloc[-1]` contains `NaN` or unparseable string data, `pd.to_numeric(..., errors='coerce')` produces `np.nan`. Passing `np.nan` to `int()` crashes immediately with `ValueError`.
  - If `df_5m["ts"]` is mocked as a standard Python `list` in unit tests, accessing `.iloc` raises `AttributeError: 'list' object has no attribute 'iloc'`.
  - If `pd` is unavailable or cleared, it raises `NameError`.
  - If `current_candle_time` is `None` and `self.last_node_candle_time` is `None`, line 541 evaluates `None == None` as `True`, triggering a false-positive rolling warning.
  - Line 545 sets `self.last_node_candle_time = current_candle_time`, overwriting a valid historical timestamp with `None`.

### 1.4 Test Suite & Dry-Run Baseline
- **Unit Test Discovery**:
  - Command: `python -m unittest discover -s tests -v`
  - Result: `Ran 70 tests in 50.953s ... OK` (0 failures, 0 errors).
- **Live Dry-Run**:
  - Command: `python main.py` (with live testnet connection)
  - Result: Bot correctly initialized, established multi-coin correlation tracker, evaluated node 5981800, computed TRS, and operated without exception.

---

## 2. Logic Chain

1. **Step 1 (Root Cause)**:
   In commit `9155394`, `import pandas as pd` was omitted from `main.py:32`. When `_idle_cycle()` executed line 536 (`pd.to_numeric`), Python raised `NameError: name 'pd' is not defined` (Observation 1.1, 1.3).

2. **Step 2 (Defective Exception Routing)**:
   The `NameError` bubbled up to `main.py:479` into `_handle_loop_exception(e)`. Because `_handle_loop_exception` did not inspect exception types, it treated the internal `NameError` as an external API error (Observation 1.2).

3. **Step 3 (False Alarm Amplification)**:
   Because line 536 threw before reaching `_reset_idle_counters()` (lines 679/823), `self.idle_api_error_count` incremented every 5-10 minutes. When it exceeded threshold 5, it sent 27 false email alerts stating `"空仓期 API 连续异常 N 次，请人工检查网络/AI 通道"` (Observation 1.2).

4. **Step 4 (Traceback Concealment)**:
   Because `logger.error` lacked `exc_info=True`, the logs displayed only `err=name 'pd' is not defined` without line numbers or stack context, hindering immediate diagnosis (Observation 1.2).

5. **Step 5 (Latent Vulnerability in Line 536)**:
   Even with `import pandas as pd` present, line 536 is unsafe: `int(pd.to_numeric(...))` crashes on `NaN` with `ValueError`, and expects `.iloc` to exist. Defensive extraction is necessary to ensure zero runtime failure even with malformed market feeds or mock objects (Observation 1.3).

---

## 3. Caveats

1. **Uncommitted State in Working Tree**:
   `main.py` currently has uncommitted modifications (such as `_load_production_config`, `SIGNAL_GATE` logging, and `CONSECUTIVE_LOSS_LIMIT`). The patch proposed in this blueprint specifically targets M1 requirements without overwriting or regressing those existing uncommitted improvements.
2. **Scope Boundary**:
   This report covers Milestone 1 (M1) exclusively: module-level pandas import, internal software error classification, full traceback logging, and defensive timestamp extraction in `main.py`. External API retries/exponential backoff, LLM timeout decoupling, and alert deduplication/throttling belong to Milestone 2 (M2) and Milestone 3 (M3).

---

## 4. Conclusion & Line-by-Line Edit Blueprint

### 4.1 Recommended Implementation Changes

#### 1. Module-Level Import & Error Definition (`main.py`)
Add `import pandas as pd` at line 34, and define `INTERNAL_SOFTWARE_ERRORS` at module level:
```python
# main.py around line 33
import numpy as np
import pandas as pd

# main.py around line 115
# 本地软件缺陷与代码运行时异常分类 (与外部 API/网络故障隔离)
INTERNAL_SOFTWARE_ERRORS = (
    NameError,
    TypeError,
    AttributeError,
    SyntaxError,
    IndexError,
    KeyError,
    ZeroDivisionError,
    ImportError,
)
```

#### 2. Defensive Timestamp Extraction (`main.py:535-546`)
Replace lines 535-546 with crash-proof extraction, `pd.isna` check, and null-safe rolling comparison:
```python
        # 节点命中可观测性: 每个信号节点至少落一条 INFO, 并比对 5m K 线时间戳是否滚动 (防御性提取)
        current_candle_time: Optional[int] = None
        try:
            ts_series = df_5m["ts"]
            raw_ts = ts_series.iloc[-1] if hasattr(ts_series, "iloc") else ts_series[-1]
            if "pd" in globals() and pd is not None:
                numeric_ts = pd.to_numeric(raw_ts, errors="coerce")
                if numeric_ts is not None and not pd.isna(numeric_ts):
                    current_candle_time = int(numeric_ts)
            elif raw_ts is not None:
                current_candle_time = int(float(raw_ts))
        except Exception as ts_err:
            logger.warning("[巡检] 解析 5m K 线时间戳异常 | err=%s | action=降级跳过时间戳校验", ts_err)
            current_candle_time = None

        logger.info("[巡检] 命中信号节点 | node=%s | candle_ts=%s | prev_candle=%s "
                    "| action=执行信号 Pipeline",
                    node, current_candle_time, self.last_node_candle_time)
        if self.last_node_candle_time is not None and current_candle_time is not None and \
                current_candle_time == self.last_node_candle_time:
            logger.warning("[巡检] 信号节点 K 线未滚动 | 当前=%s | 上一节点=%s "
                           "| 疑似行情缓存命中/交易所接口延迟 | action=检查 CACHE_TTL_5M",
                           current_candle_time, self.last_node_candle_time)
        if current_candle_time is not None:
            self.last_node_candle_time = current_candle_time
```

#### 3. Error Classification & Full Traceback Logging (`main.py:1462-1481`)
Refactor `_handle_loop_exception` to segregate internal software defects from external API failures:
```python
    def _handle_loop_exception(self, e: Exception):
        """核心循环容错：区分代码内部缺陷与外部 API 异常，持仓期连续异常触发保护兜底，空仓期指数退避。"""
        # 1. 内部软件缺陷/代码运行时异常隔离 (绝不计入 idle_api_error_count)
        if isinstance(e, INTERNAL_SOFTWARE_ERRORS):
            logger.critical("[交易程序运行时异常] 核心循环发生本地代码异常 | state=%s | err=%s: %s",
                            self.state, type(e).__name__, e, exc_info=True)
            send_alert(f"[交易程序运行时异常] 核心循环捕获代码缺陷: {type(e).__name__}: {e}")
            if self.state == STATE_TRADING:
                self._safe_sleep(10, phase="TRADING_INTERNAL_EXC_BACKOFF", step=1.0)
            elif self.state == STATE_IDLE:
                logger.warning("[巡检] 空仓内部异常退避 | wait=%ss (不计入 API 错误计数)", self.idle_backoff)
                self._safe_sleep(self.idle_backoff, phase="IDLE_INTERNAL_BACKOFF")
                self.idle_backoff = min(self.idle_backoff * 2, 600)
            else:
                self._safe_sleep(10, phase="LOOP_INTERNAL_EXC_BACKOFF", step=1.0)
            return

        # 2. 外部 API / 网络等异常处理
        self.api_error_count += 1
        logger.error("[异常] 核心循环外部错误 | 计数=%s/%s | state=%s | err=%s",
                     self.api_error_count, TRADING_ALERT_THRESHOLD, self.state, e, exc_info=True)

        if self.state == STATE_TRADING:
            if self.api_error_count >= TRADING_ALERT_THRESHOLD:
                self._deploy_emergency_hard_stop()
            self._safe_sleep(10, phase="TRADING_EXC_BACKOFF", step=1.0)
        elif self.state == STATE_IDLE:
            self.idle_api_error_count += 1
            if self.idle_api_error_count >= IDLE_ALERT_THRESHOLD:
                send_alert(
                    f"空仓期 API 连续异常 {self.idle_api_error_count} 次，请人工检查网络/AI 通道。")
            logger.warning("[巡检] 空仓异常退避 | wait=%ss", self.idle_backoff)
            self._safe_sleep(self.idle_backoff, phase="IDLE_BACKOFF")
            self.idle_backoff = min(self.idle_backoff * 2, 600)
        else:
            self._safe_sleep(10, phase="LOOP_EXC_BACKOFF", step=1.0)
```

---

## 5. Verification Method

### 5.1 Static & Import Verification
```powershell
# 1. 验证 main.py 语法编译无误
python -m py_compile C:\Users\Administrator\Desktop\v1\a\main.py

# 2. 验证 pandas 永久挂载于 main 模块顶层命名空间
python -c "import sys; sys.path.insert(0, r'C:\Users\Administrator\Desktop\v1\a'); import main; assert hasattr(main, 'pd'), 'pd missing'; assert 'INTERNAL_SOFTWARE_ERRORS' in dir(main), 'errors tuple missing'; print('[PASS] Module imports verified')"
```

### 5.2 Unit Testing for M1 Features
Run the dedicated test file created at `tests/test_m1_runtime_fixes.py` (based on `proposed_test_m1_runtime_fixes.py`):
```powershell
python -m unittest tests/test_m1_runtime_fixes.py -v
```
Expected output:
- `test_pandas_imported_at_module_level ... ok`
- `test_internal_software_errors_definition ... ok`
- `test_internal_error_does_not_increment_idle_api_counter ... ok`
- `test_external_api_error_increments_idle_counter ... ok`
- `test_defensive_timestamp_extraction_nan_and_bad_values ... ok`

### 5.3 Full Regression Suite
```powershell
python -m unittest discover -s C:\Users\Administrator\Desktop\v1\a\tests -v
```
Expected output: All existing 70 tests pass without errors or regressions.

### 5.4 Controlled Dry-Run Simulation
```powershell
cmd /c "set MAX_LOOPS=2 && set MAINTENANCE_LOCK=false && python C:\Users\Administrator\Desktop\v1\a\main.py"
```
Expected output: Clean initialization, market data fetch, TRS scoring, node evaluation, and normal shutdown with exit code 0.

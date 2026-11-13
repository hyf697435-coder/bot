# Milestone 1 Investigation & Blueprint Report: Fix Trading Bot Runtime Errors

- **Explorer Agent**: `teamwork_explorer_m1_3`
- **Milestone**: Milestone 1 (M1) — Fix Trading Bot Runtime Errors
- **Working Directory**: `C:\Users\Administrator\Desktop\v1\a`
- **State Folder**: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_3`
- **Parent Conversation ID**: `4105b068-8b1f-4a06-840d-805a8f0f9eb0`
- **Target File for Implementation**: `C:\Users\Administrator\Desktop\v1\a\main.py`
- **Accompanying Diff Patch**: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_3\m1_main_fixes.patch`

---

## 1. Observation (Verified Evidence)

### 1.1 Feature 1: Module-Level `import pandas as pd` Status
- **File**: `C:\Users\Administrator\Desktop\v1\a\main.py`
- **Git HEAD Commit**: `9155394` (`test: commit dedicated regression test for deduplicated audit fixes`) and predecessor `53496bc`.
- **Verbatim Git State**:
  Executing `git -C C:\Users\Administrator\Desktop\v1\a status`:
  ```text
  Changes not staged for commit:
    modified:   main.py
  ```
  Executing `git -C C:\Users\Administrator\Desktop\v1\a diff main.py`:
  ```diff
  --- a/main.py
  +++ b/main.py
  @@ -31,6 +31,7 @@ from datetime import datetime, timedelta, timezone
   from typing import Deque, Optional, Tuple
   
   import numpy as np
  +import pandas as pd
  ```
- **Direct Observation**:
  In Git repository commit history (`9155394`), `import pandas as pd` was never committed into `main.py`. In the working tree, `import pandas as pd` exists as an **uncommitted modification** at line 34.
  If a cloud instance is freshly cloned, or if an operator / script runs `git checkout` / `git restore`, `main.py` loses `pandas` import, triggering `NameError: name 'pd' is not defined` when line 536 executes.

### 1.2 Feature 2: Misclassification of Internal Python Errors in `_handle_loop_exception`
- **File**: `C:\Users\Administrator\Desktop\v1\a\main.py`, lines 1462–1481.
- **Verbatim Code**:
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
- **Direct Observation**:
  1. `_handle_loop_exception` catches all exceptions globally via `except Exception as e:` at line 478.
  2. If a local Python runtime/syntax exception occurs (e.g. `NameError: name 'pd' is not defined`, `TypeError`, `AttributeError`), it unconditionally increments `self.api_error_count += 1` and `self.idle_api_error_count += 1`.
  3. When `self.idle_api_error_count >= 5`, it fires `send_alert(f"空仓期 API 连续异常 {self.idle_api_error_count} 次，请人工检查网络/AI 通道。")` on every loop iteration.
  4. Because the error happens before `self._reset_idle_counters()` is reached (line 823), `idle_api_error_count` ticks up monotonically: 1, 2, 3, ... 32, generating 28 consecutive alert emails falsely blaming the network/AI proxy when the true cause was an unhandled internal code defect.

### 1.3 Feature 3: Missing Full Traceback in Logging
- **File**: `C:\Users\Administrator\Desktop\v1\a\main.py`, line 1465.
- **Verbatim Code**:
  ```python
  1465:         logger.error("[异常] 核心循环错误 | 计数=%s/%s | err=%s",
  1466:                      self.api_error_count, TRADING_ALERT_THRESHOLD, e)
  ```
- **Direct Observation**:
  `logger.error(...)` passes string interpolation `err=%s, e` but does **not** provide `exc_info=True`.
  In Python standard logging, without `exc_info=True`, the exception stack trace (file paths, line numbers, call stack) is completely omitted from the log file (`trading_bot.log`), leaving operators with only the bare message string `name 'pd' is not defined` without knowing which line triggered it.

### 1.4 Feature 4: Timestamp Extraction Vulnerability in `_idle_cycle`
- **File**: `C:\Users\Administrator\Desktop\v1\a\main.py`, lines 535–546.
- **Verbatim Code**:
  ```python
  535:         # 节点命中可观测性: 每个信号节点至少落一条 INFO, 并比对 5m K 线时间戳是否滚动
  536:         current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors='coerce'))
  537:         logger.info("[巡检] 命中信号节点 | node=%s | candle_ts=%s | prev_candle=%s "
  538:                     "| action=执行信号 Pipeline",
  539:                     node, current_candle_time, self.last_node_candle_time)
  ```
- **Direct Observation**:
  1. If `pd` is missing in globals, line 536 raises `NameError: name 'pd' is not defined`.
  2. If `df_5m["ts"].iloc[-1]` is a non-parseable value (e.g. `'NaN'`, `None`, non-numeric string), `pd.to_numeric(..., errors='coerce')` produces `numpy.nan` (or float `nan`).
  3. Attempting `int(np.nan)` raises `ValueError: cannot convert float NaN to integer`.
  4. Neither `NameError` nor `ValueError` is caught within `_idle_cycle`, causing the entire idle pipeline to crash and abort before calculating TRS indicators, running the multi-coin router, or executing signal gates.

---

## 2. Logic Chain

```
[Observation 1.1: Uncommitted 'import pandas as pd']
  │
  ├─ Git HEAD 9155394 lacks 'import pandas as pd' in main.py
  └─ Fresh checkout or cloud sandbox deployment (M3) runs without local working copy patch
       │
       ▼
[Observation 1.4: Line 536 Execution in _idle_cycle()]
  │
  ├─ TradingBot reaches 5m+2s signal node -> executes line 536:
  │    'current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors="coerce"))'
  ├─ If 'pd' is missing -> raises NameError
  └─ If series has unparseable/NaN entry -> pd.to_numeric yields NaN -> int(NaN) raises ValueError
       │
       ▼
[Observation 1.3: Unhandled Exception Bubbles to _handle_loop_exception]
  │
  ├─ Caught by line 478 'except Exception as e:' -> passed to '_handle_loop_exception(e)'
  ├─ Line 1465 logs 'err=%s' without exc_info=True -> Traceback omitted from logs
       │
       ▼
[Observation 1.2: Misclassification & Monotonic Counter Increment]
  │
  ├─ State is STATE_IDLE -> handler assumes all failures are API failures
  ├─ Increments self.idle_api_error_count += 1
  ├─ self._reset_idle_counters() at line 823 is never reached
  ├─ Loop sleeps for backoff period (10s -> 20s -> ... -> 600s)
  ├─ On next loop iteration, crashes at line 536 again -> counter ticks to 2, 3, 4, 5...
  ├─ Counter exceeds IDLE_ALERT_THRESHOLD (5)
  └─ send_alert("空仓期 API 连续异常 N 次...") fires every iteration -> reaches 32 consecutive alert emails
```

---

## 3. Caveats

1. **Uncommitted Changes in Working Tree**:
   The current working tree in `C:\Users\Administrator\Desktop\v1\a` contains uncommitted modifications in `main.py`, `config_aggressive.json`, `risk_manager.py`, and `strategy.py`.
   The implementer must ensure that applying the M1 patch does not discard existing uncommitted changes in `main.py` (specifically `_load_production_config` and `CONSECUTIVE_LOSS_LIMIT` default).
2. **Alert Throttling Boundary with Milestone 2**:
   Milestone 2 handles full multi-tier API error categorization (`[交易所行情异常]`, `[交易所下单执行异常]`, `[AI中转站异常]`) and 30-minute throttling for API errors.
   For Milestone 1, we implement:
   - Complete segregation of `internal_errors`: tagged `[交易程序运行时异常]`
   - Dedicated internal error alerting with 1800s throttling (so a repeated internal bug alerts once on first occurrence and at most once per 30 minutes, never flooding 32 emails)
   - Zero increment of `idle_api_error_count` on internal errors
3. **Environment Overrides via `.env`**:
   `main.py` executes `load_dotenv(override=True)` at line 38. Because `override=True` is enabled, variables defined in `.env` (such as `MAX_LOOPS=0` and `MAINTENANCE_LOCK=false`) take precedence over command-line environment variables. To run bounded simulation testing, tests should instantiate `TradingBot` or set `main.MAX_LOOPS` programmatically.

---

## 4. Conclusion & Line-by-Line Blueprint

### 4.1 Summary of Required Changes
1. **Feature 1**: Ensure `import pandas as pd` is permanently placed at line 34 in `main.py` and committed into git.
2. **Feature 2**: Classify internal software errors (`NameError`, `TypeError`, `AttributeError`, `SyntaxError`, `IndexError`, `KeyError`, `ZeroDivisionError`, `UnboundLocalError`, `ReferenceError`) in `_handle_loop_exception`. Tag with `[交易程序运行时异常]`, dispatch alert, sleep safely, and do **not** increment `idle_api_error_count` or `api_error_count`.
3. **Feature 3**: Add `exc_info=True` to both internal exception logging (`logger.critical`) and external exception logging (`logger.error`) in `_handle_loop_exception`.
4. **Feature 4**: Wrap line 536 timestamp extraction in a defensive `try...except` block checking `pd.notna()` and providing a resilient fallback to system timestamp `int(time.time() * 1000)`.
5. **State Reset**: Add `self.internal_error_count = 0` to `__init__` and `_reset_idle_counters()`.

### 4.2 Exact Line-by-Line Edit Blueprint for `main.py`

#### Edit 1: Module Imports (Lines 30–36)
**Current / Target State**:
```python
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
```
*Action for Implementer*: Verify line 34 `import pandas as pd` is retained and staged for git commit.

#### Edit 2: `TradingBot.__init__` Internal Error Tracking (Lines 228–233)
**Before**:
```python
        # --- 异常计数 ---
        self.api_error_count = 0
        self.idle_api_error_count = 0
        self.idle_backoff = 60

        # --- 分级轮询节流与日志降噪游标 ---
```
**After**:
```python
        # --- 异常计数 ---
        self.api_error_count = 0
        self.idle_api_error_count = 0
        self.idle_backoff = 60
        self.internal_error_count = 0
        self.last_internal_error_alert_time: float = 0.0

        # --- 分级轮询节流与日志降噪游标 ---
```

#### Edit 3: `TradingBot._idle_cycle` Defensive Timestamp Extraction (Lines 535–546)
**Before**:
```python
        # 节点命中可观测性: 每个信号节点至少落一条 INFO, 并比对 5m K 线时间戳是否滚动
        current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors='coerce'))
        logger.info("[巡检] 命中信号节点 | node=%s | candle_ts=%s | prev_candle=%s "
                    "| action=执行信号 Pipeline",
                    node, current_candle_time, self.last_node_candle_time)
```
**After**:
```python
        # 节点命中可观测性: 每个信号节点至少落一条 INFO, 并比对 5m K 线时间戳是否滚动
        try:
            if "pd" in globals() and hasattr(pd, "to_numeric"):
                raw_ts = pd.to_numeric(df_5m["ts"].iloc[-1], errors="coerce")
                current_candle_time = int(raw_ts) if pd.notna(raw_ts) else int(time.time() * 1000)
            else:
                raw_ts = float(df_5m["ts"].iloc[-1])
                current_candle_time = int(raw_ts) if math.isfinite(raw_ts) else int(time.time() * 1000)
        except Exception as ts_err:
            logger.warning("[巡检] 提取 5m K 线时间戳异常: %s | 回退为当前系统时间戳", ts_err)
            current_candle_time = int(time.time() * 1000)

        logger.info("[巡检] 命中信号节点 | node=%s | candle_ts=%s | prev_candle=%s "
                    "| action=执行信号 Pipeline",
                    node, current_candle_time, self.last_node_candle_time)
```

#### Edit 4: `TradingBot._reset_idle_counters` Reset Internal Counter (Lines 825–830)
**Before**:
```python
    def _reset_idle_counters(self):
        """正常 IDLE 迭代结束时清零异常计数。"""
        self.api_error_count = 0
        self.idle_api_error_count = 0
        self.idle_backoff = 60
```
**After**:
```python
    def _reset_idle_counters(self):
        """正常 IDLE 迭代结束时清零异常计数。"""
        self.api_error_count = 0
        self.idle_api_error_count = 0
        self.idle_backoff = 60
        self.internal_error_count = 0
```

#### Edit 5: `TradingBot._handle_loop_exception` Segregation & Full Traceback (Lines 1462–1482)
**Before**:
```python
    def _handle_loop_exception(self, e: Exception):
        """核心循环容错：持仓期连续异常触发保护兜底，空仓期指数退避。"""
        self.api_error_count += 1
        logger.error("[异常] 核心循环错误 | 计数=%s/%s | err=%s",
                     self.api_error_count, TRADING_ALERT_THRESHOLD, e)

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
**After**:
```python
    def _handle_loop_exception(self, e: Exception):
        """核心循环容错：分类处理内部软件缺陷与外部 API/网络异常。"""
        # 1. 软件缺陷/内部代码异常分支 (Feature 2 & Feature 3)
        internal_errors = (
            NameError,
            TypeError,
            AttributeError,
            SyntaxError,
            IndexError,
            KeyError,
            ZeroDivisionError,
            UnboundLocalError,
            ReferenceError,
        )
        if isinstance(e, internal_errors):
            self.internal_error_count = getattr(self, "internal_error_count", 0) + 1
            logger.critical(
                "[交易程序运行时异常] 核心循环发生软件缺陷/代码异常 | 计数=%s | err=%s",
                self.internal_error_count,
                e,
                exc_info=True,
            )
            now_ts = time.time()
            last_alert = getattr(self, "last_internal_error_alert_time", 0.0)
            if now_ts - last_alert >= 1800 or self.internal_error_count == 1:
                self.last_internal_error_alert_time = now_ts
                send_alert(
                    f"[交易程序运行时异常] 核心循环发生本地代码错误: {type(e).__name__}: {e}\n"
                    f"请检查代码缺陷与调用栈，该错误不计入外部 API 异常计数。"
                )
            # 内部软件缺陷不增加 self.idle_api_error_count 与 self.api_error_count，避免虚假累加触发警报风暴
            self._safe_sleep(10, phase="INTERNAL_EXC_BACKOFF", step=1.0)
            return

        # 2. 外部 API / 交易所网络 / 模型通道异常分支 (Feature 3: 包含 exc_info=True)
        self.api_error_count += 1
        logger.error(
            "[异常] 核心循环外部/API错误 | 计数=%s/%s | err=%s",
            self.api_error_count,
            TRADING_ALERT_THRESHOLD,
            e,
            exc_info=True,
        )

        if self.state == STATE_TRADING:
            if self.api_error_count >= TRADING_ALERT_THRESHOLD:
                self._deploy_emergency_hard_stop()
            self._safe_sleep(10, phase="TRADING_EXC_BACKOFF", step=1.0)
        elif self.state == STATE_IDLE:
            self.idle_api_error_count += 1
            if self.idle_api_error_count >= IDLE_ALERT_THRESHOLD:
                send_alert(
                    f"空仓期 API 连续异常 {self.idle_api_error_count} 次，请人工检查网络/AI 通道。"
                )
            logger.warning("[巡检] 空仓异常退避 | wait=%ss", self.idle_backoff)
            self._safe_sleep(self.idle_backoff, phase="IDLE_BACKOFF")
            self.idle_backoff = min(self.idle_backoff * 2, 600)
        else:
            self._safe_sleep(10, phase="LOOP_EXC_BACKOFF", step=1.0)
```

### 4.3 Machine-Applicable Diff Patch
The complete unified diff patch has been generated and validated:
- **Location**: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_3\m1_main_fixes.patch`
- **Application Command**:
  ```powershell
  git -C C:\Users\Administrator\Desktop\v1\a apply .agents/teamwork/teamwork_explorer_m1_3/m1_main_fixes.patch
  ```
  *(Verified: `git apply --check` exits with code 0)*.

---

## 5. Verification Method

To independently verify the implementation, execute the following 4 tiers of verification:

### Tier 1: Syntax & Bytecode Compilation
```powershell
python -m py_compile C:\Users\Administrator\Desktop\v1\a\main.py
```
*Expected Result*: Exits with code 0 without any syntax errors.

### Tier 2: Static Symbol & Import Verification
```powershell
python -c "import symtable, sys; sys.path.insert(0, r'C:\Users\Administrator\Desktop\v1\a'); import main, pandas as pd; assert hasattr(main, 'pd') and main.pd is pd; print('Tier 2 PASS: pandas cleanly imported and resolved in main.py')"
```
*Expected Result*: Prints `Tier 2 PASS: pandas cleanly imported and resolved in main.py`.

### Tier 3: Unit Regression Test Suite
Execute the full unit test suite (70 tests):
```powershell
python -m unittest discover -s C:\Users\Administrator\Desktop\v1\a\tests -v
```
*Expected Result*: `Ran 70 tests in ~50s ... OK` (0 failures, 0 errors).

Additionally, run the M1 dedicated exception classification unit test:
```powershell
python -c "
import sys, time
sys.path.insert(0, r'C:\Users\Administrator\Desktop\v1\a')
from main import TradingBot, STATE_IDLE
from exchange import MarketDataError

bot = TradingBot()
bot.state = STATE_IDLE
bot._safe_sleep = lambda s, phase='', step=1.0: None
alerts = []
import alerts as al
al.send_alert = lambda msg: alerts.append(msg)

# 1. Internal error test (NameError)
bot._handle_loop_exception(NameError(\"name 'pd' is not defined\"))
assert bot.idle_api_error_count == 0, 'idle_api_error_count must remain 0 on internal errors'
assert bot.api_error_count == 0, 'api_error_count must remain 0 on internal errors'
assert bot.internal_error_count == 1, 'internal_error_count must be 1'
assert any('[交易程序运行时异常]' in a for a in alerts), 'Alert must tag [交易程序运行时异常]'

# 2. External error test (MarketDataError)
bot._handle_loop_exception(MarketDataError('502 Bad Gateway'))
assert bot.idle_api_error_count == 1, 'idle_api_error_count must increment on external error'
assert bot.api_error_count == 1, 'api_error_count must increment on external error'

print('Tier 3 PASS: Exception classification and alert separation verified successfully!')
"
```
*Expected Result*: Prints `Tier 3 PASS: Exception classification and alert separation verified successfully!`.

### Tier 4: Single-Cycle Real-World End-to-End Simulation
Run 1 controlled cycle of `_idle_cycle()` with full pipeline execution:
```powershell
python -c "
import sys
sys.path.insert(0, r'C:\Users\Administrator\Desktop\v1\a')
import main
bot = main.TradingBot()
bot._idle_cycle()
print('Tier 4 PASS: 1 cycle executed with 0 unhandled exceptions!')
"
```
*Expected Result*:
1. Fetches market data without crashing on timestamp extraction.
2. Triggers `AI_macro` and logs recommendation.
3. Scans multi-coin router without error.
4. Completes cleanly with `Tier 4 PASS: 1 cycle executed with 0 unhandled exceptions!`.

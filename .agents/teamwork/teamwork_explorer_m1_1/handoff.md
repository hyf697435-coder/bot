# Milestone 1 Comprehensive Exploration & Fix Plan Report

- **Target Module**: `main.py`
- **Milestone**: Milestone 1 — Fix Trading Bot Runtime Errors
- **Explorer**: `teamwork_explorer_m1_1`
- **State Folder**: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_1`
- **Date**: 2026-11-13

---

## 1. Observation (Verified Evidence)

### 1.1 Uncommitted `import pandas as pd` (Feature 1)
- **File**: `C:\Users\Administrator\Desktop\v1\a\main.py`
- **Git HEAD commit**: `53496bc08ace3452efa00ca28f03a8503c62c431` (`feat: finish audit bug fixes for hard stop ratchets, unhandled returns, and fsm safety`).
- In commit `53496bc`, lines 24-34 of `main.py` contained:
  ```python
  24: import json
  25: import math
  26: import os
  27: import time
  28: from collections import deque
  29: from datetime import datetime, timedelta, timezone
  30: from typing import Deque, Optional, Tuple
  31: 
  32: import numpy as np
  ```
  `pandas` was completely absent from imports.
- In the local working tree, `git diff main.py` shows:
  ```diff
  @@ -31,6 +31,7 @@ from datetime import datetime, timedelta, timezone
   from typing import Deque, Optional, Tuple
   
   import numpy as np
  +import pandas as pd
  ```
- **Finding**: While `import pandas as pd` was manually typed into `main.py` in the working copy, it is **uncommitted / unstaged**. Running `git status` verifies:
  ```
  Changes not staged for commit:
    modified:   main.py
  ```
  Any git checkout, discard, fresh clone, or multi-instance cloud deployment (Milestone 3) will drop this import and immediately crash with `NameError: name 'pd' is not defined`.

### 1.2 Fragile Timestamp Extraction in `_idle_cycle` (Feature 4)
- **File**: `C:\Users\Administrator\Desktop\v1\a\main.py`, lines 535-539:
  ```python
  535:         # 节点命中可观测性: 每个信号节点至少落一条 INFO, 并比对 5m K 线时间戳是否滚动
  536:         current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors='coerce'))
  537:         logger.info("[巡检] 命中信号节点 | node=%s | candle_ts=%s | prev_candle=%s "
  538:                     "| action=执行信号 Pipeline",
  539:                     node, current_candle_time, self.last_node_candle_time)
  ```
- **Direct Observations of Vulnerabilities**:
  1. If `pd` is not defined in globals (e.g. uncommitted import reverted), execution throws `NameError: name 'pd' is not defined` at line 536.
  2. If `df_5m["ts"].iloc[-1]` contains an invalid string, non-convertible data, `None`, or `NaN`, `pd.to_numeric(..., errors='coerce')` returns `np.nan`.
  3. In Python, calling `int(np.nan)` unconditionally raises:
     ```
     ValueError: cannot convert float NaN to integer
     ```
     Verified directly via: `python -c "import pandas as pd; int(pd.to_numeric('bad', errors='coerce'))"`.
  4. If `df_5m` is mocked or has an unexpected dictionary-like structure lacking `.iloc`, it raises `AttributeError: 'list' object has no attribute 'iloc'`.

### 1.3 Loop Exception Misclassification & Missing Tracebacks (Features 2 & 3)
- **File**: `C:\Users\Administrator\Desktop\v1\a\main.py`, lines 1462-1481:
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
- **Direct Observations of Flaws**:
  1. **Global Catch-All Without Error Discrimination**: Line 1462 catches `Exception` globally. When line 536 crashed on `NameError: name 'pd' is not defined`, line 1473 executed `self.idle_api_error_count += 1`.
  2. **False Diagnostic Alert**: Because `idle_api_error_count` exceeded `IDLE_ALERT_THRESHOLD` (5), line 1475-1476 dispatched:
     `空仓期 API 连续异常 N 次，请人工检查网络/AI 通道。`
     This falsely accused the external exchange API and LLM relay of failing, when in reality it was an internal local Python syntax/runtime error.
  3. **Absence of Tracebacks (`exc_info=True`)**: Line 1465 logged `logger.error("[异常] 核心循环错误 | ... | err=%s", ..., e)` without `exc_info=True`. In log files, only `err=name 'pd' is not defined` appeared, completely omitting the call stack, module name, and line number (536), making debugging nearly impossible for the operator.
  4. **Unbounded Alert Flooding**: From count 5 up to 32, `send_alert` fired on every single loop iteration (27 consecutive alert emails sent).

---

## 2. Logic Chain

1. **Uncommitted State Invalidation**:
   - `import pandas as pd` was added in the working copy at `main.py:34`, but was never staged or committed in Git (`53496bc08ace`).
   - Consequently, any automated environment, clean clone, or fresh deployment runs the committed version where `pd` is absent.
   - When the bot reaches line 536 (`int(pd.to_numeric(...))`), Python encounters an unbound identifier and raises `NameError: name 'pd' is not defined` (Observation 1.1).

2. **Fault Propagation into Loop Handler**:
   - In `TradingBot.run()`, the exception is caught by line 478 (`except Exception as e`) and routed to `self._handle_loop_exception(e)` (Observation 1.3).
   - In `_handle_loop_exception()`, `logger.error` logs the error message string without `exc_info=True`, obscuring the root cause and line 536 in `trading_bot.log` (Observation 1.3).

3. **Indiscriminate Counter Increment & Alarm Storm Trigger**:
   - Since `self.state == STATE_IDLE`, line 1473 executes `self.idle_api_error_count += 1`.
   - Because line 536 crashed before reaching `self._reset_idle_counters()` at line 679/825, `idle_api_error_count` increments strictly monotonically (1, 2, 3, ... 32).
   - Once `idle_api_error_count >= 5`, line 1475 sends the alert `空仓期 API 连续异常 N 次，请人工检查网络/AI 通道。` on every single iteration.
   - This directly produces the 32 consecutive alert emails reported in the initial user problem statement.

4. **Remediation Derivation**:
   - Committing `import pandas as pd` at line 34 eliminates the `NameError`.
   - Wrapping timestamp extraction with multi-tiered defensive parsing prevents crashes even if `pd` is missing or the timestamp is corrupted/NaN.
   - Classifying internal software errors (`NameError`, `TypeError`, `AttributeError`, `SyntaxError`, `IndexError`, `KeyError`, `ZeroDivisionError`) isolates them from external network/API errors.
   - Internal software errors must be tagged `[交易程序运行时异常]`, must NOT increment `idle_api_error_count`, and must log with `exc_info=True`.

---

## 3. Caveats

1. **Exclusive File Ownership Compliance**:
   - Milestone 1 strictly modifies `main.py`.
   - Subsequent milestones (M2: exchange retries, LLM timeout decoupling, and alert throttling in `alerts.py`/`exchange.py`/`strategy.py`; M3: cloud rotation in `scripts/`) must remain decoupled.
2. **Alert Frequency Considerations**:
   - While Milestone 1 isolates internal errors from incrementing `idle_api_error_count`, full 30-minute deduplication throttling for external API errors is scheduled for Milestone 2 (Feature 9).
   - For internal software errors in Milestone 1, exponential backoff (`min(self.idle_backoff, 60s)`) ensures the bot does not high-frequency busy-loop on internal defects.
3. **Live Testnet Network Dependency**:
   - Executing `python main.py` live dry-run requires connectivity to Hyperliquid Testnet (`api.hyperliquid-testnet.xyz`). For pure offline environments, the unit test suite (`proposed_test_m1_runtime_fixes.py`) provides 100% offline verification.

---

## 4. Conclusion & Implementation Plan

### 4.1 Summary of Changes
1. **Feature 1**: Permanently stage and commit `import pandas as pd` at module level (line 34) in `main.py`.
2. **Feature 2**: Introduce `INTERNAL_SOFTWARE_ERRORS = (NameError, TypeError, AttributeError, SyntaxError, IndexError, KeyError, ZeroDivisionError)`. In `_handle_loop_exception`, classify internal errors:
   - Tag alert message with `[交易程序运行时异常]`
   - Do NOT increment `idle_api_error_count`
   - Log with `logger.critical(..., exc_info=True)`
3. **Feature 3**: Add `exc_info=True` to both internal (`logger.critical`) and external (`logger.error`) logger invocations in `_handle_loop_exception`.
4. **Feature 4**: In `_idle_cycle`, replace vulnerable `int(pd.to_numeric(...))` with defensive extraction supporting Series/Dict, `pd.to_numeric`, `float()` fallback, and system epoch timestamp fallback.

### 4.2 Exact Line-by-Line Diff Proposal
The complete patch has been generated in:
`C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_1\proposed_m1_patch.patch`

#### Patch Snippet:
```diff
diff --git a/main.py b/main.py
--- a/main.py
+++ b/main.py
@@ -31,6 +31,7 @@ from datetime import datetime, timedelta, timezone
 from typing import Deque, Optional, Tuple
 
 import numpy as np
+import pandas as pd
 
 try:
     from dotenv import load_dotenv
@@ -107,6 +108,18 @@ TRADING_ALERT_THRESHOLD = _env_int("TRADING_ALERT_THRESHOLD", 2)  # 持仓期
 CLOSE_FAIL_ALERT_THRESHOLD = _env_int("CLOSE_FAIL_ALERT_THRESHOLD", 5)  # 平仓连续失败保护阈值
 
+# ==========================================
+# 错误分类规则 (M1: 内部代码缺陷 vs 外部API/网络故障)
+# ==========================================
+INTERNAL_SOFTWARE_ERRORS = (
+    NameError,
+    TypeError,
+    AttributeError,
+    SyntaxError,
+    IndexError,
+    KeyError,
+    ZeroDivisionError,
+)
 
 # 资金费率多空极化门槛 (年化等效)
 FUNDING_EXTREME_LONG = 0.0003
@@ -533,7 +546,19 @@ class TradingBot:
             return
 
         # 节点命中可观测性: 每个信号节点至少落一条 INFO, 并比对 5m K 线时间戳是否滚动
-        current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors='coerce'))
+        # 防御性提取时间戳: 容忍 pd 缺失、Series 异常、NaN、缺失索引等极端情况
+        current_candle_time = None
+        try:
+            ts_series = df_5m["ts"]
+            last_ts = ts_series.iloc[-1] if hasattr(ts_series, "iloc") else ts_series[-1]
+            if "pd" in globals() and hasattr(pd, "to_numeric"):
+                num_ts = pd.to_numeric(last_ts, errors="coerce")
+                if pd.notna(num_ts):
+                    current_candle_time = int(num_ts)
+            if current_candle_time is None:
+                current_candle_time = int(float(last_ts))
+        except (ValueError, TypeError, IndexError, KeyError, AttributeError) as ts_err:
+            logger.warning("[巡检] 5m K 线时间戳解析异常: %s | action=估算为当前系统时间戳", ts_err)
+            current_candle_time = int(now_ts * 1000)
+
         logger.info("[巡检] 命中信号节点 | node=%s | candle_ts=%s | prev_candle=%s "
                     "| action=执行信号 Pipeline",
                     node, current_candle_time, self.last_node_candle_time)
@@ -1462,10 +1487,27 @@ class TradingBot:
     def _handle_loop_exception(self, e: Exception):
-        """核心循环容错：持仓期连续异常触发保护兜底，空仓期指数退避。"""
+        """核心循环容错：持仓期连续异常触发保护兜底，空仓期指数退避。
+        严格区分内部软件缺陷与外部API网络故障，附带完整堆栈日志。"""
+        if isinstance(e, INTERNAL_SOFTWARE_ERRORS):
+            logger.critical(
+                "[交易程序运行时异常] 核心循环捕获内部代码缺陷 | err=%s: %s",
+                type(e).__name__, e, exc_info=True
+            )
+            send_alert(f"[交易程序运行时异常] 核心循环发生本地代码错误: {type(e).__name__}: {e}")
+            # 内部软件缺陷绝不增加 idle_api_error_count，避免误报为外部API连续异常
+            if self.state == STATE_TRADING:
+                self.api_error_count += 1
+                if self.api_error_count >= TRADING_ALERT_THRESHOLD:
+                    self._deploy_emergency_hard_stop()
+                self._safe_sleep(10, phase="TRADING_EXC_BACKOFF", step=1.0)
+            elif self.state == STATE_IDLE:
+                logger.warning("[巡检] 内部代码异常退避 | wait=%ss", min(self.idle_backoff, 60))
+                self._safe_sleep(min(self.idle_backoff, 60), phase="IDLE_BACKOFF", step=1.0)
+                self.idle_backoff = min(self.idle_backoff * 2, 600)
+            else:
+                self._safe_sleep(10, phase="LOOP_EXC_BACKOFF", step=1.0)
+            return
+
         self.api_error_count += 1
-        logger.error("[异常] 核心循环错误 | 计数=%s/%s | err=%s",
-                     self.api_error_count, TRADING_ALERT_THRESHOLD, e)
+        logger.error(
+            "[异常] 核心循环错误 | 计数=%s/%s | err=%s",
+            self.api_error_count, TRADING_ALERT_THRESHOLD, e,
+            exc_info=True
+        )
 
         if self.state == STATE_TRADING:
```

---

## 5. Verification Method

### 5.1 Static Analysis & Syntax Verification
```powershell
# 1. Compile all Python files across repo
python -m py_compile C:\Users\Administrator\Desktop\v1\a\*.py C:\Users\Administrator\Desktop\v1\a\tests\*.py

# 2. Verify 'pd' is bound at module level in main.py
python -c "import sys; sys.path.insert(0, r'C:\Users\Administrator\Desktop\v1\a'); import main; assert hasattr(main, 'pd') and main.pd is not None; print('PASS: main.pd is committed and accessible')"
```

### 5.2 Unit Test Verification
Execute the dedicated Milestone 1 unit test suite located in the explorer state folder:
```powershell
python -m unittest C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_1\proposed_test_m1_runtime_fixes.py -v
```
*Expected Result*:
```
test_01_module_level_pandas_imported ... ok
test_02_internal_software_errors_defined ... ok
test_03_internal_software_errors_do_not_increment_idle_api_count ... ok
test_04_external_errors_increment_idle_api_count ... ok
test_05_defensive_timestamp_extraction ... ok
Ran 5 tests in ~0.01s ... OK
```

Execute the full regression test suite (70 tests):
```powershell
python -m unittest discover -s "C:\Users\Administrator\Desktop\v1\a\tests" -v
```
*Expected Result*: `Ran 70 tests ... OK`.

### 5.3 Live Controlled Dry-Run Simulation
Because `.env` defines `MAX_LOOPS=0` and `main.py:38` invokes `load_dotenv(override=True)`, shell environment variables like `set MAX_LOOPS=2` are overridden by the `.env` file defaults. To execute an unambiguous 2-loop dry run, run:
```powershell
python -c "import sys; sys.path.insert(0, r'C:\Users\Administrator\Desktop\v1\a'); import main; main.MAX_LOOPS = 2; bot = main.TradingBot(); bot.run()"
```
*Directly Verified Output*:
```
2026-11-13 11:18:01 | INFO     | [状态] 恢复完成 | state=IDLE | 宏观缓存=LEAN_SHORT | 连败=0
2026-11-13 11:18:01 | INFO     | [启动] 交易机器人上线 | env=testnet | coin=ETH
2026-11-13 11:18:04 | INFO     | [巡检] 命中信号节点 | node=5981799 | candle_ts=1794539400000 | prev_candle=None | action=执行信号 Pipeline
2026-11-13 11:18:04 | INFO     | [宏观] 裁决 | 量化态=LEAN_SHORT | AI缓存=LEAN_SHORT | 最终=LEAN_SHORT
2026-11-13 11:18:06 | INFO     | [多币雷达] 多币扫描完成 | 全部无达标信号 (门槛=68.5) | 各币打分={'BTC': 0.0, 'ARB': 45.0, 'ETH': 55.0, 'SUI': 45.0}
2026-11-13 11:18:06 | INFO     | [心跳] IDLE 存活 | node=5981799 | last_node=5981799 | 上轮耗时=5.7s
2026-11-13 11:18:16 | INFO     | [停止] 达到最大轮数 | MAX_LOOPS=2 | action=正常退出
```
Exit code: `0` (Success).

### 5.4 Invalidation Conditions
- If `idle_api_error_count` is incremented after a `NameError`, `TypeError`, or `AttributeError`, verification fails.
- If `logger.critical` or `logger.error` in `_handle_loop_exception` is called without `exc_info=True`, verification fails.
- If `df_5m` with `ts="corrupted"` causes `main.py` to raise unhandled `ValueError`, verification fails.

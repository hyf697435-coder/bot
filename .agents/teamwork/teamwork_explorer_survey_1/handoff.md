# R1 Codebase Architecture Survey & Runtime Bug Investigation Report

- **Surveyor**: `teamwork_explorer_survey_1`
- **Scope**: Codebase architecture mapping, runtime bug diagnosis (R1 Focus: `name 'pd' is not defined`), syntax/import audit, execution paths, dependencies, and simulation procedures.
- **Target Working Directory**: `C:\Users\Administrator\Desktop\v1\a`
- **State Directory**: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_survey_1`

---

## 1. Observation (Verified Evidence)

### 1.1 Architecture & File Structure Mapping

The repository `C:\Users\Administrator\Desktop\v1\a` is a production-grade algorithmic trading bot engineered for Hyperliquid perpetual futures, integrating local quantitative signals (Trend Resonance Score / TRS) with an AI gateway (AI_macro and AI_Trade) and an FSM (Finite State Machine) execution engine.

#### Core Modules Directory Layout:
| File / Directory | Size | Role & Architecture Function |
|---|---|---|
| `main.py` | 100.8 KB | **Main Controller & Pipeline Workflow**: Orchestrates FSM states (`IDLE`, `TRADING`, `COOL_DOWN`, `SYSTEM_HALT`), 5m+2s signal node alignment, heartbeat, and state persistence. |
| `strategy.py` | 62.9 KB | **Strategy & Signal Scoring**: Dual-agent prompt builders, local indicator scoring (TRS, 5m fractal breakout, ATR, BB, RSI, CCI), and `update_position_fsm()`. |
| `exchange.py` | 55.7 KB | **Hyperliquid Interaction Layer**: Market data fetching (`/info`), atomic order execution (`/exchange`), two-phase trigger stops (AI SL, hard SL ratchet, TP), and Android cURL bridge. |
| `risk_manager.py` | 37.1 KB | **Risk Governance Engine**: Continuous Kelly sizing, multi-coin portfolio heat budget, drawdown circuit breakers, consecutive loss streak cooling, and TOCTOU concurrency lock. |
| `multi_coin_router.py` | 24.1 KB | **Multi-Coin Rotation Dispatcher**: Parallel scanner across BTC, ETH, SUI, ARB; correlation penalty tracker; dynamic coin switching. |
| `sentiment_feed.py` | 7.0 KB | **Sentiment Ingestion**: Cointelegraph RSS + Alternative.me Crypto Fear & Greed Index caching for `AI_macro`. |
| `alerts.py` | 15.1 KB | **Alerting & Logging Foundation**: Async daemon thread mailer (SMTP over SSL) + rotating weekly log handler (`WeeklyResetFileHandler`). |
| `watchdog.py` | 6.1 KB | **Process Supervisor**: Subprocess watcher monitoring `.heartbeat` (150s timeout), emergency non-trigger order cancellation, and crash auto-healing. |
| `dashboard.py` | 80.0 KB | **Web Monitoring Service**: Port 8080 HTTP server delivering real-time charts, state dumps, radar signals, and manual override controls. |
| `utils.py` | 20.4 KB | **Vectorized Math & Technical Indicators**: Wilder RMA ATR, EMA, SMA, Welford variance, and candle timing validators. |
| `optimizer.py` / `download_data.py` | 9.3 KB / 4.4 KB | Backtesting and historical data downloading utilities. |
| `tests/` | 75+ KB | Unit test suite (`test_suite.py`, `test_audit_hardening.py`, `test_dedup_bugs_fixes.py`, `test_sentiment.py`). |

#### Configuration & State Files:
- `.env`: Environment variables covering credentials (`HYPE_USER_ADDRESS`, `HYPE_SECRET_KEY`, `HYPE_API_WALLET_ADDRESS`), endpoints, timeouts, and `MAINTENANCE_LOCK=false`.
- `config_aggressive.json`: Strategy parameters (Kelly fraction 0.20, single risk 2.0%, trailing ATR multiplier 2.35, max holding bars 45, correlation threshold 0.70).
- `state.json`: FSM recovery snapshot (`state`, `position`, `cached_ai_macro_state`, `consecutive_failures`, `last_signal_node`).
- `risk_state_aggressive.json`: Risk manager state (`daily_loss_pnl`, `peak_equity`, `streak_count`, trade logs).
- `.heartbeat`: Heartbeat timestamp and active phase (`FETCH_MARKET`, `AI_MACRO`, `AI_TRADE`, `IDLE_POLL`, etc.).

#### Log Files:
- Default configured path: Resolved by `alerts.py:_resolve_log_path()` to `C:\Users\Administrator\Desktop\v1\log\trading_bot.log`.
- Historical archive: `C:\Users\Administrator\Desktop\v1\log\trading_bot.log.2026-W45` (1.91 MB) produced by `WeeklyResetFileHandler` on weekly boundary rollover.

---

### 1.2 Root Cause of `name 'pd' is not defined`

#### Verbatim Code Evidence:
In `C:\Users\Administrator\Desktop\v1\a\main.py`, line 536 inside `TradingBot._idle_cycle()`:
```python
535:         # 节点命中可观测性: 每个信号节点至少落一条 INFO, 并比对 5m K 线时间戳是否滚动
536:         current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors='coerce'))
537:         logger.info("[巡检] 命中信号节点 | node=%s | candle_ts=%s | prev_candle=%s "
538:                     "| action=执行信号 Pipeline",
539:                     node, current_candle_time, self.last_node_candle_time)
```

#### Git Commit Discrepancy:
In commit `53496bc08ace3452efa00ca28f03a8503c62c431` (and `b3541ae`), the module imports at lines 24-34 of `main.py` were:
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
# NOTE: 'import pandas as pd' was completely absent!
```
When `git diff main.py` is inspected in the local working copy:
```diff
--- a/main.py
+++ b/main.py
@@ -31,6 +31,7 @@ from datetime import datetime, timedelta, timezone
 from typing import Deque, Optional, Tuple
 
 import numpy as np
+import pandas as pd
```
- In the Git repository commit history (`53496bc`), `import pandas as pd` was never committed into `main.py`.
- In the local working tree, `import pandas as pd` was added as an **uncommitted / unstaged modification**.
- Because this change was uncommitted, any checkout, reset, fresh clone, or alternative instance deployment (e.g., free cloud servers in R3) executing `main.py` will crash at line 536 with `NameError: name 'pd' is not defined`.

---

### 1.3 Exception Routing and the "32次连续异常" Mechanism

#### Verbatim Loop & Exception Handler in `main.py`:
In `TradingBot.run()` lines 398-480:
```python
398:             try:
...
432:                 if self.state == STATE_IDLE:
...
446:                     self._idle_cycle()
...
478:             except Exception as e:
479:                 self._handle_loop_exception(e)
```

In `TradingBot._handle_loop_exception(e)` lines 1462-1481:
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
```

#### Diagnostic Breakdown:
1. **Misclassification of Internal Python Errors**: `_handle_loop_exception` catches `Exception` globally and unconditionally classifies every error during `STATE_IDLE` as an "API 异常" (`空仓期 API 连续异常 N 次`). A local `NameError: name 'pd' is not defined` is falsely broadcast to the user as a network/AI failure.
2. **Missing Traceback in Logs**: `logger.error` at line 1465 did not pass `exc_info=True`, masking the exact traceback and line number in log files.
3. **Counter Never Resets**: In `_idle_cycle()`, `self._reset_idle_counters()` is only called at the successful completion of an iteration (lines 680, 687, 715, 720, 727, 755, 778). Because line 536 threw before reaching line 680, `idle_api_error_count` increments monotonically: 1, 2, 3, ... 32.
4. **Alarm Storm**: Once `idle_api_error_count >= IDLE_ALERT_THRESHOLD` (5), `send_alert` fires on **every single loop iteration**, flooding the user's email inbox 27 consecutive times up to count 32.

---

### 1.4 Codebase-Wide Syntax, Import, and Symbol Audit

1. **Python Syntax Compilation (`py_compile`)**:
   Command: `python -m py_compile` executed on all 12 root modules and all 4 test modules.
   Result: **100% Pass** (No syntax errors detected across any file).
2. **Standard Library `symtable` Symbol Analysis**:
   - `main.py`: No unresolved symbols in local working copy; verified that without line 34 `import pandas as pd`, `pd` is unresolved.
   - `utils.py`, `risk_manager.py`, `watchdog.py`, `download_data.py`, `optimizer.py`: Zero unresolved symbols.
   - `alerts.py`, `dashboard.py`, `exchange.py`, `multi_coin_router.py`, `strategy.py`: All inner closures (`_eval`, `_CurlBridgeHandler`) and generator expressions (`prefix`, `breakdown`) correctly resolve within their respective lexical scopes.
3. **Unit Test Suite**:
   Command: `python -m unittest discover -s C:\Users\Administrator\Desktop\v1\a\tests -v`
   Result: **70 tests ran in 61.1s, 0 failures, 0 errors (OK)**.

---

## 2. Logic Chain

```
[Git Commit 53496bc]
  └─ 'main.py' contains 'pd.to_numeric(df_5m["ts"].iloc[-1], errors="coerce")' at line 536
  └─ Top of 'main.py' imports 'numpy as np' but OMITS 'import pandas as pd'
       │
       ▼
[Runtime Execution in STATE_IDLE]
  └─ Bot enters 'TradingBot.run()' -> calls 'self._idle_cycle()' at 5m signal node
  └─ Executes line 536 -> Python cannot resolve 'pd'
  └─ Raises 'NameError: name "pd" is not defined'
       │
       ▼
[Exception Bubbling to Loop Handler]
  └─ Caught by 'main.py:478' -> 'self._handle_loop_exception(e)'
  └─ 'logger.error' logs error string without exc_info=True (hiding traceback)
  └─ Since state == STATE_IDLE, increments 'self.idle_api_error_count += 1'
  └─ 'self._reset_idle_counters()' at end of _idle_cycle is never reached
       │
       ▼
[Alarm Storm Generation]
  └─ Loop sleeps for backoff period (10s -> 20s -> 40s -> ... -> 600s)
  └─ On next iteration, hits line 536 again -> raises NameError again
  └─ When count >= 5, fires 'send_alert("空仓期 API 连续异常 N 次...")' every cycle
  └─ Repeated 32 times -> produces the exact "空仓期api连续异常32次" alarm loop reported by user
```

---

## 3. Caveats

1. **Uncommitted Working Copy vs Version Control**: `import pandas as pd` has been patched locally in the working copy of `main.py`, but Git still marks `main.py` as `modified`. If an automated script or operator runs `git restore main.py` or clones the repository anew onto a cloud instance, the `NameError` will reoccur.
2. **Scope Boundary**: This report focuses on R1 (codebase mapping, import/syntax/unhandled exception audit, simulation procedures). In-depth investigation of LLM relay API vs Exchange API timeout separation (R2) and cloud instance failover rotation (R3) are assigned to peer surveys `survey_2` and `survey_3`.
3. **Live Testnet Call In Dry-Run**: When executing `python main.py` with `MAX_LOOPS=2` for live dry-run, access to Hyperliquid Testnet (`api.hyperliquid-testnet.xyz`) requires outbound internet / proxy access. If the network interface is restricted, unit tests (`test_suite.py`) provide 100% offline coverage.

---

## 4. Conclusion & Recommended Fix Strategy for R1

### 4.1 Root Cause Summary
1. **Primary Bug (R1)**: Missing `import pandas as pd` in `main.py` at the module level in committed HEAD.
2. **Secondary Amplifiers**:
   - Lack of error differentiation in `_handle_loop_exception`: Python software bugs (`NameError`, `TypeError`, `ValueError`) were conflated with external API failures.
   - Missing `exc_info=True` in `_handle_loop_exception`: concealed the stack trace from logs.
   - Linear un-throttled alert sending: sent an email alert on every single loop iteration after threshold 5 without throttling.

### 4.2 Recommended Fix Strategy (for Implementer)
1. **Commit & Harden Import in `main.py`**:
   - Ensure `import pandas as pd` is permanently committed at line 34 of `main.py`.
   - Optionally add defensive timestamp extraction:
     `current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors='coerce')) if "pd" in globals() else int(float(df_5m["ts"].iloc[-1]))`.
2. **Differentiate Software Faults vs API Exceptions in `_handle_loop_exception`**:
   - Check exception type:
     ```python
     if isinstance(e, (NameError, TypeError, AttributeError, SyntaxError, IndexError, KeyError, ZeroDivisionError)):
         logger.critical("[系统软件故障] 核心循环本地代码异常: %s", e, exc_info=True)
         send_alert(f"[软件缺陷/内部异常] 核心循环发生本地代码错误: {type(e).__name__}: {e}")
     elif isinstance(e, (requests.RequestException, httpx.HTTPError, MarketDataError, ExecutionError)):
         # Handle legitimate API network failures with backoff
     ```
3. **Implement Alert Throttling in `_handle_loop_exception`**:
   - Suppress repeated identical alerts: alert on threshold (5), then only on exponential milestones (e.g. 10, 20, 50) or once every 30 minutes, preventing 32-email alarm storms.
4. **Log Full Tracebacks**:
   - Change `logger.error("[异常] 核心循环错误 | 计数=%s/%s | err=%s", ..., e)` to include `exc_info=True`.

---

## 5. Verification Method

To independently reproduce and verify the findings:

### 5.1 Verification of `pd` Import & Syntax
```powershell
# 1. Check all modules compile cleanly
python -m py_compile C:\Users\Administrator\Desktop\v1\a\*.py C:\Users\Administrator\Desktop\v1\a\tests\*.py

# 2. Check for undefined 'pd' or unresolved globals using Python's symtable
python -c "import symtable, glob, os; [print(os.path.basename(f), 'pd in globals:', 'pd' in symtable.symtable(open(f, encoding='utf-8').read(), f, 'exec').get_identifiers()) for f in glob.glob(r'C:\Users\Administrator\Desktop\v1\a\*.py') if 'pd' in open(f, encoding='utf-8').read()]"
```

### 5.2 Verification of Full Test Suite
```powershell
# Execute all 70 unit tests (tests mocked trading, FSM, and ratchets)
python -m unittest discover -s "C:\Users\Administrator\Desktop\v1\a\tests" -v
```
*Expected Result*: `Ran 70 tests in ~61s ... OK`.

### 5.3 Verification of Controlled Dry-Run / Simulation
```powershell
# Run 2 safe iterations in fail-closed simulation mode with loop exit guard
cmd /c "set MAX_LOOPS=2 && set MAINTENANCE_LOCK=false && python C:\Users\Administrator\Desktop\v1\a\main.py"
```
*Expected Result*: Bot initializes channels, performs IDLE cycle, calculates indicators without crashing on `pd`, logs `[停止] 达到最大轮数 | MAX_LOOPS=2 | action=正常退出`, and terminates cleanly with exit code 0.

# Handoff Report: E2E Test Architecture & Tier 1 Feature Coverage

## 1. Observation

### 1.1 Codebase & Entry Points Inspected
Direct inspection of the codebase in `C:\Users\Administrator\Desktop\v1\a` revealed the following key files, line numbers, and runtime mechanisms:

1. **`main.py` Runtime Error Handling and Loop**:
   - Lines 34: `import pandas as pd` is present at line 34, but historically logs reported `NameError: name 'pd' is not defined`, suggesting missing or conditional imports in certain runtime code paths or uncommitted revisions.
   - Lines 525-528: `_idle_cycle()` calls:
     ```python
     df_5m, price = self.market.get_closed_ohlcv("5m", 120, use_cache=False)
     df_1h = self.market.get_ohlcv("1h", 500)
     df_4h = self.market.get_ohlcv("4h", 500)
     fr_stats = self.market.get_funding_stats(FR_WINDOW_HOURS)
     ```
     Any unhandled exception from these calls bubbles out of `_idle_cycle()` into `_handle_loop_exception()`.
   - Line 536: `current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors='coerce'))`. If `df_5m["ts"]` is malformed or NaN, defensive type casting is required to prevent `ValueError: cannot convert float NaN to integer`.
   - Lines 1462-1481 (`_handle_loop_exception`):
     ```python
     def _handle_loop_exception(self, e: Exception):
         self.api_error_count += 1
         logger.error("[异常] 核心循环错误 | 计数=%s/%s | err=%s",
                      self.api_error_count, TRADING_ALERT_THRESHOLD, e)
         ...
         elif self.state == STATE_IDLE:
             self.idle_api_error_count += 1
             if self.idle_api_error_count >= IDLE_ALERT_THRESHOLD:
                 send_alert(
                     f"空仓期 API 连续异常 {self.idle_api_error_count} 次，请人工检查网络/AI 通道。")
             logger.warning("[巡检] 空仓异常退避 | wait=%ss", self.idle_backoff)
             self._safe_sleep(self.idle_backoff, phase="IDLE_BACKOFF")
             self.idle_backoff = min(self.idle_backoff * 2, 600)
     ```
     Observations on `_handle_loop_exception`:
     - `logger.error` does NOT use `exc_info=True`, causing traceback loss in production logs.
     - The alert condition `self.idle_api_error_count >= IDLE_ALERT_THRESHOLD` triggers on *every single iteration* once count >= 5 (hence "32 consecutive exceptions" sent 28 email alarms).
     - No distinction is made between internal Python runtime bugs (`NameError`, `TypeError`, `AttributeError`) and external network errors.
     - The error message text lumps network and AI together: `"请人工检查网络/AI 通道"`.
   - Lines 822-829: `_reset_idle_counters()` was only called at the very end of `_idle_cycle()`. In-between short polls (lines 498-500) that returned early never cleared `idle_api_error_count`.

2. **`exchange.py` Network Call Sites**:
   - Lines 180-188: `resolve_base_url()` supports `HYPE_BASE_URL` environment override:
     ```python
     target = os.getenv("HYPE_BASE_URL", "").strip() or by_env.get(env, by_env["testnet"])
     ```
   - Lines 203-288: `MarketData` uses `httpx.Client.post(self.base_url + "/info", json=payload, timeout=15)`.
   - Lines 290-327: `get_closed_ohlcv` strictly checks candle closure timestamps.
   - Lines 332-345: `mark_price()` posts `{"type": "allMids"}` to `/info`.
   - Lines 350-405: `get_funding_stats()` posts `{"type": "fundingHistory", ...}` to `/info`.
   - Lines 415-430: `HyperliquidExecutor` manages signed orders (`/exchange`) via the official SDK (`hyperliquid.info.Info` and `hyperliquid.exchange.Exchange`) with fail-closed semantics.

3. **`strategy.py` LLM Relay Call Sites**:
   - Lines 124-129:
     ```python
     base = os.getenv("THIRD_PARTY_BASE_URL", "").rstrip("/")
     if not base or base == "https://api.lu" or not base.startswith("http"):
         base = "https://api.ludies.best/v1"
     ```
   - Lines 203-206: `resp = httpx.post(url, json=payload, headers=headers, timeout=eff_timeout)` where `url = base_url + "/chat/completions"`.
   - Lines 234-235: `notifier.record_model_error(model, str(e))` is invoked whenever any model in the fallback chain fails.
   - Lines 645-648: In `fetch_ai_macro_state`:
     ```python
     except AIError as e:
         logger.warning("[宏观] AI_macro 调用失败 | action=沿用本地量化态 | err=%s", e)
         send_alert(f"AI_macro 调用失败，已沿用本地量化趋势态: {e}")
     ```
     This sends an alert on every single macro failure during flat positions, violating the requirement to decouple and suppress non-critical timeouts.

4. **`alerts.py` SMTP Mailer Call Sites**:
   - Lines 212-240: `Mailer` runs an asynchronous background daemon thread consuming `self._queue`.
   - Lines 262-270: Connects via `smtplib.SMTP_SSL` (or `smtplib.SMTP`) and calls `server.sendmail(...)`.
   - Lines 194-206: `send_alert(message)` writes `logger.warning("[告警] %s", message)` and puts the message into `notifier.send("[交易机器人] 系统告警", message)`.

5. **Existing Test Execution**:
   - Command executed: `python -m unittest discover -s "C:\Users\Administrator\Desktop\v1\a\tests" -p "test_*.py" -t "C:\Users\Administrator\Desktop\v1\a"`
   - Result: `Ran 70 tests in 45.334s, OK`. Existing unit and regression tests pass cleanly on Python 3.12.10.

---

## 2. Logic Chain & Test Architecture Design

### 2.1 Opaque-Box Isolation Without Live Network or Exchange Funds

In opaque-box testing, the test treats the application as a black box, verifying inputs, actions, and externally observable outputs (logs, alert queues, state files, process exit codes) without modifying production code during the test run.

To ensure deterministic, zero-cost, zero-fund test execution:

#### 1. Hyperliquid REST API (`/info` & `/exchange`) Isolation
- **Mechanism**:
  - `exchange.py` resolves its target endpoint via `resolve_base_url()` which checks `os.getenv("HYPE_BASE_URL")`.
  - For tests requiring HTTP wire simulation: An ephemeral in-process `http.server.HTTPServer` on `127.0.0.1:<free_port>` is launched in a daemon thread. `os.environ["HYPE_BASE_URL"] = f"http://127.0.0.1:{port}"`.
  - For standard unittest harness: An opaque mock adapter intercepts `httpx.Client.post` and `requests.Session.send` for URLs matching `*/info` and `*/exchange`.
  - **Deterministic Payloads Provided by Mock Hyperliquid**:
    - `candleSnapshot`: List of OHLCV dictionaries `[{"t": timestamp, "o": 2200.0, "h": 2220.0, "l": 2190.0, "c": 2210.0, "v": 1500.0}, ...]`.
    - `allMids`: `{"ETH": "2250.0", "BTC": "76000.0", "SUI": "0.85", "ARB": "0.55"}`.
    - `fundingHistory`: List of funding events `[{"time": timestamp, "fundingRate": "0.0000125"}, ...]`.
    - `/exchange`: Order ack response `{"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 99999}}]}}}`.
  - **Fault Injection Capabilities**:
    - Return HTTP 500 / 502 / 504.
    - Raise `httpx.ConnectTimeout` or `httpx.ReadTimeout`.
    - Return empty list `[]` or corrupted JSON.
    - Inject rate-limit HTTP 429.

#### 2. LLM Relay API (`api.ludies.best`) Isolation
- **Mechanism**:
  - `strategy.py` checks `os.getenv("THIRD_PARTY_BASE_URL")`.
  - Set `THIRD_PARTY_BASE_URL = "http://127.0.0.1:<mock_port>/v1"` or mock `httpx.post` when `url.endswith("/chat/completions")`.
  - **Deterministic Payloads**:
    - AI_macro valid response: `{"choices": [{"message": {"content": "{\"suggested_state\": \"NEUTRAL\", \"reason\": \"Local TRS balanced\", \"news_impact_analysis\": \"None\"}"}}]}`.
    - AI_Trade valid response: `{"choices": [{"message": {"content": "{\"decision\": \"HOLD\", \"confidence\": 0.85, \"reason\": \"Wait for breakout\"}"}}]}`.
  - **Fault Injection Capabilities**:
    - Raise `httpx.TimeoutException` to simulate relay proxy timeout.
    - Return HTTP 504 Gateway Timeout or HTTP 502 Bad Gateway.
    - Return malformed JSON or markdown text without valid JSON block.
    - Return invalid state string e.g. `{"suggested_state": "INVALID_STATE"}`.

#### 3. SMTP Mailer Isolation
- **Mechanism**:
  - In `alerts.py`, `Mailer` sends via `smtplib.SMTP_SSL` or `smtplib.SMTP`.
  - Configure `os.environ["MAIL_ENABLED"] = "true"`, `SMTP_USER="test@test.com"`, `SMTP_PASSWORD="test_password"`, `MAIL_TO="alert@test.com"`.
  - Provide a test fixture `MockMailbox`:
    - Intercepts `smtplib.SMTP_SSL` / `smtplib.SMTP` (or `alerts.notifier._queue` and `_deliver`) so no actual network connections are attempted.
    - Stores all dispatched emails in `MockMailbox.sent_messages: List[Dict[str, str]]` (with fields: `timestamp`, `subject`, `body`, `from`, `to`).
  - **Verification Method**:
    - Tests assert exact counts: e.g. `len(mock_mailbox.sent_messages) == 1` when 32 errors occur (throttling verified).
    - Tests inspect `msg["subject"]` and `msg["body"]` for required disambiguation tags: `[交易程序运行时异常]`, `[交易所行情异常]`, `[AI中转站异常]`, `[交易所下单执行异常]`.

#### 4. Filesystem & Sandbox Isolation
- **Mechanism**:
  - Use `tempfile.TemporaryDirectory()` for all state files (`state.json`, `risk_state_aggressive.json`, `equity_history_*.json`, `control.json`, `.heartbeat`).
  - Set `STATE_FILE` environment variable to target the isolated test directory.
  - Teardown cleanly removes all test artifacts, preventing side effects on production data.

---

### 2.2 Tier 1: Feature Coverage Test Specifications

Each feature area (R1, R2, R3) contains at least 5 fully specified test cases.

```
┌────────────────────────────────────────────────────────────────────────┐
│                   TIER 1 TEST SUITE SPECIFICATION                      │
├──────────────────┬─────────────────────────────────────────────────────┤
│ Feature Area R1  │ 6 Test Cases (Runtime Stability & Classification)  │
│ Feature Area R2  │ 7 Test Cases (API Alert Decoupling & Throttling)    │
│ Feature Area R3  │ 7 Test Cases (24/7 Cloud Rotation & State Sync)     │
│ Total Tier 1     │ 20 Test Cases (All >= 5 per area)                   │
└──────────────────┴─────────────────────────────────────────────────────┘
```

#### R1: Bot Runtime Stability & Software Exception Classification

##### Test Case R1-1: Module-Level Pandas Availability and Import Integrity
- **Name**: `test_r1_pandas_import_and_module_presence`
- **Input**: Clean Python environment without preloaded modified globals; `main.py` source module.
- **Action**: Dynamically import `main` and inspect module attributes.
- **Expected Opaque-Box Verification Check**:
  - `hasattr(main, "pd")` is `True`.
  - `main.pd.__name__ == "pandas"`.
  - Calling `main.pd.Series([1.0, 2.0]).mean()` returns `1.5` without `NameError`.

##### Test Case R1-2: Defensive Timestamp Parsing Under Malformed/Non-Numeric Series
- **Name**: `test_r1_defensive_timestamp_extraction_resilience`
- **Input**: Synthetic `df_5m` DataFrame containing edge-case `ts` column values:
  1. Valid millisecond int: `1731456000000`
  2. String numeric: `"1731456000000"`
  3. Floating point with fraction: `1731456000000.5`
  4. Missing/NaN value: `float('nan')`
  5. Corrupted string: `"MALFORMED_TIMESTAMP"`
- **Action**: Pass each DataFrame variant to the timestamp extraction logic in `_idle_cycle()`.
- **Expected Opaque-Box Verification Check**:
  - Case 1, 2, 3: Successfully parsed to exact integer timestamp without exception.
  - Case 4, 5: Safely detected; logs warning `[巡检] 5m K 线数据不足` or skips cycle; does NOT crash with unhandled `ValueError` or `TypeError`.

##### Test Case R1-3: Software Exception Classification and Disambiguation Tag
- **Name**: `test_r1_software_exception_classification_tag`
- **Input**: Bot instance in `STATE_IDLE`. Inject software exceptions into `_handle_loop_exception`:
  1. `NameError("name 'pd' is not defined")`
  2. `TypeError("unsupported operand type(s) for +: 'NoneType' and 'int'")`
  3. `AttributeError("'NoneType' object has no attribute 'get'")`
  4. `KeyError("missing_candle_key")`
  5. `ZeroDivisionError("division by zero")`
- **Action**: Invoke `bot._handle_loop_exception(exc)` for each error type.
- **Expected Opaque-Box Verification Check**:
  - Dispatched alert message/subject contains the exact prefix tag: `[交易程序运行时异常]`.
  - The alert does NOT contain `[交易所行情异常]` or `[AI中转站异常]`.
  - `MockMailbox` captures the alert with priority HIGH / CRITICAL.

##### Test Case R1-4: Full Traceback Logging Preservation in Exception Handler
- **Name**: `test_r1_traceback_preservation_in_critical_log`
- **Input**: Bot instance with a monitored log stream handler attached to `logging.getLogger("trading_bot")`.
- **Action**: Trigger a nested exception inside a simulated loop cycle and invoke `_handle_loop_exception(e)`.
- **Expected Opaque-Box Verification Check**:
  - The log stream contains full multiline traceback lines:
    - `Traceback (most recent call last):`
    - Filename and line number of the raised exception
    - Exception class name and description
  - Confirms `exc_info=True` was used in `logger.critical` / `logger.error`.

##### Test Case R1-5: Clean Simulation Multi-Iteration Core Loop Execution
- **Name**: `test_r1_clean_simulation_core_loop_execution`
- **Input**: Bot instantiated in temporary test directory; mock Hyperliquid `/info` returning valid 5m/1h/4h OHLCV candles; mock AI returning valid `MacroState.NEUTRAL`; mock exchange with zero position (`state = "IDLE"`).
- **Action**: Execute 5 consecutive iterations of the core loop (`bot._idle_cycle()`).
- **Expected Opaque-Box Verification Check**:
  - `bot.state` remains `STATE_IDLE`.
  - `bot.api_error_count == 0` and `bot.idle_api_error_count == 0`.
  - Valid `.heartbeat` file is written with updated timestamp and phase `FETCH_MARKET` or `AI_MACRO`.
  - Zero unhandled exceptions raised during execution.

##### Test Case R1-6: Static Syntax and AST Integrity Across Entire Project
- **Name**: `test_r1_static_syntax_and_import_integrity`
- **Input**: Source file list: `main.py`, `exchange.py`, `strategy.py`, `risk_manager.py`, `alerts.py`, `watchdog.py`, `dashboard.py`.
- **Action**: Parse each file using `ast.parse()` and compile using `compile(source, filename, 'exec')`.
- **Expected Opaque-Box Verification Check**:
  - 100% of files parse into valid AST trees.
  - 0 syntax errors (`SyntaxError`), 0 indentation errors (`IndentationError`).

---

#### R2: API Alert Decoupling & Disambiguation

##### Test Case R2-1: Exchange MarketData Resilient Retry with Exponential Backoff
- **Name**: `test_r2_exchange_marketdata_retry_and_backoff`
- **Input**: `MarketData` client. Mock Hyperliquid `/info` endpoint configured with a transient failure sequence:
  - Attempt 1: HTTP 502 Bad Gateway
  - Attempt 2: `httpx.ConnectTimeout("Connection timed out")`
  - Attempt 3: HTTP 200 with valid 120-bar 5m OHLCV JSON
- **Action**: Call `market_data.get_closed_ohlcv("5m", 120, use_cache=False)`.
- **Expected Opaque-Box Verification Check**:
  - Call succeeds and returns `(df_5m, latest_price)`.
  - Total HTTP POST calls executed against `/info` equals exactly 3.
  - Log records show 2 retry warnings with backoff delays before final success.
  - No `MarketDataError` is raised.

##### Test Case R2-2: Exchange MarketData Exhaustion Raises Typed MarketDataError
- **Name**: `test_r2_exchange_marketdata_exhaustion_raises_typed_error`
- **Input**: `MarketData` client. Mock Hyperliquid `/info` configured to fail continuously (attempts 1, 2, 3 all return HTTP 500).
- **Action**: Call `market_data.get_closed_ohlcv("5m", 120, use_cache=False)`.
- **Expected Opaque-Box Verification Check**:
  - Exactly 3 retry attempts are made.
  - Raises `MarketDataError` with descriptive message containing coin and timeframe.
  - When passed to `_handle_loop_exception()`, the alert message category is `[交易所行情异常]`.

##### Test Case R2-3: LLM Relay Timeout Decoupling and Graceful Quant Fallback
- **Name**: `test_r2_llm_relay_timeout_decoupling_and_quant_fallback`
- **Input**: Bot in `STATE_IDLE`. Mock LLM relay endpoint (`https://api.ludies.best/v1/chat/completions`) configured to time out (`httpx.TimeoutException`).
- **Action**: Trigger macro evaluation via `fetch_ai_macro_state()` in `_idle_cycle()`.
- **Expected Opaque-Box Verification Check**:
  - `fetch_ai_macro_state()` returns `(None, {})` or safely falls back.
  - `bot.final_macro_state` falls back to `bot.quant_state` (e.g. `MacroState.NEUTRAL`).
  - `send_alert()` is NOT called; `len(mock_mailbox.sent_messages) == 0`.
  - Log records `WARNING` for fallback, but does NOT trigger panic email.

##### Test Case R2-4: LLM Cold-Start Cooldown Throttling
- **Name**: `test_r2_llm_cold_start_cooldown_suppression`
- **Input**: Bot starts with `cached_ai_macro_state = None` (cold start). LLM relay endpoint is down (returns HTTP 503). First cycle fails to reach relay.
- **Action**: Run 10 consecutive `_idle_cycle()` polls within the next 60 seconds.
- **Expected Opaque-Box Verification Check**:
  - The failing LLM relay is called only once during initial cold-start.
  - Subsequent cycles within the cooldown window (e.g. 15 minutes / 900s) suppress calling the dead relay.
  - `bot.quant_state` is continuously used as the operational baseline without stalling the loop.

##### Test Case R2-5: Alert Throttling Eliminates 32-Consecutive-Alert Storm
- **Name**: `test_r2_alert_throttling_eliminates_alarm_storm`
- **Input**: Bot in `STATE_IDLE`. Inject 32 consecutive market data exceptions (`MarketDataError`) into `_handle_loop_exception`.
- **Action**: Run loop exception handler for 32 consecutive cycles with `IDLE_ALERT_THRESHOLD = 5`.
- **Expected Opaque-Box Verification Check**:
  - Exactly 1 email alert is queued/sent (at cycle 5 when threshold is reached).
  - Iterations 6 through 32 are throttled (cooldown period $\ge 1800$s).
  - `MockMailbox` has `len(sent_messages) == 1` instead of 28 alarms.
  - `bot.idle_backoff` increases exponentially up to capped maximum (600s).

##### Test Case R2-6: Idle Error Counter Hygiene Reset on Clean Iteration
- **Name**: `test_r2_idle_error_counter_reset_on_clean_cycle`
- **Input**: Bot with accumulated errors (`idle_api_error_count = 4`, `idle_backoff = 240`).
- **Action**: Run a clean `_idle_cycle()` where market data responds successfully (including short-polling cycle where node has not rolled).
- **Expected Opaque-Box Verification Check**:
  - `bot.idle_api_error_count` is reset to `0`.
  - `bot.idle_backoff` is reset to `60`.
  - Log confirms clean idle status.

##### Test Case R2-7: Unambiguous Alert Categorization Headers
- **Name**: `test_r2_unambiguous_alert_categorization_headers`
- **Input**: 4 distinct error objects:
  1. `MarketDataError("Hyperliquid /info 502 Bad Gateway")`
  2. `ExecutionError("Hyperliquid /exchange order rejected: Insufficient margin")`
  3. `AIError("api.ludies.best connection timeout")`
  4. `NameError("name 'pd' is not defined")`
- **Action**: Pass each error through the alert categorization formatting logic.
- **Expected Opaque-Box Verification Check**:
  - Error 1 yields subject/header containing: `[交易所行情异常]`
  - Error 2 yields subject/header containing: `[交易所下单执行异常]`
  - Error 3 yields subject/header containing: `[AI中转站异常]`
  - Error 4 yields subject/header containing: `[交易程序运行时异常]`
  - Zero ambiguous messages containing combined tags like `[网络/AI异常]`.

---

#### R3: Free Cloud Server 24/7 Rotation Scheme

##### Test Case R3-1: State Files Integrity & Schema Validation
- **Name**: `test_r3_state_files_integrity_and_schema_validation`
- **Input**: Set of state test files:
  - Valid: `state.json`, `risk_state_aggressive.json`, `equity_history_*.json` with standard schema.
  - Corrupt 1: 0-byte empty file.
  - Corrupt 2: Invalid JSON text `{"state": "IDLE", "coin": ... [unclosed]`.
  - Corrupt 3: Missing required fields (`initial_equity` missing from risk state).
- **Action**: Invoke validation function `validate_state_files(state_path, risk_state_path, equity_path)`.
- **Expected Opaque-Box Verification Check**:
  - Valid files return `(True, "OK")`.
  - Corrupted and truncated files return `(False, <error_message>)`.
  - Corrupt state files are immediately rejected before standby injection.

##### Test Case R3-2: 6-Phase Handover Quiescence Freeze (Active Node Quiescence)
- **Name**: `test_r3_six_phase_handover_quiescence_freeze`
- **Input**: Running active bot process.
- **Action**: Rotator creates `control.json` with payload `{"action": "pause"}`.
- **Expected Opaque-Box Verification Check**:
  - Bot reads `control.json`, executes transition to `STATE_HALT`.
  - `control.json` is consumed (deleted from disk).
  - `state.json` is persisted with `"state": "HALT"`.
  - Active node is quiescent: no new orders placed, ready for safe state extraction.

##### Test Case R3-3: State Extraction and Standby Injection (Atomic State Transfer)
- **Name**: `test_r3_state_extraction_and_standby_injection`
- **Input**: Directory `node_a/` representing Active Node; Directory `node_b/` representing Standby Node. State files contain populated trades, peak equity, and FSM parameters.
- **Action**: Run rotator state transfer routine: pull state from Node A, validate, write to Node B.
- **Expected Opaque-Box Verification Check**:
  - All state files exist in `node_b/`.
  - SHA-256 hash of `node_b/state.json` equals SHA-256 hash of `node_a/state.json`.
  - SHA-256 hash of `node_b/risk_state_aggressive.json` equals SHA-256 hash of `node_a/risk_state_aggressive.json`.
  - Zero state drift or byte truncation occurred.

##### Test Case R3-4: Standby Health and Heartbeat Freshness Verification
- **Name**: `test_r3_standby_health_and_heartbeat_verification`
- **Input**: Standby Node B environment.
  - Scenario A: Node B writes `.heartbeat` with `timestamp = time.time()` (fresh < 10s).
  - Scenario B: Node B has stale `.heartbeat` (`timestamp = time.time() - 120s`).
  - Scenario C: `.heartbeat` file is missing.
- **Action**: Call rotator health check routine `verify_standby_health(node_b_dir, timeout=30)`.
- **Expected Opaque-Box Verification Check**:
  - Scenario A: Returns `True`.
  - Scenario B: Returns `False` (stale heartbeat detected).
  - Scenario C: Returns `False` (missing heartbeat detected).

##### Test Case R3-5: Safe Decommission of Active Node After Verified Handover
- **Name**: `test_r3_active_node_decommission_after_handover`
- **Input**: Handover state transfer complete and Standby Node B confirmed healthy.
- **Action**: Rotator executes decommission phase on Node A.
- **Expected Opaque-Box Verification Check**:
  - Node A process receives termination signal and exits cleanly.
  - Node A process is verified dead (`poll() is not None` or PID gone).
  - Exactly ONE instance (Node B) remains active, preventing split-brain dual-trading.

##### Test Case R3-6: Rotator CLI Shift Scheduler and Dry-Run Invocations
- **Name**: `test_r3_cli_rotator_shift_and_dry_run_modes`
- **Input**: Rotator script `scripts/rotator.py`.
- **Action**:
  1. Execute via CLI: `python scripts/rotator.py --check-shift`
  2. Execute via CLI: `python scripts/rotator.py --dry-run`
- **Expected Opaque-Box Verification Check**:
  - Process exits with returncode `0`.
  - `--check-shift` outputs current shift status, instance uptime, and remaining quota hours (< 6h).
  - `--dry-run` outputs simulated 6-phase handover logs without executing external shell commands or terminating processes.

##### Test Case R3-7: Sandbox Provisioning Script Idempotence and Dependency Completeness
- **Name**: `test_r3_sandbox_provisioning_script_dependencies`
- **Input**: Shell provisioning script `scripts/deploy_sandbox.sh` and `requirements.txt`.
- **Action**: Inspect provisioning script commands and verify:
  1. Installs Python dependencies (`requirements.txt`).
  2. Prepares directory hierarchy (`log/`, `historical_data/`, etc.).
  3. Verifies required environment variable placeholders.
- **Expected Opaque-Box Verification Check**:
  - Script passes bash syntax check (`bash -n scripts/deploy_sandbox.sh` or shlex verification).
  - Execution steps are idempotent (subsequent runs do not fail or overwrite existing configuration).

---

## 3. Caveats

1. **Development Status of M3 Artifacts**:
   - `scripts/rotator.py` and `scripts/deploy_sandbox.sh` are currently planned for implementation under Milestone 3 (M3). The test specifications designed here establish the external contract that M3 must satisfy. Tests for R3 can initially run against the specification interface and mock directory structures.
2. **Asynchronous SMTP Delivery Worker**:
   - `alerts.Mailer` delivers messages via a daemon worker thread. Opaque tests intercepting SMTP via `smtplib` mocks must ensure the worker thread has processed the queue (`mailer._queue.join()` or polling with a short 1-second timeout) before asserting sent counts.
3. **Hyperliquid Testnet Availability**:
   - Hyperliquid public testnet (`api.hyperliquid-testnet.xyz`) can occasionally experience maintenance downtime. Therefore, the E2E test suite MUST default to local mock isolation rather than relying on live testnet reachability during standard CI / automated regression runs.

---

## 4. Conclusion

The investigation confirms that:
1. The root cause of the "32 consecutive exceptions alert loop" was a combination of:
   - Unhandled exceptions in `_idle_cycle()` bubbling to `_handle_loop_exception()`.
   - Alert threshold trigger `idle_api_error_count >= IDLE_ALERT_THRESHOLD` firing on every cycle without throttling.
   - Conflated alert text blaming both network and AI.
   - Direct `send_alert()` calls inside `strategy.py` on non-critical LLM relay timeouts during flat position.
2. Complete opaque-box isolation can be achieved cleanly using standard Python libraries:
   - Hyperliquid REST: Intercepted via `HYPE_BASE_URL` override or `httpx.Client.post` mock adapter.
   - LLM Relay: Intercepted via `THIRD_PARTY_BASE_URL` override or `httpx.post` mock adapter.
   - SMTP Mailer: Intercepted via `smtplib.SMTP_SSL` mock or `MockMailbox` capturing list.
   - Filesystem: Isolated via `tempfile.TemporaryDirectory` and `STATE_FILE` environment override.
3. A total of 20 Tier 1 test specifications have been designed:
   - 6 for R1 (Runtime Stability & Exception Classification)
   - 7 for R2 (API Alert Decoupling & Throttling)
   - 7 for R3 (24/7 Cloud Rotation & State Sync)
   All specifications meet and exceed the $\ge 5$ requirement per feature area and provide explicit names, inputs, actions, and opaque-box verification criteria.

---

## 5. Verification Method

To independently verify the test infrastructure and findings:

1. **Verify Python & Test Runner Environment**:
   ```powershell
   python --version
   python -m unittest discover -s "C:\Users\Administrator\Desktop\v1\a\tests" -p "test_*.py" -t "C:\Users\Administrator\Desktop\v1\a"
   ```
   *Expected*: Passes all 70 existing tests in ~45 seconds.

2. **Verify Code Locations & Line References**:
   Inspect `main.py` lines 446, 525-536, 1462-1481; `exchange.py` lines 180-188, 260-275; `strategy.py` lines 124-129, 645-648; `alerts.py` lines 212-270.

3. **Invalidation Conditions**:
   - If `main.py` removes `HYPE_BASE_URL` or `THIRD_PARTY_BASE_URL` environment overrides, network isolation would require bytecode patching.
   - If `alerts.py` replaces `smtplib` with a compiled C binary, SMTP mailer mocking would require external network socket trapping.

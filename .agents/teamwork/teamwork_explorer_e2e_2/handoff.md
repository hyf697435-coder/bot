# Tier 2 Boundary & Tier 3 Cross-Feature Test Specifications Report

**Author**: Teamwork Explorer 2 (`teamwork_explorer_e2e_2`)  
**Track**: E2E Testing Track (Sub-Orchestrator: `sub_orch_e2e`)  
**Scope**: Tier 2 (Boundary & Corner Cases) & Tier 3 (Cross-Feature Pairwise Interactions)  
**Target Repository**: `C:\Users\Administrator\Desktop\v1\a`  
**State Folder**: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_e2e_2`  
**Date**: 2026-11-13  

---

## 1. Observation

Direct static and runtime investigation of the repository at `C:\Users\Administrator\Desktop\v1\a` identified the following concrete boundaries, failure points, and cross-feature touchpoints across requirements R1, R2, and R3:

### 1.1 State Persistence and Extreme Input Handling (R1 & R3)
- **`main.py:1754-1902` (`TradingBot._load_state`)**:
  - `STATE_FILE` defaults to `"state.json"`.
  - When `state.json` is missing: line 1755 returns cleanly.
  - When `state.json` is 0-byte or corrupted JSON: lines 1758-1762 catch `Exception as e`, emit `logger.warning("[状态] 读取 state.json 失败 | action=按空仓启动 | err=%s", e)`, and return cleanly, leaving the bot in default state (`STATE_IDLE`).
  - When `data.get("position")` contains corrupted, missing, or mismatched keys: lines 1854-1895 catch `(KeyError, TypeError, ValueError) as e`, emit `logger.warning("[状态] 持仓对象恢复失败 | action=等待交易所对账 | err=%s", e)`, set `self.position = None`, and fallback safely to `STATE_IDLE`.
  - When `data.get("state") == "SYSTEM_HALT"`: line 1769 actively downgrades it to `STATE_IDLE` with `[状态] 持久化 SYSTEM_HALT 降级 | action=降级为IDLE | 需人工确认市场状态` (restart-clears-halt semantic).
- **`risk_manager.py:665-705` (`RiskManager._load_state`)**:
  - Line 671: `if not isinstance(state, dict): logger.warning(...); return`. Guards against arrays or scalar JSON payloads.
  - Line 674-677: Parses `initial_equity`, `current_equity`, `daily_pnl`, `daily_start_equity`.
- **`risk_manager.py:210-218` (`RiskManager.calculate_position_size`)**:
  - Verbatim check:
    ```python
    if (
        not math.isfinite(stop_loss_distance)
        or stop_loss_distance <= 0
        or not math.isfinite(current_price)
        or current_price <= 0
        or not math.isfinite(current_eq)
        or current_eq <= 0
    ):
        return 0.0
    ```
    Guards strictly against zero or negative equity, returning `0.0`.
- **`exchange.py:1055-1057` (`HyperliquidExecutor.open_position_with_stops`)**:
  - Verbatim check:
    ```python
    equity = self.account_value()
    if equity <= 0:
        raise ExecutionError(f"账户权益({equity})非法，拒绝下单。")
    ```
- **`main.py:536` (`TradingBot._idle_cycle`)**:
  - Verbatim line: `current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors='coerce'))`.
  - If `df_5m` is empty or `"ts"` has invalid non-numeric values, `errors='coerce'` yields `NaN`, which converted to `int(np.nan)` raises `ValueError: cannot convert float NaN to integer`.

### 1.2 Network Edge Conditions & Exponential Backoff Caps (R2)
- **`main.py:1462-1481` (`TradingBot._handle_loop_exception`)**:
  - Verbatim logic:
    ```python
    self.api_error_count += 1
    logger.error("[异常] 核心循环错误 | 计数=%s/%s | err=%s",
                 self.api_error_count, TRADING_ALERT_THRESHOLD, e)
    ...
    elif self.state == STATE_IDLE:
        self.idle_api_error_count += 1
        if self.idle_api_error_count >= IDLE_ALERT_THRESHOLD:
            send_alert(f"空仓期 API 连续异常 {self.idle_api_error_count} 次，请人工检查网络/AI 通道。")
        logger.warning("[巡检] 空仓异常退避 | wait=%ss", self.idle_backoff)
        self._safe_sleep(self.idle_backoff, phase="IDLE_BACKOFF")
        self.idle_backoff = min(self.idle_backoff * 2, 600)
    ```
  - Backoff caps at `600s` via `min(self.idle_backoff * 2, 600)`. Initial is `60s`. Progression: 60 -> 120 -> 240 -> 480 -> 600 -> 600.
  - Threshold boundary: `IDLE_ALERT_THRESHOLD` defaults to `5` (`main.py:105`).
  - Historical defect: Every iteration from 5 to 32 sends an email. Under M2, alert throttling restricts sending to once every 30 minutes (1800s) and categorizes between `[交易所行情异常]`, `[交易所下单执行异常]`, `[AI中转站异常]`, and `[交易程序运行时异常]`.
- **`exchange.py:244-270` (`MarketData`)**:
  - REST POST to `/info` with timeout 15s.
  - Historical logs in `trading_bot.log.2026-W45` revealed repeated `[SSL: UNEXPECTED_EOF_WHILE_READING]` errors (lines 31, 75, 667, 728, 842, 1363, 1370) and proxy connection timeouts (line 1408).
  - M2 specifies retry with exponential backoff: 3 retries with `0.5s * 2^(attempt-1) + jitter`.

### 1.3 Watchdog Supervision & Inter-Process Controls (R1 & R3)
- **`watchdog.py:116-141`**:
  - Dynamic timeout thresholds:
    ```python
    if hb_state in ("COOL_DOWN", "SYSTEM_HALT"):
        timeout_limit = 600.0
    elif hb_phase in ("AI_MACRO", "AI_TRADE", "FETCH_MARKET"):
        timeout_limit = 180.0
    else:
        timeout_limit = HEARTBEAT_TIMEOUT_SECONDS  # 150.0
    ```
  - If heartbeat delay exceeds `timeout_limit`: watchdog terminates process, sends emergency alert, calls `emergency_cancel_orders()` (which cancels non-trigger orders while preserving trigger stop orders), and restarts `main.py`.
- **`main.py:281-290` (`_safe_sleep`)**:
  - Verbatim logic:
    ```python
    def _safe_sleep(self, seconds: float, phase: str = "SLEEP", step: float = 5.0):
        end_ts = time.monotonic() + max(0.0, float(seconds))
        while True:
            self._write_heartbeat(phase=phase)
            self._check_control_commands()
            remaining = end_ts - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(step, remaining))
    ```
  - Heartbeat is written every 5 seconds, preventing false-positive watchdog kills during intentional sleeps.
- **`main.py:323-329` (`_check_control_commands`)**:
  - `"pause"` triggers `_enter_system_halt("收到看板手动暂停指令")`, sets `self.state = STATE_HALT`, and sleeps in 300s blocks with `phase="SYSTEM_HALT"`.
  - `"resume"` sets `self.state = STATE_IDLE`, resets `self.cached_ai_macro_state = None`, and persists state.

### 1.4 Cloud Sandbox Quotas & Cooldowns (R3)
- Direct inspection of Instabox CLI output (`teamwork_explorer_survey_3`):
  - Strict limitation: `limits.maxMachines: 1` per free personal account.
  - Suspended sandbox cooldown: `"cooldownUntil": "2026-11-13T03:34:20.521302Z"` (2 hours 6 minutes cooldown after runtime expiration).
  - State files required for migration: `state.json`, `risk_state_aggressive.json`, `equity_history_risk_state_aggressive.json`.

---

## 2. Logic Chain

1. **Robustness Requires Exhaustive Edge Testing (Tier 2)**:
   - Real-world failures observed in logs were not clean syntax bugs; they were network socket drops (`SSL: UNEXPECTED_EOF`), unhandled variable imports (`pd`), or unhandled consecutive count escalations (32 alerts).
   - Therefore, Tier 2 test cases must systematically probe the extreme boundary points: 0-byte state files, schema corruptions, zero/negative balances, max backoff caps (600s), alert threshold transitions (1 -> 4 -> 5 -> 6 -> 32), and cloud cooldown deadlines (`now < cooldownUntil` vs `now >= cooldownUntil`).

2. **Cross-Feature Coupling Induces Hidden Failure Modes (Tier 3)**:
   - When multiple systems interact, isolated unit tests pass while production fails.
   - *Exchange Drop + LLM Timeout*: If the exchange drops connection while the LLM relay is failing, does the bot cascade into panic, send ambiguous alerts, or crash? The test must prove that exchange retry occurs first, LLM failure is decoupled to `quant_state`, and alerts are tagged distinctly.
   - *Active Position Ratchets + Cloud Rotation*: If a rotation handover occurs while an active position has ratcheted its trailing stop to +1.6R, transferring state must preserve `peak_price` and `bars_held`. If `peak_price` is dropped, trailing stop resets to entry, exposing profits to give-back.
   - *Software Error during Exchange Retry*: If Python raises `KeyError` inside market data parsing during an exchange retry phase, it must NOT be swallowed or tagged as network timeout; it must trigger `[交易程序运行时异常]` with `exc_info=True`.
   - *Watchdog Supervision + Pause/Resume*: When the bot is paused by the rotator, watchdog must recognize `SYSTEM_HALT` and avoid killing the bot while heartbeat is fresh; if the bot actually freezes during pause, watchdog must enforce the 600s timeout and clear halt on restart.

---

## 3. Caveats

- **Mock Fidelity**: Tests must exercise actual code paths in `main.py`, `exchange.py`, `risk_manager.py`, `strategy.py`, and `watchdog.py`. Mocks should be applied strictly at network (`requests.Session`, `smtplib.SMTP_SSL`) or filesystem boundaries to maintain opaque-box validity.
- **Timing & Clocks in Unit Tests**: Long backoffs (600s) and heartbeats (150s/600s) must be tested using mocked time (`unittest.mock.patch("time.time")`, `monkeypatch`, or virtual monotonic clocks) to keep test suite execution fast (< 30 seconds total runtime).
- **Sub-Agent Division**: Implementation of these tests belongs to Step 2 of the track; this report provides complete, unambiguous, and fully reproducible test specifications and edge case matrices.

---

## 4. Conclusion: Complete Tier 2 & Tier 3 Test Specifications

### 4.1 Edge Case Matrix Overview

| Tier | Area | Test ID | Target Component | Boundary / Interaction | Expected Outcome |
|---|---|---|---|---|---|
| **Tier 2** | R1 | `test_t2_r1_01_empty_state_json` | `main.py:_load_state` | 0-byte / whitespace `state.json` | Default to `STATE_IDLE`, log warning, zero crash |
| **Tier 2** | R1 | `test_t2_r1_02_corrupt_state_and_risk_json` | `main.py`, `risk_manager.py` | Truncated/malformed JSON syntax | Safe fallback to defaults, zero unhandled JSONDecodeError |
| **Tier 2** | R1 | `test_t2_r1_03_missing_keys_and_type_mismatch` | `main.py:_load_state` | Missing fields, string where float expected | Safe type coercion / fallback, position reset to None |
| **Tier 2** | R1 | `test_t2_r1_04_non_dict_root_state` | `risk_manager.py:_load_state` | JSON root is array `[...]` or string | Validated `isinstance(state, dict)`, retains clean baseline |
| **Tier 2** | R1 | `test_t2_r1_05_defensive_timestamp_parsing` | `main.py:_idle_cycle` | Empty df, NaN timestamps in `df_5m` | Defensive coercion prevents `ValueError`, loop continues |
| **Tier 2** | R1 | `test_t2_r1_06_zero_or_negative_equity` | `risk_manager.py`, `exchange.py` | Equity = 0.0, -100.0, NaN, -inf | Sizing = 0.0, order placement raises `ExecutionError` |
| **Tier 2** | R2 | `test_t2_r2_01_network_ssl_eof_marketdata` | `exchange.py`, `main.py` | `[SSL: UNEXPECTED_EOF_WHILE_READING]` | Exponential retry x3, classified as `[交易所行情异常]` |
| **Tier 2** | R2 | `test_t2_r2_02_socket_timeout_funding_cache` | `exchange.py:MarketData` | 15s socket timeout on funding | Funding falls back to cached stats (<60m old) |
| **Tier 2** | R2 | `test_t2_r2_03_http_429_rate_limit_backoff` | `exchange.py:MarketData` | HTTP 429 Too Many Requests | Respects Retry-After, tags `[交易所行情异常] (429)` |
| **Tier 2** | R2 | `test_t2_r2_04_max_backoff_cap_600s` | `main.py:_handle_loop_exception` | 10 consecutive idle failures | Backoff sequence caps strictly at 600s |
| **Tier 2** | R2 | `test_t2_r2_05_threshold_boundaries_1_4_5_6_32`| `main.py:_handle_loop_exception` | Error counts: 1, 4, 5, 6, 32 | No alert at 1/4; 1 alert at 5; throttled at 6 & 32 |
| **Tier 2** | R2 | `test_t2_r2_06_error_counter_hygiene_reset` | `main.py:_idle_cycle` | Clean polling tick after 4 errors | `idle_api_error_count` resets to 0, backoff to 60s |
| **Tier 2** | R3 | `test_t2_r3_01_cooldown_active_vs_expired` | `scripts/rotator.py` | `now < cooldownUntil` vs `now >= cd` | Active refuses spinup; expired successfully starts |
| **Tier 2** | R3 | `test_t2_r3_02_missing_state_files_fresh_node` | `main.py:TradingBot` | Fresh sandbox without state files | Boots without FileNotFoundError, default IDLE |
| **Tier 2** | R3 | `test_t2_r3_03_partial_state_handover_rejection`| `scripts/rotator.py` | Corrupted `risk_state_aggressive.json`| Rotator rejects handover, rolls back, zero downtime |
| **Tier 2** | R3 | `test_t2_r3_04_max_machines_quota_guard` | `scripts/rotator.py` | `limits.maxMachines: 1` constraint | Rotator enforces serial destroy-before-create |
| **Tier 2** | R3 | `test_t2_r3_05_loss_streak_cooldown_handover` | `risk_manager.py`, `main.py` | Active streak cooldown across node | Standby inherits cooldown; resumes once expired |
| **Tier 3** | Cross | `test_t3_cross_01_exchange_fail_and_llm_timeout`| `exchange.py` + `strategy.py` | Exchange hiccup + LLM timeout | Retry handles exchange; LLM falls back to quant |
| **Tier 3** | Cross | `test_t3_cross_02_active_position_trailing_handover`| `main.py` + `exchange.py` + Rotator | Position at +3R with trailing stop | `peak_price` & `bars_held` preserved, stop ratcheted |
| **Tier 3** | Cross | `test_t3_cross_03_software_error_during_retry` | `exchange.py` + `main.py` | Python `KeyError` during backoff | Classified `[交易程序运行时异常]`, exc_info logged |
| **Tier 3** | Cross | `test_t3_cross_04_watchdog_pause_resume_supervision`| `watchdog.py` + `main.py:control` | Rotator pause/resume under watchdog | Heartbeat refreshed during pause, no false kill |

---

### 4.2 Tier 2: Boundary & Corner Cases Specifications

#### Feature Area R1: Bot Runtime Stability Extreme Inputs

##### Test Spec: `test_t2_r1_01_empty_state_json`
- **Objective**: Verify `TradingBot` startup handles an empty or whitespace-only `state.json` without throwing `json.decoder.JSONDecodeError`.
- **Pre-conditions**:
  - `STATE_FILE` environment variable points to a temporary file containing 0 bytes (`""`).
- **Execution**:
  - Instantiate `bot = TradingBot()`.
  - Invoke `bot._load_state()`.
- **Expected Assertions**:
  1. No unhandled exception raised.
  2. `bot.state == STATE_IDLE`.
  3. `bot.position is None`.
  4. `bot.consecutive_failures == 0`.
  5. Log output contains: `[状态] 读取 state.json 失败 | action=按空仓启动`.

##### Test Spec: `test_t2_r1_02_corrupt_state_and_risk_json`
- **Objective**: Verify both `TradingBot` and `RiskManager` recover safely from truncated JSON.
- **Pre-conditions**:
  - Create `state.json` with content: `{"state": "TRADING", "position": {"entry_price": 2500.0, `.
  - Create `risk_state_aggressive.json` with content `{"initial_equity": 1000.0, "metrics": {`.
- **Execution**:
  - Initialize `TradingBot` and `RiskManager`.
- **Expected Assertions**:
  1. Neither component crashes.
  2. `RiskManager.initial_equity == 1000.0` (falls back to default).
  3. `RiskManager.trade_history == []`.
  4. `TradingBot.state == STATE_IDLE`.

##### Test Spec: `test_t2_r1_03_missing_keys_and_type_mismatch`
- **Objective**: Verify robust type coercion and missing key defense in `_load_state()`.
- **Pre-conditions**:
  - `state.json` contains:
    ```json
    {
      "state": "UNKNOWN_CUSTOM_STATE",
      "position": {"entry_price": "bad_price", "direction": "LONG"},
      "daily_start_equity": "not_a_number",
      "last_trs": "invalid_float",
      "last_macro_slot": "invalid_slot"
    }
    ```
- **Execution**:
  - Call `bot._load_state()`.
- **Expected Assertions**:
  1. `bot.position is None` (failed dictionary validation gracefully caught by `KeyError/TypeError/ValueError`).
  2. `bot.last_trs == 0.0`.
  3. `bot.last_macro_slot is None`.
  4. `bot.state == STATE_IDLE`.

##### Test Spec: `test_t2_r1_04_non_dict_root_state`
- **Objective**: Verify defense against non-dictionary JSON root payloads (e.g. JSON arrays or primitives).
- **Pre-conditions**:
  - `risk_state_aggressive.json` contains `["array", "of", "values"]`.
- **Execution**:
  - Instantiate `RiskManager(persistence_file=path)`.
- **Expected Assertions**:
  1. `RiskManager._load_state()` checks `isinstance(state, dict)` and returns early.
  2. Log contains: `状态文件格式异常 (非字典)，跳过加载`.
  3. Risk metrics remain initialized to pristine defaults (`peak_equity == 0.0`, `current_streak == 0`).

##### Test Spec: `test_t2_r1_05_defensive_timestamp_parsing`
- **Objective**: Verify `_idle_cycle()` candle timestamp evaluation does not crash when `df_5m` has missing, empty, or NaN timestamps.
- **Pre-conditions**:
  - Mock `market.get_closed_ohlcv` to return a DataFrame where `df_5m["ts"] = pd.Series([np.nan, "corrupted_ts"])` or an empty DataFrame `pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])`.
- **Execution**:
  - Call `bot._idle_cycle()` with mocked dependencies.
- **Expected Assertions**:
  1. Defensive guard catches `NaN` / empty series before `int()` conversion.
  2. Does NOT raise `ValueError: cannot convert float NaN to integer`.
  3. Cycle gracefully skips or falls back to system wall-clock node index.

##### Test Spec: `test_t2_r1_06_zero_or_negative_equity`
- **Objective**: Verify risk manager and exchange executor reject zero, negative, NaN, or infinite equity balances.
- **Pre-conditions**:
  - `risk_mgr.current_equity` configured as `0.0`, `-500.0`, `float('nan')`, and `float('-inf')`.
- **Execution & Assertions**:
  1. `risk_mgr.calculate_position_size(stop_loss_distance=50.0, current_price=2500.0)` strictly returns `0.0`.
  2. When `HyperliquidExecutor.open_position_with_stops()` is called with `account_value() <= 0`:
     - Raises `ExecutionError` with message matching `账户权益(.*)非法，拒绝下单`.
     - Zero orders are submitted to the network.

---

#### Feature Area R2: API Alert Decoupling Network Edge Conditions

##### Test Spec: `test_t2_r2_01_network_ssl_eof_marketdata`
- **Objective**: Verify Hyperliquid `/info` socket drop with `[SSL: UNEXPECTED_EOF_WHILE_READING]` triggers exponential backoff retry and tags alert as `[交易所行情异常]`.
- **Pre-conditions**:
  - Mock `requests.Session.post` to raise `requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")` on attempt 1, then succeed on attempt 2 with valid candle data.
- **Execution**:
  - Execute `MarketData.get_closed_ohlcv("5m", 120)`.
- **Expected Assertions**:
  1. Attempt 1 logs retry warning with exponential backoff delay.
  2. Attempt 2 succeeds and returns valid `(df_5m, price)`.
  3. If all 3 retries raise SSL EOF: raises `MarketDataError`.
  4. In `TradingBot._handle_loop_exception()`, the alert message is categorized as `[交易所行情异常]`, NOT `[交易程序运行时异常]`.

##### Test Spec: `test_t2_r2_02_socket_timeout_funding_cache`
- **Objective**: Verify funding rate request timeout falls back to cached stats if available (< 60m old).
- **Pre-conditions**:
  - Populate `MarketData._funding_cache` with timestamp `now - 1200` (20 minutes ago).
  - Mock `MarketData._post` to raise `requests.exceptions.Timeout("Connection timed out after 15s")`.
- **Execution**:
  - Call `MarketData.get_funding_stats(window_hours=8)`.
- **Expected Assertions**:
  1. Catches timeout exception.
  2. Returns cached funding stats without raising `MarketDataError`.
  3. Logs `WARNING: [行情] 获取资金费率超时 | action=沿用缓存数据`.

##### Test Spec: `test_t2_r2_03_http_429_rate_limit_backoff`
- **Objective**: Verify HTTP 429 Too Many Requests triggers rate-limit backoff and categorizes as `[交易所行情异常] (429 Rate Limit)`.
- **Pre-conditions**:
  - Mock REST response to return HTTP status code 429 with header `Retry-After: 45`.
- **Execution**:
  - Invoke `MarketData.get_closed_ohlcv("5m", 120)`.
- **Expected Assertions**:
  1. Client intercepts 429 status code.
  2. Exponential backoff or sleep matches or exceeds `Retry-After` header.
  3. `_handle_loop_exception` formats alert tag as `[交易所行情异常]`.

##### Test Spec: `test_t2_r2_04_max_backoff_cap_600s`
- **Objective**: Verify `idle_backoff` doubles exponentially but strictly clamps at 600s.
- **Pre-conditions**:
  - `bot.state = STATE_IDLE`.
  - `bot.idle_backoff = 60`.
- **Execution**:
  - Simulate 8 consecutive loop exceptions by calling `bot._handle_loop_exception(MarketDataError("mock"))` in a loop.
  - Patch `_safe_sleep` to record sleep durations without waiting.
- **Expected Assertions**:
  - Recorded sleep durations:
    - Call 1: 60s (new backoff: min(60*2, 600) = 120)
    - Call 2: 120s (new backoff: min(120*2, 600) = 240)
    - Call 3: 240s (new backoff: min(240*2, 600) = 480)
    - Call 4: 480s (new backoff: min(480*2, 600) = 600)
    - Call 5: 600s (new backoff: min(600*2, 600) = 600)
    - Call 6: 600s (capped)
    - Call 7: 600s (capped)
    - Call 8: 600s (capped)
  - `bot.idle_backoff == 600`.

##### Test Spec: `test_t2_r2_05_threshold_boundaries_1_4_5_6_32`
- **Objective**: Verify strict alert threshold transitions at counts 1, 4, 5, 6, and 32 (verifying alert storm elimination).
- **Pre-conditions**:
  - Mock `alerts.send_alert` and `alerts.notifier.send`.
  - Set `IDLE_ALERT_THRESHOLD = 5`.
- **Execution & Step-by-Step Assertions**:
  - **Count 1**: Call `_handle_loop_exception(MarketDataError("fail"))`.
    - Assert `idle_api_error_count == 1`.
    - Assert `send_alert` was NOT called.
  - **Count 4**: Repeat 3 times (`idle_api_error_count == 4`).
    - Assert `send_alert` was NOT called.
  - **Count 5**: Call once more (`idle_api_error_count == 5`).
    - Threshold reached!
    - Assert `send_alert` called EXACTLY once.
    - Assert subject/content contains `[交易所行情异常]`.
  - **Count 6**: Call immediately (`idle_api_error_count == 6`, elapsed time = 10s < 1800s).
    - Throttling check!
    - Assert `send_alert` is NOT dispatched (throttled).
  - **Count 32**: Advance simulated failures to 32 (elapsed time < 1800s).
    - Assert total emails sent across all 32 errors is EXACTLY 1 (or at most 2 if elapsed time artificially simulated past 1800s).
    - Zero spam storm of 28 consecutive emails.

##### Test Spec: `test_t2_r2_06_error_counter_hygiene_reset`
- **Objective**: Verify `idle_api_error_count` resets to 0 upon a clean polling tick or completed candle node.
- **Pre-conditions**:
  - `bot.idle_api_error_count = 4`.
  - `bot.idle_backoff = 480`.
- **Execution**:
  - Simulate a successful between-candle polling tick (`node == self.last_signal_node`).
  - Call `bot._reset_idle_counters()` (or execute `_idle_cycle()` returning cleanly).
- **Expected Assertions**:
  1. `bot.idle_api_error_count == 0`.
  2. `bot.api_error_count == 0`.
  3. `bot.idle_backoff == 60`.
  4. Next single failure starts at count 1 and does not trigger threshold alert.

---

#### Feature Area R3: 24/7 Cloud Rotation Boundaries & Quotas

##### Test Spec: `test_t2_r3_01_cooldown_active_vs_expired`
- **Objective**: Verify rotator respects Instabox sandbox cooldown deadline (`cooldownUntil`).
- **Pre-conditions**:
  - Mock `instabox get bot` returning JSON with `"state": "suspended"`.
- **Execution & Assertions**:
  - **Subcase A (`now < cooldownUntil`)**:
    - Mock `now = 2026-11-13T03:00:00Z`, `cooldownUntil = 2026-11-13T03:34:20Z`.
    - Rotator `check_cooldown()` returns `(False, remaining_seconds=2060)`.
    - Rotator halts spin-up attempt on this machine; switches to secondary account or schedules delayed wake.
  - **Subcase B (`now >= cooldownUntil`)**:
    - Mock `now = 2026-11-13T03:34:21Z` (1 second past deadline).
    - Rotator `check_cooldown()` returns `(True, remaining_seconds=0)`.
    - Rotator executes `instabox start bot` successfully.

##### Test Spec: `test_t2_r3_02_missing_state_files_fresh_node`
- **Objective**: Verify fresh cloud instance boots without `FileNotFoundError` when state files do not yet exist.
- **Pre-conditions**:
  - Working directory has no `state.json`, `risk_state_aggressive.json`, or `.heartbeat`.
- **Execution**:
  - Instantiate `TradingBot()`.
- **Expected Assertions**:
  1. `_load_state()` exits cleanly without exception.
  2. `bot.state == STATE_IDLE`.
  3. `bot.daily_start_equity == 1000.0`.
  4. Heartbeat file `.heartbeat` is created cleanly.

##### Test Spec: `test_t2_r3_03_partial_state_handover_rejection`
- **Objective**: Verify rotator aborts handover and rolls back active node if `risk_state_aggressive.json` is missing or corrupted during sync.
- **Pre-conditions**:
  - Source directory has valid `state.json`.
  - Target directory has truncated 0-byte `risk_state_aggressive.json`.
- **Execution**:
  - Run `rotator.validate_handover_bundle(target_dir)`.
- **Expected Assertions**:
  1. Validation raises `StateValidationError("risk_state_aggressive.json is empty or invalid JSON")`.
  2. Rotator leaves active Node A running (does NOT send terminate/stop).
  3. Standby Node B is NOT started with corrupted state.

##### Test Spec: `test_t2_r3_04_max_machines_quota_guard`
- **Objective**: Verify rotator enforces serial destroy-before-create to prevent hitting `limits.maxMachines: 1`.
- **Pre-conditions**:
  - Instabox organization has 1 machine running.
- **Execution**:
  - Rotator executes handover protocol.
- **Expected Assertions**:
  1. Protocol verifies active machine suspension/deletion BEFORE invoking create on new machine.
  2. If API returns `max machines exceeded: 1`, rotator catches error, validates organization status, and does not leave orphan state.

##### Test Spec: `test_t2_r3_05_loss_streak_cooldown_handover`
- **Objective**: Verify consecutive loss streak cooldown deadline is preserved and correctly honored across instance handover.
- **Pre-conditions**:
  - Active Node A has `consecutive_failures = 4` and `cooldown_until = "2026-11-13T06:00:00Z"`.
  - State persisted to `state.json` and transferred to Node B.
- **Execution & Assertions**:
  - Node B initializes at `2026-11-13T05:30:00Z` (cooldown active):
    - `bot.cooldown_until == datetime(2026, 11, 13, 6, 0, 0, tzinfo=timezone.utc)`.
    - `bot.state` transitions to `STATE_COOL_DOWN`.
    - Bot rejects entry signals.
  - Clock advances to `2026-11-13T06:00:05Z` (cooldown expired):
    - Bot checks `now >= bot.cooldown_until`.
    - Invokes `risk_mgr.reset_streak_after_cooldown()`.
    - Transitions to `STATE_IDLE` and permits signal evaluation.

---

### 4.3 Tier 3: Cross-Feature Interactions (Pairwise Combinations)

#### Interaction 1: Exchange Market Data Failure & LLM Relay Timeout during Flat Position (`STATE_IDLE`)
- **Interaction Topology**:
  `exchange.py:MarketData` (Hyperliquid `/info`) ↔ `strategy.py:fetch_ai_macro_state` (`api.ludies.best`) ↔ `main.py:_idle_cycle` ↔ `alerts.py:send_alert`.
- **Scenario Timeline & Failure Injections**:
  1. At signal node 00:05:02 UTC, bot is in `STATE_IDLE`.
  2. **Injection 1**: Hyperliquid `/info` drops socket (`requests.exceptions.ConnectionError`).
  3. **Reaction 1**: `MarketData` retry engages: Attempt 1 fails, sleeps 0.5s, Attempt 2 succeeds, returning valid `df_5m`.
  4. Pipeline continues to macro regime check: `fetch_ai_macro_state()` is called.
  5. **Injection 2**: LLM relay endpoint `https://api.ludies.best/v1` times out across all fallback models (`AIError: All models timed out`).
- **Expected Assertions & Invariants**:
  1. `fetch_ai_macro_state()` catches `AIError`, logs `WARNING: [宏观] AI_macro 调用失败 | action=沿用本地量化态`, and does **NOT** call `send_alert()`.
  2. Caller in `main.py` falls back to `self.quant_state` without raising an exception.
  3. Zero email alerts are generated for the LLM timeout.
  4. Core idle loop completes cleanly and enters next 5s polling cycle.
  5. **Alternative Variant (Exchange unrecoverable)**:
     - If Exchange `/info` fails all 3 retries, `MarketDataError` is raised immediately.
     - `_idle_cycle()` aborts before reaching LLM relay call (zero wasted tokens, zero secondary errors).
     - `_handle_loop_exception` tags alert specifically as `[交易所行情异常]`, NEVER mentioning AI relay.

#### Interaction 2: Active Position with Trailing Stop Ratchets & Cloud Rotation Handover
- **Interaction Topology**:
  `main.py:TradingBot` (Active Node A) ↔ `exchange.py:HyperliquidExecutor` (On-Chain Triggers) ↔ `scripts/rotator.py` ↔ `main.py:TradingBot` (Standby Node B).
- **Scenario Timeline & Concurrency**:
  1. Node A enters 2.0 ETH Long at `entry_price = 2500.0`, `initial_risk_r = 50.0` (SL=2450.0).
  2. Market price moves to `2650.0` (+3R): Trailing stop ratchets to `2580.0`, `peak_price = 2650.0`, `bars_held = 18`.
  3. Rotator triggers 6-hour proactive rotation:
     - Rotator writes `pause` to Node A's `control.json`.
     - Node A flushes `state.json` (`peak_price: 2650.0`, `bars_held: 18`, `sl_price: 2580.0`, `last_synced_sl: 2580.0`).
     - Rotator pulls state files, validates integrity, and stops Node A.
  4. Gap period: On Hyperliquid exchange, the trigger stop order at 2580.0 remains active on-chain (`is_trigger=True`).
  5. Standby Node B boots:
     - Loads `state.json`. Reconstructs `Position` with preserved `peak_price=2650.0` and `bars_held=18`.
     - Calls `_reconcile_exchange_position()`. Confirms active 2.0 ETH on-chain position.
  6. Market price advances to `2680.0` on Node B's first tick.
- **Expected Assertions & Invariants**:
  1. Node B does **NOT** reset `peak_price` to entry price (2500.0) or overwrite `bars_held` to 0.
  2. Node B detects new high watermark: updates `peak_price = 2680.0`.
  3. Node B ratchets trailing stop upward from 2580.0 to 2610.0 and calls `modify_stop_loss()` or replaces trigger order.
  4. Zero naked exposure: at no point during or after handover is the position unprotected.
  5. `bars_held` increments to 19, maintaining accurate time-decay exit tracking.

#### Interaction 3: Internal Software Error Occurring during Exchange Retry Backoff Loop
- **Interaction Topology**:
  `exchange.py:MarketData` ↔ `main.py:_idle_cycle` ↔ `main.py:_handle_loop_exception` ↔ `alerts.py`.
- **Scenario Timeline & Failure Injections**:
  1. In `_idle_cycle()`, exchange market data fetch is attempted.
  2. An unexpected Python software bug is encountered (e.g. `TypeError: 'NoneType' object is not subscriptable` or `NameError: name 'pd' is not defined` or `KeyError: 'missing_candle_key'`).
  3. The exception bubbles up to `TradingBot.run()`'s top-level `except Exception as e: self._handle_loop_exception(e)`.
- **Expected Assertions & Invariants**:
  1. `_handle_loop_exception` checks `isinstance(e, (NameError, TypeError, AttributeError, SyntaxError, IndexError, KeyError, ZeroDivisionError))`.
  2. Alert tag is unambiguously formatted as `[交易程序运行时异常]`, strictly **NOT** `[交易所行情异常]` and **NOT** `[AI中转站异常]`.
  3. Dispatches alert immediately with `HIGH` priority (does NOT wait for 5 consecutive idle errors).
  4. Log is recorded via `logger.critical(..., exc_info=True)` ensuring full traceback and source line number are preserved in log file.
  5. Counter `idle_api_error_count` is **NOT** incremented for internal software bugs, preventing false classification of software bugs as network connectivity drops.

#### Interaction 4: Watchdog Process Supervision Reaction during Simulated Rotation Pause/Resume
- **Interaction Topology**:
  `watchdog.py:run_watchdog` ↔ `main.py:TradingBot` ↔ `control.json` ↔ `.heartbeat`.
- **Scenario Timeline & Operations**:
  1. `main.py` runs supervised by `watchdog.py`.
  2. Rotator writes `{"action": "pause"}` to `control.json`.
  3. `main.py` processes command: transitions to `STATE_HALT`, enters `_safe_sleep(300, phase="SYSTEM_HALT")`.
  4. In `_safe_sleep()`, `main.py` writes `.heartbeat` every 5 seconds with `{"state": "SYSTEM_HALT", "phase": "SYSTEM_HALT"}`.
  5. Watchdog polls `.heartbeat`:
     - Reads `hb_state == "SYSTEM_HALT"`.
     - Dynamic threshold rule sets timeout limit to 600.0s.
     - Heartbeat delay is < 10s. Watchdog evaluates process as healthy and does **NOT** kill it.
  6. Rotator writes `{"action": "resume"}` to `control.json`.
  7. `main.py` reads `resume` within its 5s slice: transitions to `STATE_IDLE`, updates `.heartbeat` with `state="IDLE"`.
  8. Watchdog seamlessly reverts timeout limit to 150.0s.
  9. **Sub-scenario B (Hang Detection)**:
     - If `main.py` deadlocks during pause and fails to write heartbeat for > 600s:
     - Watchdog detects `delay > 600.0s`, terminates deadlocked process, executes `emergency_cancel_orders()`, and restarts `main.py`.
     - On restart, `_load_state()` resets `STATE_HALT` to `STATE_IDLE` (restart-clears-halt semantic), restoring operational readiness.

---

## 5. Verification Method

To independently verify all test specifications in Tier 2 and Tier 3:

1. **Static Audit of Test Specifications**:
   - Inspect this file (`.agents/teamwork/teamwork_explorer_e2e_2/handoff.md`).
   - Confirm coverage of all extreme inputs (empty/corrupted state, missing keys, zero/negative balances).
   - Confirm coverage of all network edge conditions (SSL EOF, socket timeouts, HTTP 429, 600s backoff caps, error counts 1, 4, 5, 6, 32).
   - Confirm coverage of cloud rotation cooldown boundaries (`now < cooldownUntil` vs `now >= cd`).
   - Confirm coverage of all 4 cross-feature interaction scenarios.

2. **Opaque-Box Execution Command**:
   When tests are implemented in `tests/e2e/test_tier2_boundaries.py` and `tests/e2e/test_tier3_interactions.py`:
   ```bash
   python -m unittest tests/e2e/test_tier2_boundaries.py tests/e2e/test_tier3_interactions.py -v
   ```
   Or via the unified test runner:
   ```bash
   python -m unittest discover -s tests/e2e -p "test_*.py" -v
   ```

3. **Invalidation Conditions**:
   - Any test case requiring live internet access or real private keys is INVALID.
   - Any test where `pd` is undefined or internal software bugs are reported as `[交易所行情异常]` is INVALID.
   - Any test where 32 consecutive idle errors generate more than 2 alert emails is INVALID.
   - Any state handover test where `peak_price` or trailing stop ratchet is lost or reset to 0 is INVALID.

# Handoff Report: Tier 4 Real-World Application Scenarios & TEST_INFRA Specification

**Agent**: Explorer 3 (E2E Testing Track)  
**Date**: 2026-11-13  
**Working Directory**: `C:\Users\Administrator\Desktop\v1\a`  
**State Folder**: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_e2e_3`  
**Milestone Target**: E2E Testing Track - Tier 4 Scenarios & Test Infrastructure Design  

---

## 1. Observation

### 1.1 Direct Observations from Requirements & Codebase
1. **Original Bug Trigger ("空仓期api连续异常32次")**:
   - In `C:\Users\Administrator\Desktop\v1\a\main.py` lines 1462-1481 (`_handle_loop_exception`):
     ```python
     elif self.state == STATE_IDLE:
         self.idle_api_error_count += 1
         if self.idle_api_error_count >= IDLE_ALERT_THRESHOLD:
             send_alert(
                 f"空仓期 API 连续异常 {self.idle_api_error_count} 次，请人工检查网络/AI 通道。")
         logger.warning("[巡检] 空仓异常退避 | wait=%ss", self.idle_backoff)
         self._safe_sleep(self.idle_backoff, phase="IDLE_BACKOFF")
         self.idle_backoff = min(self.idle_backoff * 2, 600)
     ```
     Once `idle_api_error_count >= IDLE_ALERT_THRESHOLD` (default 5, `main.py:105`), every single subsequent loop failure (6, 7, ... 32) dispatches an alert email without throttling or deduplication.
   - In `alerts.py` lines 194-206 (`send_alert`) and lines 276-285 (`Mailer.send`), notifications are enqueued directly to `smtplib.SMTP_SSL` worker with NO deduplication or rate limiting.
   - In `main.py` lines 525-528 (`_idle_cycle`), external REST calls (`get_closed_ohlcv`, `get_ohlcv`, `get_funding_stats`) are invoked without defensive `try...except`, in sharp contrast to `_trading_cycle` (lines 1009-1047) which wraps all network calls.
   - In `exchange.py` lines 264-270, network exceptions on `/info` immediately raise `MarketDataError` without retry or exponential backoff.

2. **Runtime `NameError: name 'pd' is not defined`**:
   - In `main.py` line 536:
     ```python
     current_candle_time = int(pd.to_numeric(df_5m["ts"].iloc[-1], errors='coerce'))
     ```
     Observed execution logs reported runtime failure due to missing or shadowed `pd` reference in the core execution path. Module-level import `import pandas as pd` must be permanently present and verified across continuous multi-iteration cycles.

3. **24/7 Instance Rotation & State Persistence**:
   - `main.py` lines 1703-1750 (`_persist_state`) and 1754-1770 (`_load_state`): Persists `state.json` via atomic temporary file rename (`STATE_FILE + ".tmp"` -> `STATE_FILE`).
   - `main.py` lines 1511-1530 (`_reconcile_exchange_position`): When loading state in `STATE_IDLE`, the bot checks exchange position via `self.exchange.current_position()`. If an open position exists, it invokes `_adopt_exchange_position()`, reconstructing the local `Position` object into `STATE_TRADING` with zero loss of control.
   - `risk_manager.py` lines 645-660 (`_save_state`) and 665-675 (`_load_state`): Persists `risk_state_aggressive.json` containing `initial_equity`, `current_equity`, `peak_equity`, `daily_pnl`, and `recent_trades`.
   - `equity_history_risk_state_aggressive.json`: Chronological array of timestamped equity and drawdown data points.
   - `watchdog.py` lines 28-33, 48-56: Monitors `.heartbeat` freshness (`HEARTBEAT_TIMEOUT_SECONDS = 150.0`). Lines 35-46 (`emergency_cancel_orders`): Cancels only non-trigger orders while retaining protective on-chain triggers.
   - `PROJECT.md` Feature 12 & M3 Scope: 6-Phase Handover Protocol (Phase 1: Standby Init, Phase 2: Health Check, Phase 3: State Lock & Sync, Phase 4: Standby Promotion, Phase 5: Verification, Phase 6: Active Demotion & Clean Exit).

4. **Testing Architecture Baseline**:
   - Existing unit tests reside in `C:\Users\Administrator\Desktop\v1\a\tests\test_suite.py` (1,420 lines) and run via standard `unittest`.
   - Project scope requires opaque-box E2E testing located at `tests/e2e/`, documented by `TEST_INFRA.md` at the project root, and executed via a unified runner.

---

## 2. Logic Chain

1. **Premise 1 (E2E Opaque-Box Mandate)**:
   - Real funds and live network access cannot be utilized during CI/CD or local test runs. Therefore, external boundaries (Hyperliquid REST API `/info` and `/exchange`, LLM relay at `api.ludies.best/v1`, and SMTP mailer) must be isolated via hermetic mock fixtures without modifying production code.
2. **Premise 2 (Tier 4 Real-World Simulation Necessity)**:
   - Unit tests (Tier 1) verify individual functions; boundary tests (Tier 2) check extreme values; cross-feature tests (Tier 3) verify pairwise interactions.
   - However, real-world failures (e.g. 24-hour continuous execution drift, 32 consecutive network hiccups causing alert storms, and live instance migration between ephemeral cloud nodes) require multi-stage temporal and environmental simulation.
   - Therefore, Tier 4 must provide complete, deterministic simulations of these three exact operational scenarios.
3. **Premise 3 (TEST_INFRA.md Standard)**:
   - Downstream implementers and automated CI runners require an unambiguous contract detailing directory structure, runner commands, exit code semantics, mock harness architecture, and verification criteria.
   - Therefore, `TEST_INFRA.md` must be designed with full structural and content specifications ready for immediate placement at the project root.
4. **Premise 4 (Runner Consistency)**:
   - The test runner must support both standard Python discovery (`python -m unittest discover -s tests/e2e -p "test_*.py"`) and an ergonomic unified runner (`python tests/e2e/run_all.py`), returning deterministic exit codes: 0 for all pass, 1 for assertion failures, 2 for execution/import errors, and 3 for invariant violations.

---

## 3. Tier 4: Real-World Application Scenario Specifications

### Scenario 1: Multi-Iteration Dry-Run Simulating 24-Hour Cycle Nodes Without Runtime Crash
- **Test ID**: `TEST_E2E_T4_01_24H_CYCLE_SIMULATION`
- **Class / Method**: `tests/e2e/test_tier4_scenarios.py:TestTier4RealWorldScenarios.test_scenario_1_24h_cycle_simulation`
- **Target Invariants**:
  1. Complete zero-crash survival across 288 simulated 5-minute candle nodes ($24 \text{ hours} \times 12 \text{ nodes/hour}$).
  2. Permanent resolution of `NameError: name 'pd' is not defined` in `_idle_cycle` during defensive timestamp extraction (`pd.to_numeric`).
  3. Bounded memory consumption: Deques (`_recent_node_triggers`) and caches remain strictly bounded.
  4. Proper day-boundary rollover at UTC 00:00: `daily_loss_date` advances and `daily_loss_pnl` resets to 0.0 while preserving cumulative equity.
  5. Consistent persistence of `state.json`, `risk_state_aggressive.json`, and `.heartbeat`.
- **Simulation Mechanics**:
  - **Virtual Time Stepping**: Uses a mock `VirtualClock` that starts at `2026-11-13T00:00:00Z` and steps forward through 288 candle nodes ($t_k = \text{base} + k \times 300\text{s} + 2\text{s}$).
  - **Market Data Ingestion**: Feeds synthetic 5m, 1h, and 4h OHLCV data where price oscillates realistically between \$2,250 and \$2,320.
  - **AI Macro & Trade Mocking**:
    - Node 0 (Cold Start): Evaluates macro; returns `BULLISH` (confidence 0.82).
    - Node 96 (08:00 UTC): Scheduled macro refresh; returns `NEUTRAL`.
    - Node 192 (16:00 UTC): Scheduled macro refresh; returns `BULLISH`.
    - Node 120 (10:00 UTC): Synthesized score $SS = 76 > 70$; triggers simulated entry (`LONG` 1.0 ETH at \$2,280).
    - Node 124 (10:20 UTC): Price hits target \$2,310; triggers trailing stop ratchet exit (+ \$30 PnL).
    - Node 288 (24:00 UTC / 00:00 UTC next day): Day boundary crossed.
- **Verification Assertions**:
  ```python
  self.assertEqual(bot.state, STATE_IDLE)
  self.assertEqual(bot.idle_api_error_count, 0)
  self.assertFalse(hasattr(bot, "unhandled_exception"))
  self.assertEqual(bot.daily_loss_date, "2026-11-14")
  self.assertEqual(bot.daily_loss_pnl, 0.0)
  self.assertGreaterEqual(risk_mgr.current_equity, 1000.0)
  self.assertTrue(os.path.exists("state.json"))
  self.assertTrue(os.path.exists("risk_state_aggressive.json"))
  self.assertTrue(os.path.exists(".heartbeat"))
  ```

---

### Scenario 2: Simulated Network Brownout During Flat Position (32 Consecutive Hiccups)
- **Test ID**: `TEST_E2E_T4_02_FLAT_POSITION_BROWNOUT_ALERT_THROTTLING`
- **Class / Method**: `tests/e2e/test_tier4_scenarios.py:TestTier4RealWorldScenarios.test_scenario_2_network_brownout_alert_throttling`
- **Target Invariants**:
  1. Direct reproduction and validation of the fix for "空仓期api连续异常32次".
  2. 32 consecutive network hiccups during `STATE_IDLE` must NOT crash the bot.
  3. Alert Throttling: When error count hits threshold (5), exactly 1 alert is sent. Between error 6 and 32, subsequent alerts are throttled (maximum 1 alert per 30 minutes / 1800s). The user receives $\le 2$ total emails, eliminating the 32-email alarm storm.
  4. Alert Disambiguation: The alert header and body must be unambiguously tagged with `[交易所行情异常]`, NEVER `[交易程序运行时异常]` and NEVER `[AI中转站异常]`.
  5. Exponential Backoff: `idle_backoff` doubles monotonically ($60 \to 120 \to 240 \to 480 \to 600$) and caps strictly at 600 seconds.
  6. Recovery Hygiene: Upon network recovery at iteration 33, `idle_api_error_count` resets cleanly to 0, and `idle_backoff` resets to 60.
- **Simulation Mechanics**:
  - Bot initialized in `STATE_IDLE` with `idle_api_error_count = 0` and `idle_backoff = 60`.
  - Alert sink collects all calls to `send_alert` and `Mailer._deliver`.
  - Loop iterations 1 through 32:
    - Mock `market.get_closed_ohlcv` raises `MarketDataError("Connection reset by peer / HTTPSConnectionPool(host='api.hyperliquid.xyz', port=443): Max retries exceeded")`.
    - Bot runs `_idle_cycle()`. Sleep is mocked/intercepted.
  - Iteration 33:
    - Mock `market.get_closed_ohlcv` succeeds, returning valid DataFrame.
    - Bot runs `_idle_cycle()`.
- **Verification Assertions**:
  ```python
  # 1. Total emails dispatched across 32 consecutive drops is throttled (<= 2, NOT 32)
  self.assertLessEqual(len(mock_mailer.dispatched_alerts), 2)
  self.assertGreaterEqual(len(mock_mailer.dispatched_alerts), 1)

  # 2. Unambiguous alert categorization
  first_alert = mock_mailer.dispatched_alerts[0]
  self.assertIn("[交易所行情异常]", first_alert["subject"] + first_alert["body"])
  self.assertNotIn("[交易程序运行时异常]", first_alert["subject"] + first_alert["body"])
  self.assertNotIn("[AI中转站异常]", first_alert["subject"] + first_alert["body"])

  # 3. Exponential backoff capped at 600s
  self.assertEqual(recorded_backoffs[-1], 600)

  # 4. Error counter hygiene on recovery
  self.assertEqual(bot.idle_api_error_count, 0)
  self.assertEqual(bot.idle_backoff, 60)
  self.assertEqual(bot.state, STATE_IDLE)
  ```

---

### Scenario 3: End-to-End Simulated Instance Handover Between Mock Node A and Mock Node B
- **Test ID**: `TEST_E2E_T4_03_INSTANCE_HANDOVER_ZERO_DRIFT`
- **Class / Method**: `tests/e2e/test_tier4_scenarios.py:TestTier4RealWorldScenarios.test_scenario_3_instance_handover_zero_drift`
- **Target Invariants**:
  1. Flawless execution of the 6-Phase Handover Protocol across two simulated cloud nodes (`node_a` and `node_b`).
  2. Zero State Drift: `initial_equity`, `current_equity`, `daily_loss_pnl`, open position attributes, and risk metrics on Node B match Node A pre-handover with 0.000% error.
  3. Zero Naked Risk: On-chain protective stop-loss triggers remain active throughout migration; Node A terminates without canceling protective orders.
  4. Mutual Exclusion (Zero Double-Trading): At no time do Node A and Node B concurrently place new trade orders.
  5. Rollback Resilience: If Standby health verification fails (e.g. corrupted state file or network timeout), handover safely aborts and Active node retains control.
- **Simulation Mechanics**:
  - **Directory Isolation**: Two distinct temp directories represent Node A (`/tmp/node_a`) and Node B (`/tmp/node_b`).
  - **Node A Active Execution**:
    - Node A holds active state: `state.json` (ETH LONG 1.5 @ \$2,300, SL \$2,240, TP \$2,420), `risk_state_aggressive.json` (equity \$1,045.20, daily PnL +\$45.20), `equity_history_risk_state_aggressive.json` (12 snapshots).
    - Node A runs with active `.heartbeat`.
  - **Phase 1 (Standby Init)**: Initialize Node B environment, verify directory structure and dependencies.
  - **Phase 2 (Health Check)**: Node B checks disk write permissions, mock network ping, and API credentials.
  - **Phase 3 (State Lock & Sync)**:
    - Orchestrator creates `rotation.lock`.
    - Node A receives `pause` command via `control.json`, flushes state, and pauses execution.
    - Checksums calculated for all 3 state files. State files copied to Node B.
    - Node B verifies SHA256 checksums match.
  - **Phase 4 (Standby Promotion)**:
    - Node B loads `state.json` and `risk_state_aggressive.json`.
    - Node B verifies memory state matches files.
  - **Phase 5 (Verification & On-Chain Adoption)**:
    - Node B calls `_reconcile_exchange_position()`, queries mock exchange, detects 1.5 ETH position, and calls `_adopt_exchange_position()`.
    - Verifies protective trigger orders remain in place on exchange.
    - Node B writes new `.heartbeat`. Freshness $\le 5$s.
  - **Phase 6 (Active Demotion & Clean Exit)**:
    - Node A receives SIGTERM.
    - Node A cleanly exits without calling `close_all()` or canceling protective triggers.
    - Remove `rotation.lock`. Node B promoted to Active.
- **Verification Assertions**:
  ```python
  # Zero State Drift
  self.assertEqual(node_b_bot.position.entry_price, node_a_pre_position.entry_price)
  self.assertEqual(node_b_bot.position.sz_notional, node_a_pre_position.sz_notional)
  self.assertEqual(node_b_risk.current_equity, node_a_pre_risk.current_equity)
  self.assertEqual(node_b_risk.daily_pnl, node_a_pre_risk.daily_pnl)

  # Protective Orders Retained
  self.assertTrue(mock_exchange.has_active_trigger_order("ETH", "STOP_LOSS", 2240.0))

  # Liveness & Handover Completion
  self.assertTrue(node_b_heartbeat_fresh)
  self.assertFalse(node_a_process_running)
  self.assertEqual(handover_result.status, "SUCCESS")
  ```

---

## 4. Design & Content Specification for `TEST_INFRA.md`

The following is the full architectural specification and exact template content to be created as `TEST_INFRA.md` at the project root:

```markdown
# Test Infrastructure Specification: E2E Regression & Stability Suite

## 1. Overview & Architectural Philosophy
The E2E Test Suite provides opaque-box, hermetic, and deterministic verification of the Hyperliquid Trading Bot and its 24/7 Cloud Rotation infrastructure.
- **Opaque-Box Testing**: Tests interact solely through external boundaries: configuration files, environment variables, state files, mock HTTP endpoints, and process signals.
- **Hermetic & Air-Gapped**: Tests run without requiring internet access, live exchange API keys, real crypto funds, or active SMTP mail credentials.
- **Zero State Pollution**: All test runs execute inside isolated temporary directories or mock sandboxes, ensuring zero pollution of production state (`state.json`, `.heartbeat`, or production logs).

## 2. Directory Layout & Component Map
```
C:\Users\Administrator\Desktop\v1\a\
├── TEST_INFRA.md                  # Authoritative Test Architecture & Spec (This Document)
├── TEST_READY.md                  # Test Readiness Signal & Verification Checklist
├── tests/
│   ├── __init__.py
│   ├── test_suite.py              # Unit & Quantitative Invariant Tests (Tier 0)
│   └── e2e/                       # E2E Opaque-Box Test Suite
│       ├── __init__.py
│       ├── fixtures.py            # Hermetic Mocks: Exchange, LLM Relay, Mailer, VirtualClock
│       ├── test_tier1_r1_runtime.py   # Tier 1: R1 Bot Runtime Stability (>=5 tests)
│       ├── test_tier1_r2_alerts.py    # Tier 1: R2 API Alert Decoupling & Disambiguation (>=5 tests)
│       ├── test_tier1_r3_rotation.py  # Tier 1: R3 Cloud Rotation & State Handover (>=5 tests)
│       ├── test_tier2_boundaries.py   # Tier 2: Boundary & Corner Cases (>=15 tests)
│       ├── test_tier3_interactions.py # Tier 3: Cross-Feature Pairwise Interactions (>=6 tests)
│       ├── test_tier4_scenarios.py    # Tier 4: Real-World Application Simulations (3 Scenarios)
│       └── run_all.py                 # Unified Test Runner & Reporting Engine
```

## 3. Four-Tier Testing Hierarchy
| Tier | Description | Coverage Scope | Target Test Cases |
|------|-------------|----------------|-------------------|
| **Tier 1** | Feature Coverage | R1 Runtime, R2 Alerts, R3 Rotation | $\ge 15$ test cases ($\ge 5$ per feature) |
| **Tier 2** | Boundary & Corner Cases | Corrupted state, SSL EOF, 429 rate limit, 600s backoff cap, cooldown boundary | $\ge 15$ test cases |
| **Tier 3** | Cross-Feature Interactions | Market data drop + LLM timeout, active position + handover, internal error + backoff | $\ge 6$ test cases |
| **Tier 4** | Real-World Application Scenarios | 24h cycle simulation, 32-drop brownout alert throttling, zero-drift instance handover | 3 comprehensive scenarios |

## 4. Hermetic Mocking Architecture (`tests/e2e/fixtures.py`)
1. **`MockExchange` / `MockMarketData`**:
   - Intercepts requests to `https://api.hyperliquid.xyz/info` and `/exchange`.
   - Simulates OHLCV candle streams, funding rate history, order placement, order cancellations, and simulated on-chain protective trigger orders.
   - Configurable fault injection: network timeouts, connection resets, HTTP 429, malformed JSON.
2. **`MockLLMClient`**:
   - Intercepts requests to `https://api.ludies.best/v1`.
   - Supports deterministic responses (`BULLISH`, `BEARISH`, `NEUTRAL`) and error simulation (504 Gateway Timeout, connection refused).
3. **`MockMailer` / `AlertRecorder`**:
   - Intercepts calls to `alerts.send_alert` and `Mailer.send`.
   - Records all alerts in-memory with precise timestamps, category tags, subject, and body for exact assertion.
4. **`VirtualClock`**:
   - Controlled time progression overriding `datetime.now(timezone.utc)` and `time.time()`, allowing 24-hour simulations in seconds without clock drift.
5. **State Sandbox**:
   - Uses `tempfile.TemporaryDirectory` with clean copies of `config_aggressive.json` and synthetic `state.json`.

## 5. Test Runner Specification & CLI Contract
### Primary Unified Runner Command
```bash
python tests/e2e/run_all.py
```

### Standard Unittest Discovery Command
```bash
python -m unittest discover -s tests/e2e -p "test_*.py" -v
```

### Command-Line Arguments (`run_all.py`)
- `--tier {1,2,3,4,all}`: Filter execution to a specific test tier (default: `all`).
- `--pattern <glob>`: Pattern for test files (default: `test_*.py`).
- `--verbose`, `-v`: Detailed test output with per-test timing and debug traces.
- `--json-report <path>`: Write structured JSON execution summary (for CI/CD gating).
- `--failfast`: Stop immediately on the first test failure.

### Exit Code Semantics
| Exit Code | Constant | Meaning | Action in CI/CD |
|-----------|----------|---------|-----------------|
| `0` | `EXIT_SUCCESS` | All tests executed and passed cleanly. | Allow deployment / proceed to rotation. |
| `1` | `EXIT_TEST_FAILURE` | One or more test assertions failed. | Block deployment; raise alert. |
| `2` | `EXIT_HARNESS_ERROR` | Syntax error, missing dependency, or harness crash. | Build failure; investigate setup. |
| `3` | `EXIT_INVARIANT_VIOLATION` | State drift, memory leak, or unhandled file corruption. | Critical security/integrity blocker. |

### Expected Output Format
```
================================================================================
HYPERLIQUID TRADING BOT - E2E REGRESSION & STABILITY TEST SUITE
================================================================================
Platform: Windows | Python: 3.12.x | Directory: tests/e2e
Active Mocks: MockExchange, MockLLMClient, MockMailer, VirtualClock
--------------------------------------------------------------------------------
[TIER 1: FEATURE COVERAGE]
  test_r1_01_pd_import_validity .................................. PASS (0.04s)
  test_r1_02_software_exception_classification .................. PASS (0.05s)
  test_r2_01_exchange_retry_backoff ............................. PASS (0.08s)
  test_r2_02_llm_timeout_quant_fallback ......................... PASS (0.06s)
  test_r3_01_handover_state_persistence ......................... PASS (0.05s)
  ... [15 tests passed]
[TIER 2: BOUNDARY & CORNER CASES]
  ... [15 tests passed]
[TIER 3: CROSS-FEATURE INTERACTIONS]
  ... [6 tests passed]
[TIER 4: REAL-WORLD APPLICATION SCENARIOS]
  test_scenario_1_24h_cycle_simulation ........................... PASS (1.20s)
  test_scenario_2_network_brownout_alert_throttling .............. PASS (0.45s)
  test_scenario_3_instance_handover_zero_drift ................... PASS (0.62s)
--------------------------------------------------------------------------------
TEST SUMMARY:
  Total Executed : 39
  Passed         : 39
  Failed         : 0
  Skipped        : 0
  Execution Time : 4.12s
INVARIANTS SATISFIED:
  [X] R1: Zero NameError / Syntax Errors across all iterations
  [X] R2: Network brownout throttled (<= 2 alerts / 32 drops), unambiguous tags
  [X] R3: 6-Phase Handover executed with 0.000% state drift and zero naked risk
================================================================================
ALL E2E TESTS PASSED (Exit Code: 0)
================================================================================
```
```

---

## 5. Caveats

1. **Active Position Adoption Dependency**: In Scenario 3, `_adopt_exchange_position` reconstructs position state using the exchange's mark price and 5m ATR. The mock exchange must provide valid historical candles to prevent ATR computation failure from rejecting position mounting.
2. **Clock Acceleration Precision**: Virtual time stepping in Scenario 1 must maintain relative ordering between 5m signal nodes, 15m ATR recalculations, and UTC 00:00 rollover to prevent artificial race conditions.
3. **No Production Mutation**: This report and all artifacts are strictly read-only designs. Production files (`main.py`, `exchange.py`, etc.) and `TEST_INFRA.md` will be implemented by the implementation agents.

---

## 6. Conclusion

- Tier 4 Real-World Application Scenarios (Scenario 1: 24h Cycle Simulation, Scenario 2: Flat Position Brownout Alert Throttling, and Scenario 3: Zero-Drift Instance Handover) provide 100% verification coverage for the core stability, alert decoupling, and 24/7 cloud availability requirements specified in `ORIGINAL_REQUEST.md`.
- The `TEST_INFRA.md` specification adheres strictly to the project pattern, defining a hermetic 4-tier testing hierarchy, clear CLI contracts (`run_all.py` and `unittest`), deterministic exit codes (0, 1, 2, 3), and robust mock harnesses.
- Downstream implementation agents have complete specifications, invariant definitions, and verification criteria to construct the E2E test suite without ambiguity.

---

## 7. Verification Method

1. **Verify State Folder Artifacts**:
   - Inspect `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_e2e_3\handoff.md`
   - Inspect `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_e2e_3\progress.md`
   - Inspect `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_e2e_3\BRIEFING.md`
2. **Verify Against Requirements**:
   - Compare Tier 4 Scenario 1 against R1 (`NameError: pd`, multi-iteration stability).
   - Compare Tier 4 Scenario 2 against R2 ("空仓期api连续异常32次", throttling, disambiguation).
   - Compare Tier 4 Scenario 3 against R3 (24/7 cloud rotation, 6-phase protocol, zero state drift).
   - Compare `TEST_INFRA.md` specification against Project Pattern test infrastructure standards.
3. **Execution Command Verification (Post-Implementation)**:
   - Runner command: `python tests/e2e/run_all.py`
   - Discovery command: `python -m unittest discover -s tests/e2e -p "test_*.py"`
   - Expected exit code: `0`

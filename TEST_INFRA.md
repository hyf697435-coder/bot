# Test Infrastructure Specification: E2E Regression & Stability Suite

## 1. Overview & Architectural Philosophy
The E2E Test Suite provides opaque-box, hermetic, and deterministic verification of the Hyperliquid Trading Bot and its 24/7 Cloud Rotation infrastructure.
- **Opaque-Box Testing**: Tests interact solely through external boundaries: configuration files, environment variables, state files, mock HTTP endpoints, process signals, and log/alert streams. Production code is never modified during test runs.
- **Hermetic & Air-Gapped**: Tests run without requiring internet access, live exchange API keys, real crypto funds, or active SMTP mail credentials. All network interactions (Hyperliquid REST API, LLM relay API, SMTP delivery) are safely intercepted.
- **Zero State Pollution**: All test runs execute inside isolated temporary directories or mock sandboxes, ensuring zero pollution of production state (`state.json`, `.heartbeat`, `risk_state_aggressive.json`, or production logs).
- **Deterministic & High-Speed**: Uses virtual clock stepping and mock network latencies to execute full 24-hour simulations and multi-retry sequences in seconds.

## 2. Directory Layout & Component Map
```
C:\Users\Administrator\Desktop\v1\a\
├── TEST_INFRA.md                      # Authoritative Test Architecture & Spec (This Document)
├── tests/
│   ├── __init__.py
│   ├── test_suite.py                  # Unit & Quantitative Invariant Tests (Tier 0)
│   └── e2e/                           # E2E Opaque-Box Test Suite
│       ├── __init__.py                # Package initialization
│       ├── fixtures.py                # Hermetic Mocks: Exchange, LLM Relay, Mailer, VirtualClock, Sandbox
│       ├── test_tier1_r1_runtime.py   # Tier 1: R1 Bot Runtime Stability (6 tests)
│       ├── test_tier1_r2_alerts.py    # Tier 1: R2 API Alert Decoupling & Disambiguation (7 tests)
│       ├── test_tier1_r3_rotation.py  # Tier 1: R3 Cloud Rotation & State Handover (7 tests)
│       ├── test_tier2_boundaries.py   # Tier 2: Boundary & Corner Cases (17 tests)
│       ├── test_tier3_interactions.py # Tier 3: Cross-Feature Pairwise Interactions (4 tests)
│       ├── test_tier4_scenarios.py    # Tier 4: Real-World Application Simulations (3 Scenarios)
│       └── run_all.py                 # Unified Test Runner & Reporting Engine
```

## 3. Four-Tier Testing Hierarchy
| Tier | Description | Coverage Scope | Test Cases |
|------|-------------|----------------|------------|
| **Tier 1** | Feature Coverage | R1 Runtime Stability, R2 Alerts Decoupling, R3 Cloud Rotation | 20 test cases (>=5 per feature) |
| **Tier 2** | Boundary & Corner Cases | Empty/corrupted state, SSL EOF, 429 rate limit, 600s backoff cap, cooldown boundary | 17 test cases |
| **Tier 3** | Cross-Feature Interactions | Market data drop + LLM timeout, active position + handover, internal error + backoff, watchdog + pause | 4 test cases |
| **Tier 4** | Real-World Application Scenarios | 24h cycle simulation, 32-drop brownout alert throttling, zero-drift instance handover | 3 comprehensive scenarios |

Total Suite Coverage: 44 test cases across all 4 tiers.

---

## 4. Hermetic Mocking Architecture (`tests/e2e/fixtures.py`)

### 4.1 `MockExchange` / `MockMarketData`
- **Interception Mechanism**: Intercepts requests to `https://api.hyperliquid.xyz/info` and `/exchange` via `requests.Session` / `httpx.Client` adapters or environment override (`HYPE_BASE_URL`).
- **Simulated Endpoints**:
  - `candleSnapshot`: Returns deterministic list of OHLCV candle dictionaries `[{"t": timestamp, "o": 2200.0, "h": 2220.0, "l": 2190.0, "c": 2210.0, "v": 1500.0}, ...]`.
  - `allMids`: Returns current mark prices e.g. `{"ETH": "2250.0", "BTC": "76000.0"}`.
  - `fundingHistory`: Returns historical funding rates.
  - `/exchange`: Returns signed order responses and tracks resting on-chain trigger orders (`STOP_LOSS`, `TAKE_PROFIT`).
- **Fault Injection Capabilities**:
  - Programmable failures: HTTP 500, 502, 504 status codes.
  - Socket exceptions: `requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")`, `requests.exceptions.ConnectTimeout`.
  - Rate limiting: HTTP 429 Too Many Requests with `Retry-After` header.
  - Corrupted data: Malformed JSON, empty candle arrays, NaN values.
  - Trigger inspection: `has_active_trigger_order(coin, order_type, price)`.

### 4.2 `MockLLMClient`
- **Interception Mechanism**: Intercepts requests to `THIRD_PARTY_BASE_URL` (`https://api.ludies.best/v1/chat/completions`).
- **Deterministic Payloads**:
  - AI_macro valid response: `{"choices": [{"message": {"content": "{\"suggested_state\": \"BULLISH\", \"reason\": \"Strong momentum\", \"news_impact_analysis\": \"None\"}"}}]}`.
  - AI_Trade valid response: `{"choices": [{"message": {"content": "{\"decision\": \"ENTER_LONG\", \"confidence\": 0.85, \"reason\": \"Breakout detected\"}"}}]}`.
- **Fault Injection Capabilities**:
  - `httpx.TimeoutException` or `requests.exceptions.Timeout` to simulate relay proxy timeouts.
  - HTTP 502 / 504 gateway errors.
  - Malformed JSON responses without valid JSON schema.

### 4.3 `MockMailbox` / `MockMailer`
- **Interception Mechanism**: Intercepts `alerts.send_alert` and `alerts.Mailer._deliver` / `smtplib.SMTP_SSL`.
- **Recorded Data**:
  - Each alert recorded in `MockMailbox.sent_messages` with `{timestamp, subject, body, priority}`.
- **Verification Assertions**:
  - Exact count assertion (e.g. `len(sent_messages) == 1` when 32 errors occur).
  - Unambiguous header checks: `[交易程序运行时异常]`, `[交易所行情异常]`, `[AI中转站异常]`, `[交易所下单执行异常]`.

### 4.4 `VirtualClock`
- **Interception Mechanism**: Patches `time.time`, `datetime.now(timezone.utc)`, `time.monotonic`, and intercepts `time.sleep` / `_safe_sleep`.
- **Capabilities**:
  - Allows accelerated 24-hour simulation across 288 candle nodes in seconds.
  - Records sleep durations for backoff verification (e.g. 60 -> 120 -> 240 -> 480 -> 600s).

### 4.5 `StateSandbox`
- **Interception Mechanism**: Manages a self-contained temporary directory per test case.
- **Capabilities**:
  - Generates pristine configuration and state files (`config_aggressive.json`, `state.json`, `risk_state_aggressive.json`, `.heartbeat`).
  - Automatically cleans up state upon test teardown.

---

## 5. Tier Breakdown & Test Matrix

### Tier 1: Feature Coverage (20 tests)
- **R1: Bot Runtime Stability** (6 tests)
  - `test_r1_pandas_import_and_module_presence`: Verifies `import pandas as pd`, module attribute presence, calculation without `NameError`.
  - `test_r1_defensive_timestamp_extraction_resilience`: Verifies handling of int, string numeric, float, NaN, and corrupted timestamp series.
  - `test_r1_software_exception_classification_tag`: Verifies internal errors (`NameError`, `TypeError`, etc.) get `[交易程序运行时异常]`.
  - `test_r1_traceback_preservation_in_critical_log`: Verifies full multiline traceback preservation with `exc_info=True`.
  - `test_r1_clean_simulation_core_loop_execution`: Verifies 5 consecutive clean cycles with 0 errors and fresh `.heartbeat`.
  - `test_r1_static_syntax_and_import_integrity`: Verifies 100% AST parse and compile pass across all source files.
- **R2: API Alert Decoupling & Disambiguation** (7 tests)
  - `test_r2_exchange_marketdata_retry_and_backoff`: Verifies 3-attempt retry with backoff on transient failure.
  - `test_r2_exchange_marketdata_exhaustion_raises_typed_error`: Verifies exhausted retries raise `MarketDataError` tagged `[交易所行情异常]`.
  - `test_r2_llm_relay_timeout_decoupling_and_quant_fallback`: Verifies LLM timeout safely falls back to `quant_state` without sending panic alert.
  - `test_r2_llm_cold_start_cooldown_suppression`: Verifies dead LLM relay is throttled on cold start.
  - `test_r2_alert_throttling_eliminates_alarm_storm`: Verifies 32 consecutive market data errors send exactly 1 alert at threshold.
  - `test_r2_idle_error_counter_reset_on_clean_cycle`: Verifies error count and backoff reset to defaults on clean cycle.
  - `test_r2_unambiguous_alert_categorization_headers`: Verifies 4 distinct error categories map to 4 distinct alert headers.
- **R3: Free Cloud Server 24/7 Rotation Scheme** (7 tests)
  - `test_r3_state_files_integrity_and_schema_validation`: Validates state files against corruption, 0-byte, and missing keys.
  - `test_r3_six_phase_handover_quiescence_freeze`: Verifies rotator `pause` command quiesces bot into `STATE_HALT`.
  - `test_r3_state_extraction_and_standby_injection`: Verifies byte-identical state transfer with SHA-256 match.
  - `test_r3_standby_health_and_heartbeat_verification`: Verifies heartbeat freshness checks (<10s fresh, >120s stale, missing).
  - `test_r3_active_node_decommission_after_handover`: Verifies clean termination of active node without dual-trading.
  - `test_r3_cli_rotator_shift_and_dry_run_modes`: Verifies rotator CLI execution in `--check-shift` and `--dry-run` modes.
  - `test_r3_sandbox_provisioning_script_dependencies`: Verifies sandbox provisioning idempotency and dependency completeness.

### Tier 2: Boundary & Corner Cases (17 tests)
- `test_t2_r1_01_empty_state_json`: 0-byte `state.json` defaults safely to `STATE_IDLE`.
- `test_t2_r1_02_corrupt_state_and_risk_json`: Truncated JSON recovers to default state without crash.
- `test_t2_r1_03_missing_keys_and_type_mismatch`: Missing/invalid fields in state dictionary handled gracefully.
- `test_t2_r1_04_non_dict_root_state`: Non-dict JSON root (array/scalar) rejected, pristine defaults retained.
- `test_t2_r1_05_defensive_timestamp_parsing`: Empty DataFrame and NaN series handled without `ValueError`.
- `test_t2_r1_06_zero_or_negative_equity`: Equity <= 0 yields sizing = 0.0 and order rejection.
- `test_t2_r2_01_network_ssl_eof_marketdata`: SSL EOF triggers retry and maps to `[交易所行情异常]`.
- `test_t2_r2_02_socket_timeout_funding_cache`: Funding timeout falls back to cached stats (<60m old).
- `test_t2_r2_03_http_429_rate_limit_backoff`: HTTP 429 respects backoff and tags `[交易所行情异常]`.
- `test_t2_r2_04_max_backoff_cap_600s`: Idle backoff doubles and strictly caps at 600s.
- `test_t2_r2_05_threshold_boundaries_1_4_5_6_32`: Precise alert threshold checks at 1, 4, 5, 6, 32.
- `test_t2_r2_06_error_counter_hygiene_reset`: Polling tick resets `idle_api_error_count` to 0.
- `test_t2_r3_01_cooldown_active_vs_expired`: Sandboxes respect cooldownUntil deadline.
- `test_t2_r3_02_missing_state_files_fresh_node`: Fresh node boots cleanly with default state.
- `test_t2_r3_03_partial_state_handover_rejection`: Corrupt risk state halts handover and rolls back.
- `test_t2_r3_04_max_machines_quota_guard`: Serial destroy-before-create prevents machine quota violation.
- `test_t2_r3_05_loss_streak_cooldown_handover`: Loss streak cooldown preserved and honored across nodes.

### Tier 3: Cross-Feature Interactions (4 tests)
- `test_t3_cross_01_exchange_fail_and_llm_timeout`: Concurrent exchange drop and LLM timeout handled safely.
- `test_t3_cross_02_active_position_trailing_handover`: Trailing stop ratchets preserved across instance migration with zero naked risk.
- `test_t3_cross_03_software_error_during_retry`: Internal software error during backoff classified as software exception with exc_info.
- `test_t3_cross_04_watchdog_pause_resume_supervision`: Watchdog supervision accommodates pause/resume without false kill.

### Tier 4: Real-World Application Scenarios (3 Scenarios)
- `test_scenario_1_24h_cycle_simulation`: 24-hour simulation across 288 candle nodes without unhandled runtime crash, verifying day-boundary rollover and zero `NameError`.
- `test_scenario_2_network_brownout_alert_throttling`: 32 consecutive network hiccups during flat position throttled to <= 2 alerts, correctly tagged `[交易所行情异常]`, and reset cleanly upon recovery.
- `test_scenario_3_instance_handover_zero_drift`: End-to-end 6-Phase Handover between mock Node A and Node B with 0.000% state drift, continuous on-chain protective trigger retention, and clean active demotion.

---

## 6. Test Runner Specification & CLI Contract

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

---

## 7. Verification Checklist & Invariants
- [ ] R1: Zero `NameError: name 'pd' is not defined` across any code path.
- [ ] R1: All internal software exceptions classified as `[交易程序运行时异常]` with `exc_info=True`.
- [ ] R2: Exchange network exceptions retried with exponential backoff and classified as `[交易所行情异常]`.
- [ ] R2: LLM relay timeouts during flat position suppressed/decoupled and safely fall back to `quant_state`.
- [ ] R2: Alert throttling eliminates 32-alert storm (dispatches $\le 2$ alerts for 32 consecutive errors).
- [ ] R3: 6-Phase Handover Protocol preserves 100% state integrity with 0.000% state drift.
- [ ] R3: On-chain protective trigger stop-loss orders remain active and unbroken during handover.

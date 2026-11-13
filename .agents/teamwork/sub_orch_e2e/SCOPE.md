# Scope: E2E Testing Track

## Architecture
Opaque-box, requirement-driven test infrastructure exercising the Hyperliquid trading bot and cloud rotation tooling as an end-user / external environment.

- Test runner: Python standard `unittest` or `pytest` executable from workspace root via unified entry point.
- Target Directory: `tests/e2e/`
- Documentation: `TEST_INFRA.md` (architecture, test tiers, runner command, scenario descriptions) and `TEST_READY.md` (completion signal & coverage checklist).

## Feature Inventory
| # | Feature Area | Description | Test Tiers |
|---|--------------|-------------|------------|
| 1 | R1: Bot Runtime Stability | Syntax, imports, `pd` availability, software error classification vs external API exceptions, traceback logging | Tier 1 (>=5), Tier 2, Tier 3, Tier 4 |
| 2 | R2: API Alert Decoupling | Flat position idle cycle resilience, exchange retry/backoff, LLM timeout fallback to quant_state, alert throttling (max 1/30min), alert message disambiguation tags | Tier 1 (>=5), Tier 2, Tier 3, Tier 4 |
| 3 | R3: 24/7 Cloud Rotation | 6-phase state handover protocol, state file integrity validation (`state.json`, `risk_state_aggressive.json`), rotation script CLI, sandbox provisioning, failover recovery | Tier 1 (>=5), Tier 2, Tier 3, Tier 4 |

## Test Tiers Breakdown
- **Tier 1 - Feature Coverage (>=5 per feature area)**:
  - R1: Valid imports without NameError, clean loop execution, software exception tag check, traceback logging verification, defensive timestamp parsing.
  - R2: Exchange retry with backoff, idle cycle exception survival, LLM relay timeout fallback to quant_state, cold-start cooldown, alert throttling (single alert on repeated failures), unambiguous category prefix verification.
  - R3: Handover state extraction, state injection into standby, standby health verification, process liveness check, sandbox deploy dependency verification.
- **Tier 2 - Boundary & Corner Cases**:
  - Empty or corrupt state files, zero-balance or extreme equity values, network connection reset / SSL EOF, max backoff caps (min(idle_backoff * 2, 600)), cooldown time boundaries, consecutive error counter boundary (threshold 5).
- **Tier 3 - Cross-Feature Interactions (Pairwise Combinations)**:
  - Flat position network exception occurring concurrently with LLM relay timeout;
  - State handover executed while bot has an open active position with trailing stop;
  - Software exception occurring during exchange retry backoff phase;
  - Watchdog reaction during simulated rotation pause command.
- **Tier 4 - Real-World Application Scenarios**:
  - Scenario 1: Multi-iteration dry-run execution simulating 24-hour cycle nodes without runtime crash.
  - Scenario 2: Simulated network brownout during flat position: verify 32 consecutive network hiccups generate throttled and disambiguated alerts without crashing.
  - Scenario 3: End-to-end simulated instance handover between mock Node A and mock Node B with state persistence, verification, and zero state drift.

## Milestones & Status
| Step | Task | Status | Output Files |
|------|------|--------|--------------|
| 1 | Test Architecture Exploration | IN_PROGRESS | reports in `sub_orch_e2e/` |
| 2 | Test Infra & Suite Implementation | PLANNED | `TEST_INFRA.md`, `tests/e2e/*.py` |
| 3 | Review & Challenge | PLANNED | review reports |
| 4 | Forensic Integrity Audit | PLANNED | audit report |
| 5 | Publication of TEST_READY.md | PLANNED | `TEST_READY.md` |

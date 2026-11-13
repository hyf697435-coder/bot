# Dispatch: Test Writer (E2E Test Suite Implementation)

**Original Request Path**: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
**Project Document**: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
**Scope Document**: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_e2e\SCOPE.md
**Working Directory**: C:\Users\Administrator\Desktop\v1\a
**Your State Folder**: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_worker_e2e_1

**Input Explorer Reports**:
- Explorer 1 (Architecture & Tier 1 Specs): `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_e2e_1\handoff.md`
- Explorer 2 (Tier 2 Boundaries & Tier 3 Interactions): `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_e2e_2\handoff.md`
- Explorer 3 (Tier 4 Scenarios & TEST_INFRA Spec): `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_e2e_3\handoff.md`

**Write Ownership**:
You have exclusive write ownership of:
- `C:\Users\Administrator\Desktop\v1\a\TEST_INFRA.md`
- `C:\Users\Administrator\Desktop\v1\a\tests\e2e\` (and all files inside: `__init__.py`, `fixtures.py`, `test_tier1_*.py`, `test_tier2_*.py`, `test_tier3_*.py`, `test_tier4_*.py`, `run_all.py`)
- Your state folder: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_worker_e2e_1`
**CRITICAL**: You MUST NOT modify or overwrite implementation source files (`main.py`, `exchange.py`, `strategy.py`, `risk_manager.py`, `alerts.py`, `watchdog.py`, `dashboard.py`, `utils.py`).

**MANDATORY INTEGRITY WARNING**:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

**Task**:
1. Read `ORIGINAL_REQUEST.md`, `PROJECT.md`, `SCOPE.md`, and the three Explorer reports in detail.
2. Create `TEST_INFRA.md` at project root following Explorer 3's complete specification and template.
3. Implement hermetic mock fixtures in `tests/e2e/fixtures.py`:
   - `MockExchange` / `MockMarketData`: Simulates `/info` (candleSnapshot, allMids, fundingHistory) and `/exchange` (signed orders, protective trigger stop-loss/take-profit).
   - `MockLLMClient`: Intercepts `THIRD_PARTY_BASE_URL` (`api.ludies.best`), provides deterministic responses and timeout/failure simulations.
   - `MockMailbox`: Intercepts SMTP mail delivery, captures all alerts with timestamps, subjects, and bodies for verification.
   - `VirtualClock`: Controllable virtual time stepping for 24h dry-run acceleration.
4. Implement the complete 4-tier test suite in `tests/e2e/`:
   - `tests/e2e/test_tier1_r1_runtime.py` (6 tests from Explorer 1)
   - `tests/e2e/test_tier1_r2_alerts.py` (7 tests from Explorer 1)
   - `tests/e2e/test_tier1_r3_rotation.py` (7 tests from Explorer 1)
   - `tests/e2e/test_tier2_boundaries.py` (17 tests from Explorer 2)
   - `tests/e2e/test_tier3_interactions.py` (4 tests from Explorer 2)
   - `tests/e2e/test_tier4_scenarios.py` (3 comprehensive simulation scenarios from Explorer 3)
5. Implement `tests/e2e/run_all.py` as a unified test runner supporting `--tier`, `--verbose`, `--failfast`, formatted output, and exit codes:
   - 0: All passed
   - 1: Test failure
   - 2: Harness / environment error
   - 3: Invariant violation
6. Run the test suite using `python tests/e2e/run_all.py` and `python -m unittest discover -s tests/e2e -p "test_*.py"`.
   Verify all test cases execute cleanly and document the execution commands, results, and timing.
7. Write `handoff.md` in your state folder (`C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_worker_e2e_1\handoff.md`).
8. Update `progress.md` and send a completion message back to the orchestrator.

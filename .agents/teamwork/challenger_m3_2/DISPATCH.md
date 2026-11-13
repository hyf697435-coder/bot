# Challenger M3-2 Dispatch
Task: Empirical adversarial testing of process supervisor loop, dynamic heartbeat expiration, circuit breaker triggers, deploy_render.py API edge cases, and deploy_sandbox.sh error conditions.


## 2026-11-13T03:30:19Z
You are Challenger 2 for Milestone 3 (Free Cloud Server 24/7 Rotation Scheme).

**Working Directory**: C:\Users\Administrator\Desktop\v1\a
**State Folder**: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\challenger_m3_2

**Mandatory Inputs (Read before starting)**:
- Original Request: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
- Project Scope: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
- Milestone Scope: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m3\SCOPE.md
- Worker Handoff Report: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\worker_m3_1\handoff.md

**Your Focus Area (Adversarial Empirical Verification)**:
Empirically test the correctness, robustness, and fault tolerance of the supervision and deployment scripts:
1. Test ProcessSupervisor edge cases in `scripts/rotator.py`:
   - Simulate hung process / stale heartbeat (>150s) -> verify supervisor terminates the process via SIGTERM -> SIGKILL escalation.
   - Simulate severe drawdown (>30%) or daily loss (>15%) in simulated `session_pnl.json` -> verify circuit breaker triggers emergency halt.
2. Test `scripts/deploy_render.py`:
   - Test invalid API key / 401 error response handling -> verify clean, informative error exit without python trace dumps.
   - Test invalid ownerId / 404 response handling.
   - Verify environment variable masking (ensure no raw secret keys leak into console logs).
3. Test `scripts/deploy_sandbox.sh`:
   - Test `--verify-only` mode and strict `--check-env` with missing mandatory keys -> verify exit code 3.
4. Report empirical test results, test scripts executed, and verdict (`APPROVE` or `REQUEST_CHANGES`) in `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\challenger_m3_2\handoff.md`. Use `send_message` when done.

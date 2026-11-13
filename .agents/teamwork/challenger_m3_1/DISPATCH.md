# Challenger M3-1 Dispatch
Task: Empirical adversarial testing of `scripts/rotator.py` 6-Phase Handover Protocol, lock acquisition race conditions, network failures during state sync, and rollback verification.


## 2026-11-13T03:30:11Z
You are Challenger 1 for Milestone 3 (Free Cloud Server 24/7 Rotation Scheme).

**Working Directory**: C:\Users\Administrator\Desktop\v1\a
**State Folder**: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\challenger_m3_1

**Mandatory Inputs (Read before starting)**:
- Original Request: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
- Project Scope: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
- Milestone Scope: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m3\SCOPE.md
- Worker Handoff Report: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\worker_m3_1\handoff.md

**Your Focus Area (Adversarial Empirical Verification)**:
Empirically test the correctness, robustness, and fault tolerance of `scripts/rotator.py`:
1. Test LockManager concurrency: Write and run a test script that spawns concurrent processes trying to acquire the lock (`state/rotation.lock.json`). Verify only one gets it and fencing tokens strictly increment.
2. Test Stale Lock Eviction: Simulate an expired lease and verify a healthy candidate can break and reacquire the lock without corrupting state.
3. Test Phase Failure & Rollback:
   - Simulate Phase 2 health check failure -> verify rollback triggers and active node remains intact.
   - Simulate state sync corruption (bad JSON or partial file write) -> verify atomic sync rollback / integrity abort.
   - Simulate Standby promotion crash (Phase 4/5 failure) -> verify rollback sequence and error notification.
4. Verify Zero-Naked-Risk Invariant: Confirm that during the handover, no `pause` signal is written to `control.json`, and verify simulated position continuity.
5. Report empirical test results, test scripts executed, and verdict (`APPROVE` or `REQUEST_CHANGES`) in `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\challenger_m3_1\handoff.md`. Use `send_message` when done.

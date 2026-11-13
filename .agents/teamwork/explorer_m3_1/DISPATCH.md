## 2026-11-13T03:09:12Z

You are Explorer 1 for Milestone 3 (Free Cloud Server 24/7 Rotation Scheme).

**Working Directory**: C:\Users\Administrator\Desktop\v1\a
**State Folder**: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_1

**Mandatory Inputs (Read before starting)**:
- Original Request: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
- Project Scope: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
- Milestone Scope: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m3\SCOPE.md
- Survey 3 Report: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_survey_3\handoff.md

**Your Focus Area**:
Analyze and provide complete technical architecture and design specifications for `scripts/rotator.py` (Feature 12 & Feature 13):
1. Detailed implementation of the 6-Phase Handover Protocol:
   - Phase 1: Standby Init (spin up Standby container/sandbox, clone repo, install env)
   - Phase 2: Health Check (verify API keys, network ping, disk write, process ready)
   - Phase 3: State Lock & Sync (pessimistic lock on Active, flush in-memory state to disk, atomic push/pull of state files)
   - Phase 4: Standby Promotion (launch main.py on Standby with state, confirm trade loop starts)
   - Phase 5: Verification (verify Standby heartbeat, orderbook subscription, and trade execution)
   - Phase 6: Active Demotion & Clean Exit (graceful SIGTERM to old Active, verify process exit, release lock, shutdown container)
2. Command-line interface design for `rotator.py`: modes (e.g. `--role active|standby|orchestrator`, `--action handoff|run|status|rollback`), config file support, environment variable overrides.
3. Lock management: pessimistic lock format, TTL, stale lock recovery, collision prevention.
4. Process supervisor loop: launching `main.py`, logging, restart on unexpected crash, graceful termination (SIGTERM -> timeout -> SIGKILL).
5. Rollback mechanisms: automatic rollback to Active if Standby fails Phase 2, 4, or 5.
6. Write a comprehensive report in `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_1\handoff.md`.
Remember: You are read-only. Do not modify production files. Use send_message when done.

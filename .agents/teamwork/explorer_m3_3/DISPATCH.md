# Explorer M3-3 Dispatch
Assigned task: Technical specification & detailed architecture design for `DEPLOYMENT_24_7.md` and failure recovery / operational playbooks.


## 2026-11-13T03:09:29Z
You are Explorer 3 for Milestone 3 (Free Cloud Server 24/7 Rotation Scheme).

**Working Directory**: C:\Users\Administrator\Desktop\v1\a
**State Folder**: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_3

**Mandatory Inputs (Read before starting)**:
- Original Request: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
- Project Scope: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
- Milestone Scope: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m3\SCOPE.md
- Survey 3 Report: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_survey_3\handoff.md

**Your Focus Area**:
Analyze and provide complete technical architecture and design specifications for `DEPLOYMENT_24_7.md` (Feature 16) and end-to-end failure resilience:
1. `DEPLOYMENT_24_7.md` structure & completeness:
   - High-level architecture: Ephemeral cloud sandbox model, 6-hour rotation ring, zero-cost operational model
   - Step-by-step account onboarding: Account A & Account B setup on Instabox / GitHub Actions
   - Secrets and environment configuration guide (.env, GitHub repository secrets)
   - Provisioning walkthrough using `scripts/deploy_sandbox.sh`
   - Orchestration execution using `scripts/rotator.py`
   - State synchronization architecture: remote storage options (GitHub Gist, S3-compatible, Redis/KV, or encrypted repo commit) vs direct SSH/HTTPS push-pull
   - Operational runbook: status checks, manual force handover, emergency rollback, logs inspection
2. Edge cases and failure mode analysis:
   - Network partition during state sync in Phase 3
   - Standby crash or timeout during Phase 4/5
   - Active crash before scheduled rotation (unplanned failover / heartbeat expiration)
   - Stale lock resolution and split-brain prevention (ensuring never 2 active trading bots simultaneously)
3. Write a comprehensive report in `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_3\handoff.md`.
Remember: You are read-only. Do not modify production files. Use send_message when done.

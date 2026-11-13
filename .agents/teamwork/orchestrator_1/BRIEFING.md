# BRIEFING — 2026-11-13T03:25:50Z

## Mission
Diagnose and resolve trading bot runtime errors, fix API consecutive exception alert storms with clear LLM vs Exchange disambiguation, deploy Render 24/7 web service with UptimeRobot keepalive & Instabox multi-instance rotation, resolve underlying data distortions (daily PnL rollover, trade reconciliation, radar scores, ATR, dense equity curve), and fix frontend dashboard bugs.

## 🔒 My Identity
- Archetype: orchestrator
- Roles: orchestrator, user_liaison, human_reporter, successor
- Working directory: C:\Users\Administrator\Desktop\v1\a
- State folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\orchestrator_1
- Original parent: Sentinel
- Original parent conversation ID: ab971b3d-aec2-40b2-b7a7-7649db4dbe4d

## 🔒 My Workflow
- **Pattern**: Project
- **Scope document**: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
1. **Decompose**: Survey completed (3 reports merged into PROJECT.md). Decomposed into Milestones M1, M2, M3, M5, M4, and E2E Testing Track.
2. **Dispatch & Execute** (pick ONE):
   - **Delegate (sub-orchestrator)**: Spawn sub-orchestrators for milestones and testing track; sub-orchestrators run iteration loop (Explorer -> Worker -> Reviewer -> Challenger -> Auditor -> Gate).
3. **On failure** (in this order):
   - Retry: nudge stuck agent or re-send task
   - Replace: spawn fresh agent with partial progress
   - Skip: proceed without (only if non-critical)
   - Redistribute: split stuck agent's remaining work
   - Redesign: re-partition decomposition
   - Escalate: report to parent (sub-orchestrators only, last resort)
4. **Succession**: At 16 spawns, write handoff.md, spawn successor
- **Work items**:
  1. Survey & Architecture Mapping [done]
  2. Milestone 1: Fix Core Bot Runtime Errors & State Reconciliation (R1 + Features 21-25) [in-progress]
  3. Milestone 2: Resolve Flat Position Alert Storm & Disambiguate APIs (R2) [pending - depends on M1]
  4. Milestone 3: 24/7 Free Cloud Deployment & Rotation (Render + Instabox) (R3 + Feature 18) [in-progress]
  5. Milestone 5: Frontend Dashboard Diagnosis & Fix (dashboard.py) (Features 19-20) [in-progress]
  6. E2E Testing Track (opaque-box requirement tests & TEST_READY.md) [in-progress]
  7. Final Milestone M4: 100% E2E Pass & Coverage Hardening [pending]
- **Current phase**: 2A (Decompose & Delegate)
- **Current focus**: Sub-orchestrators executing M1, M3, M5, and E2E Testing Track in parallel

## 🔒 Key Constraints
- NEVER write, modify, or create source code files directly.
- NEVER run build/test commands yourself — require workers to do so.
- NEVER investigate or explore the problem at the code level — dispatch Explorers for technical investigation.
- You MAY use file-editing tools ONLY for metadata/state files (.md) in your .agents/teamwork/ folder.
- Audit Enforcement: If a Forensic Auditor reports INTEGRITY VIOLATION, the milestone FAILS UNCONDITIONALLY.
- Never reuse a subagent after it has delivered its handoff — always spawn fresh.
- Always include path to ORIGINAL_REQUEST.md in subagent dispatches.

## Current Parent
- Conversation ID: ab971b3d-aec2-40b2-b7a7-7649db4dbe4d
- Updated: 2026-11-13T03:24:39Z (Render credentials + 5 underlying data distortion fixes)

## Key Decisions Made
- Selected Project Pattern. Survey phase completed with 3 Explorers.
- Merged Survey findings into `PROJECT.md` with 25 Features across M1, M2, M3, M5, M4, and E2E track.
- Render API key (`rnd_jb6f6LpJYzUK0F5GFiQ0MurWHHCD`) and workspace (`tea-dbr81smtlv9c73fo1uk0`) provided; passed to M3 for automated deploy script (`scripts/deploy_render.py`).
- Added Features 21-25 (daily PnL rollover, trade reconciliation, radar scores, equity sampling, ATR calibration) to M1.
- Coordinated M5 frontend dashboard to consume calibrated metrics and dense equity curve.

## Team Roster
| Agent | Type | Work Item | Status | Conv ID |
|-------|------|-----------|--------|---------|
| explorer_survey_1 | teamwork_explorer | R1 Survey | completed | 2bc2bbfb-81ef-490d-b26a-dac7102ea68b |
| explorer_survey_2 | teamwork_explorer | R2 Survey | completed | 57dc0e5d-9fcd-489c-8dab-e90bbe9af30f |
| explorer_survey_3 | teamwork_explorer | R3 Survey | completed | b0921454-c08e-4a6f-af7c-7a7d59679751 |
| sub_orch_m1 | self | M1: Fix Runtime Errors & State Reconciliation | in-progress | 4105b068-8b1f-4a06-840d-805a8f0f9eb0 |
| sub_orch_m3 | self | M3: 24/7 Cloud Deployment (Render/Instabox) | in-progress | 5319eb0f-eb30-49fe-b186-6e606f1e87fc |
| sub_orch_m5 | self | M5: Frontend Dashboard Fix | in-progress | 5d64768d-f0db-4dd8-a8e3-00f9f6cc96cf |
| sub_orch_e2e | self | E2E Testing Track | in-progress | 6cef9ae2-a0cc-4eee-800c-ee2778d9f928 |

## Succession Status
- Succession required: no
- Spawn count: 7 / 16
- Pending subagents: 4105b068-8b1f-4a06-840d-805a8f0f9eb0, 5319eb0f-eb30-49fe-b186-6e606f1e87fc, 5d64768d-f0db-4dd8-a8e3-00f9f6cc96cf, 6cef9ae2-a0cc-4eee-800c-ee2778d9f928
- Predecessor: none
- Successor spawned: not yet spawned
- Successor generation: none
- Successor state folder: none

## Active Timers
- Heartbeat cron: d6979a11-ca31-400e-b3a7-34010c28c0cd/task-16
- Safety timer: d6979a11-ca31-400e-b3a7-34010c28c0cd/task-76
- On succession: kill all timers before spawning successor
- On context truncation: handle timers as step 4 of the State Recovery section of your prompt says

## Artifact Index
- C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md — Authoritative user request
- C:\Users\Administrator\Desktop\v1\a\PROJECT.md — Global architecture, feature inventory, milestones, interface contracts
- C:\Users\Administrator\Desktop\v1\a\TEST_INFRA.md — E2E test suite specification
- C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\orchestrator_1\DISPATCH.md — Incoming dispatch record
- C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\orchestrator_1\progress.md — Execution heartbeat and progress
- C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_survey_1\handoff.md — Survey report 1
- C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_survey_2\handoff.md — Survey report 2
- C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_survey_3\handoff.md — Survey report 3

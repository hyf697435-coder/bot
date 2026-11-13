## 2026-11-13T02:55:47Z
**Identity & Workspace**:
You are a teamwork_explorer.
Working directory: C:\Users\Administrator\Desktop\v1\a
State folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_survey_3
Original Request Path: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md

**Objective**:
Investigate the requirements and design for the Free Cloud Server 24/7 Rotation Scheme (R3 Focus).
Specifically investigate:
1. State persistence in the trading bot: How does the bot save state, position info, order history, configuration, and credentials? What files or artifacts must be synchronized between instances during a handover?
2. Free cloud environments: Investigate free cloud tiers (specifically instabox sandboxes or similar free tiers mentioned in the skills or available tools). Check the instabox skill at `C:\Users\Administrator\.gemini\config\skills\instabox\SKILL.md` or check system command `instabox` to see how boxes are created, started, paused, snapshotted, or destroyed, and their quota/time limits (e.g. idle auto-pause, daily execution limits).
3. Rotation & Failover Architecture: How can an automated rotation script alternate instances safely before quota exhaustion? What is the handover sequence (graceful stop instance A -> sync state/position -> start instance B -> health check -> decommission instance A)?
4. Deployment requirements: What dependencies, environment variables, credentials, and scheduling mechanisms (e.g., Python rotation daemon, cron, or GitHub Actions / local controller) are needed?

**Scope Boundaries**:
- You are READ-ONLY. DO NOT modify any source code files.

**Input Information**:
- Read C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md first.
- Read `C:\Users\Administrator\.gemini\config\skills\instabox\SKILL.md`, `instabox-snapshots\SKILL.md`, etc.
- Inspect files in C:\Users\Administrator\Desktop\v1\a.

**Output Requirements**:
Write a comprehensive report to `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_survey_3\handoff.md` with:
- State Analysis: exact files, paths, and data structures needed for handover
- Free Cloud Platform Analysis: capabilities, quotas, CLI commands for instabox sandboxes
- Proposed 24/7 Multi-Instance Rotation & Scheduling Architecture
- Step-by-step handover procedure, failure recovery plan, and deployment guide structure
- Verification method

When finished, send a message to the orchestrator summarizing completion and the handoff file path.

## 2026-11-13T03:16:52Z
You are the Worker for Milestone 3 (Free Cloud Server 24/7 Rotation Scheme).

**Working Directory**: C:\Users\Administrator\Desktop\v1\a
**State Folder**: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\worker_m3_1

**Mandatory Inputs (Read before starting)**:
- Original Request: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
- Project Scope: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
- Milestone Scope: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m3\SCOPE.md
- Architecture Synthesis: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m3\SYNTHESIS.md
- Explorer 1 Report: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_1\handoff.md
- Explorer 2 Report: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_2\handoff.md
- Explorer 3 Report: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_3\handoff.md
- Proposed Deployment Guide: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_3\proposed_DEPLOYMENT_24_7.md

**MANDATORY INTEGRITY WARNING**:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

**File Boundaries & Exclusive Ownership**:
You EXCLUSIVELY own and will create/modify:
- `scripts/rotator.py`
- `scripts/deploy_sandbox.sh`
- `.github/workflows/rotation.yml`
- `Dockerfile`
- `render.yaml`
- `DEPLOYMENT_24_7.md`
You MUST NOT modify: `main.py`, `exchange.py`, or any files in `tests/`.

**Implementation Requirements**:
1. `scripts/rotator.py`:
   - Complete, genuine, standalone Python script.
   - Implement the 6-Phase Zero-Naked-Risk Handover Protocol (Standby Init, Health Check, State Lock & Sync via atomic write and process SIGTERM, Standby Promotion, Verification, Active Demotion).
   - CRITICAL: Do NOT write `pause` to `control.json` because `main.py` liquidates positions on pause; use SIGTERM/graceful termination so market positions remain open and state is saved cleanly.
   - CLI flags: `--role` (active, standby, orchestrator, supervisor), `--action` (handoff, run, health-check, status, rollback), `--config`, `--interval`, `--dry-run`, etc.
   - Pessimistic lock manager (`state/rotation.lock.json`) with lease TTL and fencing token protection.
   - Process supervisor for `main.py` with heartbeat monitoring and graceful SIGTERM -> 15s timeout -> SIGKILL escalation.
   - Automated multi-stage rollback logic if health checks fail.
2. `scripts/deploy_sandbox.sh`:
   - Production bash script for provisioning on Instabox sandboxes, GitHub Actions runners, and Ubuntu/Debian containers.
   - Virtualenv setup, pip install, inline dependency check for critical modules, masked .env credential validation, state directory preparation, distinct POSIX exit codes.
3. `.github/workflows/rotation.yml`:
   - Cron every 6 hours (`0 */6 * * *`) + `workflow_dispatch` with input parameters.
   - Concurrency group `trading-bot-rotation-ring` with `cancel-in-progress: false`.
   - Dual-account orchestration, secret injection, rotator execution, and failure alerting.
4. `Dockerfile`:
   - Production multi-stage or clean Python container image for Render / cloud deployment, installing dependencies, copying application files, exposing PORT for Dashboard, and launching main bot + dashboard.
5. `render.yaml`:
   - Render Web Service blueprint specification for 24/7 continuous hosting (750 free hours/month tier).
6. `DEPLOYMENT_24_7.md`:
   - Comprehensive production guide covering:
     * Architecture overview
     * Track 1: Render Cloud Deployment (Primary recommended 24/7 free tier) + UptimeRobot keepalive probe configuration (5-min HTTP ping to Dashboard)
     * Track 2: Dual-Account Ephemeral Sandbox Rotation (Instabox + GitHub Actions 6-hour ring)
     * 6-Phase Handover Protocol technical breakdown
     * Step-by-step setup, secrets configuration, verification commands, and failover runbooks.

**Verification Requirements**:
You must run verification commands for your work:
- Run Python syntax checks: `python -m py_compile scripts/rotator.py`
- Test `scripts/rotator.py --help` and dry-run execution
- Validate YAML files and shell script syntax
Document all execution commands and test outputs in your handoff report at `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\worker_m3_1\handoff.md`. Use `send_message` when done.

## 2026-11-13T03:25:55Z
**Context**: Render Credentials & Automated Deployment Script for Milestone 3 (Feature 18 Update)
**Content**: The user has provided Render credentials:
- Workspace: 塔菲's workspace (ID: `tea-dbr81smtlv9c73fo1uk0`)
- Render API Key: `rnd_jb6f6LpJYzUK0F5GFiQ0MurWHHCD`

Please implement `scripts/deploy_render.py` as an additional deliverable owned by M3:
- Standalone Python script using `urllib.request` or `requests` to call Render REST API (`https://api.render.com/v1/services` and related endpoints).
- Support creating or updating the Web Service using the provided ownerId (`tea-dbr81smtlv9c73fo1uk0`), referencing repo or Docker image, setting environment variables, and configuring health check path.
- Support reading `RENDER_API_KEY` from environment or `--api-key` flag (defaulting to the provided key if not set in env).
- CLI arguments: `--action [create|deploy|status|list]`, `--service-id`, `--name`, etc.
- Validate Python syntax with `python -m py_compile scripts/deploy_render.py` and test `--help`.
- Update `DEPLOYMENT_24_7.md` with instructions on running `python scripts/deploy_render.py`.
**Action**: Implement `scripts/deploy_render.py`, verify syntax, and document it in your `handoff.md`.

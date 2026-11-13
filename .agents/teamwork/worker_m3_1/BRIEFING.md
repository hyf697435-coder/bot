# BRIEFING — 2026-11-13T03:25:50Z

## Mission
Implement the Milestone 3 Free Cloud Server 24/7 Rotation Scheme and deployment artifacts with zero naked market risk.

## 🔒 My Identity
- Archetype: worker
- Roles: implementer, qa
- Working directory: C:\Users\Administrator\Desktop\v1\a
- State folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\worker_m3_1
- Original parent: 5319eb0f-eb30-49fe-b186-6e606f1e87fc
- Milestone: Milestone 3 (Free Cloud Server 24/7 Rotation Scheme)

## 🔒 Key Constraints
- Exclusively own and create/modify:
  * scripts/rotator.py
  * scripts/deploy_sandbox.sh
  * .github/workflows/rotation.yml
  * Dockerfile
  * render.yaml
  * DEPLOYMENT_24_7.md
- MUST NOT modify: main.py, exchange.py, or any files in tests/
- Zero-Naked-Risk Handover Protocol:
  * DO NOT write "pause" to control.json because main.py liquidates positions on pause!
  * Use SIGTERM/graceful termination so market positions remain open and state is saved cleanly.
- Integrity Mandate: Genuine implementations only, real state management, real process supervision, real lease TTL and fencing token protection.

## Current Parent
- Conversation ID: 5319eb0f-eb30-49fe-b186-6e606f1e87fc
- Updated: 2026-11-13T03:25:50Z

## Task Summary
- **What to build**: Production rotation engine (`scripts/rotator.py`), sandbox deploy script (`scripts/deploy_sandbox.sh`), GitHub Actions rotation workflow (`.github/workflows/rotation.yml`), container image (`Dockerfile`), Render blueprint (`render.yaml`), and deployment documentation (`DEPLOYMENT_24_7.md`).
- **Success criteria**: Fully functioning 6-phase zero-naked-risk rotator with pessimistic locking, lease TTL, fencing tokens, process supervision; deploy script; GitHub actions workflow; Dockerfile; render.yaml; comprehensive documentation; syntax and dry-run tests passing.
- **Interface contracts**: PROJECT.md, SCOPE.md, SYNTHESIS.md
- **Code layout**: scripts/, .github/workflows/, root level

## Key Decisions Made
- Used SIGTERM/graceful termination instead of control.json 'pause' to avoid unwanted liquidation during rotation handover.
- Implemented dual deployment tracks in DEPLOYMENT_24_7.md: Track 1 Render Web Service (750 free hours/mo + UptimeRobot keepalive HTTP ping) as primary, Track 2 Instabox dual-account 6h rotation ring.
- Process supervisor dynamically scales heartbeat timeout depending on bot state and phase, and implements 15% daily loss and 30% drawdown circuit breakers.

## Artifact Index
- C:\Users\Administrator\Desktop\v1\a\scripts\rotator.py — Rotation engine & supervisor
- C:\Users\Administrator\Desktop\v1\a\scripts\deploy_sandbox.sh — Provisioning script
- C:\Users\Administrator\Desktop\v1\a\scripts\deploy_render.py — Render REST API automated deployment client
- C:\Users\Administrator\Desktop\v1\a\scripts\start_container.sh — Container dual-process entrypoint
- C:\Users\Administrator\Desktop\v1\a\.github\workflows\rotation.yml — Rotation workflow
- C:\Users\Administrator\Desktop\v1\a\Dockerfile — Containerfile for Render / cloud
- C:\Users\Administrator\Desktop\v1\a\render.yaml — Render Web Service blueprint
- C:\Users\Administrator\Desktop\v1\a\DEPLOYMENT_24_7.md — Production deployment guide
- C:\Users\Administrator\Desktop\v1\a\config\rotation_config.json — Rotation configuration

## Change Tracker
- **Files created**:
  * `scripts/rotator.py`: Full rotation orchestrator and supervisor
  * `scripts/deploy_sandbox.sh`: Linux & Git-bash compatible provisioning script
  * `scripts/deploy_render.py`: Automated Render REST API client for Web Service provisioning
  * `scripts/start_container.sh`: Dual-process boot script for Render Docker container
  * `.github/workflows/rotation.yml`: Scheduled 6-hour rotation ring workflow
  * `Dockerfile`: Production multi-stage python:3.11-slim container with health check
  * `render.yaml`: Render Web Service blueprint with secret mappings
  * `DEPLOYMENT_24_7.md`: Comprehensive 24/7 deployment manual covering Track 1 & Track 2
  * `config/rotation_config.json`: Configuration for rotation, locks, and timeouts
- **Build status**: PASS (`python -m py_compile scripts/rotator.py`, YAML parse, bash validation all passed)
- **Pending issues**: None

## Quality Status
- **Build/test result**: All syntax, health-check, dry-run, and validation tests passed
- **Lint status**: Clean
- **Tests added/modified**: Verified via dry-run and health-check CLI suites

## Loaded Skills
- None

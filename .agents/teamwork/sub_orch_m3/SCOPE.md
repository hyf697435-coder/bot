# Scope: Milestone 3 - Free Cloud Server 24/7 Rotation Scheme

## Architecture
- Ephemeral cloud sandbox rotation (e.g. Instabox, dual-account ring) ensuring 24/7 automated trading without downtime.
- Proactive 6-hour rotation ring across dual accounts (Active/Standby).
- 6-Phase Zero-Naked-Risk Handover Protocol ensuring the bot is never completely down and never double-trading.
- Atomic state file synchronization (`state/active_strategy.json`, `state/session_pnl.json`) and heartbeat monitoring.
- Health check verification, pessimistic locking, and automatic rollback on failure.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 12 | 6-Phase Handover Protocol | Zero-naked-risk state handover: Phase 1 Standby Init, Phase 2 Health Check, Phase 3 State Lock & Sync, Phase 4 Standby Promotion, Phase 5 Verification, Phase 6 Active Demotion & Clean Exit | M3 | Survey 3 & ORIGINAL_REQUEST |
| 13 | Rotation Orchestrator (`scripts/rotator.py`) | Standalone Python script managing 6h proactively scheduled ring, dual-account alternation, lock management, state sync, failure detection, and rollback | M3 | Survey 3 & ORIGINAL_REQUEST |
| 14 | Instance Provisioner (`scripts/deploy_sandbox.sh`) | Bash provisioning script installing Python dependencies, verifying imports, configuring directories, and testing environment | M3 | Survey 3 & ORIGINAL_REQUEST |
| 15 | GitHub Actions Automation (`.github/workflows/rotation.yml`) | Workflow triggering proactive rotation every 6 hours via schedule and workflow_dispatch | M3 | Survey 3 & ORIGINAL_REQUEST |
| 16 | 24/7 Deployment Guide (`DEPLOYMENT_24_7.md`) | Comprehensive documentation detailing architecture, provisioning, dual-account rotation, credential setup, failover, and operational playbooks | M3 | Survey 3 & ORIGINAL_REQUEST |
| 18 | Render Cloud Deployment (`Dockerfile`, `render.yaml`, `scripts/deploy_render.py`) | Primary 24/7 free cloud deployment on Render (750h/mo): Dockerfile packaging bot + dashboard + watchdog, render.yaml blueprint, automated API deployment script `scripts/deploy_render.py`, and UptimeRobot keepalive configuration | M3 | User Decision & Credentials (2026-11-13) |

## Code Layout Ownership
- `scripts/rotator.py` (Owned by M3)
- `scripts/deploy_sandbox.sh` (Owned by M3)
- `scripts/deploy_render.py` (Owned by M3)
- `.github/workflows/rotation.yml` (Owned by M3)
- `DEPLOYMENT_24_7.md` (Owned by M3)
- `Dockerfile` (Owned by M3)
- `render.yaml` (Owned by M3)
- `main.py` (READ-ONLY, owned by M1)
- `exchange.py` (READ-ONLY, owned by M1)
- `tests/*` (READ-ONLY, owned by E2E Testing track)

## Interface Contracts
- **Process Management**: `scripts/rotator.py` executes, monitors, and terminates `main.py` via standard OS process controls (subprocess, PID tracking, SIGTERM graceful shutdown with configurable timeout, SIGKILL fallback).
- **State Management**: Atomic state syncing handles files in `state/` directory (e.g., `state/active_strategy.json`, `state/session_pnl.json`, `state/heartbeat.json`).
- **Health Verification**: Standby verifies network connectivity, exchange API credentials, disk write access, and process responsiveness before handover lock is released.
- **Rollback Contract**: If Standby fails health check in Phase 5, lock reverts to Active instance, abort signal sent, alert logged.

# Synthesis: Milestone 3 (Free Cloud Server 24/7 Rotation Scheme)

## Consensus Architecture
1. **Core Philosophy**:
   - Zero-naked-risk continuous 24/7 trading without incurring infrastructure costs.
   - Two complementary deployment models:
     - **Primary Production Model (Render Web Service)**: Continuous deployment with 750 free hours/month on Render, sustained by UptimeRobot external keepalive pings every 5 minutes targeting the Dashboard HTTP endpoint.
     - **Ephemeral Cloud Rotation Ring (Instabox / GitHub Actions)**: Multi-instance rotation ring proactively alternating between dual accounts every 6 hours (360 min) before the 8-hour ephemeral cap.
2. **6-Phase Handover Protocol (`scripts/rotator.py`)**:
   - Phase 1: Standby Init (spin up container, pull repo, prepare env)
   - Phase 2: Health Check (verify credentials, network ping, disk write, process ready)
   - Phase 3: State Lock & Sync (pessimistic lock, state flush via SIGTERM, atomic sync of `state/*.json`)
     * **CRITICAL ARCHITECTURAL RULE**: Never write `pause` to `control.json` because `main.py:323` executes `_enter_system_halt()` which liquidates all open positions (`close_all()`). Instead, send OS `SIGTERM` to allow `main.py` to save its state cleanly and exit without market liquidation.
   - Phase 4: Standby Promotion (launch `main.py` on Standby, monitor startup)
   - Phase 5: Verification (verify heartbeat update, orderbook sync, position state)
   - Phase 6: Active Demotion & Clean Exit (verify old Active stopped, release lock, shutdown old sandbox)
3. **Provisioning (`scripts/deploy_sandbox.sh`)**:
   - Idempotent script for Debian/Ubuntu, Instabox sandboxes, and GitHub Actions runners.
   - Virtualenv isolation, pip dependency install, Python import validation test, masked `.env` check, directory creation.
4. **Automation (`.github/workflows/rotation.yml`)**:
   - Cron every 6 hours (`0 */6 * * *`) + `workflow_dispatch`.
   - Concurrency group `trading-bot-rotation-ring` with `cancel-in-progress: false`.
   - Dual-account secret management and alerting on failure.
5. **Render Cloud Deployment (`Dockerfile` & `render.yaml`)**:
   - Production Docker container running the trading bot alongside Dashboard on `$PORT`.
   - Blueprint `render.yaml` defining web service spec, health check path, and environment variables.
6. **Documentation (`DEPLOYMENT_24_7.md`)**:
   - Comprehensive documentation covering Render setup + UptimeRobot keepalive, alongside Instabox rotation setup, secrets, failure recovery runbooks, and CLI commands.

## File Boundaries & Ownership
- `scripts/rotator.py` (Owned by M3 Worker)
- `scripts/deploy_sandbox.sh` (Owned by M3 Worker)
- `.github/workflows/rotation.yml` (Owned by M3 Worker)
- `Dockerfile` (Owned by M3 Worker)
- `render.yaml` (Owned by M3 Worker)
- `DEPLOYMENT_24_7.md` (Owned by M3 Worker)
- `main.py`, `exchange.py`, `tests/*` (READ-ONLY, MUST NOT MODIFY)

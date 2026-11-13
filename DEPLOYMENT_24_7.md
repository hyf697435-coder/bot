# 24/7 Cloud Deployment & Multi-Instance High-Availability Architecture Guide

**Document Version**: 2.0.0  
**Target System**: Hyperliquid Quantitative Trading Bot & Terminal Dashboard  
**Classification**: Production Deployment Manual  
**Last Updated**: 2026-11-13  

---

## 1. Architecture Overview & High-Availability Topology

To ensure zero-downtime, continuous 24/7/365 algorithmic execution without recurring infrastructure hosting costs, the system supports two battle-tested production tracks:

```
                                  ┌────────────────────────────────────────────────────────┐
                                  │           PRODUCTION DEPLOYMENT TRACKS                 │
                                  └────────────────────────────────────────────────────────┘
                                                               │
                     ┌─────────────────────────────────────────┴─────────────────────────────────────────┐
                     ▼                                                                                   ▼
   ┌──────────────────────────────────────────────────┐                                ┌──────────────────────────────────────────────────┐
   │       TRACK 1: RENDER CLUSTER (PRIMARY)          │                                │       TRACK 2: DUAL-ACCOUNT ROTATION RING        │
   │  • 750 Free Hours / Month Web Service            │                                │  • Dual Ephemeral Instabox Sandboxes (A & B)     │
   │  • Docker Container (Bot Supervisor + Dashboard) │                                │  • 6-Hour Proactive Shift Ring (360 min)         │
   │  • UptimeRobot 5-Min Keepalive HTTP Probe        │                                │  • Serverless Coordinator via GitHub Actions     │
   │  • 100% Zero-Maintenance Continuous Hosting      │                                │  • 6-Phase Zero-Naked-Risk Handover Protocol     │
   └──────────────────────────────────────────────────┘                                └──────────────────────────────────────────────────┘
```

### Core Tenets of the System:
1. **Zero-Naked-Risk Handover Invariant**: Stop-loss and take-profit orders reside natively on Hyperliquid's validator matching engine (`is_trigger=True`, `reduce_only=True`). In the event of process restarts or instance migration, active positions are 100% protected on-chain against market flash-crashes.
2. **Never Liquidate on Migration**: During instance transitions, `scripts/rotator.py` uses graceful OS signals (`SIGTERM`/`SIGINT`). The system **strictly forbids** writing `"pause"` to `control.json`, because in `main.py:323`, `"pause"` triggers `_enter_system_halt()` which liquidates all market positions at market price.
3. **Pessimistic Lease Locking & Fencing Tokens**: Distributed state is guarded by `state/rotation.lock.json` with lease TTLs and monotonic fencing tokens to eliminate split-brain double trading.

---

## 2. Track 1: Render Cloud Deployment (Primary 24/7 Free Tier)

Render provides **750 free instance hours per month** for Web Services, which is sufficient for continuous 24/7 operation across a full 31-day month (744 hours).

### 2.1 The Free Tier Sleeping Constraint & UptimeRobot Solution
- **The Problem**: Render free Web Services automatically spin down (sleep) after **15 minutes of zero inbound HTTP traffic**. While sleeping, background loops stop executing.
- **The Solution**: An external keepalive HTTP probe pinging the Web Dashboard's `/api/state` or `/` endpoint every **5 minutes** (via [UptimeRobot](https://uptimerobot.com), [BetterUptime](https://betteruptime.com), or [Cron-Job.org](https://cron-job.org)). This ensures Render never spins down the container, maintaining 100% 24/7 bot uptime at $0.00 cost.

### 2.2 Container Dual-Process Topology (`Dockerfile` + `scripts/start_container.sh`)
Inside the container, two coordinated processes run concurrently:
1. **Interactive Web Dashboard (`dashboard.py`)**: Listens on dynamic `$PORT` (assigned by Render, defaults to `10000`), serving the real-time trading UI, system health, and HTTP routes.
2. **Process Supervisor (`scripts/rotator.py --role active --action run`)**: Supervises `main.py`, monitors `.heartbeat` freshness, executes dynamic timeouts, and terminates runaway loops.

### 2.3 Step-by-Step Render Deployment Guide

#### Step 1: Push Repository to GitHub
Ensure your repository is hosted on GitHub (Public or Private).

#### Step 2: Create Web Service on Render
1. Log in to [Render Dashboard](https://dashboard.render.com).
2. Click **New +** > **Blueprint** (or **Web Service**).
3. Connect your GitHub repository.
4. If using **Blueprint**, Render automatically reads `render.yaml`. If creating manually, set:
   - **Name**: `hyperliquid-trading-bot`
   - **Environment**: `Docker`
   - **Region**: `Oregon (US West)` or `Frankfurt (EU Central)`
   - **Branch**: `main`
   - **Plan**: `Free`
   - **Health Check Path**: `/api/state`

#### Step 3: Configure Environment Variables on Render
Under the **Environment** tab, set the following secrets:

| Variable Name | Required | Value / Description | Example |
| :--- | :--- | :--- | :--- |
| `HYPE_USER_ADDRESS` | **Yes** | Your main Hyperliquid wallet address (42 hex chars) | `0x0a65...66de` |
| `HYPE_SECRET_KEY` | **Yes** | ECDSA private key for API signing (64/66 hex chars) | `0x5a86...fe0b` |
| `HYPE_API_WALLET_ADDRESS`| No | Agent wallet address if using separate API agent | `0x...` |
| `MAINTENANCE_LOCK` | **Yes** | Set to `false` to enable live trading engine execution | `false` |
| `THIRD_PARTY_BASE_URL` | No | OpenAI-compatible LLM proxy endpoint | `https://api.ludies.best/v1` |
| `THIRD_PARTY_API_KEY` | No | Proxy API access token | `sk-...` |
| `MODEL_MACRO` | No | Macro evaluation model identifier | `deepseek-chat` |
| `MODEL_AI_TRADE` | No | Trade proposal model identifier | `deepseek-reasoner` |
| `MAIL_ENABLED` | No | `true` if SMTP email alerts are desired | `false` |
| `TELEGRAM_BOT_TOKEN` | No | Telegram Bot Token for push alerts | `123456:ABC...` |
| `TELEGRAM_CHAT_ID` | No | Telegram Chat ID for push alerts | `987654321` |

#### Step 4: Deploy and Verify Service
1. Click **Create Web Service** (or **Manual Deploy**).
2. Monitor build logs: dependencies install via `pip`, image compiles, and container spawns.
3. Once deployed, verify your service URL (e.g. `https://hyperliquid-trading-bot.onrender.com`).
4. Access `https://hyperliquid-trading-bot.onrender.com/` in your browser to view the live Web Dashboard.

### 2.4 UptimeRobot Keepalive Probe Setup (Anti-Sleep)
1. Register for a free account at [UptimeRobot](https://uptimerobot.com).
2. Click **+ Add New Monitor**.
3. Configure the monitor:
   - **Monitor Type**: `HTTP(s)`
   - **Friendly Name**: `Hyperliquid Bot Render Keepalive`
   - **URL (or IP)**: `https://hyperliquid-trading-bot.onrender.com/api/state` (or root `/`)
   - **Monitoring Interval**: `5 minutes`
   - **Monitor Timeout**: `30 seconds`
4. Click **Create Monitor**.
5. **Outcome**: UptimeRobot will ping the service every 5 minutes. Render will recognize continuous inbound HTTP traffic and **never spin down** the trading bot.

### 2.5 Automated Deployment via Render REST API (`scripts/deploy_render.py`)

For automated zero-touch provisioning and continuous updates, `scripts/deploy_render.py` interacts directly with Render's REST API (`https://api.render.com/v1`).

#### Pre-Configured Credentials
- **Workspace Name**: 塔菲's workspace
- **Workspace Owner ID**: `tea-dbr81smtlv9c73fo1uk0`
- **Render API Key**: Configured in script default or via `RENDER_API_KEY` environment variable.

#### Available Commands:
```bash
# 1. List all active services in the workspace
python scripts/deploy_render.py --action list

# 2. Check deployment and health status of the trading bot service
python scripts/deploy_render.py --action status --name hyperliquid-trading-bot

# 3. Create a new Web Service automatically with all .env variables injected
python scripts/deploy_render.py --action create \
    --name hyperliquid-trading-bot \
    --repo https://github.com/your-username/trading-bot \
    --branch main \
    --plan free \
    --region oregon \
    --health-check-path /api/state \
    --env-file .env

# 4. Trigger an immediate re-deployment
python scripts/deploy_render.py --action deploy --name hyperliquid-trading-bot

# 5. Push updated local .env credentials to Render
python scripts/deploy_render.py --action update-env --name hyperliquid-trading-bot --env-file .env

# 6. Dry-run simulation (validates payload without mutating cloud resources)
python scripts/deploy_render.py --action create --repo https://github.com/example/trading-bot --dry-run
```
   - **URL (or IP)**: `https://hyperliquid-trading-bot.onrender.com/api/state` (or root `/`)
   - **Monitoring Interval**: `5 minutes`
   - **Monitor Timeout**: `30 seconds`
4. Click **Create Monitor**.
5. **Outcome**: UptimeRobot will ping the service every 5 minutes. Render will recognize continuous inbound HTTP traffic and **never spin down** the trading bot.

---

## 3. Track 2: Dual-Account Ephemeral Sandbox Rotation (Instabox Ring)

For users who prefer bare Linux compute sandboxes or require isolated multi-account redundancy, Track 2 orchestrates a 6-hour proactive rotation ring across two free cloud accounts.

### 3.1 Quota Mechanics & Mathematical Feasibility
- **Instabox Free Tier Quota**:
  * 1 sandbox per account (`limits.maxMachines = 1`).
  * Continuous run limit: 480 minutes (8 hours).
  * Cooldown penalty if hitting 480m: 60 to 135 minutes suspension (`409 machine_cooldown`).
  * **Cooldown Exemption**: Stopping voluntarily before 480 minutes with $\ge 2$ hours rest incurs **zero penalty**.
- **The 6-Hour Ring Formula**:
  * Node A runs for **360 minutes (6 hours)**, then rests for **360 minutes**.
  * Node B runs for **360 minutes (6 hours)**, then rests for **360 minutes**.
  * Handover occurs at minute 350 (T+5h 50m), safely **70 minutes before the 8-hour cap**.
  * Net result: 100% 24/7/365 continuous uptime at $0.00 cost.

```
UTC Timeline:
00:00                06:00                12:00                18:00                24:00
  ┌────────────────────┬────────────────────┬────────────────────┬────────────────────┐
A │  ACTIVE (350m)     │  RESTING (370m)    │  ACTIVE (350m)     │  RESTING (370m)    │
  └────────────────────┴────────────────────┴────────────────────┴────────────────────┘
B │  RESTING (370m)    │  ACTIVE (350m)     │  RESTING (370m)    │  ACTIVE (350m)     │
  └────────────────────┴────────────────────┴────────────────────┴────────────────────┘
                       ▲                    ▲                    ▲                    ▲
             Handover T+5h50m      Handover T+5h50m      Handover T+5h50m      Handover T+5h50m
```

### 3.2 Dual-Account Setup & Secrets Configuration
1. Register two free Instabox accounts:
   - Account A (`INSTABOX_API_KEY_A`)
   - Account B (`INSTABOX_API_KEY_B`)
2. Create sandbox on Account A:
   ```bash
   INSTABOX_API_KEY=$INSTABOX_API_KEY_A instabox create --name "bot-node-a" --template "ci"
   ```
3. Create sandbox on Account B:
   ```bash
   INSTABOX_API_KEY=$INSTABOX_API_KEY_B instabox create --name "bot-node-b" --template "ci"
   ```
4. Set up GitHub Actions Secrets in your repository:
   - `INSTABOX_API_KEY_A`
   - `INSTABOX_API_KEY_B`
   - `TELEGRAM_BOT_TOKEN` (optional)
   - `TELEGRAM_CHAT_ID` (optional)

### 3.3 Instance Provisioning (`scripts/deploy_sandbox.sh`)
Execute the automated provisioner on each sandbox to prepare the Python virtualenv and dependencies:

```bash
# Provision Node A
INSTABOX_API_KEY=$INSTABOX_API_KEY_A instabox push bot-node-a scripts/deploy_sandbox.sh /data/deploy_sandbox.sh
INSTABOX_API_KEY=$INSTABOX_API_KEY_A instabox exec bot-node-a -- bash /data/deploy_sandbox.sh --target-dir /data/home/trading-bot

# Provision Node B
INSTABOX_API_KEY=$INSTABOX_API_KEY_B instabox push bot-node-b scripts/deploy_sandbox.sh /data/deploy_sandbox.sh
INSTABOX_API_KEY=$INSTABOX_API_KEY_B instabox exec bot-node-b -- bash /data/deploy_sandbox.sh --target-dir /data/home/trading-bot
```

---

## 4. The 6-Phase Zero-Naked-Risk Handover Protocol

`scripts/rotator.py` executes the 6-phase handover sequence with automated rollback:

```
   Node_Active (Shift T)                                               Node_Standby (Shift T+1)
+-------------------------+                                           +-------------------------+
| Trading Loop Running    |                                           | Suspended / Resting     |
+-------------------------+                                           +-------------------------+
             |                                                                     |
             | <==================== [PHASE 1: Standby Init] ===================== |
             |   - Wake Standby instance via Provider API                          |
             |   - Poll until machine status == "running"                          |
             |                                                                     |
             | <================== [PHASE 2: Pre-Flight Health] ================== |
             |   - Standby tests Exchange REST /info (HTTP 200, < 2500ms)          |
             |   - Standby validates ECDSA credentials & disk write access         |
             |   [FAIL -> Abort handover, Active unaffected]                       |
             |                                                                     |
+-------------------------+                                                        |
| [PHASE 3: Lock & Sync]  |                                                        |
| - Acquire lease lock    |                                                        |
| - Quiesce Active via    |                                                        |
|   SIGTERM (NO pause!)   |                                                        |
| - Flush state to disk   |                                                        |
+-------------------------+                                                        |
             |                                                                     |
             | ---------- Pull state.json, risk_state.json ----------------------->|
             | ---------- Validate JSON schemas & non-empty integrity ------------>|
             | ---------- Push state files to Standby node ----------------------->|
             |                                                                     |
             |                                                        +-------------------------+
             |                                                        | [PHASE 4: Promotion]    |
             |                                                        | - Spawn bot supervisor  |
             |                                                        |   with inherited state  |
             |                                                        +-------------------------+
             |                                                                     |
             | <================== [PHASE 5: Verification] ======================= |
             |   - Poll Standby .heartbeat freshness (< 30s)                       |
             |   - Verify process liveness (main.py + supervisor)                  |
             |   [FAIL -> Emergency Rollback: Kill Standby, Resurrect Active]      |
             |                                                                     |
+-------------------------+                                                        |
| [PHASE 6: Demotion]     |                                                        |
| - Confirm old process   |                                                        |
|   completely stopped    |                                                        |
| - Stop old sandbox      |                                           +-------------------------+
| - Advance active lock   |                                           | Active Trading Engine   |
+-------------------------+                                           +-------------------------+
      (Resting 6h)
```

### Detailed Handover Phase Specifications:
- **Phase 1 (Standby Init)**: Wakes target container from resting state. Waits up to 90 seconds for status `running`.
- **Phase 2 (Pre-Flight Health)**: Executes `rotator.py --role standby --action health-check`. Verifies imports, Hyperliquid `/info` response latency, credentials validity, and disk I/O.
- **Phase 3 (State Lock & Sync)**:
  * Acquires pessimistic lock with lease TTL (600s) and fencing token.
  * Quiesces Active process with graceful SIGTERM/SIGINT.
  * **Critical Architecture Rule**: Does **NOT** push `"pause"` to `control.json`, strictly avoiding `main.py`'s market position liquidation.
  * Atomically pulls `state.json`, `risk_state_aggressive.json`, and `equity_history_risk_state_aggressive.json`.
  * Verifies non-empty file size and schema fields.
  * Pushes state to Standby. Clears stale `.heartbeat` and `control.json`.
- **Phase 4 (Standby Promotion)**: Spawns supervisor daemon on Standby in background. Verifies PID assigned.
- **Phase 5 (Standby Verification)**: Polls Standby `.heartbeat` every 5s for up to 60s. Verifies timestamp age `< 30s` and state `IDLE` or `TRADING`.
- **Phase 6 (Active Demotion)**: Verifies old Active processes terminated, stops old sandbox for 6-hour cooldown rest, and updates lock status to `ACTIVE_HELD`.

---

## 5. Automated GitHub Actions Rotation Ring

The workflow `.github/workflows/rotation.yml` automates Track 2 scheduling:

### 5.1 Trigger Schedule
- **Cron**: `0 */6 * * *` (00:00, 06:00, 12:00, 18:00 UTC).
- **Manual Dispatch**: Supports manual triggers with `force_rotate`, `target_account` (`auto`, `account_a`, `account_b`), and `dry_run`.

### 5.2 Concurrency Safeguard
```yaml
concurrency:
  group: trading-bot-rotation-ring
  cancel-in-progress: false
```
Setting `cancel-in-progress: false` ensures that an in-flight state handover is **never killed midway** by a concurrent trigger, completely eliminating split-brain risks.

---

## 6. Operational Runbook & Playbooks

### Playbook 1: Check System & Lock Status
```bash
# Query lock status and active node pointer
python scripts/rotator.py --action status

# Run local health diagnostic
python scripts/rotator.py --action health-check
```

### Playbook 2: Manual Dry-Run Handover
```bash
# Dry run: simulates full 6-phase sequence without killing processes or modifying cloud state
python scripts/rotator.py --action handoff --dry-run
```

### Playbook 3: Force Immediate Handover
```bash
# Force immediate rotation to Node B
python scripts/rotator.py --action handoff --active-node bot-node-a --standby-node bot-node-b --force
```

### Playbook 4: Emergency Manual Rollback
```bash
# Terminate Standby and reactivate original Active node
python scripts/rotator.py --action rollback --active-node bot-node-a --standby-node bot-node-b
```

### Playbook 5: Local Node Quiescence
```bash
# Safely stop local trading processes with graceful state preservation
python scripts/rotator.py --action quiesce
```

---

## 7. Failure Recovery Protocols & Edge Cases

| Failure Mode | Detection | Automated Action | Residual Risk |
| :--- | :--- | :--- | :--- |
| **Phase 1 Standby Init Timeout** | Machine fails to enter `running` within 90s | Handover aborted; Active node untouched and continues trading | Zero risk |
| **Phase 2 Health Check Failure** | Exchange endpoint latency > 2500ms or bad credentials | Handover aborted; diagnostic logged; Active node continues trading | Zero risk |
| **Phase 3 Network / Sync Failure** | State pull fails or JSON parse error | Aborts transfer; restarts supervisor on Active node; restores lock | Zero risk (Active resumes from local disk) |
| **Phase 4 Standby Promotion Crash** | Standby `main.py` fails to spawn within 5s | Standby terminated; Active node immediately resumed; alert dispatched | Zero risk |
| **Phase 5 Standby Verification Timeout** | Heartbeat stale (> 30s) or missing after 60s | Standby killed; old Active node resumed; critical alert dispatched | Zero risk (On-chain triggers intact) |
| **Catastrophic Double Fault** | Both nodes fail during failover | On-chain protective orders remain live on Hyperliquid; alert dispatched for operator intervention | Minimal (Protected by remote SL/TP) |

# 24/7 Multi-Instance Cloud Rotation & Operations Guide (`DEPLOYMENT_24_7.md`)

**Document Version**: 1.0.0  
**Target System**: Hyperliquid Quantitative Trading Bot  
**Architecture**: Dual-Account Ephemeral Cloud Sandbox Rotation Ring (Zero Cost)  
**Applicability**: Production Linux Deployment (Instabox / GitHub Actions)  

---

## 1. Executive Summary & Zero-Cost Architecture

### 1.1 The Ephemeral Cloud Sandbox Model
Modern cloud developer platforms provide generous free compute sandboxes but enforce strict runtime quotas and resource constraints. Specifically, the **Instabox Free Tier** provides:
- **Compute Specs**: 4 vCPUs, 8192 MiB RAM, 16 GiB root OS disk, and **20 GiB persistent `/data` disk**.
- **Max Machines Constraint**: `limits.maxMachines = 1` per free organization/account. Only one sandbox can exist simultaneously per account.
- **Continuous Runtime Limit**: `maxRunMinutes = 480` (8 hours).
- **Mandatory Cooldown Penalty**: If a sandbox runs continuously until the 480-minute cap, it is forcefully suspended with a mandatory cooldown of **60 to 135 minutes** (`409 machine_cooldown`), during which any wake, exec, or resume attempts are rejected.
- **Cooldown Exemption Rule**: If a machine is gracefully stopped or paused **before** hitting the 480-minute cap, and allowed to rest for $\ge 2$ hours, **zero cooldown penalty is owed**.

### 1.2 The 6-Hour Proactive Dual-Account Rotation Ring
To achieve uninterrupted 24/7/365 uptime at **$0.00 operational cost**, the system coordinates **two separate free accounts** (Account A and Account B) in an alternating 6-hour rotation ring:

```
Timeline (UTC):
00:00                06:00                12:00                18:00                24:00
  ┌────────────────────┬────────────────────┬────────────────────┬────────────────────┐
A │  ACTIVE (350m)     │  RESTING (370m)    │  ACTIVE (350m)     │  RESTING (370m)    │
  └────────────────────┴────────────────────┴────────────────────┴────────────────────┘
B │  RESTING (370m)    │  ACTIVE (350m)     │  RESTING (370m)    │  ACTIVE (350m)     │
  └────────────────────┴────────────────────┴────────────────────┴────────────────────┘
                       ▲                    ▲                    ▲                    ▲
             Handover T+5h50m      Handover T+5h50m      Handover T+5h50m      Handover T+5h50m
```

- **Shift Duration**: Exactly **6 hours (360 minutes)**.
- **Handover Trigger**: Proactively executed at **T+5h 50m (minute 350)**, safely 70 minutes before the 480-minute hard cap.
- **Resting Period**: The decommissioned instance rests for **~6 hours**, completely resetting all platform runtime counters and clearing any cooldown state.
- **Zero Host Overhead**: The central rotation coordinator runs via a lightweight GitHub Actions workflow (consuming < 15 runner minutes per month out of GitHub's free 2,000 min/mo allowance) or a lightweight local CLI script.

### 1.3 Protection-First Exchange Model (Zero Naked Risk)
A critical concern in cloud instance migration is whether active market positions are left unprotected during the 30–45 second migration gap:
- **Native On-Chain Triggers**: In `exchange.py`, all stop-loss and take-profit orders are submitted to the Hyperliquid exchange engine as native on-chain trigger orders (`is_trigger=True`, `reduce_only=True`).
- **Exchange-Side Execution**: These protective orders reside directly on Hyperliquid's validator matching engine. If a market flash-crash occurs while the bot process transitions between Node A and Node B, the exchange automatically triggers the stop-loss order with sub-millisecond precision.
- **Continuous FSM Adoption**: Upon booting, Standby loads `state.json` and invokes `_reconcile_exchange_position()`, flawlessly preserving `initial_risk_r`, `peak_price`, `bars_held`, and ATR trailing stops.
- **Conclusion**: There is **zero naked risk** during instance handovers.

---

## 2. Prerequisites & Multi-Account Onboarding

### 2.1 Registering Dual Accounts
You will need two separate free email addresses (e.g., `user-node-a@gmail.com` and `user-node-b@gmail.com`).

#### Step 1: Account A Setup
1. Sign up or log in to [Instabox](https://instabox.sh) with Account A credentials.
2. In your terminal, authenticate Account A:
   ```bash
   instabox login
   ```
3. Generate an API Key for automated orchestration:
   ```bash
   instabox keys create --name "rotator-node-a" --scope "machines:read machines:write machines:delete images:read"
   ```
   Save the output string (`ibk_live_...`) securely as `INSTABOX_API_KEY_A`.
4. Create the initial sandbox on Account A:
   ```bash
   instabox create --name "bot-node-a" --template "ci"
   ```

#### Step 2: Account B Setup
1. Log out of Account A and log in with Account B:
   ```bash
   instabox logout
   instabox login
   ```
2. Generate an API Key for Account B:
   ```bash
   instabox keys create --name "rotator-node-b" --scope "machines:read machines:write machines:delete images:read"
   ```
   Save the output string (`ibk_live_...`) securely as `INSTABOX_API_KEY_B`.
3. Create the initial sandbox on Account B:
   ```bash
   instabox create --name "bot-node-b" --template "ci"
   ```

### 2.2 GitHub Repository Setup
1. Fork or push the trading bot codebase to a **private** GitHub repository.
2. Navigate to **Settings** > **Secrets and variables** > **Actions**.
3. Create the following Repository Secrets:

| Secret Name | Value Description |
| :--- | :--- |
| `INSTABOX_API_KEY_A` | API Key for Account A (`ibk_...`) |
| `INSTABOX_API_KEY_B` | API Key for Account B (`ibk_...`) |
| `ROTATION_ALERT_WEBHOOK` | (Optional) Discord/Telegram webhook URL for failure alerts |

---

## 3. Secrets & Environment Configuration

### 3.1 Instance Local Environment (`/data/home/trading-bot/.env`)
Each Instabox sandbox has an isolated, encrypted persistent disk mounted at `/data`. The production `.env` file must be created inside `/data/home/trading-bot/.env` on **both** `bot-node-a` and `bot-node-b`.

```bash
# ============================================================
# Production .env Template for Cloud Sandbox Node
# Location: /data/home/trading-bot/.env
# File Permissions: chmod 600 /data/home/trading-bot/.env
# ============================================================

# ---------- Hyperliquid Environment & Credentials ----------
HYPE_ENV=mainnet
HYPE_USER_ADDRESS=0xYourMainAccountAddress
HYPE_SECRET_KEY=0xYourApiWalletPrivateKeyHex
HYPE_API_WALLET_ADDRESS=0xYourApiWalletAddress
HYPE_BASE_URL=
HYPE_COIN=ETH
HYPE_SLIPPAGE=0.01

# ---------- Runtime Loops & Timing ----------
IDLE_POLL_SECONDS=5
SIGNAL_CANDLE_SECONDS=300
SIGNAL_ALIGN_SECONDS=2
TRADING_POLL_SECONDS=1
ATR_REFRESH_SECONDS=60
IDLE_RECONCILE_SECONDS=10
TRADING_RECONCILE_SECONDS=5
NETWORK_TIMEOUT_SECONDS=20
HARD_STOP_REFRESH_SECONDS=900
FR_WINDOW_HOURS=168
CACHE_TTL_5M=5
CACHE_TTL_1H=600
CACHE_TTL_4H=1800
CACHE_TTL_FR=1800
ATR_SL_MULTIPLIER=1.5
MAX_LOOPS=0

# ---------- Risk & Circuit Breakers ----------
COOLDOWN_HOURS=12
IDLE_ALERT_THRESHOLD=5
TRADING_ALERT_THRESHOLD=2
CLOSE_FAIL_ALERT_THRESHOLD=5
TRADING_FEE_RATE=0.001

# ---------- AI Agent Multi-Model Fallback Chain ----------
THIRD_PARTY_BASE_URL=https://api.ludies.best/v1
THIRD_PARTY_API_KEY=sk-YourThirdPartyLlmKey
MODEL_MACRO=claude-opus-5,gpt-5.6-terra,glm-5.3-flashx,claude-sonnet-5,kimi-k2.7-code
MODEL_AI_TRADE=claude-sonnet-4-6,gpt-6-luna,deepseek-v4.1-flash-cc,glm-5.3-flashx,claude-sonnet-5,kimi-k2.7-code
AI_TIMEOUT=30
AI_RETRIES=2

# ---------- Mail Alerts ----------
MAIL_ENABLED=true
SMTP_SERVER=smtp.qq.com
SMTP_PORT=465
SMTP_USER=your_email@qq.com
SMTP_PASSWORD=your_email_smtp_token
MAIL_TO=your_email@qq.com
MODEL_ERROR_ALERT_THRESHOLD=5

# ---------- Maintenance Interlock ----------
MAINTENANCE_LOCK=false
```

> **Security Rule**: Never check `.env` into git. The file stays exclusively on the persistent `/data` partition of each authorized node.

---

## 4. Provisioning Walkthrough (`scripts/deploy_sandbox.sh`)

Provisioning prepares a clean Ubuntu sandbox environment with Python 3.11+, installs project dependencies, and verifies that the bot can start.

### 4.1 Running the Provisioner on Node A and Node B
From your management workstation:
```bash
# Provision Node A
INSTABOX_API_KEY=$INSTABOX_API_KEY_A instabox push bot-node-a scripts/deploy_sandbox.sh /tmp/deploy_sandbox.sh
INSTABOX_API_KEY=$INSTABOX_API_KEY_A instabox exec bot-node-a -- bash /tmp/deploy_sandbox.sh

# Provision Node B
INSTABOX_API_KEY=$INSTABOX_API_KEY_B instabox push bot-node-b scripts/deploy_sandbox.sh /tmp/deploy_sandbox.sh
INSTABOX_API_KEY=$INSTABOX_API_KEY_B instabox exec bot-node-b -- bash /tmp/deploy_sandbox.sh
```

### 4.2 Provisioning Script Internal Steps
The provisioning script executes four deterministic stages:
1. **Directory Structure Setup**: Creates `/data/home/trading-bot` on persistent disk, along with `state/` and `logs/`.
2. **Repository Sync**: Clones or copies project files (`main.py`, `watchdog.py`, `exchange.py`, `strategy.py`, `risk_manager.py`, `alerts.py`, `requirements.txt`, `config_aggressive.json`).
3. **Dependency Installation**:
   ```bash
   python3 -m pip install --upgrade pip
   pip install numpy pandas httpx python-dotenv eth-account hyperliquid-python-sdk
   ```
4. **Static Verification & Smoke Test**: Executes dry-run import verification:
   ```bash
   python3 -c "import numpy, pandas, httpx, dotenv, eth_account, hyperliquid; print('RUNTIME OK')"
   ```

---

## 5. Orchestration Execution (`scripts/rotator.py`)

### 5.1 Orchestrator CLI Architecture
The `scripts/rotator.py` script acts as the master coordinator. It can be invoked directly by operators or scheduled automatically via GitHub Actions:

```bash
# Query status of both nodes across accounts
python scripts/rotator.py --action status

# Execute planned 6-phase proactive handover
python scripts/rotator.py --action handoff --from bot-node-a --to bot-node-b

# Force immediate handover regardless of schedule
python scripts/rotator.py --action force-handover --to bot-node-b

# Emergency abort and rollback to source node
python scripts/rotator.py --action rollback --target bot-node-a

# Enforce single-active invariant (scan for split-brain and freeze unauthorized node)
python scripts/rotator.py --action fence-check
```

### 5.2 The 6-Phase Handover Protocol Walkthrough

```
[Phase 1: Standby Preparation]
  │ • Authenticate as Standby Account (B)
  │ • Check `bot-node-b` state. If suspended/stopped, issue `instabox start`
  │ • Wait for `runtime.ready == true` (timeout: 60s)
  ▼
[Phase 2: Active Quiescence & Pessimistic Lock]
  │ • Authenticate as Active Account (A)
  │ • Acquire master handover lock `active_node.lock`
  │ • Send SIGTERM to Active processes: `pkill -SIGTERM -f watchdog.py; pkill -SIGTERM -f main.py`
  │ • Poll `ps` on A until all Python processes have exited cleanly (timeout: 15s)
  │ • (NOTE: DO NOT use `control.json pause` as it triggers emergency market close)
  ▼
[Phase 3: State Extraction & Validation]
  │ • Pull state files from Node A to orchestrator buffer:
  │     - /data/home/trading-bot/state.json
  │     - /data/home/trading-bot/risk_state_aggressive.json
  │     - /data/home/trading-bot/equity_history_risk_state_aggressive.json
  │ • Validate JSON syntax, non-empty payload, and critical schema keys
  │ • If validation fails -> ABORT and trigger Auto-Rollback to Node A
  ▼
[Phase 4: State Ingestion onto Standby]
  │ • Authenticate as Standby Account (B)
  │ • Push validated state files to Node B: `/data/home/trading-bot/`
  │ • Clean ephemeral files: `rm -f control.json .heartbeat`
  │ • Launch Watchdog daemon:
  │     `nohup python3 watchdog.py > watchdog.log 2>&1 &`
  ▼
[Phase 5: Activation & Health Check Verification]
  │ • Poll Node B every 5 seconds for up to 45 seconds:
  │     1. Process liveness: `watchdog.py` and `main.py` present in `instabox ps`
  │     2. Heartbeat freshness: `.heartbeat` exists and timestamp < 30s old
  │     3. Exchange API check: Node B successfully completes initial `/info` query
  │     4. Log sanity: zero unhandled exceptions or crash tracebacks in `watchdog.log`
  │ • If health check fails within 45s -> Execute Rollback to Node A!
  ▼
[Phase 6: Active Decommission & Cooldown Reset]
  │ • Authenticate as Active Account (A)
  │ • Gracefully stop sandbox: `instabox stop bot-node-a`
  │ • Instance A begins 6-hour rest period (zero cooldown incurred)
  │ • Update rotation state record (`active_node = "bot-node-b"`, `epoch = epoch + 1`)
  │ • Release handover lock
```

### 5.3 Automated GitHub Actions Scheduling (`.github/workflows/rotation.yml`)
The GitHub Actions workflow triggers every 6 hours at minute 50:
- Cron schedule: `50 5,11,17,23 * * *`
- Concurrency group: `concurrency: { group: 'trading-bot-rotation', cancel-in-progress: false }`
- Execution: Sets up Python and Instabox CLI, injects `INSTABOX_API_KEY_A` and `INSTABOX_API_KEY_B`, and runs `python scripts/rotator.py --action handoff`.

---

## 6. State Synchronization Architecture

### 6.1 State File Inventory & Roles

| File Name | Size | Purpose | Sync Priority |
| :--- | :--- | :--- | :--- |
| `state.json` | ~2.5 KB | Core trading state, active position details (`entry_price`, `direction`, `sz_notional`, `peak_price`, `bars_held`, `initial_risk_r`), ATR cache, daily PnL | **MANDATORY** |
| `risk_state_aggressive.json` | ~9.0 KB | Account equity watermark, trade win rate, drawdown stats, recent trade history for dynamic position sizing | **MANDATORY** |
| `equity_history_risk_state_aggressive.json` | ~1.5 KB | Historical equity curve points for dashboard visualization | **MANDATORY** |
| `.env` | ~4.2 KB | Secrets and API keys | **Initial Deploy Only** |
| `config_aggressive.json` | ~3.8 KB | Candidate coins, risk multipliers, leverage | **Initial Deploy Only** |
| `.heartbeat` | ~250 B | Watchdog liveness monitor | **DO NOT SYNC** (Local only) |
| `control.json` | ~100 B | Ephemeral control commands | **DO NOT SYNC** (Purge on sync) |

### 6.2 Storage Architecture Comparison

| Architecture Option | Cost | Setup Complexity | Dependency Footprint | Disaster Recovery Capability |
| :--- | :--- | :--- | :--- | :--- |
| **Option 1: Direct Instabox Transfer** (via Rotator) | $0.00 | Minimal (None) | Zero external dependencies; uses native CLI push/pull | High for planned handovers; requires instance disk availability |
| **Option 2: GitHub Secret Gist** | $0.00 | Low (1 PAT token) | GitHub Gist REST API (`api.github.com/gists`) | Very High: Complete commit history of state; available even if VM destroyed |
| **Option 3: S3 / Cloudflare R2** | $0.00 | Medium | `boto3` / AWS credentials / bucket setup | Very High: Standard cloud object storage |
| **Option 4: Upstash Redis KV** | $0.00 | Medium | `redis` / REST API tokens | Moderate: In-memory KV, needs custom serialization |

### 6.3 Dual-Tier Synchronization Specification (Recommended)
To maximize simplicity while providing ultimate disaster survivability, the recommended architecture uses a **Dual-Tier State Model**:
1. **Tier 1 (Scheduled Runtime Handover - Default)**:
   - Direct Point-to-Point transfer managed by `scripts/rotator.py`.
   - Node A $\rightarrow$ Orchestrator Buffer $\rightarrow$ Node B.
   - Zero external credentials, zero API rate limits, instantaneous.
2. **Tier 2 (Asynchronous Periodic Snapshot - Disaster Recovery)**:
   - Every 15 minutes, or upon every trade fill / position update, the active bot exports an encrypted snapshot of `state.json` and `risk_state_aggressive.json` to a private GitHub Gist (or secondary S3 bucket).
   - If Node A experiences an unrecoverable hardware failure or account lock, Node B boots directly from the latest Tier 2 Gist checkpoint and reconciles with Hyperliquid.

---

## 7. Operational Runbook & Playbooks

### Playbook 1: Daily Health & Liveness Inspection
**Frequency**: Daily or whenever checking bot health.  
**Procedure**:
1. Run status probe from local terminal:
   ```bash
   python scripts/rotator.py --action status
   ```
2. Verify output:
   - Identify which node is currently `ACTIVE` (`bot-node-a` or `bot-node-b`).
   - Check `.heartbeat` timestamp age: must be `< 30s`.
   - Check process list: both `python3 watchdog.py` and `python3 main.py` running.
   - Verify Hyperliquid position matches local state.

### Playbook 2: Manual Force Handover
**Use Case**: Maintenance on Node A or early quota clearing.  
**Procedure**:
1. Trigger force handover to Node B:
   ```bash
   python scripts/rotator.py --action force-handover --to bot-node-b
   ```
2. Monitor terminal log as it transitions through Phases 1 through 6.
3. Validate that Node B is reporting fresh heartbeats:
   ```bash
   instabox exec bot-node-b -- 'tail -n 20 /data/home/trading-bot/watchdog.log'
   ```

### Playbook 3: Emergency Rollback
**Use Case**: Standby promoted erroneously or experiencing API issues.  
**Procedure**:
1. Issue rollback command targeting the previous node:
   ```bash
   python scripts/rotator.py --action rollback --target bot-node-a
   ```
2. The orchestrator will terminate Node B, restart Node A with the preserved state, and verify Node A's heartbeat.

### Playbook 4: Emergency Stop / Freeze All
**Use Case**: Severe market black swan or critical logic defect requiring immediate trading halt.  
**Procedure**:
1. Execute emergency freeze:
   ```bash
   python scripts/rotator.py --action freeze-all
   ```
   *(Or manually push `{"action": "emergency_close"}` to `control.json` on the active node to close all positions and halt).*
2. Confirm both nodes are in `stopped` or `suspended` state in Instabox.

### Playbook 5: Log Inspection & Live Monitoring
```bash
# View watchdog self-healing log
instabox exec <active-node> -- 'tail -f /data/home/trading-bot/watchdog.log'

# View core trading bot execution log
instabox exec <active-node> -- 'tail -f /data/home/trading-bot/log/trading_bot.log'
```

---

## 8. Failure Recovery Protocols & Edge Cases

### 8.1 Network Partition During Phase 3 State Sync
- **Condition**: Node A's processes are stopped, but pulling state files times out or returns corrupted JSON.
- **Fail-Safe Mechanism**:
  1. The orchestrator rejects invalid/empty files.
  2. The promotion to Standby is immediately aborted.
  3. The orchestrator restarts `watchdog.py` on Node A.
  4. Node A recovers seamlessly from its local on-disk state.
  5. Handover is retried after a 10-minute backoff.

### 8.2 Standby Crash / Timeout During Phase 4/5
- **Condition**: Standby starts but crashes (missing dependency, bad API key) or fails to generate `.heartbeat` within 45 seconds.
- **Fail-Safe Mechanism**:
  1. Two-Phase Commit protects Node A: Node A's sandbox is still running and intact.
  2. Orchestrator executes `pkill -9 -f python3` on Standby and suspends it.
  3. Orchestrator issues resume to Node A:
     ```bash
     instabox exec bot-node-a -- 'nohup python3 watchdog.py > watchdog.log 2>&1 &'
     ```
  4. Orchestrator verifies Node A heartbeat resumption (< 20s).
  5. Critical alert dispatched with Standby crash logs.

### 8.3 Unplanned Active Node Crash (Heartbeat Expiration)
- **Condition**: Active node hardware crashes or VM drops offline at T+3h into the shift.
- **Fail-Safe Mechanism**:
  1. **Market Protection**: Hyperliquid on-chain trigger orders protect any open position.
  2. **Detection**: Monitoring workflow detects `.heartbeat` age > 180s or `instabox get` state = `suspended`.
  3. **Fencing Kill**: Orchestrator issues `instabox stop <crashed-node>` to guarantee no zombie process exists.
  4. **State Recovery**: Orchestrator extracts `state.json` from the crashed node's persistent `/data` disk (accessible while stopped), or falls back to Tier 2 Gist checkpoint.
  5. **Promotion**: Standby boots, ingests recovered state, reconciles with Hyperliquid, and assumes active trading.

### 8.4 Stale Lock Resolution & Split-Brain Prevention
- **Invariant**: **At no point in time may two instances trade against the same Hyperliquid account simultaneously.**
- **Enforcement Rules**:
  1. **Quiesce-Before-Promote**: Standby's `main.py` is never launched until Active's `main.py` is confirmed terminated via process check.
  2. **Lease Epoch**: Master lock includes a monotonically increasing `epoch` counter. The bot verifies its lease epoch against the central coordinator.
  3. **Stale Lock Recovery**: If an orchestrator run finds an existing lock where `heartbeat_at` is older than 300 seconds, the orchestrator queries the physical host. If the recorded holder process is absent, the lock is broken, logged to `lock_history.json`, and assigned to the healthy candidate with `epoch + 1`.

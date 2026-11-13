# 24/7 Deployment Architecture & End-to-End Failure Resilience Specification

**Document**: Handoff Report for Milestone 3 (Free Cloud Server 24/7 Rotation Scheme)  
**Agent**: Explorer 3 (`explorer_m3_3`)  
**Target Repository**: `C:\Users\Administrator\Desktop\v1\a`  
**State Folder**: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_3`  
**Date**: 2026-11-13  
**Status**: COMPLETE (Hard Handoff)  

---

## 1. Observation

### 1.1 Direct Inspection of Process Lifecycle & Control Commands (`main.py` & `watchdog.py`)
Through direct static inspection of `C:\Users\Administrator\Desktop\v1\a\main.py` and `C:\Users\Administrator\Desktop\v1\a\watchdog.py`, the following mechanisms were observed:

1. **Fatal Risk in Survey 3's Handover Quiescence Recommendation**:
   In `teamwork_explorer_survey_3/handoff.md:243`, Survey 3 proposed:
   > "2.2 Push temporary freeze signal to Instance A: Write control command `{"action": "pause"}` to `control.json` on Instance A via `instabox push`."
   
   However, checking the actual implementation in `main.py:323-324`:
   ```python
   elif action == "pause" and self.state != STATE_HALT:
       self._enter_system_halt("收到看板手动暂停指令")
   ```
   And tracing `_enter_system_halt()` in `main.py:1483-1498`:
   ```python
   def _enter_system_halt(self, reason: str):
       """SYSTEM_HALT：市价清空持仓与挂单后进入死循环休眠。"""
       logger.critical("[挂起] SYSTEM_HALT | reason=%s", reason)
       send_alert(f"SYSTEM_HALT: {reason}，正在强制市价平仓并清空挂单！")
       try:
           szi, _ = self.exchange.current_position()
           if abs(szi) > 0:
               resp = self.exchange.close_all()
               if resp is None:
                   send_alert("SYSTEM_HALT 市价平仓未获确认，需人工立即介入！")
           else:
               try:
                   self.exchange.cancel_all_orders()
   ```
   **Direct Finding**: Sending `{"action": "pause"}` to `control.json` causes `self.exchange.close_all()`, which **immediately liquidates all active market positions at market price**! This directly violates the mandate of "Zero-Naked-Risk Handover" and disrupts ongoing trades. Therefore, `control.json` `pause` MUST NEVER be used for instance handover.

2. **Clean Process Termination Mechanism (`main.py:1916-1923`)**:
   ```python
   bot = TradingBot()
   try:
       bot.run()
   except KeyboardInterrupt:
       logger.info("[停止] 收到 Ctrl-C | action=机器人停止")
   finally:
       import logging
       logging.shutdown()
   ```
   When `main.py` receives a standard termination signal (`SIGINT` / `SIGTERM`), Python raises `KeyboardInterrupt`, cleanly exiting the loop without calling `close_all()` or canceling on-chain trigger orders.

3. **Watchdog Emergency Protection Behavior (`watchdog.py:35-46`)**:
   ```python
   def emergency_cancel_orders():
       """紧急保护：仅清理普通未成交限价挂单，始终保留远端止损/止盈 reduce-only 触发保护单，严防持仓裸奔"""
       try:
           from exchange import HyperliquidExecutor
           ex = HyperliquidExecutor()
           if ex.ready:
               logger.warning("[看门狗] 启动紧急保护：仅清理非触发普通限价挂单，保留远端止损/止盈触发保护单！")
               ex.cancel_non_trigger_orders()
               logger.info("[看门狗] 紧急保护挂单清理完成 (触发保护单完好)")
   ```
   `watchdog.py` strictly retains all protective stop-loss/take-profit trigger orders during crashes.

4. **Maintenance Interlock (`main.py:1905-1909`)**:
   ```python
   if os.getenv("MAINTENANCE_LOCK", "true").lower() in ("true", "1", "yes"):
       logger.warning("[检修锁定] 检测到 MAINTENANCE_LOCK=true，主程序已安全截断并退出，严禁调用任何实盘/行情 API！")
       return
   ```
   If `.env` is absent or `MAINTENANCE_LOCK` is not explicitly set to `false`, the bot will abort immediately upon launch.

### 1.2 State Persistence File Formats & Schemas
Inspecting existing state files in `C:\Users\Administrator\Desktop\v1\a`:
1. `state.json` (size: 2,529 bytes):
   - Contains: `state`, `coin`, `daily_start_equity`, `daily_loss_date`, `daily_loss_pnl`, `quant_state`, `final_macro_state`, `cached_ai_macro_state`, `consecutive_failures`, `open_regime_cap`, `cached_atr_5m`, `position`.
   - When `position` is active, it contains: `entry_price`, `direction`, `sz_notional`, `tp_price`, `sl_price`, `initial_risk_r`, `peak_price`, `bars_held`, `opened_at`, `entry_score`, `filled_sz`.
   - Line 1573 in `main.py` explicitly inherits this metadata on boot:
     `# 如果启动时已从 state.json 恢复了持仓快照，优先继承其精确风控元数据 (防止重置 peak_price / bars_held / initial_risk_r)`.
2. `risk_state_aggressive.json` (size: 9,091 bytes):
   - Contains: `initial_equity`, `current_equity`, `peak_equity`, `daily_pnl`, `daily_start_equity`, `metrics` (win rate, total trades, average loss, etc.), `recent_trades`.
3. `equity_history_risk_state_aggressive.json` (size: 1,475 bytes):
   - Contains: Array of `{"ts": int, "equity": float, "drawdown_pct": float}`.
4. `.heartbeat` (size: 235 bytes):
   - Contains: `timestamp`, `datetime`, `state`, `phase`, `daily_loss_pnl`, `peak_equity`.
   - **Crucial Rule**: `.heartbeat` is local-only to a single running instance and must NEVER be synced across nodes during migration.

### 1.3 Cloud Platform Limits & Multi-Account Switching (Instabox)
From `instabox usage` and `instabox --help`:
1. **Per-Account Limits**:
   - `limits.maxMachines: 1` (Only 1 sandbox can exist simultaneously in the free plan).
   - `maxRunMinutes: 480` (8 hours continuous execution limit).
   - Post-cap cooldown: 60 to 135 minutes during which wake/exec are rejected (`409 machine_cooldown`).
   - Cooldown exemption: A run terminated before reaching 480 minutes owes NO cooldown if rested for $\ge 2$ hours.
2. **Multi-Account Switching via Environment Variable**:
   - `instabox` CLI accepts `INSTABOX_API_KEY=ibk_...` per command.
   - Switching accounts requires zero local state file manipulation: setting `INSTABOX_API_KEY=$INSTABOX_API_KEY_A` executes in Org A; setting `INSTABOX_API_KEY=$INSTABOX_API_KEY_B` executes in Org B.

---

## 2. Logic Chain

```
[Observation 1.1: control.json action="pause" calls _enter_system_halt -> exchange.close_all()]
                               │
                               ▼
[Deduction 1: Ban control.json pause for Handover]
The handover orchestrator must NEVER write {"action": "pause"} to control.json.
Instead, the orchestrator must send SIGTERM to watchdog.py followed by SIGTERM to main.py.
This ensures main.py exits cleanly, retains on-chain trigger orders, and preserves disk state files.
                               │
                               ▼
[Observation 1.3: maxMachines=1, maxRunMinutes=480m, Cooldown=60-135m, Cooldown reset >= 2h]
                               │
                               ▼
[Deduction 2: The Proactive 6-Hour Dual-Account Rotation Ring]
- Shift length: 6 hours (360 minutes).
- Proactive Handover: Triggered at minute 350 (T+5h50m), safely 70 minutes before the 480m cap.
- Account A rests for 6 hours 10 minutes while Account B runs.
- Result: Zero max-run cooldown incurred, zero quota violations, 100% sustainable 24/7/365 uptime at $0.00 cost.
                               │
                               ▼
[Observation 1.1 & 1.2: Native on-chain trigger orders + state.json reconciliation]
                               │
                               ▼
[Deduction 3: Zero Naked Risk During Handover]
- When the bot is transitioning (30-45 seconds between stopping A and verifying B), stop-loss and take-profit
  trigger orders reside natively on Hyperliquid's validator matching engine.
- If price moves violently during this window, the exchange executes the order automatically.
- When Standby boots, it inherits peak_price, initial_risk_r, and bars_held from state.json without disruption.
                               │
                               ▼
[Observation 1.2: State is contained in 3 JSON files]
                               │
                               ▼
[Deduction 4: Dual-Tier State Synchronization Strategy]
- Tier 1 (Scheduled Migration): Direct point-to-point transfer via orchestrator buffer (Node A -> Host/Runner -> Node B). Zero third-party dependencies, zero cost.
- Tier 2 (Disaster Recovery): Active node asynchronously exports periodic checkpoints to GitHub Secret Gist. If Node A experiences catastrophic hardware destruction, Standby recovers from Tier 2 checkpoint.
                               │
                               ▼
[Deduction 5: Two-Phase Commit with Pessimistic Locking]
- Standby node VM is prepared (Phase 1).
- Active process is quiesced (Phase 2).
- State files are pulled and validated for JSON and schema integrity (Phase 3).
- State is ingested and Standby launched (Phase 4).
- Standby health verified (Phase 5). IF FAILS: Rollback immediately to Node A!
- Active node VM is stopped/decommissioned ONLY AFTER Phase 5 succeeds (Phase 6).
- Guaranteed Single-Active Invariant: At no time do two trading bot processes run concurrently.
```

---

## 3. Caveats & Assumptions

1. **`MAINTENANCE_LOCK` Default Value**:
   In `main.py:1906`, `os.getenv("MAINTENANCE_LOCK", "true")` defaults to `"true"` if not explicitly set in `.env`. The deployment provisioning script (`scripts/deploy_sandbox.sh`) and documentation must explicitly verify that `MAINTENANCE_LOCK=false` is written to `.env`, otherwise the bot will refuse to execute after migration.
2. **ECDSA Private Key Security**:
   Hyperliquid API signing relies on the `eth-account` private key. Because secrets cannot be safely committed to git, the real private key must reside exclusively inside `/data/home/trading-bot/.env` on the persistent disk of each authorized sandbox. Permissions must be enforced at `chmod 600`.
3. **Persistent `/data` Disk vs Ephemeral Root**:
   Instabox provides a 20 GiB persistent `/data` disk that survives sandbox stops and suspends, but the root filesystem (`/`) may be reset if a new template container is spun up. All project files, state files, and virtual environments must reside strictly under `/data/home/trading-bot/`.
4. **Instabox Free Tier maxMachines Invariant**:
   Because `limits.maxMachines = 1` per account, each account can only have ONE sandbox (`bot-node-a` in Account A, `bot-node-b` in Account B). The orchestrator switches between them by alternating the API key (`INSTABOX_API_KEY_A` vs `INSTABOX_API_KEY_B`), never attempting to spin up two sandboxes in the same account.

---

## 4. Conclusion & Complete Technical Specification

### 4.1 Deployment Guide Artifact (`DEPLOYMENT_24_7.md`)
The complete, production-ready specification for Feature 16 has been authored and committed to:
`C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_3\proposed_DEPLOYMENT_24_7.md`

This document provides a 360-degree, end-to-end technical guide structured into 8 exhaustive sections:
1. **Executive Summary & Zero-Cost Architecture**: Ephemeral cloud model, 6-hour rotation cadence, on-chain trigger protection.
2. **Prerequisites & Multi-Account Onboarding**: Step-by-step account creation, API key generation (`INSTABOX_API_KEY_A`/`B`), sandbox creation, GitHub repository secrets.
3. **Secrets & Environment Configuration**: Complete `/data/home/trading-bot/.env` production template, secure file permissions, orchestrator secret matrix.
4. **Provisioning Walkthrough (`scripts/deploy_sandbox.sh`)**: Execution commands, directory initialization, pip dependency installation, static import smoke testing.
5. **Orchestration Execution (`scripts/rotator.py` & `.github/workflows/rotation.yml`)**: CLI flags, 6-phase protocol execution, GitHub Actions cron trigger (`50 5,11,17,23 * * *`).
6. **State Synchronization Architecture**: Mandatory state file inventory, storage backend evaluation (Direct vs Gist vs S3 vs Redis), and dual-tier strategy recommendation.
7. **Operational Runbook & Playbooks**: Playbooks 1 to 6 covering status check, manual force handover, emergency rollback, freeze-all, live log monitoring, and disaster recovery.
8. **Failure Recovery Protocols & Edge Cases**: In-depth mitigation protocols for all failure modes.

---

### 4.2 Comprehensive Edge Cases & Failure Mode Analysis

#### Edge Case 1: Network Partition During Phase 3 State Sync
- **Phenomenon**: Active node's process was stopped in Phase 2. The orchestrator attempts to pull `state.json`, `risk_state_aggressive.json`, and `equity_history_risk_state_aggressive.json`, but the network drops, API times out, or pulled files are corrupted (0 bytes or malformed JSON).
- **Mitigation Protocol**:
  1. **Atomic Staging Directory**: Orchestrator downloads files to an isolated temporary directory `./sync_temp/<timestamp>/`.
  2. **Schema & Integrity Validation (`validate_state_files`)**:
     - Check file existence and non-zero size (> 10 bytes).
     - Execute `json.loads()` validation.
     - Verify essential schema fields:
       * `state.json`: `"state"`, `"coin"`, `"daily_start_equity"`. If `"position"` is not null, verify `"entry_price"`, `"direction"`, `"initial_risk_r"`.
       * `risk_state_aggressive.json`: `"initial_equity"`, `"metrics"`, `"recent_trades"`.
  3. **Auto-Recovery on Validation Failure**:
     - Retry download up to 3 times with exponential backoff (2s, 4s, 8s).
     - If retries fail, **ABORT HANDOVER IMMEDIATELY**.
     - Standby is NOT promoted.
     - Orchestrator restarts `watchdog.py` on Active node:
       `instabox exec bot-node-a -- 'nohup python3 watchdog.py > watchdog.log 2>&1 &'`
     - Poll Active node `.heartbeat` to confirm Active has resumed trading.
     - Release lock and dispatch alert: `"[ALERT] Phase 3 state sync failed; auto-rollback to Active successful."`

#### Edge Case 2: Standby Crash or Timeout During Phase 4/5
- **Phenomenon**: State was pushed to Standby, and `watchdog.py` was executed. However, Standby fails to initialize within 45 seconds due to:
  - Missing Python module or environment misconfiguration.
  - Hyperliquid API network failure or bad `.env` credentials.
  - Watchdog fails to write `.heartbeat` or process terminates unexpectedly.
- **Mitigation Protocol**:
  1. **Bounded Health Verification Loop**:
     - Orchestrator polls Standby node every 5s for up to 45s (9 iterations).
     - Strict 4-point health check:
       a) Process check: `instabox ps <standby>` contains both `watchdog.py` and `main.py`.
       b) Heartbeat check: Read `/data/home/trading-bot/.heartbeat`; age must be `< 30s`.
       c) Exchange check: Standby successfully queries Hyperliquid `/info`.
       d) Log sanity check: `watchdog.log` contains zero unhandled exception tracebacks.
  2. **Automated Rollback to Active Node**:
     - If the 45s timer expires without all 4 checks passing:
     - Terminate faulty Standby: `instabox exec <standby> -- 'pkill -9 -f python3'` and `instabox stop <standby>`.
     - Reactivate Active node:
       `instabox exec <active> -- 'nohup python3 watchdog.py > watchdog.log 2>&1 &'`.
     - Verify Active node heartbeat resumes (< 20s).
     - Dispatch critical alert email containing the Standby error logs (`tail -n 50 watchdog.log`).
     - Reset orchestrator state to maintain Active node.

#### Edge Case 3: Active Node Crash Before Scheduled Rotation (Heartbeat Expiration)
- **Phenomenon**: At T+2h into a shift, the Active node crashes abruptly (OOM killer, cloud VM reboot, or unhandled fatal loop).
- **Protection Anchor**: On-chain trigger stop-loss and take-profit orders already exist on Hyperliquid. Market position is 100% protected.
- **Mitigation Protocol**:
  1. **External Heartbeat Liveness Monitor**:
     - GitHub Actions runs an hourly liveness check (or a secondary cron):
       `python scripts/rotator.py --action health-check`.
     - Reads Active node status via `instabox get <active> --json` and checks `.heartbeat` age.
     - If machine status is `suspended` OR `.heartbeat` age > 180s (3x watchdog interval), trigger **Unplanned Emergency Failover**.
  2. **Emergency Failover Execution**:
     - Step 1 (Fencing Kill): Send `instabox stop <active>` to ensure no zombie processes can revive.
     - Step 2 (State Extraction): Instabox persistent `/data` disk retains files even when stopped. Orchestrator extracts `state.json` from the stopped machine. If disk is unreachable, fall back to Tier 2 GitHub Gist checkpoint.
     - Step 3 (Cold Reconciliation Fallback): If neither disk nor Gist is accessible, Standby boots with clean state; `_reconcile_exchange_position()` queries Hyperliquid on-chain state directly and reconstructs the position.
     - Step 4 (Promotion): Standby starts `watchdog.py`, verifies health, and assumes active trading.
     - Step 5 (Alert): Dispatch HIGH alert: `"[FAILOVER] Active node crashed; emergency handover to Standby completed."`

#### Edge Case 4: Stale Lock Resolution & Split-Brain Prevention
- **Core Invariant**: **Under NO circumstance may two instances trade against the same Hyperliquid account simultaneously.**
- **Enforcement Mechanisms**:
  1. **Quiesce-Before-Promote Rule**:
     - In planned handover: Active processes MUST be confirmed terminated (`pkill` + verified empty `ps`) BEFORE Standby's `main.py` is started.
     - In emergency failover: Active VM MUST be confirmed stopped (`instabox stop`) BEFORE Standby is started.
  2. **Distributed Fencing Token (`active_node.lock`)**:
     - The lock contains a monotonically increasing `lease_epoch`:
       ```json
       {
         "lock_id": "8f3e1b74-...",
         "active_node": "bot-node-a",
         "lease_epoch": 105,
         "acquired_at": "2026-11-13T06:00:00Z",
         "expires_at": "2026-11-13T12:15:00Z",
         "heartbeat_at": "2026-11-13T09:12:00Z"
       }
       ```
     - Only the instance matching the latest `lease_epoch` is authorized to run.
  3. **Stale Lock Recovery Algorithm**:
     - If the orchestrator detects an existing lock during handover:
       a) Check expiration: Is `now > expires_at` (TTL = 6h 15m)?
       b) Check heartbeat: Is `now - heartbeat_at > 300s` (5 minutes)?
     - If lock is suspected stale, orchestrator verifies the recorded holder's physical host (`instabox ps <holder>`).
     - If no active trading process exists on the holder node, the lock is broken.
     - The orchestrator logs an audit entry to `lock_history.json`, increments `lease_epoch = epoch + 1`, and assigns the lock to the new active node.

---

## 5. Verification Method

To independently verify the observations, failure mode analysis, and technical designs documented in this report:

### 5.1 Verification of the `pause` Liquidation Defect
Verify that `control.json` action `"pause"` invokes `_enter_system_halt` and calls `close_all()`:
```powershell
# 1. Inspect main.py control command handler
Select-String -Path "C:\Users\Administrator\Desktop\v1\a\main.py" -Pattern "action == .pause." -Context 0,2

# 2. Inspect _enter_system_halt implementation
Select-String -Path "C:\Users\Administrator\Desktop\v1\a\main.py" -Pattern "def _enter_system_halt" -Context 0,12
```
*Expected Result*: Output confirms line 324 calls `_enter_system_halt()`, which at lines 1488-1490 executes `self.exchange.close_all()`. This proves the necessity of using OS SIGTERM process termination instead of `control.json pause`.

### 5.2 Verification of Proposed Deployment Guide
Verify the syntax, completeness, and structure of `proposed_DEPLOYMENT_24_7.md`:
```powershell
Test-Path "C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_3\proposed_DEPLOYMENT_24_7.md"
Get-Item "C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_3\proposed_DEPLOYMENT_24_7.md" | Select-Object Name, Length, LastWriteTime
```
*Expected Result*: File exists, length $\ge 15,000$ bytes, fully populated with all 8 operational sections.

### 5.3 Verification of State File Schema Compatibility
Verify that the current repository state files match the schema requirements specified in Section 4.2:
```powershell
python -c "
import json
for name, req_keys in [
    ('state.json', ['state', 'coin', 'daily_start_equity', 'position']),
    ('risk_state_aggressive.json', ['initial_equity', 'metrics', 'recent_trades']),
    ('equity_history_risk_state_aggressive.json', [])
]:
    data = json.load(open('C:\\Users\\Administrator\\Desktop\\v1\\a\\' + name, encoding='utf-8'))
    if isinstance(data, dict):
        missing = [k for k in req_keys if k not in data]
        print(f'{name}: OK (missing required keys: {missing})')
    elif isinstance(data, list):
        print(f'{name}: OK (list length: {len(data)})')
"
```
*Expected Result*: All three files validate with 0 missing required keys.

### 5.4 Verification of Instabox Multi-Account Token Switching
Verify that setting `INSTABOX_API_KEY` operates as a per-command override without affecting system credentials:
```powershell
python -c "
import subprocess, os
env = os.environ.copy()
env['INSTABOX_API_KEY'] = 'ibk_dummy_test_token'
res = subprocess.run(['instabox', 'whoami'], env=env, capture_output=True, text=True)
print('Instabox token override exit code:', res.returncode)
print('Error output (expected auth failure with dummy token):', res.stderr.strip() or res.stdout.strip())
"
```
*Expected Result*: Returns exit code 3 (auth failure) demonstrating that `INSTABOX_API_KEY` dynamically overrides credentials on every CLI invocation.

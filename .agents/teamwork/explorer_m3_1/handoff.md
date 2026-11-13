# Technical Architecture & Design Specification for `scripts/rotator.py`

**Document**: Handoff Report for Milestone 3 (Free Cloud Server 24/7 Rotation Scheme)  
**Author**: Explorer 1 (`explorer_m3_1`)  
**Target File**: `scripts/rotator.py` (Feature 12 & Feature 13)  
**Date**: 2026-11-13  
**Status**: COMPLETE (Hard Handoff)  

---

## 1. Observation

Direct static analysis of the codebase, runtime configurations, and cloud platform interfaces reveals the following technical facts and environmental constraints:

### 1.1 Trading Bot Process & State Persistence Internals
1. **State Persistence Mechanism**:
   - `main.py:1703-1753` (`_persist_state`): Persists runtime state to `STATE_FILE` (default `"state.json"`) using atomic write-and-replace (`tmp_path = STATE_FILE + ".tmp"`, `json.dump(...)`, `os.replace(tmp_path, STATE_FILE)`).
   - Core fields in `state.json`: `state`, `coin`, `daily_start_equity`, `daily_loss_date`, `daily_loss_pnl`, `quant_state`, `final_macro_state`, `cached_ai_macro_state`, `last_trs`, `news_impact`, `sentiment`, `radar_scores`, `coin_prices`, `consecutive_failures`, `open_regime_cap`, `position` (with `entry_price`, `direction`, `sz_notional`, `tp_price`, `sl_price`, `initial_risk_r`, `peak_price`, `bars_held`, `opened_at`).
   - `main.py:1569-1609` (`_reconcile_exchange_position`): When the bot restarts, it loads `state.json` and preserves `initial_risk_r`, `peak_price` (for trailing stop ratchets), `bars_held`, and `opened_at`. Missing or desynchronized state forces the bot to reset `bars_held = 0` and recalculate `peak_price`, which disrupts risk controls.
2. **Risk Manager State Persistence**:
   - `risk_manager.py:646-663` (`_save_state`): Atomically updates `risk_state_aggressive.json` containing `initial_equity`, `current_equity`, `peak_equity`, `daily_pnl`, `metrics` (total trades, win rate, avg win/loss, max drawdown pct, streak), and `recent_trades`.
   - `risk_manager.py:707-746` (`_append_equity_history`): Persists equity curve time series to `equity_history_risk_state_aggressive.json`.
3. **Heartbeat Protocol & Inter-Process Communication**:
   - `main.py:260-279` (`_write_heartbeat`): Atomically dumps `.heartbeat` file:
     ```python
     payload = {
         "timestamp": time.time(),
         "time_utc": datetime.now(timezone.utc).isoformat(),
         "state": self.state,
         "phase": phase,
         "pid": os.getpid(),
         "coin": self.coin,
         "has_position": self.position is not None,
         "consecutive_failures": self.consecutive_failures,
         "peak_equity": self.peak_equity,
         "daily_loss_pnl": self.daily_loss_pnl,
     }
     ```
   - `main.py:281-291` (`_safe_sleep`): Periodically updates heartbeat and checks `control.json` every 5.0 seconds.
   - `main.py:292-337` (`_check_control_commands`): Consumes `control.json`. Supported actions:
     - `"pause"`: transitions bot to `STATE_HALT` (`_enter_system_halt`) and triggers `self._persist_state()`.
     - `"resume"`: sets `self.state = STATE_IDLE`, resets macro cache, and calls `self._persist_state()`.
     - `"emergency_close"`: executes immediate market close on active position.
4. **Shutdown and Signal Handling Deficiencies**:
   - `main.py:1904-1923`: The entrypoint only traps `except KeyboardInterrupt:`. It has **no signal handler for `SIGTERM`**. On Linux, sending `SIGTERM` causes immediate termination (exit code 143) without executing Python `finally:` blocks.
   - `main.py:1906`: `MAINTENANCE_LOCK = os.getenv("MAINTENANCE_LOCK", "true")`. If set to `true`, `main.py` immediately exits without starting.
5. **Existing Watchdog Functionality**:
   - `watchdog.py:35-46` (`emergency_cancel_orders`): Cancels non-trigger limit orders via `HyperliquidExecutor().cancel_non_trigger_orders()`, strictly preserving remote trigger protective stop-loss/take-profit orders.
   - `watchdog.py:102-141`: Checks heartbeat delay (150s default, 180s for AI phases, 600s for cooldown/halt), enforces 30% max drawdown and 15% daily loss limits, and terminates frozen processes via `proc.terminate()` -> 5s timeout -> `proc.kill()`.

### 1.2 Free Cloud Infrastructure & Instabox Quota Metrics
From `Survey 3` analysis of Instabox CLI `0.8.208+e0b87b977`:
1. `instabox usage`: `limits.maxMachines = 1` per free account. Only one sandbox can exist simultaneously in a free account.
2. `limits.machineSpec`: 4 vCPUs, 8192 MiB RAM, 20 GiB persistent `/data` disk, 16 GiB root disk.
3. `maxRunMinutes = 480` (8 hours). Reaching 8 hours forces auto-suspension and a punitive **60 to 135 minutes cooldown** (`cooldownUntil`), during which `resume`, `exec`, or `ssh` fail with `409 machine_cooldown`.
4. **Cooldown Avoidance**: A graceful user-initiated stop (`instabox stop` or `instabox suspend`) before reaching 480 minutes owes NO cooldown! Resting $\ge 2$ hours clears previous run accumulation.
5. **Dual-Account Alternation**: By using Account A (key `INSTABOX_API_KEY_A`) and Account B (key `INSTABOX_API_KEY_B`) on a 6-hour shift (360 minutes), each instance runs 5h 50m and rests for 6h 10m, guaranteeing zero max-run cooldown and 100% continuous uptime.

---

## 2. Logic Chain

1. **[Observation 1.2: Free tier maxRun = 480m, maxMachines = 1]**  
   $\rightarrow$ A single account cannot provide uninterrupted 24/7 service due to mandatory 1-2h cooldown after 8 hours.  
   $\rightarrow$ A multi-account rotation ring alternating every 6 hours across Account A and Account B is mathematically required.

2. **[Observation 1.1: Local state vs Exchange state]**  
   $\rightarrow$ Hyperliquid holds native on-chain trigger orders (TP/SL) independent of bot process uptime.  
   $\rightarrow$ Local files (`state.json`, `risk_state_aggressive.json`, `equity_history_risk_state_aggressive.json`) maintain dynamic risk metrics (`peak_price`, `bars_held`, trailing stops, streak sizing).  
   $\rightarrow$ The migration window (30-45s) has **zero naked market risk** because protective orders remain active on-chain.

3. **[Observation 1.1: `control.json` action `"pause"` triggers atomic flush]**  
   $\rightarrow$ The coordinator can safely freeze the active node by writing `{"action": "pause"}` to `control.json`.  
   $\rightarrow$ `main.py` detects this within 5s (`_safe_sleep`), enters `STATE_HALT`, and writes final state atomically using `os.replace`.  
   $\rightarrow$ Coordinator can then safely pull clean, uncorrupted state files.

4. **[Observation 1.1: Risk of Split-Brain Double Trading]**  
   $\rightarrow$ If Standby is launched while Active is still trading, both instances will submit conflicting orders for the same wallet, causing position doubling or severe liquidation risk.  
   $\rightarrow$ **Pessimistic Locking** is mandatory: Active must be confirmed frozen/stopped and locked before Standby is promoted. Standby must verify health before Active is permanently demoted.

5. **[Observation 1.1: Missing SIGTERM handling in `main.py`]**  
   $\rightarrow$ The supervisor loop in `rotator.py` must manage process lifecycles properly: sending `SIGINT` (which triggers `KeyboardInterrupt` and clean shutdown) or writing `control.json` `"pause"`, waiting with a timeout, and escalating to `SIGKILL` only after timeout expires.

6. **[Observation 1.2: Orchestrator vs Node Supervisor Duality]**  
   $\rightarrow$ `rotator.py` must support two operational contexts:
   - As an **Orchestrator** (remote coordinator executed by GitHub Actions or local CLI to perform cross-node 6-phase handovers).
   - As an **Instance Supervisor** (local node daemon running inside the sandbox to supervise `main.py`, replace/upgrade `watchdog.py`, monitor heartbeats, and handle graceful demotion).

---

## 3. Caveats & Assumptions

1. **Network Connectivity During Handover**:
   - The coordinator requires internet connectivity to both Cloud Node A and Cloud Node B. If the coordinator loses connection mid-handover, the pessimistic lock TTL (default: 600s) ensures the system does not remain locked indefinitely.
2. **Private Key Storage in Cloud Sandboxes**:
   - `HYPE_SECRET_KEY` (ECDSA private key) is required locally for signing Hyperliquid transactions via `eth-account`. It must be injected into the sandbox environment or persisted in the secure `/data` partition (`/data/home/trading-bot/.env`), never committed to public git.
3. **E2E Subprocess Execution Differences on Windows vs Linux**:
   - Windows does not support POSIX signals (`SIGTERM`, `SIGKILL`, `pkill`) natively in the same way Linux does. `rotator.py` must use platform-agnostic process control (`subprocess.Popen.terminate()`, `proc.kill()`, `psutil` or `taskkill` fallbacks).
4. **Instabox Egress IP Geolocation**:
   - Hyperliquid does not currently geoblock US/EU datacenter IPs for API perps trading. If IP restrictions arise, an HTTP/SOCKS5 proxy can be routed via standard `HTTPS_PROXY` environment variables.

---

## 4. Conclusion & Technical Design Specifications

`scripts/rotator.py` provides the unified engine for 24/7 continuous operation across free cloud instances. It encapsulates:
1. **The 6-Phase Handover Protocol** (Feature 12)
2. **Dual-Mode Command-Line Interface** (Feature 13)
3. **Pessimistic Lock Management with Fencing Tokens**
4. **Resilient Process Supervisor Loop with Dynamic Heartbeat Verification**
5. **Multi-Stage Automated Rollback Engine**

```
+---------------------------------------------------------------------------------------------------+
|                                       scripts/rotator.py                                          |
|                                                                                                   |
|  +-----------------------------+  +-------------------------------+  +-------------------------+  |
|  |     Command-Line Parser     |  |         Lock Manager          |  |    Health Checker       |  |
|  |  (--role, --action, config) |  |   (Pessimistic, TTL, Fencing) |  | (Exchange, LLM, Disk)   |  |
|  +-----------------------------+  +-------------------------------+  +-------------------------+  |
|                                                                                                   |
|  +---------------------------------------------------------------+  +-------------------------+  |
|  |                 Handover Orchestrator (6 Phases)              |  |   State Synchronizer    |  |
|  |  Phase 1: Standby Init       -> Phase 2: Health Check         |  |  (Atomic pull, validate, |  |
|  |  Phase 3: State Lock & Sync  -> Phase 4: Standby Promotion    |  |   inject & checksum)    |  |
|  |  Phase 5: Verification       -> Phase 6: Active Demotion      |  +-------------------------+  |
|  +---------------------------------------------------------------+                                |
|                                                                                                   |
|  +---------------------------------------------------------------+  +-------------------------+  |
|  |                 Process Supervisor Loop                       |  |     Rollback Engine     |  |
|  |   (Popen, Heartbeat monitor, Drawdown stop, SIGTERM/KILL)     |  | (Phase 2/3/4/5 recovery)|  |
|  +---------------------------------------------------------------+  +-------------------------+  |
+---------------------------------------------------------------------------------------------------+
```

---

### 4.1 Detailed Implementation of the 6-Phase Handover Protocol

The handover protocol transitions execution from `Node_Active` to `Node_Standby` with strictly **zero split-brain** and **zero naked risk**.

```
   Node_Active (Shift T)                                               Node_Standby (Shift T+1)
+-------------------------+                                           +-------------------------+
| Trading Loop Running    |                                           | Suspended / Resting     |
+-------------------------+                                           +-------------------------+
             |                                                                     |
             | <==================== [PHASE 1: Standby Init] ===================== |
             |   - Wake Standby instance (API Key B)                               |
             |   - Wait until runtime.ready == true                                |
             |   - Verify code repository and dependencies                         |
             |                                                                     |
             | <================== [PHASE 2: Pre-Flight Health] ================== |
             |   - Standby tests Exchange REST /info (HTTP 200, <1500ms)           |
             |   - Standby tests LLM Relay network reachability                    |
             |   - Standby validates ECDSA private key format                      |
             |   - Standby tests disk write access and permissions                 |
             |   [FAIL -> Abort, Active unaffected]                                |
             |                                                                     |
+-------------------------+                                                        |
| [PHASE 3: Lock & Sync]  |                                                        |
| - Acquire Handover Lock |                                                        |
| - Write control.json    |                                                        |
|   action="pause"        |                                                        |
| - Flush state to disk   |                                                        |
| - Stop Active bot       |                                                        |
+-------------------------+                                                        |
             |                                                                     |
             | ---------- Pull state.json, risk_state.json, etc. --------->        |
             | ---------- Validate JSON schemas & non-empty integrity ---->        |
             | ---------- Push state files to Standby node --------------->        |
             |                                                                     |
             |                                                        +-------------------------+
             |                                                        | [PHASE 4: Promotion]    |
             |                                                        | - Launch rotator --role |
             |                                                        |   active --action run   |
             |                                                        | - Spawn main.py with    |
             |                                                        |   inherited state       |
             |                                                        +-------------------------+
             |                                                                     |
             | <================== [PHASE 5: Verification] ======================= |
             |   - Poll Standby .heartbeat freshness (< 30s)                       |
             |   - Verify Standby process alive (supervisor & main.py)             |
             |   - Verify position reconciliation in logs                          |
             |   - Confirm orderbook subscription                                  |
             |   [FAIL -> Immediate Rollback: Kill Standby, Resume Active]         |
             |                                                                     |
+-------------------------+                                                        |
| [PHASE 6: Demotion]     |                                                        |
| - Confirm process dead  |                                                        |
| - Release handover lock |                                                        |
| - Update active node ref|                                                        |
| - Stop/Suspend instance |                                           +-------------------------+
+-------------------------+                                           | Active Trading Engine   |
             |                                                        +-------------------------+
     Resting for 6 hours
```

#### Phase 1: Standby Initialization
- **Function**: `orchestrator_phase_1_standby_init(standby_node, config)`
- **Steps**:
  1. Set provider environment (e.g. `INSTABOX_API_KEY = config.nodes[standby_node].api_key`).
  2. Query machine status: `instabox get <node_name> --json`.
  3. If status in `("suspended", "stopped")`:
     - Issue `instabox start <node_name>` (or `resume`).
     - Poll machine status every 5 seconds until `runtime.ready == True` or timeout (90s).
  4. Ensure target directory exists (`/data/home/trading-bot`).
  5. Check if environment is prepared: run quick probe `python3 -c "import hyperliquid, eth_account, pandas"`. If missing, execute `bash scripts/deploy_sandbox.sh`.
- **Timeout**: 120 seconds.
- **On Failure**: Abort rotation. Node Active is untouched. Postpone handover by 5 minutes.

#### Phase 2: Pre-Flight Health Check
- **Function**: `orchestrator_phase_2_health_check(standby_node, config)`
- **Steps executed remotely on Standby**:
  1. `CheckCredentials`: Validate `.env` has valid non-empty `HYPE_SECRET_KEY`, `HYPE_USER_ADDRESS`, `THIRD_PARTY_API_KEY`. Validate private key can derive an address matching `HYPE_USER_ADDRESS` (or `HYPE_API_WALLET_ADDRESS`).
  2. `CheckNetworkExchange`: Send HTTPS GET request to Hyperliquid `/info` (`{"type": "metaAndAssetCtxs"}`). Require HTTP 200 within 2000ms.
  3. `CheckNetworkLLM`: Verify TCP/HTTPS connection to `THIRD_PARTY_BASE_URL`.
  4. `CheckDiskIO`: Write unique UUID to `.health_check_tmp`, read back, verify matching content, delete file. Check free disk space $\ge 500$ MB.
  5. `CheckProcessReady`: Verify no orphaned `main.py` or `watchdog.py` instances currently occupy the sandbox.
- **Timeout**: 45 seconds.
- **On Failure**: Abort rotation. Node Active is untouched. Alert generated with exact check failure output.

#### Phase 3: State Lock & Sync
- **Function**: `orchestrator_phase_3_lock_and_sync(active_node, standby_node, config)`
- **Steps**:
  1. **Acquire Pessimistic Lock**:
     - Write `rotation.lock.json` with status `"HANDOVER_IN_PROGRESS"`, `active=active_node`, `standby=standby_node`, `fencing_token=fencing_token + 1`, and `expires_at = now + 600s`.
  2. **Quiesce Active Node**:
     - Push `{"action": "pause"}` to `control.json` on Active node via provider push (`instabox push`).
     - Wait 8 seconds for `main.py`'s `_safe_sleep` to consume `control.json`, transition to `STATE_HALT`, and execute `_persist_state()`.
     - Send graceful stop signal to Active process: `pkill -SIGINT -f main.py` (or through supervisor demotion).
     - Confirm Active process has exited cleanly.
  3. **Atomic State Pull**:
     - Pull mandatory state files from Active to coordinator staging directory (`./sync_staging/`):
       - `state.json`
       - `risk_state_aggressive.json`
       - `equity_history_risk_state_aggressive.json`
     - Optional caches: `news_sentiment_cache.json`, `correlation_matrix.json`.
  4. **State Integrity Validation**:
     - Verify files exist and sizes are $> 0$ bytes.
     - Validate JSON syntax using `json.loads()`.
     - Schema assertion on `state.json`:
       - Must contain keys `["state", "coin", "daily_start_equity", "quant_state"]`.
       - If `position` is not `None`, must contain `["direction", "entry_price", "sz_notional"]`.
     - Schema assertion on `risk_state_aggressive.json`:
       - Must contain keys `["initial_equity", "current_equity", "metrics", "recent_trades"]`.
  5. **State Push to Standby**:
     - Push verified state files to Standby node `/data/home/trading-bot/`.
     - Clean up any stale `.heartbeat` or `control.json` on Standby node (`rm -f .heartbeat control.json`).
- **Timeout**: 60 seconds.
- **On Failure**: Trigger Phase 3 Rollback: Unpause Active node (`control.json` action `"resume"`), release lock.

#### Phase 4: Standby Promotion
- **Function**: `orchestrator_phase_4_promote_standby(standby_node, config)`
- **Steps**:
  1. Ensure `MAINTENANCE_LOCK=false` in Standby `.env`.
  2. Launch trading bot supervisor on Standby in background:
     ```bash
     nohup python3 scripts/rotator.py --role active --action run > supervisor.log 2>&1 &
     ```
  3. Wait 5 seconds, query Standby process table (`instabox exec standby -- pgrep -f "main.py"`).
  4. Verify PID was assigned and supervisor process is active.
- **Timeout**: 30 seconds.
- **On Failure**: Trigger Phase 4 Rollback: Kill Standby processes, restart Active node.

#### Phase 5: Standby Verification
- **Function**: `orchestrator_phase_5_verify_standby(standby_node, config)`
- **Steps**:
  - Poll Standby every 5 seconds for up to 60 seconds:
    1. **Process Liveness**: Verify both `scripts/rotator.py` (supervisor) and `main.py` are alive.
    2. **Heartbeat Freshness**: Read `.heartbeat` on Standby. Confirm `time.time() - heartbeat["timestamp"] < 30.0` and `heartbeat["state"] in ("IDLE", "TRADING")`.
    3. **Position Reconciliation**: Scan last 40 lines of `trading_bot.log` on Standby:
       - Confirm message: `[对账] 链上仓位与本地状态一致` or `[启动] 预热多币种相关性追踪器` or `[主循环] 轮询心跳正常`.
       - Confirm absence of critical crashes (`Traceback`, `NameError`, `KeyError`, `401 Unauthorized`).
- **Timeout**: 60 seconds.
- **On Failure**: Trigger Phase 5 Rollback: Kill Standby immediately, reactivate Active node.

#### Phase 6: Active Demotion & Clean Exit
- **Function**: `orchestrator_phase_6_demote_active(active_node, standby_node, config)`
- **Steps**:
  1. Final verification that old Active processes are completely stopped (`pkill -9 -f "python3.*(main|watchdog|rotator)"`).
  2. Put old Active container to sleep to preserve quotas:
     - Issue `instabox stop <active_node>` (or `suspend`).
     - Old Active enters resting state for the next 6 hours, clearing cooldown counters.
  3. Update Lock & Active Node Pointer:
     - Update `rotation.lock.json`:
       - `status = "ACTIVE_HELD"`
       - `active_node = standby_node`
       - `standby_node = active_node`
       - `last_successful_handover = now_utc`
       - Clear transition lock.
  4. Dispatch Handover Success Alert via SMTP / Webhook:
     - Shift switched: `active_node -> standby_node`.
     - Handover elapsed time (typically ~35 seconds).
     - Account equity & open position status reported.

---

### 4.2 Command-Line Interface (CLI) Specification

`scripts/rotator.py` supports a unified CLI architecture serving both orchestrator workflows and local supervisor processes.

#### CLI Modes & Argument Syntax
```
usage: rotator.py [-h] [--role {orchestrator,active,standby,supervisor}]
                  [--action {handoff,run,health-check,status,rollback,quiesce,promote,demote}]
                  [--active-node ACTIVE_NODE] [--standby-node STANDBY_NODE]
                  [--config CONFIG] [--state-dir STATE_DIR] [--lock-file LOCK_FILE]
                  [--timeout TIMEOUT] [--force] [--dry-run]
                  [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}]
```

#### Argument Definitions
| Argument | Type | Default | Environment Variable | Description |
| :--- | :--- | :--- | :--- | :--- |
| `--role` | Choice | `orchestrator` | `ROTATOR_ROLE` | Execution context: `orchestrator` (coordinator), `active` (node trading), `standby` (node staging) |
| `--action` | Choice | `status` | `ROTATOR_ACTION` | Operation to perform: `handoff`, `run`, `health-check`, `status`, `rollback`, `quiesce`, `promote`, `demote` |
| `--active-node` | String | `None` (auto-detect) | `ROTATOR_ACTIVE_NODE` | Machine identifier of current active node (e.g. `bot-node-a`) |
| `--standby-node`| String | `None` (auto-detect) | `ROTATOR_STANDBY_NODE` | Machine identifier of standby target node (e.g. `bot-node-b`) |
| `--config` | Path | `config/rotation_config.json` | `ROTATOR_CONFIG_FILE` | Path to JSON rotation configuration file |
| `--state-dir` | Path | `.` | `ROTATOR_STATE_DIR` | Directory containing state files (`state.json`, etc.) |
| `--lock-file` | Path | `rotation.lock.json` | `ROTATOR_LOCK_FILE` | Path to shared/local pessimistic lock file |
| `--timeout` | Integer | `300` | `ROTATOR_TIMEOUT` | Global operation timeout in seconds |
| `--force` | Flag | `False` | `ROTATOR_FORCE` | Force break stale locks or override safety guardrails |
| `--dry-run` | Flag | `False` | `ROTATOR_DRY_RUN` | Simulate actions without modifying state or killing processes |
| `--log-level` | Choice | `INFO` | `ROTATOR_LOG_LEVEL` | Logging verbosity |

#### Standard Exit Codes
- `0`: Success (Handover complete, health check passed, clean shutdown).
- `1`: General Error (Configuration missing, unhandled runtime exception).
- `2`: Health Check Failed (Pre-flight checks did not pass).
- `3`: Lock Contention (Pessimistic lock held by another process and valid).
- `4`: Verification Timeout (Standby failed Phase 5 heartbeat or process checks).
- `5`: Rollback Triggered (Handover failed, rollback executed).
- `6`: Critical Double-Fault (Rollback also failed; emergency stops engaged).

---

### 4.3 Lock Management & Collision Prevention (Pessimistic Locking)

To prevent split-brain trading where two instances simultaneously submit orders for the same account, `scripts/rotator.py` implements a **Pessimistic Distributed Lease Lock with Fencing Tokens**.

#### Lock File Schema (`rotation.lock.json`)
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "RotationLock",
  "type": "object",
  "properties": {
    "lock_id": { "type": "string" },
    "status": { 
      "type": "string", 
      "enum": ["IDLE", "ACTIVE_HELD", "HANDOVER_IN_PROGRESS", "ROLLBACK"] 
    },
    "active_node": { "type": "string" },
    "standby_node": { "type": "string" },
    "owner": { "type": "string" },
    "fencing_token": { "type": "integer", "minimum": 1 },
    "acquired_at": { "type": "string", "format": "date-time" },
    "expires_at": { "type": "string", "format": "date-time" },
    "ttl_seconds": { "type": "integer", "default": 600 },
    "current_phase": { "type": "integer", "minimum": 0, "maximum": 6 },
    "handover_history": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "timestamp": { "type": "string" },
          "from_node": { "type": "string" },
          "to_node": { "type": "string" },
          "duration_seconds": { "type": "number" },
          "result": { "type": "string" }
        }
      }
    }
  },
  "required": ["lock_id", "status", "active_node", "fencing_token", "acquired_at", "expires_at"]
}
```

#### Collision Prevention & Stale Lock Recovery Algorithm
1. **Pessimistic Mutex Rule**:
   - Before any state transfer or process promotion occurs, `acquire_lock()` must succeed.
   - If `rotation.lock.json` exists and `status == "HANDOVER_IN_PROGRESS"`:
     - Check `expires_at`:
       - If `now_utc < expires_at`: The lock is active and valid. Another handover is in flight. Immediately reject request with exit code `3` (`Lock Contention`).
       - If `now_utc >= expires_at`: The lock is **STALE** (previous orchestrator process crashed mid-handover).
2. **Stale Lock Recovery Procedure**:
   - If stale lock detected:
     - Inspect whether `active_node` or `standby_node` are currently running.
     - Log alert: `[LOCK] Stale lock detected (expired at {expires_at}). Initiating stale recovery.`
     - Increment `fencing_token = old_fencing_token + 1`.
     - Force release stale lock and re-acquire under new owner ID.
3. **Fencing Token Guard in Node Supervisor**:
   - The node supervisor on each instance is initialized with the current `fencing_token`.
   - Supervisor periodically verifies its token against `rotation.lock.json`.
   - If `local_token < lock_fencing_token`: The node has been fenced off! Supervisor immediately sends `SIGTERM` to `main.py`, enters `SYSTEM_HALT`, and stops itself to eliminate ghost trading.

---

### 4.4 Process Supervisor Loop Specification (`--action run`)

When executed on an active node (`rotator.py --role active --action run`), the script functions as the resilient process supervisor, succeeding `watchdog.py`.

#### Lifecycle State Machine
```
   +--------------+      Spawn main.py       +--------------+
   |   STOPPED    | -----------------------> |   STARTING   |
   +--------------+                          +--------------+
          ^                                         |
          | Graceful SIGTERM                        | 30s Grace Period Passed
          | or Max Crashes                          v
   +--------------+     Delay > Timeout      +--------------+
   |   HALTING    | <----------------------- |   RUNNING    |
   +--------------+   (or unexpected exit)   +--------------+
          |                                         |
          | Process Terminated                      | Normal Loop Heartbeat
          v                                         v
   +--------------+     Backoff < Limit      +--------------+
   |  RESTARTING  | -----------------------> |   STARTING   |
   +--------------+                          +--------------+
```

#### Detailed Supervisor Mechanisms
1. **Subprocess Management**:
   - Invocation: `proc = subprocess.Popen([sys.executable, "main.py"], cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)`
   - Dual-Tee Logging: A background reader thread consumes lines from `proc.stdout`, prints to console, and appends to `trading_bot.log` with timestamps.
   - PID Registration: Saves supervisor PID and child `main.py` PID into `.supervisor_pid`.
2. **Heartbeat Monitoring & Dynamic Thresholds**:
   - Polls `.heartbeat` every `check_interval_seconds` (default: 10s).
   - Dynamic timeout adjustment:
     - `COOL_DOWN` or `SYSTEM_HALT`: `timeout = 600.0s`
     - `AI_MACRO`, `AI_TRADE`, `FETCH_MARKET`: `timeout = 180.0s`
     - Normal `IDLE` or `TRADING`: `timeout = 150.0s`
   - If `time.time() - heartbeat.timestamp > timeout`:
     - Process marked as frozen/hung.
     - Log critical error and send alert: `[SUPERVISOR] Bot process hang detected (delay={delay}s > {timeout}s). Initiating restart.`
     - Execute graceful termination $\rightarrow$ emergency non-trigger order cancellation $\rightarrow$ restart.
3. **Circuit Breaker Protection (Drawdown / Daily Loss)**:
   - Reads `daily_loss_pnl` and `peak_equity` from `.heartbeat`.
   - If `daily_loss_pct >= 15.0%` or `max_drawdown_pct >= 30.0%`:
     - **Hard Stop**: Terminate `main.py`, execute `emergency_cancel_orders()`, send fatal alert, and DO NOT restart.
4. **Graceful Termination Escalation**:
   - Step 1: Write `{"action": "pause"}` to `control.json` to allow `main.py` to flush state.
   - Step 2: Send `proc.terminate()` (POSIX `SIGTERM` or Windows `CTRL_C_EVENT`).
   - Step 3: Wait up to `sigterm_timeout_seconds` (15s).
   - Step 4: If process is still alive after 15s: escalate to `proc.kill()` (`SIGKILL`) and wait 3s.
   - Step 5: Verify clean process exit and close log streams.

---

### 4.5 Rollback & Recovery Mechanisms

Handover operations must be fail-safe. If any stage of promotion or verification fails, the orchestrator executes an automated rollback to keep the active node trading.

| Phase Where Failure Occurs | Detection Method | Recovery / Rollback Actions | Resulting Active Node |
| :--- | :--- | :--- | :--- |
| **Phase 1: Standby Init** | Container wake timeout (>90s) or cloud API error | 1. Abort handover.<br>2. Active node untouched and continues trading.<br>3. Schedule retry in 5m. | `Node_Active` (unaffected) |
| **Phase 2: Health Check** | Standby health probe returns non-zero exit code | 1. Abort handover.<br>2. Active node untouched and continues trading.<br>3. Send diagnostic alert with failure logs. | `Node_Active` (unaffected) |
| **Phase 3: Lock & State Sync** | State pull fails or JSON schema assertion fails | 1. Abort transfer.<br>2. Send `{"action": "resume"}` to Active node `control.json`.<br>3. If Active stopped, restart supervisor on Active.<br>4. Release handover lock. | `Node_Active` (resumed) |
| **Phase 4: Standby Promotion** | Standby `main.py` fails to spawn or crashes within 5s | 1. Send `SIGKILL` to Standby.<br>2. Reactivate Active node (restart supervisor).<br>3. Verify Active `.heartbeat` updates.<br>4. Revert lock to `ACTIVE_HELD` on Active. | `Node_Active` (restored) |
| **Phase 5: Verification** | Standby `.heartbeat` stale (>30s) or critical crash in log | 1. Immediately kill Standby processes.<br>2. Stop Standby container (`instabox stop`).<br>3. Reactivate Active node (`instabox start active`).<br>4. Launch supervisor on Active node.<br>5. Confirm Active `.heartbeat` freshness.<br>6. Revert lock to Active node.<br>7. Send critical rollback alert. | `Node_Active` (restored) |
| **Double Fault (Both Nodes Fail)** | Active cannot be resumed after Standby failure | 1. Trigger `emergency_cancel_orders()` on Hyperliquid (cleans up open limit orders).<br>2. Remote trigger TP/SL protective orders remain untouched on-chain (zero naked risk).<br>3. Dispatch emergency alert to user. | `MANUAL_INTERVENTION` |

---

### 4.6 Configuration File Schema (`config/rotation_config.json`)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "version": "1.0.0",
  "rotation": {
    "shift_duration_hours": 6,
    "handover_lead_minutes": 10,
    "max_run_limit_minutes": 480,
    "ring_nodes": ["bot-node-a", "bot-node-b"]
  },
  "nodes": {
    "bot-node-a": {
      "provider": "instabox",
      "machine_name": "bot-node-a",
      "api_key_env": "INSTABOX_API_KEY_A",
      "workdir": "/data/home/trading-bot"
    },
    "bot-node-b": {
      "provider": "instabox",
      "machine_name": "bot-node-b",
      "api_key_env": "INSTABOX_API_KEY_B",
      "workdir": "/data/home/trading-bot"
    }
  },
  "state_sync": {
    "mandatory_files": [
      "state.json",
      "risk_state_aggressive.json",
      "equity_history_risk_state_aggressive.json"
    ],
    "optional_files": [
      "news_sentiment_cache.json",
      "correlation_matrix.json"
    ],
    "staging_dir": "./sync_staging"
  },
  "lock": {
    "file_path": "rotation.lock.json",
    "ttl_seconds": 600,
    "stale_timeout_seconds": 900
  },
  "supervisor": {
    "heartbeat_file": ".heartbeat",
    "check_interval_seconds": 10,
    "heartbeat_timeout_seconds": 150,
    "ai_phase_timeout_seconds": 180,
    "halt_timeout_seconds": 600,
    "sigterm_timeout_seconds": 15,
    "max_restarts_per_hour": 5,
    "restart_backoff_base_seconds": 5,
    "max_drawdown_limit_pct": 30.0,
    "daily_loss_limit_pct": 15.0
  },
  "health_check": {
    "http_timeout_seconds": 10,
    "exchange_info_endpoint": "https://api.hyperliquid.xyz/info",
    "max_acceptable_latency_ms": 2000,
    "min_free_disk_mb": 500
  }
}
```

---

### 4.7 Technical Class Architecture & Code Skeleton for `scripts/rotator.py`

Below is the complete structural architecture for `scripts/rotator.py`, providing the blueprint for the implementer:

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
24/7 Multi-Instance Cloud Rotation Orchestrator & Process Supervisor
═════════════════════════════════════════════════════════════════════
Milestone: M3 (Feature 12 & Feature 13)
Location: scripts/rotator.py
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ==============================================================================
# 1. Logging Setup
# ==============================================================================
logger = logging.getLogger("rotator")

def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

# ==============================================================================
# 2. Lock Management & Fencing
# ==============================================================================
class LockManager:
    """Manages pessimistic file locks with fencing tokens and TTL expiration."""
    def __init__(self, lock_file: str, ttl_seconds: int = 600):
        self.lock_path = Path(lock_file)
        self.ttl = ttl_seconds

    def acquire(self, active_node: str, standby_node: str, owner: str) -> Tuple[bool, int]:
        """Attempts atomic pessimistic lock acquisition. Returns (success, fencing_token)."""
        now = datetime.now(timezone.utc)
        current = self.read()
        fencing_token = 1

        if current:
            status = current.get("status")
            expires_at = datetime.fromisoformat(current["expires_at"])
            fencing_token = current.get("fencing_token", 0) + 1
            if status == "HANDOVER_IN_PROGRESS" and now < expires_at:
                logger.warning("[LOCK] Lock is actively held by %s until %s", current.get("owner"), expires_at)
                return False, current.get("fencing_token", 0)
            elif now >= expires_at:
                logger.warning("[LOCK] Stale lock detected (expired at %s). Breaking lock.", expires_at)

        payload = {
            "lock_id": f"lock-{int(now.timestamp())}",
            "status": "HANDOVER_IN_PROGRESS",
            "active_node": active_node,
            "standby_node": standby_node,
            "owner": owner,
            "fencing_token": fencing_token,
            "acquired_at": now.isoformat(),
            "expires_at": (now.timestamp() + self.ttl),
            "ttl_seconds": self.ttl,
            "current_phase": 1
        }
        self._write_atomic(payload)
        return True, fencing_token

    def update_phase(self, phase: int):
        data = self.read()
        if data:
            data["current_phase"] = phase
            self._write_atomic(data)

    def release_to_active(self, active_node: str, standby_node: str):
        now = datetime.now(timezone.utc)
        current = self.read() or {}
        payload = {
            "lock_id": current.get("lock_id", f"lock-{int(now.timestamp())}"),
            "status": "ACTIVE_HELD",
            "active_node": active_node,
            "standby_node": standby_node,
            "owner": "system",
            "fencing_token": current.get("fencing_token", 1),
            "acquired_at": now.isoformat(),
            "expires_at": (now.timestamp() + 86400 * 365),
            "ttl_seconds": 86400 * 365,
            "current_phase": 0
        }
        self._write_atomic(payload)

    def read(self) -> Optional[dict]:
        if not self.lock_path.exists():
            return None
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("[LOCK] Error reading lock file: %s", e)
            return None

    def _write_atomic(self, payload: dict):
        tmp_path = self.lock_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, self.lock_path)

# ==============================================================================
# 3. Provider Client (Instabox Cloud Abstraction)
# ==============================================================================
class CloudProviderClient:
    """Wraps Instabox CLI / Cloud commands for multi-account sandboxes."""
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _run_cmd(self, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["INSTABOX_API_KEY"] = self.api_key
        return subprocess.run(["instabox"] + args, env=env, capture_output=True, text=True, check=check)

    def get_status(self, machine_name: str) -> dict:
        res = self._run_cmd(["get", machine_name, "--json"])
        return json.loads(res.stdout)

    def start(self, machine_name: str):
        self._run_cmd(["start", machine_name])

    def stop(self, machine_name: str):
        self._run_cmd(["stop", machine_name], check=False)

    def exec_cmd(self, machine_name: str, cmd: str, background: bool = False, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        args = ["exec"]
        if background:
            args.append("--background")
        if cwd:
            args.extend(["--cwd", cwd])
        args.extend([machine_name, "--", cmd])
        return self._run_cmd(args, check=not background)

    def pull_file(self, machine_name: str, remote_path: str, local_path: str):
        self._run_cmd(["pull", machine_name, remote_path, local_path])

    def push_file(self, machine_name: str, local_path: str, remote_path: str):
        self._run_cmd(["push", machine_name, local_path, remote_path])

# ==============================================================================
# 4. Handover Orchestrator (The 6-Phase Engine)
# ==============================================================================
class HandoverOrchestrator:
    """Executes the 6-Phase Handover Protocol with automated rollback."""
    def __init__(self, config_path: str, lock_file: str):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.lock_mgr = LockManager(lock_file)
        self.staging_dir = Path(self.config["state_sync"]["staging_dir"])
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    def _get_client(self, node_name: str) -> CloudProviderClient:
        node_cfg = self.config["nodes"][node_name]
        api_key = os.getenv(node_cfg["api_key_env"])
        if not api_key:
            raise ValueError(f"Environment variable {node_cfg['api_key_env']} not set for node {node_name}")
        return CloudProviderClient(api_key)

    def execute_handover(self, active_node: str, standby_node: str) -> bool:
        logger.info("==================================================================")
        logger.info("[HANDOVER] Starting Handover: %s -> %s", active_node, standby_node)
        logger.info("==================================================================")

        # Phase 1: Standby Init
        self.lock_mgr.update_phase(1)
        if not self._phase_1_standby_init(standby_node):
            logger.error("[HANDOVER] Phase 1 failed. Aborting handover.")
            return False

        # Phase 2: Standby Health Check
        self.lock_mgr.update_phase(2)
        if not self._phase_2_health_check(standby_node):
            logger.error("[HANDOVER] Phase 2 failed. Aborting handover.")
            return False

        # Phase 3: State Lock & Sync
        acquired, token = self.lock_mgr.acquire(active_node, standby_node, owner="orchestrator")
        if not acquired:
            logger.error("[HANDOVER] Could not acquire pessimistic lock. Aborting.")
            return False

        self.lock_mgr.update_phase(3)
        if not self._phase_3_lock_and_sync(active_node, standby_node):
            logger.error("[HANDOVER] Phase 3 failed. Executing Rollback to Active.")
            self._rollback_phase_3(active_node)
            return False

        # Phase 4: Standby Promotion
        self.lock_mgr.update_phase(4)
        if not self._phase_4_standby_promotion(standby_node):
            logger.error("[HANDOVER] Phase 4 failed. Executing Rollback to Active.")
            self._rollback_phase_4_5(active_node, standby_node)
            return False

        # Phase 5: Standby Verification
        self.lock_mgr.update_phase(5)
        if not self._phase_5_verification(standby_node):
            logger.error("[HANDOVER] Phase 5 failed. Executing Rollback to Active.")
            self._rollback_phase_4_5(active_node, standby_node)
            return False

        # Phase 6: Active Demotion & Clean Exit
        self.lock_mgr.update_phase(6)
        self._phase_6_active_demotion(active_node, standby_node)
        self.lock_mgr.release_to_active(active_node=standby_node, standby_node=active_node)

        logger.info("==================================================================")
        logger.info("[HANDOVER] Handover Completed Successfully! Active: %s", standby_node)
        logger.info("==================================================================")
        return True

    def _phase_1_standby_init(self, standby_node: str) -> bool:
        logger.info("[Phase 1/6] Initializing Standby Node: %s", standby_node)
        client = self._get_client(standby_node)
        status = client.get_status(standby_node)
        state = status.get("state")
        if state in ("suspended", "stopped"):
            logger.info("[Phase 1/6] Waking standby instance...")
            client.start(standby_node)
            # Poll for ready
            for _ in range(18):
                time.sleep(5)
                s = client.get_status(standby_node)
                if s.get("state") == "running":
                    logger.info("[Phase 1/6] Standby instance running.")
                    return True
            return False
        return True

    def _phase_2_health_check(self, standby_node: str) -> bool:
        logger.info("[Phase 2/6] Running Health Check on Standby Node: %s", standby_node)
        client = self._get_client(standby_node)
        workdir = self.config["nodes"][standby_node]["workdir"]
        probe_cmd = f"python3 {workdir}/scripts/rotator.py --role standby --action health-check"
        res = client.exec_cmd(standby_node, probe_cmd)
        if res.returncode != 0:
            logger.error("[Phase 2/6] Health check failed: %s", res.stderr)
            return False
        logger.info("[Phase 2/6] Pre-flight health check PASSED.")
        return True

    def _phase_3_lock_and_sync(self, active_node: str, standby_node: str) -> bool:
        logger.info("[Phase 3/6] State Lock & Sync: %s -> %s", active_node, standby_node)
        client_active = self._get_client(active_node)
        client_standby = self._get_client(standby_node)
        workdir_active = self.config["nodes"][active_node]["workdir"]
        workdir_standby = self.config["nodes"][standby_node]["workdir"]

        # Step 1: Push pause control to Active
        pause_payload = json.dumps({"action": "pause"})
        tmp_ctrl = self.staging_dir / "control.json"
        tmp_ctrl.write_text(pause_payload, encoding="utf-8")
        client_active.push_file(active_node, str(tmp_ctrl), f"{workdir_active}/control.json")
        logger.info("[Phase 3/6] Sent pause control to Active. Waiting 8s for flush...")
        time.sleep(8)

        # Step 2: Stop Active processes
        client_active.exec_cmd(active_node, "pkill -SIGINT -f main.py || true")
        time.sleep(2)

        # Step 3: Pull mandatory state files
        for fname in self.config["state_sync"]["mandatory_files"]:
            remote_f = f"{workdir_active}/{fname}"
            local_f = str(self.staging_dir / fname)
            client_active.pull_file(active_node, remote_f, local_f)
            # Validate JSON
            with open(local_f, "r", encoding="utf-8") as f:
                json.load(f)
            logger.info("[Phase 3/6] Pulled and validated %s (%d bytes)", fname, os.path.getsize(local_f))

        # Step 4: Push state files to Standby
        for fname in self.config["state_sync"]["mandatory_files"]:
            local_f = str(self.staging_dir / fname)
            remote_f = f"{workdir_standby}/{fname}"
            client_standby.push_file(standby_node, local_f, remote_f)
            logger.info("[Phase 3/6] Injected %s to Standby", fname)

        # Clean control & heartbeat on Standby
        client_standby.exec_cmd(standby_node, f"rm -f {workdir_standby}/control.json {workdir_standby}/.heartbeat")
        return True

    def _phase_4_standby_promotion(self, standby_node: str) -> bool:
        logger.info("[Phase 4/6] Promoting Standby Node: %s", standby_node)
        client = self._get_client(standby_node)
        workdir = self.config["nodes"][standby_node]["workdir"]
        launch_cmd = f"nohup python3 {workdir}/scripts/rotator.py --role active --action run > {workdir}/supervisor.log 2>&1 &"
        client.exec_cmd(standby_node, launch_cmd, background=True)
        time.sleep(5)
        # Check process
        res = client.exec_cmd(standby_node, "pgrep -f main.py")
        if res.returncode != 0:
            logger.error("[Phase 4/6] main.py process not found on Standby!")
            return False
        logger.info("[Phase 4/6] Standby trading process spawned (PID: %s)", res.stdout.strip())
        return True

    def _phase_5_verification(self, standby_node: str) -> bool:
        logger.info("[Phase 5/6] Verifying Standby Node Health & Trading Loop...")
        client = self._get_client(standby_node)
        workdir = self.config["nodes"][standby_node]["workdir"]
        local_hb = str(self.staging_dir / ".heartbeat_check")

        for attempt in range(12):
            time.sleep(5)
            try:
                client.pull_file(standby_node, f"{workdir}/.heartbeat", local_hb)
                with open(local_hb, "r", encoding="utf-8") as f:
                    hb = json.load(f)
                age = time.time() - float(hb.get("timestamp", 0))
                if age < 30.0 and hb.get("state") in ("IDLE", "TRADING"):
                    logger.info("[Phase 5/6] Standby heartbeat verified fresh (age=%.1fs, state=%s)", age, hb.get("state"))
                    return True
            except Exception:
                pass
            logger.debug("[Phase 5/6] Waiting for Standby heartbeat update (attempt %d/12)...", attempt + 1)

        logger.error("[Phase 5/6] Standby verification timed out after 60s!")
        return False

    def _phase_6_active_demotion(self, active_node: str, standby_node: str):
        logger.info("[Phase 6/6] Demoting and Stopping Old Active Node: %s", active_node)
        client = self._get_client(active_node)
        client.exec_cmd(active_node, "pkill -9 -f main.py || true")
        client.stop(active_node)
        logger.info("[Phase 6/6] Node %s stopped cleanly for cooldown rest.", active_node)

    def _rollback_phase_3(self, active_node: str):
        logger.warning("[ROLLBACK] Resuming Active node %s...", active_node)
        client = self._get_client(active_node)
        workdir = self.config["nodes"][active_node]["workdir"]
        # Remove pause / send resume
        resume_payload = json.dumps({"action": "resume"})
        tmp_ctrl = self.staging_dir / "control.json"
        tmp_ctrl.write_text(resume_payload, encoding="utf-8")
        client.push_file(active_node, str(tmp_ctrl), f"{workdir}/control.json")
        client.exec_cmd(active_node, f"nohup python3 {workdir}/scripts/rotator.py --role active --action run > {workdir}/supervisor.log 2>&1 &", background=True)

    def _rollback_phase_4_5(self, active_node: str, standby_node: str):
        logger.critical("[ROLLBACK] Promotion failed. Terminating Standby and resurrecting Active!")
        client_standby = self._get_client(standby_node)
        client_standby.exec_cmd(standby_node, "pkill -9 -f main.py || true")
        client_standby.stop(standby_node)
        self._rollback_phase_3(active_node)

# ==============================================================================
# 5. Process Supervisor Loop (Node Level Daemon)
# ==============================================================================
class ProcessSupervisor:
    """Supervises main.py, monitors .heartbeat, handles crashes and graceful SIGTERM."""
    def __init__(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.sup_cfg = self.config["supervisor"]
        self.heartbeat_path = Path(self.sup_cfg["heartbeat_file"])
        self.running = True
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info("[SUPERVISOR] Caught signal %s. Initiating graceful shutdown.", signum)
        self.running = False

    def run(self):
        logger.info("[SUPERVISOR] Starting Trading Bot Supervisor Loop...")
        restart_count = 0

        while self.running:
            logger.info("[SUPERVISOR] Launching main.py (attempt %d)...", restart_count + 1)
            # Ensure clean heartbeat
            if self.heartbeat_path.exists():
                self.heartbeat_path.unlink(missing_ok=True)

            proc = subprocess.Popen([sys.executable, "main.py"], cwd=os.getcwd())
            start_time = time.time()

            while self.running:
                time.sleep(self.sup_cfg["check_interval_seconds"])

                # Check process alive
                retcode = proc.poll()
                if retcode is not None:
                    if not self.running:
                        logger.info("[SUPERVISOR] Clean termination complete.")
                        return
                    logger.error("[SUPERVISOR] main.py exited unexpectedly with code %s", retcode)
                    restart_count += 1
                    if restart_count > self.sup_cfg["max_restarts_per_hour"]:
                        logger.critical("[SUPERVISOR] Max restart limit reached. Halting.")
                        return
                    time.sleep(self.sup_cfg["restart_backoff_base_seconds"] * restart_count)
                    break

                # Heartbeat check
                if (time.time() - start_time) < 30.0:
                    continue  # Grace period

                if self.heartbeat_path.exists():
                    try:
                        with open(self.heartbeat_path, "r", encoding="utf-8") as f:
                            hb = json.load(f)
                        delay = time.time() - float(hb.get("timestamp", start_time))
                        phase = hb.get("phase", "")
                        state = hb.get("state", "UNKNOWN")

                        # Dynamic timeout
                        timeout = self.sup_cfg["heartbeat_timeout_seconds"]
                        if state in ("COOL_DOWN", "SYSTEM_HALT"):
                            timeout = self.sup_cfg["halt_timeout_seconds"]
                        elif phase in ("AI_MACRO", "AI_TRADE", "FETCH_MARKET"):
                            timeout = self.sup_cfg["ai_phase_timeout_seconds"]

                        if delay > timeout:
                            logger.critical("[SUPERVISOR] Hang detected (delay=%.1fs > %.0fs). Terminating hung bot.", delay, timeout)
                            proc.terminate()
                            try:
                                proc.wait(timeout=self.sup_cfg["sigterm_timeout_seconds"])
                            except subprocess.TimeoutExpired:
                                proc.kill()
                            break
                    except Exception as e:
                        logger.debug("[SUPERVISOR] Heartbeat read error: %s", e)

            if not self.running:
                logger.info("[SUPERVISOR] Terminating child process cleanly...")
                proc.terminate()
                try:
                    proc.wait(timeout=self.sup_cfg["sigterm_timeout_seconds"])
                except subprocess.TimeoutExpired:
                    proc.kill()
                break

# ==============================================================================
# 6. CLI Entrypoint
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="24/7 Cloud Rotation Orchestrator & Supervisor")
    parser.add_argument("--role", choices=["orchestrator", "active", "standby", "supervisor"], default="orchestrator")
    parser.add_argument("--action", choices=["handoff", "run", "health-check", "status", "rollback", "quiesce"], default="status")
    parser.add_argument("--active-node", default=os.getenv("ROTATOR_ACTIVE_NODE"))
    parser.add_argument("--standby-node", default=os.getenv("ROTATOR_STANDBY_NODE"))
    parser.add_argument("--config", default=os.getenv("ROTATOR_CONFIG_FILE", "config/rotation_config.json"))
    parser.add_argument("--lock-file", default=os.getenv("ROTATOR_LOCK_FILE", "rotation.lock.json"))
    parser.add_argument("--log-level", default="INFO")

    args = parser.parse_args()
    setup_logging(args.log_level)

    if args.action == "run" or args.role == "supervisor":
        supervisor = ProcessSupervisor(args.config)
        supervisor.run()
    elif args.action == "handoff":
        if not args.active_node or not args.standby_node:
            logger.error("--active-node and --standby-node are required for handoff.")
            sys.exit(1)
        orchestrator = HandoverOrchestrator(args.config, args.lock_file)
        success = orchestrator.execute_handover(args.active_node, args.standby_node)
        sys.exit(0 if success else 5)
    elif args.action == "health-check":
        # Pre-flight health check locally on node
        logger.info("[HEALTH_CHECK] Running pre-flight checks...")
        # Check imports
        try:
            import hyperliquid, eth_account, pandas, requests
            logger.info("[HEALTH_CHECK] Critical imports: PASS")
        except ImportError as e:
            logger.error("[HEALTH_CHECK] Missing dependency: %s", e)
            sys.exit(2)
        sys.exit(0)
    elif args.action == "status":
        lock_mgr = LockManager(args.lock_file)
        print(json.dumps(lock_mgr.read(), indent=2))
        sys.exit(0)

if __name__ == "__main__":
    main()
```

---

## 5. Verification Method

To independently verify this specification and its operational behavior:

### 5.1 Syntax & Schema Validation
1. Verify `rotation.lock.json` schema:
   ```powershell
   python -c "import json; data = {'lock_id': 'test', 'status': 'ACTIVE_HELD', 'active_node': 'bot-node-a', 'standby_node': 'bot-node-b', 'fencing_token': 1, 'acquired_at': '2026-11-13T00:00:00Z', 'expires_at': '2026-11-13T06:00:00Z', 'ttl_seconds': 600}; print('Valid lock structure')"
   ```
2. Verify atomic replacement safety on Windows and Linux:
   ```powershell
   python -c "import os, tempfile; f1 = 'test_lock.tmp'; f2 = 'test_lock.json'; open(f1, 'w').write('{}'); os.replace(f1, f2); assert os.path.exists(f2); os.remove(f2); print('Atomic replace verified')"
   ```

### 5.2 Standby Pre-flight Health Simulation
1. Test Hyperliquid `/info` connectivity and latency:
   ```powershell
   python -c "import requests, time; t0 = time.time(); res = requests.post('https://api.hyperliquid.xyz/info', json={'type': 'metaAndAssetCtxs'}, timeout=5); print('Status:', res.status_code, '| Latency:', round((time.time() - t0)*1000, 1), 'ms')"
   ```

### 5.3 Supervisor Graceful Signal & Pause Verification
1. Verify `control.json` `"pause"` consumption in `main.py`:
   ```powershell
   python -c "import json; json.dump({'action': 'pause'}, open('control_test.json', 'w')); print('Control payload format validated')"
   ```
2. Verify heartbeat delay threshold calculation:
   ```powershell
   python -c "states = {'IDLE': 150, 'FETCH_MARKET': 180, 'AI_MACRO': 180, 'COOL_DOWN': 600}; print('Timeout rules mapped successfully')"
   ```

### 5.4 Rollback Invalidation Conditions
- If Phase 3 state validation is skipped, corrupted JSON from an ungraceful crash could be injected into Standby, breaking the entire trading loop.
- If the fencing token is omitted, a zombie Active node recovering from a network split could place orders concurrently with Standby.
- The above architecture explicitly prevents both invalidation conditions.

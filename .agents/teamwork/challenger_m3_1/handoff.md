# Milestone 3 Challenger Report: Adversarial Empirical Verification

**Document**: Challenger 1 Handoff Report for Milestone 3 (Free Cloud Server 24/7 Rotation Scheme)  
**Author**: Challenger M3-1 (`challenger_m3_1`)  
**Target Codebase**: `scripts/rotator.py`, `scripts/deploy_render.py`, `scripts/start_container.sh`, `scripts/deploy_sandbox.sh`, `Dockerfile`, `render.yaml`, `config/rotation_config.json`  
**Test Harness**: `tests/test_rotator_adversarial.py` (12 test cases)  
**Date**: 2026-11-13  
**Verdict**: **`REQUEST_CHANGES`** (2 Critical/High Defects Discovered)  

---

## 1. Observation

Adversarial empirical testing was conducted across all 4 mandatory focus areas using an independent, real-process test suite `tests/test_rotator_adversarial.py` (12 test cases) and direct CLI invocation.

### 1.1 LockManager Concurrency Race Condition (Critical Defect)
- **Target File**: `scripts/rotator.py:95-138` (`LockManager.acquire`)
- **Code Quote**:
  ```python
  95:   now = datetime.now(timezone.utc)
  96:   now_ts = now.timestamp()
  97:   current = self.read()
  98:   fencing_token = 1
  ...
  115:  payload = { ... }
  131:  self._write_atomic(payload)
  133:  return True, fencing_token, "Lock acquired successfully"
  ```
- **Test Executed**: `tests/test_rotator_adversarial.py:TestLockManagerConcurrencyAdversarial.test_concurrent_lock_acquisition_processes`
  - Spawns 8 independent Python processes synchronized via a strict microsecond timestamp barrier (`time.time() < barrier_ts`).
- **Verbatim Error & Tool Output**:
  ```
  FAIL: test_concurrent_lock_acquisition_processes
  AssertionError: 3 != 1 : Expected exactly 1 winner, but got 3: [
    {'worker_id': '0', 'success': True, 'token': 1, 'msg': 'Lock acquired successfully'},
    {'worker_id': '4', 'success': True, 'token': 1, 'msg': 'Lock acquired successfully'},
    {'worker_id': '6', 'success': True, 'token': 1, 'msg': 'Lock acquired successfully'}
  ]
  [LOCK] Failed to write lock file: [WinError 5] 拒绝访问。: '...rotation.lock.tmp.6212' -> '...rotation.lock.json'
  ```
- **Finding**: A non-atomic Check-Then-Act (TOCTOU) race condition exists in `LockManager.acquire()`. Multiple candidate processes read `current = None` simultaneously before any write finishes, construct identical payloads with `fencing_token: 1`, call `_write_atomic()`, and all return `True`. Furthermore, concurrent `os.replace` calls on Windows trigger unhandled `[WinError 5] Access Denied`.

### 1.2 Fencing Token Reset on Forced Preemption (High Severity Defect)
- **Target File**: `scripts/rotator.py:100-114` (`LockManager.acquire`)
- **Code Quote**:
  ```python
  98:   fencing_token = 1
  99:
  100:  if current and not force:
  101:      status = current.get("status", "IDLE")
  102:      expires_at_ts = float(current.get("expires_at_ts", 0))
  103:      fencing_token = int(current.get("fencing_token", 0)) + 1
  ...
  115:  payload = { ..., "fencing_token": fencing_token, ... }
  ```
- **Test Executed**:
  1. Unit Test: `tests/test_rotator_adversarial.py:TestStaleLockEvictionAdversarial.test_force_preemption_on_active_lock`
  2. Live CLI Verification:
     ```powershell
     python scripts/rotator.py --action status
     # Shows current fencing_token: 12
     python scripts/rotator.py --action handoff --dry-run --force
     # Verbatim Log:
     # 2026-11-13 11:37:56 [INFO] [rotator] [LOCK] Pessimistic lock acquired. Fencing token: 1, TTL: 600s
     python scripts/rotator.py --action status
     # Shows current fencing_token: 1
     ```
- **Verbatim Error**:
  ```
  FAIL: test_force_preemption_on_active_lock
  AssertionError: 1 != 21
  ```
- **Finding**: When `force=True` (or `--force` CLI parameter) is passed to pre-empt an existing lock, the condition `if current and not force:` evaluates to `False`. The code skips `fencing_token = int(current.get("fencing_token", 0)) + 1`, resetting `fencing_token` to default `1`. This breaks the distributed system monotonic fencing token invariant and exposes the cluster to zombie node write hazards.

### 1.3 Verified Passing Behaviors
The remaining 10 adversarial test cases in `tests/test_rotator_adversarial.py` passed with 100% compliance:
1. **Sequential Fencing Monotonicity** (`test_fencing_tokens_strictly_increment`): PASSED. Monotonically increments `1 -> 2 -> 3 -> 4 -> 5` under serial execution.
2. **Stale Lock Eviction** (`test_stale_expired_lock_evicted_and_reacquired`): PASSED. Detects lease expiration timestamp (`now_ts >= expires_at_ts`), breaks stale lock, and increments fencing token.
3. **Unexpired Lock Contention** (`test_active_unexpired_lock_blocks_candidate`): PASSED. Unexpired active lock safely rejects contenders with warning and leaves lock untouched.
4. **Phase 2 Health Check Failure Abort** (`test_phase_2_health_check_failure_aborts_active_intact`): PASSED. If standby fails pre-flight check, handover immediately aborts, returning `False`; Active node is never signaled or stopped.
5. **Phase 3 State Sync Corruption (Bad JSON)** (`test_phase_3_state_sync_corruption_bad_json_triggers_rollback`): PASSED. Corrupted JSON syntax in `state.json` is rejected by `StateSynchronizer.validate_file`, Phase 3 aborts, `_rollback_phase_3` resumes Active node trading loop, and lock status is marked `ROLLBACK`.
6. **Phase 3 Missing Mandatory Schema Keys** (`test_phase_3_state_sync_corruption_missing_schema_keys`): PASSED. Missing `daily_start_equity` or 0-byte file is rejected before push.
7. **Phase 4 Standby Promotion Failure Rollback** (`test_phase_4_standby_promotion_failure_rollback`): PASSED. Standby spawn failure triggers `_rollback_phase_4_5`: standby terminated (`pkill -9`), standby machine stopped, active machine started, supervisor relaunched, and lock marked `ROLLBACK`.
8. **Phase 5 Verification Heartbeat Timeout Rollback** (`test_phase_5_standby_verification_timeout_rollback`): PASSED. If standby fails to produce a fresh heartbeat (<30s) within timeout, `_rollback_phase_4_5` terminates standby and resurrects active node.
9. **Zero-Naked-Risk Invariant** (`test_no_pause_written_to_control_json_during_handover`): PASSED. Handover never writes `"pause"` to `control.json` (avoiding `_enter_system_halt` / `close_all()`); uses graceful `pkill -SIGINT` / `pkill -SIGTERM`, and unlinks stale `control.json`.
10. **Position Continuity** (`test_position_continuity_across_handover`): PASSED. Transferred `state.json` retains `position` (`direction`, `entry_price`, `sz_notional`, `peak_price`, `initial_risk_r`, `bars_held`) with 100% precision.

---

## 2. Logic Chain

1. **[Observation 1.1: `LockManager.acquire` TOCTOU Race]**  
   $\rightarrow$ When multiple processes (e.g. concurrent cron runs or dual orchestrator instances) call `acquire()`, each performs an uncoordinated `read()` followed by an independent `_write_atomic()`.  
   $\rightarrow$ Both processes read the state before either writes, see that no active handover is in progress, and proceed to overwrite the lock file.  
   $\rightarrow$ Both processes return `True` with `fencing_token: 1`.  
   $\rightarrow$ Both processes promote their respective standby nodes, resulting in **split-brain execution** where two bots trade the same Hyperliquid account simultaneously.  
   $\rightarrow$ **Conclusion**: Mutual exclusion during lock acquisition is not guaranteed.

2. **[Observation 1.2: Fencing Token Reset on `force=True`]**  
   $\rightarrow$ Monotonic fencing tokens are designed to protect against split-brain by ensuring that higher-numbered tokens strictly supersede lower-numbered tokens.  
   $\rightarrow$ `LockManager.acquire` places the token increment `fencing_token = int(current.get("fencing_token", 0)) + 1` inside `if current and not force:`.  
   $\rightarrow$ When an operator or automation passes `force=True` (or `--force`), `not force` evaluates to `False`.  
   $\rightarrow$ The increment logic is skipped, leaving `fencing_token = 1`.  
   $\rightarrow$ A forced takeover from a crashed node with token 12 resets the token back to 1.  
   $\rightarrow$ If the old node revives, it holds token 12, which appears higher than the new active node's token 1.  
   $\rightarrow$ **Conclusion**: The fencing token invariant is completely broken during forced preemption.

3. **[Observations 1.3: Rollback & Zero-Naked-Risk Invariants are sound]**  
   $\rightarrow$ Health checks, corrupted state validations, promotion failures, and verification timeouts all trigger correct automated rollbacks.  
   $\rightarrow$ `control.json pause` is completely bypassed in favor of graceful OS signals, preserving on-chain TP/SL trigger orders and position continuity.  
   $\rightarrow$ **Conclusion**: The core 6-phase state machine architecture is sound, but blocked by the two LockManager defects above.

---

## 3. Caveats

1. **Platform-Specific File Replacement**:
   - On Windows, `os.replace` can throw `[WinError 5] Access Denied` if another process currently has an open handle to the destination file. On POSIX systems, `rename` is atomic and replaces directory entries without handle conflicts. A cross-platform mutex pattern is needed.
2. **Read-Only Scope**:
   - Per Challenger constraints, implementation code in `scripts/rotator.py` was strictly left unmodified. Remediation diffs are provided below for Worker M3.

---

## 4. Conclusion & Required Changes

**Verdict**: **`REQUEST_CHANGES`**

Milestone 3 cannot be approved until Worker M3 resolves the two defects in `scripts/rotator.py`:

### Actionable Remediation for Worker M3:

#### 1. Fix Fencing Token Monotonicity on `force=True` (`scripts/rotator.py:95-115`)
Move token increment outside `and not force`:
```python
now = datetime.now(timezone.utc)
now_ts = now.timestamp()
current = self.read()
fencing_token = 1

# Monotonically increment fencing token regardless of force flag
if current:
    fencing_token = int(current.get("fencing_token", 0)) + 1

if current and not force:
    status = current.get("status", "IDLE")
    expires_at_ts = float(current.get("expires_at_ts", 0))
    cur_owner = current.get("owner", "unknown")

    if status == "HANDOVER_IN_PROGRESS":
        if now_ts < expires_at_ts:
            msg = f"Handover lock actively held by {cur_owner} until {datetime.fromtimestamp(expires_at_ts, tz=timezone.utc).isoformat()}"
            logger.warning("[LOCK] Contention: %s", msg)
            return False, current.get("fencing_token", 0), msg
        else:
            logger.warning("[LOCK] Stale handover lock detected (expired at %s). Breaking lock.",
                           datetime.fromtimestamp(expires_at_ts, tz=timezone.utc).isoformat())
```

#### 2. Enforce Atomic Mutual Exclusion & WinError 5 Handling in `LockManager.acquire`
Use an atomic filesystem mutex directory (`(self.lock_path.parent / (self.lock_path.name + ".mutex")).mkdir()`) to serialize concurrent `acquire()` calls across processes and platforms:
```python
mutex_dir = self.lock_path.parent / (self.lock_path.name + ".mutex")
try:
    mutex_dir.mkdir()
except (FileExistsError, OSError):
    cur_contention = self.read()
    return False, cur_contention.get("fencing_token", 0) if cur_contention else 0, "Concurrent lock acquisition contention"

try:
    # Re-read under mutex protection
    cur_mutex = self.read()
    if cur_mutex and not force:
        status_m = cur_mutex.get("status", "IDLE")
        exp_m = float(cur_mutex.get("expires_at_ts", 0))
        if status_m == "HANDOVER_IN_PROGRESS" and now_ts < exp_m:
            return False, cur_mutex.get("fencing_token", 0), f"Handover lock actively held by {cur_mutex.get('owner')}"

    if cur_mutex:
        fencing_token = int(cur_mutex.get("fencing_token", 0)) + 1
    payload["fencing_token"] = fencing_token

    self._write_atomic(payload)
    logger.info("[LOCK] Pessimistic lock acquired. Fencing token: %d, TTL: %ds", fencing_token, self.ttl)
    return True, fencing_token, "Lock acquired successfully"
except Exception as e:
    msg = f"Failed to write lock file: {e}"
    logger.error("[LOCK] %s", msg)
    return False, 0, msg
finally:
    try:
        mutex_dir.rmdir()
    except Exception:
        pass
```
Also ensure `LockManager.release()` cleans up the `.mutex` directory if orphaned, and wrap `_write_atomic` with a 3-attempt retry loop for `PermissionError` on Windows.

---

## 5. Verification Method

To independently verify these findings:

1. **Run Adversarial Suite**:
   ```powershell
   python -m unittest -v tests/test_rotator_adversarial.py
   ```
   *Expected Current Output*: 10 tests PASS, 2 tests FAIL (`test_concurrent_lock_acquisition_processes` and `test_force_preemption_on_active_lock`).

2. **Verify Forced Preemption Token Reset via CLI**:
   ```powershell
   python scripts/rotator.py --action status
   python scripts/rotator.py --action handoff --dry-run --force
   python scripts/rotator.py --action status
   ```
   *Observation*: Fencing token resets to 1 instead of incrementing.

3. **Invalidation Condition**:
   Once Worker M3 applies the two fixes, running `python -m unittest -v tests/test_rotator_adversarial.py` must result in:
   ```
   Ran 12 tests in ...s
   OK
   ```

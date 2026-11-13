# Milestone 3 Review & Adversarial Stress-Test Report

**Reviewer**: Reviewer 1 (`reviewer_m3_1`)  
**Roles**: Reviewer, Adversarial Critic  
**Review Target**: Milestone 3 Deliverables (`scripts/rotator.py`, `scripts/deploy_render.py`, configs, tests)  
**Date**: 2026-11-13  
**Verdict**: **REQUEST_CHANGES**

---

## 1. Observation

### 1.1 Invariant & 6-Phase Zero-Naked-Risk Handover Protocol (`scripts/rotator.py`)
1. **Critical Liquidation Avoidance Invariant**:
   - `main.py:340-341`:
     ```python
     elif action == "pause" and self.state != STATE_HALT:
         self._enter_system_halt("收到看板手动暂停指令")
     ```
   - `main.py:1558-1565`:
     ```python
     def _enter_system_halt(self, reason: str):
         logger.critical("[挂起] SYSTEM_HALT | reason=%s", reason)
         send_alert(f"SYSTEM_HALT: {reason}，正在强制市价平仓并清空挂单！")
         try:
             szi, _ = self.exchange.current_position()
             if abs(szi) > 0:
                 resp = self.exchange.close_all()
     ```
   - Direct inspection of `scripts/rotator.py:776-789`:
     ```python
     # Step 1: Quiesce Active Process via graceful SIGTERM / SIGINT
     logger.info("[Phase 3] Sending graceful termination signal to Active bot process...")
     logger.info("[Phase 3] NOTICE: Refraining from control.json 'pause' to preserve open positions on Hyperliquid.")
     if not self.dry_run:
         client_active.exec_cmd(active_node, "pkill -SIGINT -f main.py || true")
         time.sleep(3)
         res = client_active.exec_cmd(active_node, "pgrep -f main.py")
         if res.returncode == 0:
             client_active.exec_cmd(active_node, "pkill -SIGTERM -f main.py || true")
     ```
   - Verbatim Observation: `rotator.py` correctly avoids writing `"pause"` to `control.json`, using POSIX signals `SIGINT`/`SIGTERM` instead. `main.py:1990-1995` catches `KeyboardInterrupt` and terminates cleanly without triggering `close_all()`.

2. **LockManager & RollbackEngine**:
   - `scripts/rotator.py:68-200`: `LockManager` utilizes monotonic fencing tokens, lease TTL (600s), stale lock expiration (`now_ts >= expires_at_ts`), and atomic file replacement (`.tmp.{pid}` $\rightarrow$ `flush()` $\rightarrow$ `fsync()` $\rightarrow$ `os.replace`).
   - `scripts/rotator.py:891-919`: `_rollback_phase_3` resumes the Active node on sync failure; `_rollback_phase_4_5` forcibly kills the faulty standby (`pkill -9`), stops its machine, restarts the Active machine, and resumes supervision.

### 1.2 Major Finding 1: Unimplemented Max Drawdown Circuit Breaker in `ProcessSupervisor`
- `scripts/rotator.py:928`:
  ```python
  - Enforces max drawdown (30%) and daily loss (15%) circuit breakers
  ```
- `scripts/rotator.py:951-952`:
  ```python
  "max_drawdown_limit_pct": 30.0,
  "daily_loss_limit_pct": 15.0
  ```
- `scripts/rotator.py:1084-1095`:
  ```python
  # Circuit Breakers (Daily loss & max drawdown)
  peak_eq = float(hb.get("peak_equity", 0))
  daily_loss = float(hb.get("daily_loss_pnl", 0))
  if peak_eq > 0:
      daily_loss_pct = (abs(daily_loss) / peak_eq) * 100.0 if daily_loss < 0 else 0
      limit_dl = float(self.sup_cfg.get("daily_loss_limit_pct", 15.0))
      if daily_loss_pct >= limit_dl:
          logger.critical("[SUPERVISOR] CIRCUIT BREAKER: Daily loss %.2f%% exceeded limit %.2f%%! Stopping bot permanently.",
                          daily_loss_pct, limit_dl)
          self._terminate_proc(proc)
          return
  ```
- **Verbatim Finding**: While daily loss is evaluated, `max_drawdown_limit_pct` is **never referenced or calculated** anywhere in the evaluation loop (lines 1084-1095). Peak-to-trough drawdown from `peak_equity` or `risk_state_aggressive.json` is completely ignored at runtime.

### 1.3 Major Finding 2: Dead Secret Masking & Plaintext Credential Leakage in `deploy_render.py`
- `scripts/deploy_render.py:44-47`:
  ```python
  def mask_secret(secret: str) -> str:
      if len(secret) <= 8:
          return "********"
      return f"{secret[:4]}...{secret[-4:]}"
  ```
- Code search across `scripts/deploy_render.py` for `mask_secret`:
  ```
  scripts\deploy_render.py:44:def mask_secret(secret: str) -> str:
  (0 other occurrences)
  ```
- Verbatim Execution Observation from `python scripts/deploy_render.py --action create --dry-run`:
  ```
  2026-11-13 11:32:05 [INFO] [render] [DRY_RUN] POST https://api.render.com/v1/services -> Data: {'type': 'web_service', ..., 'envVars': [{'key': 'HYPE_USER_ADDRESS', 'value': '0x0a657bdF77cc466B833179bfeEadF2cb613c66de'}, {'key': 'HYPE_SECRET_KEY', 'value': '0x5a865b212049df8245d270282f597e2cd05ae0d5f4fe4341461d92a10341fe0b'}, {'key': 'THIRD_PARTY_API_KEY', 'value': 'sk-UgCRqbmRp57NVU3d0S6Nvm3q3Igfr4DdSwsKtcgAWJ5F0o9n'}, {'key': 'SMTP_PASSWORD', 'value': 'wdyefibmdsvthddh'}, ...]}
  ```
- **Verbatim Finding**: `mask_secret()` is completely unused (dead code). Running `--dry-run` or printing service creation payloads dumps raw, unmasked private keys (`HYPE_SECRET_KEY`), API tokens (`THIRD_PARTY_API_KEY`), and mail credentials (`SMTP_PASSWORD`) directly into stdout/stderr logs. In shared CI/CD pipelines (e.g. GitHub Actions), this will expose production credentials in execution logs.

### 1.4 Command Executions & Test Results
1. `python scripts/rotator.py --action health-check`: Exit code `0`. HTTP 200 OK to Hyperliquid `/info` (latency 1715.7ms), disk I/O passed, credential formats passed, healthy=True.
2. `python scripts/rotator.py --action status`: Exit code `0`. Correctly loaded active lease lock `lock-1794540510-6128`.
3. `python scripts/rotator.py --action handoff --dry-run`: Exit code `0`. Successfully simulated all 6 phases.
4. `python scripts/deploy_render.py --action list`: Exit code `0`. Successfully queried Render REST API v1 for owner `tea-dbr81smtlv9c73fo1uk0`.
5. `python scripts/deploy_render.py --action owners`: Exit code `0`. Confirmed owner ID `tea-dbr81smtlv9c73fo1uk0` ("塔菲's workspace").
6. `python -m unittest tests/e2e/test_tier1_r3_rotation.py`: Exit code `0`. Ran 7 tests in 9.232s, all passed (`OK`).

---

## 2. Logic Chain

1. **[Observation 1.1: `control.json pause` invokes `close_all()` in `main.py`]**  
   $\rightarrow$ `scripts/rotator.py` correctly avoids writing `pause` to `control.json` during handover.  
   $\rightarrow$ Graceful POSIX termination (`pkill -SIGINT` / `SIGTERM`) causes `main.py` to exit cleanly, leaving active Hyperliquid positions and on-chain stop-loss trigger orders live.  
   $\rightarrow$ The Zero-Naked-Risk invariant is upheld.

2. **[Observation 1.2: `max_drawdown_limit_pct` defined in config but missing in loop]**  
   $\rightarrow$ If the bot incurs a cumulative multi-day drawdown (e.g. 35% drawdown where daily loss is 8% per day), `daily_loss_limit_pct` will not trigger.  
   $\rightarrow$ Because `max_drawdown_limit_pct` is not checked in lines 1084-1095, the supervisor will continue running the bot despite exceeding the maximum drawdown threshold.  
   $\rightarrow$ This represents an incomplete implementation of the advertised circuit breaker contract.

3. **[Observation 1.3: `mask_secret` dead code in `deploy_render.py`]**  
   $\rightarrow$ The review scope explicitly mandates secret masking in `deploy_render.py`.  
   $\rightarrow$ Dry-run execution prints raw exchange private keys and SMTP passwords in plain text.  
   $\rightarrow$ This poses an immediate credential leakage vulnerability in logging environments.

4. **[Synthesis of Observations 1.1, 1.2, 1.3, and 1.4]**  
   $\rightarrow$ While the core 6-phase protocol, lock manager, rollback engine, and REST API client architecture are solid and functional, the missing drawdown evaluation and the credential leakage in `deploy_render.py` are substantive defects requiring remediation before milestone sign-off.

---

## 3. Caveats

1. **Hardware / Real Cloud Sandboxes**:
   - Verification of `instabox start/stop/exec` commands was validated via dry-run simulation. Real cloud execution requires active `INSTABOX_API_KEY_A` and `INSTABOX_API_KEY_B` cloud tokens.
2. **Local Windows `taskkill` Behavior**:
   - In `scripts/rotator.py:1167`, `--action quiesce` executes `taskkill /F /IM python.exe /T` on Windows. This terminates all Python processes on the machine, which is acceptable for dedicated sandbox instances but should be used with caution on multi-purpose developer machines.

---

## 4. Conclusion

**Verdict: REQUEST_CHANGES**

No integrity violations were found (all implementations, network queries, and tests are genuine and functional). However, two Major findings must be resolved:

1. **[Major - Security] Implement Secret Masking in `scripts/deploy_render.py`**:
   - Utilize `mask_secret()` when logging or returning dry-run request payloads.
   - Sanitize all environment variables whose key contains `SECRET`, `KEY`, `PASSWORD`, or `TOKEN` so that console stdout and log files never display raw credentials.
2. **[Major - Risk Engine] Implement Max Drawdown Check in `ProcessSupervisor` (`scripts/rotator.py`)**:
   - In `ProcessSupervisor.run()`, compute current drawdown (e.g., from `.heartbeat` or by reading `risk_state_aggressive.json` / `metrics.current_drawdown_pct` or `(peak_eq - current_equity) / peak_eq * 100.0`).
   - Enforce the `max_drawdown_limit_pct` (30.0%) circuit breaker in addition to `daily_loss_limit_pct`.

---

## 5. Verification Method

To verify the requested changes once implemented:

1. **Verify Secret Masking in `deploy_render.py`**:
   ```powershell
   python scripts/deploy_render.py --action create --repo https://github.com/example/trading-bot --dry-run
   ```
   *Expected Outcome*: Confirm that `HYPE_SECRET_KEY`, `THIRD_PARTY_API_KEY`, and `SMTP_PASSWORD` appear as masked strings (e.g. `0x5a...fe0b`, `sk-U...o9n`, `wdye...hddh` or `********`), with ZERO plaintext private keys in stdout or logs.

2. **Verify Drawdown Circuit Breaker in `rotator.py`**:
   - Inspect `scripts/rotator.py` around line 1084 to confirm that `max_drawdown_limit_pct` is compared against current drawdown percentage.
   - Run a unit test or simulation with mock heartbeat showing drawdown > 30% and verify that `ProcessSupervisor` triggers termination with log `CIRCUIT BREAKER: Max drawdown ...% exceeded limit ...%!`.

3. **Run Regression Tests**:
   ```powershell
   python -m unittest tests/e2e/test_tier1_r3_rotation.py
   python scripts/rotator.py --action health-check
   python scripts/deploy_render.py --action list
   ```
   *Expected Outcome*: All commands exit with code `0`.

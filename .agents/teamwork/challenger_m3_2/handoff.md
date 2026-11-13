# Milestone 3 Challenger Adversarial Verification Report

**Reviewer**: Challenger 2 (`challenger_m3_2`)  
**Target Milestone**: M3 (Free Cloud Server 24/7 Rotation Scheme)  
**Target Deliverables Evaluated**:
- `scripts/rotator.py`
- `scripts/deploy_render.py`
- `scripts/deploy_sandbox.sh`
- `Dockerfile`
- `render.yaml`
- `.github/workflows/rotation.yml`
- `DEPLOYMENT_24_7.md`
**Verdict**: **`REQUEST_CHANGES`** (Critical Security Failure & Circuit Breaker Logic Omission)  
**Date**: 2026-11-13  

---

## 1. Observation

### 1.1 `scripts/deploy_render.py`: Critical Plain-Text Secret Leakage
- **File**: `scripts/deploy_render.py:44-47`:
  ```python
  def mask_secret(secret: str) -> str:
      if len(secret) <= 8:
          return "********"
      return f"{secret[:4]}...{secret[-4:]}"
  ```
  `mask_secret` is declared on line 44, but a static grep across the entire repository confirms it is **never called anywhere** in `deploy_render.py`:
  `Select-String -Path "scripts\deploy_render.py" -Pattern "mask_secret"` returns only line 44.
- **File**: `scripts/deploy_render.py:77`:
  ```python
  if self.dry_run and method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
      logger.info("[DRY_RUN] %s %s -> Data: %s", method.upper(), url, data)
      return 200, {"dry_run": True, "method": method, "endpoint": endpoint, "payload": data}
  ```
- **File**: `scripts/deploy_render.py:346`:
  ```python
  if success:
      logger.info("[CLI] Service created successfully!")
      print(json.dumps(resp, indent=2))
  ```
- **Empirical Execution**:
  Command executed:
  ```powershell
  python scripts/deploy_render.py --action create --repo https://github.com/example/trading-bot --dry-run
  ```
- **Verbatim Console Output**:
  ```text
  2026-11-13 11:35:58 [INFO] [render] [DRY_RUN] POST https://api.render.com/v1/services -> Data: {'type': 'web_service', ..., 'envVars': [{'key': 'HYPE_USER_ADDRESS', 'value': '0x0a657bdF77cc466B833179bfeEadF2cb613c66de'}, {'key': 'HYPE_SECRET_KEY', 'value': '0x5a865b212049df8245d270282f597e2cd05ae0d5f4fe4341461d92a10341fe0b'}, {'key': 'THIRD_PARTY_API_KEY', 'value': 'sk-UgCRqbmRp57NVU3d0S6Nvm3q3Igfr4DdSwsKtcgAWJ5F0o9n'}, {'key': 'SMTP_PASSWORD', 'value': 'wdyefibmdsvthddh'}]}}
  ...
  {
    "dry_run": true,
    "payload": {
      "serviceDetails": {
        "envVars": [
          { "key": "HYPE_SECRET_KEY", "value": "0x5a865b212049df8245d270282f597e2cd05ae0d5f4fe4341461d92a10341fe0b" },
          { "key": "THIRD_PARTY_API_KEY", "value": "sk-UgCRqbmRp57NVU3d0S6Nvm3q3Igfr4DdSwsKtcgAWJ5F0o9n" },
          { "key": "SMTP_PASSWORD", "value": "wdyefibmdsvthddh" }
        ]
      }
    }
  }
  ```
- **Finding**: Running the command recommended by Worker M3's handoff report (section 5.6) dumps all raw private keys, LLM API keys, and email passwords in plain text to the console, violating secret protection rules and CI/CD security.

---

### 1.2 `scripts/rotator.py`: Missing Max Drawdown Circuit Breaker & Unused Config
- **File**: `scripts/rotator.py:951`:
  ```python
  self.sup_cfg = self.config.get("supervisor", {
      ...
      "max_drawdown_limit_pct": 30.0,
      "daily_loss_limit_pct": 15.0
  })
  ```
- **File**: `scripts/rotator.py:1084-1095`:
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
- **Static & Empirical Verification**:
  - `Select-String -Path "scripts\rotator.py" -Pattern "max_drawdown"` matches **only** line 951 (`"max_drawdown_limit_pct": 30.0`). It is **never queried or calculated** anywhere in `ProcessSupervisor`.
  - When simulated with a severe cumulative drawdown of 40% (peak equity = 1000, current equity = 600) and `daily_loss_pnl = 0.0`, the circuit breaker check evaluated to `triggered = False`. The bot is not halted despite exceeding the 30% drawdown ceiling.
  - Furthermore, `ProcessSupervisor` checks **only** `.heartbeat`, which does not contain `current_equity` or `session_pnl`, meaning multi-day or session-level drawdown is completely invisible to the supervisor.

---

### 1.3 `scripts/rotator.py`: Infinite Respawn Loop on Repeated Hangs (Bypassed Max Restarts)
- **File**: `scripts/rotator.py:1040-1056` vs `1078-1082`:
  ```python
  # Normal exit: restart_count IS incremented
  retcode = proc.poll()
  if retcode is not None:
      ...
      restart_count += 1
      max_restarts = self.sup_cfg.get("max_restarts_per_hour", 5)
      if restart_count > max_restarts:
          logger.critical("[SUPERVISOR] Max restart limit (%d/hr) reached. Halting supervisor.", max_restarts)
          return
      backoff = self.sup_cfg.get("restart_backoff_base_seconds", 5) * restart_count
      time.sleep(backoff)
      break
  ```
  ```python
  # Hang exit: restart_count is NOT incremented!
  if delay > timeout:
      logger.critical("[SUPERVISOR] Hang detected (heartbeat delay=%.1fs > timeout=%.0fs). Terminating hung bot PID %d...",
                      delay, timeout, proc.pid)
      self._terminate_proc(proc)
      break
  ```
- **Empirical Execution (Live Test Harness)**:
  ```text
  2026-11-13 11:34:18 [INFO] [rotator] [SUPERVISOR] Spawning main.py (attempt 1)...
  2026-11-13 11:34:18 [INFO] [rotator] [SUPERVISOR] main.py running under PID 1896
  2026-11-13 11:34:48 [CRITICAL] [rotator] [SUPERVISOR] Hang detected (heartbeat delay=230.0s > timeout=5s). Terminating hung bot PID 1896...
  2026-11-13 11:34:48 [INFO] [rotator] [SUPERVISOR] Sending SIGTERM to bot PID 1896 (graceful exit window: 2s)...
  2026-11-13 11:34:48 [INFO] [rotator] [SUPERVISOR] Bot PID 1896 exited cleanly.
  2026-11-13 11:34:48 [INFO] [rotator] [SUPERVISOR] Spawning main.py (attempt 1)...
  2026-11-13 11:34:48 [INFO] [rotator] [SUPERVISOR] main.py running under PID 12764
  ```
- **Finding**: On hang termination, the loop breaks back to `while self.running:` without incrementing `restart_count`. The supervisor immediately respawns the bot labeled `(attempt 1)` with zero backoff delay. If a bot persistently hangs, the supervisor enters an infinite, unthrottled respawn loop, completely bypassing `max_restarts_per_hour`.

---

### 1.4 `scripts/rotator.py`: Unhandled Cold-Start Boot Hang
- **File**: `scripts/rotator.py:1010-1014` & `1062`:
  ```python
  # On spawn, heartbeat file is unlinked
  if self.heartbeat_path.exists():
      try:
          self.heartbeat_path.unlink()
      except Exception:
          pass
  ...
  # During monitoring
  if self.heartbeat_path.exists():
      # evaluate heartbeat freshness
  ```
- **Finding**: If `main.py` hangs during startup (e.g. frozen imports, network hang during exchange initialization before the first `_write_heartbeat()` call), `self.heartbeat_path.exists()` remains `False`. There is no `else` timeout comparing `time.time() - start_time` against a cold-start boot timeout. The supervisor sleeps indefinitely, unable to detect that the bot hung on boot.

---

### 1.5 `scripts/deploy_render.py`: HTTP 401 Unauthorized Exit Code Suppression
- **File**: `scripts/deploy_render.py:103-108`:
  ```python
  def list_services(self, owner_id: Optional[str] = None) -> List[Dict[str, Any]]:
      params = {"limit": 50}
      if owner_id:
          params["ownerId"] = owner_id
      status, data = self._request("GET", "services", params=params)
      return data if isinstance(data, list) else []
  ```
- **Empirical Execution**:
  ```powershell
  python scripts/deploy_render.py --api-key "invalid_key_rnd_12345" --action list
  ```
- **Output**:
  ```text
  2026-11-13 11:34:52 [ERROR] [render] [API_ERROR] GET https://api.render.com/v1/services?limit=50&ownerId=tea-dbr81smtlv9c73fo1uk0 returned HTTP 401: {'message': 'Unauthorized'}
  Found 0 service(s):
    (No services found in this workspace)
  PowerShell sees exit code: 0
  ```
- **Finding**: An unauthorized 401 response is coerced into an empty list `[]`, causing the CLI to report "Found 0 service(s)" and exit with success code `0`. This masks an invalid API key error in automation pipelines.

---

### 1.6 `scripts/deploy_sandbox.sh`: Mandatory Credential Validation Exit Code
- **Empirical Execution**:
  ```bash
  bash scripts/deploy_sandbox.sh --target-dir /tmp/empty_sandbox --no-venv --verify-only --check-env
  ```
- **Output**:
  ```text
  [CRITICAL] Mandatory trading environment variables are missing or invalid!
  PowerShell sees exit code: 5
  ```
- **Finding**: The script cleanly identifies missing credentials and exits with exit code `5` (`EXIT_ENV_VALIDATION_FAIL=5`, line 28). (Dispatch prompt inquired whether it exits with 3; exit code 3 is explicitly reserved for `EXIT_PIP_INSTALL_FAIL`, confirming code 5 is the intentional POSIX credential failure code).

---

## 2. Logic Chain

1. **[Observation 1.1: `deploy_render.py` unmasked secrets in logs & payload output]**  
   $\rightarrow$ The utility function `mask_secret()` was written but never integrated into `_request()` or `load_env_vars()`.  
   $\rightarrow$ Running `deploy_render.py --action create --dry-run` or inspecting stdout logs prints raw `HYPE_SECRET_KEY` and third-party API keys in clear text.  
   $\rightarrow$ In CI/CD pipelines (e.g. GitHub Actions), this compromises trading wallet private keys. This is an unacceptable security failure.

2. **[Observation 1.2: `ProcessSupervisor` never checks `max_drawdown_limit_pct`]**  
   $\rightarrow$ The configuration contains `"max_drawdown_limit_pct": 30.0`, and the comment claims `# Circuit Breakers (Daily loss & max drawdown)`.  
   $\rightarrow$ However, the code only computes `daily_loss_pct = (abs(daily_loss) / peak_eq) * 100.0`.  
   $\rightarrow$ If the trading bot loses 35% over multiple days, but the current calendar day has `daily_loss_pnl = 0`, the supervisor never triggers the emergency halt.

3. **[Observation 1.3: `restart_count` not incremented on hang break]**  
   $\rightarrow$ When a hang is terminated via `_terminate_proc(proc)`, the supervisor loop executes `break`.  
   $\rightarrow$ `restart_count` is only incremented inside the `proc.poll() is not None` block, not the `delay > timeout` block.  
   $\rightarrow$ As a result, `restart_count` remains 0, `backoff` is never applied, and `max_restarts_per_hour` (5/hr) is never triggered, creating an infinite CPU/process respawn loop on hung workers.

4. **[Observation 1.4: Startup boot hang ignored if `.heartbeat` does not exist]**  
   $\rightarrow$ When spawning `main.py`, `.heartbeat` is unlinked.  
   $\rightarrow$ The supervision loop checks `if self.heartbeat_path.exists():`.  
   $\rightarrow$ If the bot deadlocks during import or initialization, `.heartbeat` is never written. The supervisor skips the check and sleeps indefinitely.

---

## 3. Caveats

1. **Exchange API Calls**: Live trading orders were not submitted to the Hyperliquid production validator matching engine during testing to avoid capital risk. Network latency and info queries were verified via read-only `/info` endpoints.
2. **Dual-Account Instabox Cloud Execution**: Full multi-machine rotation across two separate Instabox sandboxes was simulated via CLI mocks and dry-run, as running physical cloud sandboxes requires active external subscription tokens.

---

## 4. Conclusion & Required Action

**Verdict: `REQUEST_CHANGES`**

The following fixes must be implemented by the worker before Milestone 3 can be approved:

1. **Fix Plaintext Secret Leakage in `scripts/deploy_render.py`**:
   - Apply `mask_secret()` to all sensitive values (`HYPE_SECRET_KEY`, `THIRD_PARTY_API_KEY`, `SMTP_PASSWORD`) in:
     - `_request()` dry-run log output (line 77)
     - `create` / `update-env` / `deploy` console output (line 346 & 350)
   - Ensure `--dry-run` output displays masked keys (e.g. `0x5a86...fe0b`).
2. **Implement Max Drawdown Circuit Breaker in `scripts/rotator.py`**:
   - In `ProcessSupervisor.run()`, check cumulative drawdown:
     - Read `risk_state_aggressive.json` or compute `(peak_eq - current_eq) / peak_eq * 100.0` from state/heartbeat.
     - Compare against `self.sup_cfg.get("max_drawdown_limit_pct", 30.0)` and trigger `_terminate_proc` if exceeded.
3. **Increment Restart Counter on Hang Escalation in `scripts/rotator.py`**:
   - When `delay > timeout`, increment `restart_count += 1`, evaluate `max_restarts_per_hour`, and apply backoff sleep before spawning a new instance.
4. **Add Boot-Hang Timeout in `scripts/rotator.py`**:
   - If `(time.time() - start_time) > sup_cfg.get("boot_timeout_seconds", 60)` and `not self.heartbeat_path.exists()`, treat as cold-start boot hang and terminate.
5. **Fix HTTP 401 Exit Code in `scripts/deploy_render.py`**:
   - If `status == 401` in `list_services()` or `list_owners()`, log an error and exit with code `1` rather than reporting 0 services and exiting with code `0`.

---

## 5. Verification Method

To independently verify these findings, run the following commands in `C:\Users\Administrator\Desktop\v1\a`:

### 5.1 Verify Plaintext Secret Leakage
```powershell
python scripts/deploy_render.py --action create --repo https://github.com/example/trading-bot --dry-run
```
*Expected finding*: Output contains unmasked `0x5a865b212049df8245d270282f597e2cd05ae0d5f4fe4341461d92a10341fe0b` in clear text.

### 5.2 Verify Missing Max Drawdown Check
```powershell
python -c "
from scripts.rotator import ProcessSupervisor
sup = ProcessSupervisor('config/rotation_config.json')
import inspect
src = inspect.getsource(sup.run)
assert 'max_drawdown_limit_pct' in src, 'max_drawdown_limit_pct is never checked in supervisor run loop!'
"
```
*Expected finding*: Raises `AssertionError: max_drawdown_limit_pct is never checked in supervisor run loop!`.

### 5.3 Verify Stale Heartbeat Escalation & Uncounted Restart
```powershell
python C:\Users\Administrator\.gemini\antigravity\scratch\challenger_m3_tests\test_live_supervisor.py
```
*Expected finding*: Hang detected after 30s grace period; process terminated via SIGTERM; immediate respawn as `(attempt 1)` without backoff.

# Handoff Report: Backend Server Architecture & API Endpoint Diagnosis (`dashboard.py`)

## 1. Observation

Direct examination of `C:\Users\Administrator\Desktop\v1\a\dashboard.py`, related bot modules (`main.py`, `risk_manager.py`, `watchdog.py`), state files (`state.json`, `risk_state_aggressive.json`, `equity_history.json`, `equity_history_risk_state_aggressive.json`, `.heartbeat`, `trading_bot.log`), and contract specifications (`PROJECT.md`, `SCOPE.md`, `ORIGINAL_REQUEST.md`) revealed the following concrete findings:

### 1.1 HTTP Server Architecture, Concurrency & Port Binding
- **Threading Model (`dashboard.py:2063`)**:
  ```python
  server = ThreadingHTTPServer(("0.0.0.0", args.port), CleanPanelHandler)
  server.serve_forever()
  ```
  `ThreadingHTTPServer` creates an unbounded new OS thread per incoming request. Because routes `/api/coins` (line 148: `timeout=5.0`) and `/api/state` (line 110: `timeout=4.0`) perform synchronous blocking HTTP calls to Hyperliquid when local caches expire, high request frequency (e.g. UptimeRobot monitoring + multi-tab polling) will spawn unbounded threads, leading to thread handle exhaustion and high memory footprint.
  Socket option `allow_reuse_address` is not set to `True`, which can cause `EADDRINUSE` errors on quick restarts. Socket thread mode `daemon_threads` is not set to `True`, which causes server shutdown to hang on stuck threads.

- **Port Resolution Ignores Cloud Environment (`dashboard.py:47, 2049`)**:
  ```python
  47: PORT = 8080
  ...
  2049: parser.add_argument("--port", type=int, default=PORT)
  ```
  Render Web Services and cloud containers assign dynamic ports via the `$PORT` environment variable (e.g. `PORT=10000`). `dashboard.py` defaults to `8080` without reading `os.getenv("PORT")`, guaranteeing port binding mismatches on Render deployments unless explicitly passed via CLI.

- **Port Collision Infinite Zombie Hang (`dashboard.py:2065-2071`)**:
  ```python
  except OSError as e:
      if "10048" in str(e) or "Address already in use" in str(e):
          print(f"[提示] 端口 {args.port} 已有监控看板在运行中。")
          while True:
              time.sleep(3600)
      else:
          raise
  ```
  When port 8080 is in use, `dashboard.py` enters an infinite sleep loop (`while True: time.sleep(3600)`). In containerized environments (Render, Docker, Kubernetes), this prevents the container from exiting with an error, causing health checks to hang indefinitely in a zombie state. Furthermore, background probe thread `t_net` (`_network_monitor_worker`) was already spawned on line 2062, running orphaned in the background.

### 1.2 Route Handlers & Interface Contract Violations
- **Missing `/api/history` Route (`dashboard.py:1971`)**:
  `PROJECT.md` line 94 and `SCOPE.md` line 8 specify route `/api/history`.
  `dashboard.py` only implements `/api/equity_history`:
  ```python
  1971: elif path == "/api/equity_history":
  1972:     interval = qs.get("interval", ["1h"])[0].lower()
  1973:     self._json(_make_equity_history_json(interval))
  ```
  Any client or contract test invoking `GET /api/history` receives HTTP 404: `{"error": "not found"}`.

- **Missing `POST /api/control` Route (`dashboard.py:1990-2041`)**:
  `PROJECT.md` line 94 and `SCOPE.md` lines 9 & 34 define `POST /api/control` for manual controls (`pause`, `resume`, `emergency_close`, `refresh_macro`).
  In `dashboard.py:1990-2041`, only three URL-path actions are implemented:
  - `POST /api/action/toggle_pause` (lines 1995-2017)
  - `POST /api/action/emergency_close` (lines 2018-2027)
  - `POST /api/action/refresh_macro` (lines 2028-2037)
  There is NO `/api/control` endpoint. If an automated script, test, or multi-instance rotator sends `POST /api/control` with JSON payload `{"action": "pause"}`, it receives HTTP 404: `{"error": "unknown action"}`.
  Furthermore, `do_POST` does NOT read `self.rfile` or parse the request body at all; it ignores `Content-Length` and connection draining.

- **Missing Health Check Endpoint**:
  Neither `/health` nor `/api/health` exists. A probe hitting `/` is forced to load the full 40KB HTML template, while hitting `/api/state` forces downstream Hyperliquid API cache checks. Cloud health checkers (Render, UptimeRobot) require a zero-overhead `/health` or `/api/health` endpoint returning `{"status": "ok"}`.

- **Missing CORS Headers and `do_OPTIONS` Preflight**:
  `CleanPanelHandler._json` (lines 1889-1896) sets `Content-Type` and `Cache-Control`, but omits:
  - `Access-Control-Allow-Origin: *`
  - `Access-Control-Allow-Methods: GET, POST, OPTIONS`
  - `Access-Control-Allow-Headers: Content-Type, Authorization`
  The class lacks `do_OPTIONS`, returning HTTP 501 on browser preflight requests.

- **Static Asset Serving & 404 Failure (`dashboard.py:1914-1930`)**:
  ```python
  1914: elif path.startswith("/static/"):
  1915:     rel_path = path.lstrip("/")
  1916:     static_file = os.path.join(HERE, rel_path)
  1917:     if os.path.exists(static_file) and os.path.isfile(static_file):
  ...
  1928:     else:
  1929:         self._json({"error": "file not found"}, 404)
  ```
  `C:\Users\Administrator\Desktop\v1\a\static` does not exist on disk (verified via PowerShell `Test-Path "static"` -> `False`). Consequently, `<script src="/static/echarts.min.js"></script>` on line 705 ALWAYS returns HTTP 404, throwing errors in DevTools and forcing slow fallback to external CDN `cdn.jsdelivr.net`.
  Additionally, `rel_path = path.lstrip("/")` without verifying `os.path.commonpath([HERE, static_file]) == HERE` creates a path traversal risk.

### 1.3 File I/O, Race Conditions & State Desynchronization
- **Direct Live File Writes During HTTP POST (`dashboard.py:2005-2006, 2012-2013, 2024-2025, 2034-2035`)**:
  ```python
  with open(CONTROL_FILE, "w", encoding="utf-8") as f:
      json.dump(control_payload, f)
  ...
  with open(STATE_PATH, "w", encoding="utf-8") as f:
      json.dump(current_state, f, indent=2, ensure_ascii=False)
  ```
  These operations write directly to live files without using a `.tmp` file and `os.replace`. Meanwhile, `main.py` writes `state.json` via `.tmp` + `os.replace` (lines 1747-1750) and reads/deletes `control.json` (lines 299-304). On Windows, non-atomic concurrent file access produces `PermissionError: [WinError 32] The process cannot access the file because it is being used by another process` or corrupts JSON with partial writes.

- **Equity History Desynchronization & Hardcoded Mock Overwrite (`dashboard.py:169-205, 268`)**:
  `dashboard.py:268` hardcodes:
  ```python
  hist_file = os.path.join(HERE, "equity_history.json")
  ```
  However, in `risk_manager.py` (lines 144-151), when `persistence_file` is `risk_state_aggressive.json`, the real history file is dynamically derived as `equity_history_risk_state_aggressive.json`.
  Disk inspection confirms:
  - `equity_history_risk_state_aggressive.json`: contains 17 real trades from November 6, 2026, ending at current equity $620.95 and max drawdown 37.84%.
  - `equity_history.json`: contains stale SUI trades from October 2026 ending at $858.67.
  - Furthermore, `dashboard.py:169-205` (`_sync_equity_history_from_logs`) hardcodes mock trading anchors from October 24-31, 2026 with default `real_equity=917.35`. When `now_ts - max(p.get("ts")) > 2 days`, line 280 triggers `_sync_equity_history_from_logs`, which OVERWRITES `equity_history.json` on disk with mock data.
  This causes the top equity card to show $620.95 while the equity curve chart displays $858.67 or $917.35.

- **Log File Memory Bloat on Every Poll (`dashboard.py:85`)**:
  ```python
  def _tail_log(path, n=80):
      ...
      with open(path, "r", encoding="utf-8", errors="replace") as f:
          return [ln.rstrip("\n") for ln in f.readlines()[-n:]]
  ```
  `f.readlines()` loads the entire file into RAM. `C:\Users\Administrator\Desktop\v1\log\trading_bot.log` is already ~200KB and continuously growing. In 24/7 operation over weeks, loading megabytes of log files every 3 seconds across polling requests will cause severe memory bloat and garbage collection pauses.

- **ZeroDivisionError & Corrupted Input Crash in `/api/risk` (`dashboard.py:1942, 1949, 1962`)**:
  ```python
  max_dd = round(((peak_eq - real_eq) / peak_eq) * 100.0, 2)
  ...
  rk_data["total_pnl_pct"] = round((real_eq - init_eq) / init_eq * 100.0, 2)
  ```
  If `peak_eq` or `init_eq` is 0 (or empty state file), the server throws an uncaught `ZeroDivisionError`, resulting in HTTP 500. Non-numeric query parameters (`?n=abc` in `/api/logs` or `?limit=xyz` in `/api/ai_events`) also throw uncaught `ValueError` resulting in HTTP 500.

### 1.4 Frontend-Backend Contract Discrepancy
- **Position Exposure Field Mismatch (`dashboard.py:1522-1524` vs `main.py:1730-1745`)**:
  In `state.json`, `main.py` stores positions as:
  ```json
  "coin": "ETH",
  "position": {
    "direction": "LONG",
    "entry_price": 1944.9,
    "sz_notional": 150.63,
    "filled_sz": 0.077,
    "sl_price": 1850.0,
    "tp_price": 2100.0
  }
  ```
  The dashboard frontend JavaScript (lines 1522-1524) evaluates:
  ```javascript
  if (pos && (pos.size > 0 || pos.sz > 0)) {
    posBadge.className = 'hero-tag ' + (pos.side === 'LONG' ? 'green' : 'red');
    posBadge.innerText = (pos.coin || 'ETH') + ' ' + (pos.side || 'LONG');
  ```
  Because `main.py` writes `direction` (not `side`), `filled_sz`/`sz_notional` (not `size`/`sz`), and stores `coin` at the root object (not inside `pos`), `pos.size > 0 || pos.sz > 0` is ALWAYS FALSE. The UI permanently displays "无持仓敞口" (No position) even during active trades!

- **AI Event Stream Filter Mismatch (`dashboard.py:396`)**:
  `_extract_recent_ai_events` strictly filters on:
  ```python
  if "[AI] AI_Trade" in ln:
  ```
  In `trading_bot.log`, `AI_Trade` is gated behind signal scores $\ge 68.5/70$, which is rare in ranging markets. Meanwhile, the bot actively writes `[宏观] AI_macro 返回 | 建议=... | reason=...` every 5 minutes. Because `AI_macro` is ignored by the parser, `ai_events` returns `[]` virtually 100% of the time, leaving the AI audit stream completely empty.

- **PEP 263 Encoding Declaration Violation (`dashboard.py:1-10`)**:
  `dashboard.py` places `# -*- coding: utf-8 -*-` on line 10. PEP 263 requires the encoding cookie on line 1 or line 2. On platforms where the system default encoding is not UTF-8, Python ignores line 10, risking encoding parse errors on non-ASCII Chinese strings.

---

## 2. Logic Chain

1. **Server Resilience & Cloud Readiness**:
   - `Observation 1.1`: `PORT` is hardcoded to 8080 and does not check `os.getenv("PORT")`. On port collision, it executes `while True: time.sleep(3600)`.
   - `Deduction`: Deploying on Render (which injects dynamic `$PORT`) will fail port detection. If another process occupies the port locally or in container restarts, the process freezes as an unkillable zombie rather than failing fast or recovering.
   - `Observation 1.1`: `ThreadingHTTPServer` spawns threads with no concurrency cap, and worker threads perform synchronous `httpx` network calls (timeout=4-5s).
   - `Deduction`: When Hyperliquid or the AI relay is slow or unreachable, concurrent polls will accumulate blocked threads, exhausting memory and OS thread limits.

2. **API Contract Integrity**:
   - `Observation 1.2`: `PROJECT.md` and `SCOPE.md` require `/api/history` and `POST /api/control`. `dashboard.py` only exposes `/api/equity_history` and `/api/action/*`.
   - `Deduction`: Automated testing frameworks, multi-instance rotator scripts (`scripts/rotator.py`), and standardized REST consumers will receive HTTP 404 errors.
   - `Observation 1.2`: `do_POST` never reads `self.rfile` and matches actions only via path strings.
   - `Deduction`: Standard POST requests sending `{"action": "pause"}` to `/api/control` cannot be handled. Connection sockets are not drained, causing HTTP/1.1 protocol errors on persistent connections.

3. **Data Integrity & File I/O Safety**:
   - `Observation 1.3`: `dashboard.py` writes to `control.json` and `state.json` using direct `open(..., 'w')`.
   - `Deduction`: Concurrently, `main.py` reads/removes `control.json` and atomically replaces `state.json`. On Windows, concurrent non-shared access triggers `WinError 32 PermissionError` or reads empty JSON chunks.
   - `Observation 1.3`: `dashboard.py` reads `equity_history.json` and overwrites it with hardcoded October mock data, while the trading bot writes real equity series to `equity_history_risk_state_aggressive.json`.
   - `Deduction`: The user sees conflicting data: the KPI card shows real equity $620.95, while the chart shows mock historical data $917.35 or $858.67.

4. **Frontend Position Display Failure**:
   - `Observation 1.4`: Frontend checks `pos.size`/`pos.sz` and `pos.side`. `main.py` writes `sz_notional`/`filled_sz` and `direction`.
   - `Deduction`: Active positions are never detected by the frontend script, causing the dashboard to permanently display "无持仓敞口".

---

## 3. Caveats

- **Network Probe Keys**: `dashboard.py:614` contains a fallback API key string for testing. In production, keys must strictly be sourced from `.env` or container environment variables.
- **Frontend HTML Overhaul**: While this report focuses on backend architecture and API endpoints, the backend `/` handler serves the embedded `_PAGE_HTML` string. The 5 tabs (Overview, AI & Signals, FSM & Risk, Analytics, Logs & System) require matching data structures from these API endpoints.
- **No other caveats**: All observations are backed by exact line numbers and verified locally on the codebase.

---

## 4. Conclusion & Concrete Remediation Plan

The backend implementation in `dashboard.py` requires hardening across four key areas:
1. **Server Architecture & Port Binding**: Support `$PORT` environment variable, add socket reuse (`allow_reuse_address = True`), set `daemon_threads = True`, eliminate infinite sleep on port conflict (fail fast or pick next port), and provide a dedicated lightweight `/health` and `/api/health` endpoint.
2. **REST API Contract Compliance**: Add `/api/history` (aliased to equity history), implement `POST /api/control` (reading JSON body with actions: `pause`, `resume`, `emergency_close`, `refresh_macro`), add CORS headers (`Access-Control-Allow-Origin: *`), and implement `do_OPTIONS` preflight handling.
3. **Atomic File Operations & Memory-Safe Tailing**: Use atomic write patterns (`.tmp` + `os.replace`) for `control.json` and `state.json`. Implement backward seek tailing in `_tail_log` (reading only the last 64KB instead of `f.readlines()`). Dynamically resolve the active equity history file (`equity_history_risk_state_aggressive.json` if aggressive mode is active) and eliminate mock data overwrite.
4. **Data Normalization**: In `/api/state`, normalize position fields (`size`, `sz`, `side`, `coin`) so the frontend accurately detects active positions. In `/api/ai_events`, parse both `AI_macro` and `AI_Trade` logs so the decision stream displays live data. Guard all float conversions and divisions against 0 and `None`.

### Recommended Code Remediation (Drop-In Snippets for Implementer)

#### A. Port Resolution & Server Initialization (`dashboard.py`)
```python
# --- At top of file (PEP 263) ---
# -*- coding: utf-8 -*-
__version__ = "1.0.0"

# --- In main() ---
port_env = os.getenv("PORT", "").strip()
default_port = int(port_env) if port_env.isdigit() else PORT
parser.add_argument("--port", type=int, default=default_port)

# --- In server instantiation ---
class ThreadingHTTPServerFixed(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

try:
    server = ThreadingHTTPServerFixed(("0.0.0.0", args.port), CleanPanelHandler)
    server.serve_forever()
except OSError as e:
    if "10048" in str(e) or "Address already in use" in str(e):
        print(f"[错误] 端口 {args.port} 已被占用，服务无法绑定。请通过 --port 或 PORT 环境变量指定可用端口。")
        sys.exit(1)
    raise
```

#### B. Unified Response Helper with CORS & OPTIONS (`CleanPanelHandler`)
```python
def _set_cors_headers(self):
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

def do_OPTIONS(self):
    self.send_response(204)
    self._set_cors_headers()
    self.end_headers()

def _json(self, data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
    self._set_cors_headers()
    self.end_headers()
    self.wfile.write(body)
```

#### C. Route Handlers for Contract Endpoints (`do_GET` & `do_POST`)
```python
# --- In do_GET ---
if path in ("/", "/index.html"):
    self._html(_PAGE_HTML)
elif path in ("/health", "/api/health", "/healthz"):
    self._json({"status": "ok", "timestamp": time.time()})
elif path == "/api/state":
    st_data = _load_json(STATE_PATH) or {"state": "UNKNOWN"}
    st_data["env"] = os.getenv("HYPE_ENV", "testnet").lower()
    st_data["account_equity"] = _fetch_real_account_equity()
    # Normalize position fields for frontend compatibility
    pos = st_data.get("position")
    if pos and isinstance(pos, dict):
        notional = float(pos.get("sz_notional") or 0.0)
        filled = float(pos.get("filled_sz") or 0.0)
        pos["size"] = filled if filled > 0 else notional
        pos["sz"] = pos["size"]
        pos["side"] = str(pos.get("direction", "LONG")).upper()
        if "coin" not in pos:
            pos["coin"] = st_data.get("coin", "ETH")
    self._json(st_data)
elif path in ("/api/history", "/api/equity_history"):
    interval = qs.get("interval", ["1h"])[0].lower()
    self._json(_make_equity_history_json(interval))

# --- In do_POST ---
def do_POST(self):
    parsed = urlparse(self.path)
    path = parsed.path
    length = int(self.headers.get("Content-Length", 0))
    raw_body = self.rfile.read(length) if length > 0 else b"{}"
    try:
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:
        body = {}

    try:
        if path == "/api/control":
            action = str(body.get("action", "")).lower()
            if not action:
                self._json({"error": "missing action in request body"}, 400)
                return
            return self._handle_control_action(action)
        elif path == "/api/action/toggle_pause":
            st = (_load_json(STATE_PATH) or {}).get("state", "IDLE")
            action = "resume" if st == "SYSTEM_HALT" else "pause"
            return self._handle_control_action(action)
        elif path == "/api/action/emergency_close":
            return self._handle_control_action("emergency_close")
        elif path == "/api/action/refresh_macro":
            return self._handle_control_action("refresh_macro")
        else:
            self._json({"error": "not found"}, 404)
    except Exception as e:
        self._json({"error": str(e)}, 500)

def _handle_control_action(self, action: str):
    allowed = ("pause", "resume", "emergency_close", "refresh_macro")
    if action not in allowed:
        self._json({"error": f"invalid action: {action}"}, 400)
        return

    payload = {
        "action": action,
        "timestamp": time.time(),
        "source": "dashboard_api"
    }
    _atomic_write_json(CONTROL_FILE, payload)

    # Optimistic local state update fallback
    if action in ("pause", "resume"):
        current_state = _load_json(STATE_PATH) or {}
        current_state["state"] = "SYSTEM_HALT" if action == "pause" else "IDLE"
        _atomic_write_json(STATE_PATH, current_state)

    messages = {
        "pause": "已下发暂停交易指令 (SYSTEM_HALT)",
        "resume": "已下发恢复交易指令 (IDLE)",
        "emergency_close": "已下发市价全平指令",
        "refresh_macro": "已下发刷新宏观指令"
    }
    self._json({"status": "ok", "action": action, "message": messages.get(action, "指令已下发")})
```

#### D. Safe Atomic File Writer & Memory-Bounded Log Tailing
```python
def _atomic_write_json(path: str, data: dict):
    tmp = path + f".tmp_{os.getpid()}_{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _tail_log(path: str, n: int = 80) -> list:
    if not os.path.exists(path):
        return []
    try:
        # Seek to the end and read at most 64KB to avoid OOM
        buffer_size = 64 * 1024
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - buffer_size), os.SEEK_SET)
            lines = f.read().decode("utf-8", errors="replace").splitlines()
            return lines[-n:]
    except Exception:
        return []
```

#### E. Active Equity History File Resolution
```python
def _resolve_equity_history_path() -> str:
    # 1. Match active risk profile file
    if os.path.exists(RISK_PATH):
        base = os.path.splitext(os.path.basename(RISK_PATH))[0]
        derived = os.path.join(HERE, f"equity_history_{base}.json")
        if os.path.exists(derived):
            return derived
    # 2. General fallback
    default_hist = os.path.join(HERE, "equity_history.json")
    return default_hist
```

---

## 5. Verification Method

To independently verify the diagnosis and future fixes, execute the following commands in `C:\Users\Administrator\Desktop\v1\a`:

1. **Verify Python Syntax & Imports**:
   ```powershell
   python -m py_compile dashboard.py
   ```
   *Expected*: Zero syntax errors.

2. **Verify Route Availability & Contracts**:
   Launch `dashboard.py` on a test port:
   ```powershell
   $p = Start-Process python -ArgumentList "dashboard.py --port 8899" -PassThru
   Start-Sleep -Seconds 2
   ```
   Query all endpoints:
   ```powershell
   # 1. Root & Health
   Invoke-RestMethod http://127.0.0.1:8899/health
   # 2. Contract Routes
   Invoke-RestMethod http://127.0.0.1:8899/api/state
   Invoke-RestMethod http://127.0.0.1:8899/api/history
   Invoke-RestMethod http://127.0.0.1:8899/api/logs?n=20
   # 3. Control Route POST
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8899/api/control -ContentType "application/json" -Body '{"action":"pause"}'
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8899/api/control -ContentType "application/json" -Body '{"action":"resume"}'
   # 4. Stop test process
   Stop-Process -Id $p.Id
   ```
   *Expected*: All requests return HTTP 200 with valid JSON; no 404s or 500s.

3. **Verify State Normalization for Positions**:
   In `test_position.py`, inject a simulated position into `state.json` with `direction="LONG"`, `sz_notional=150.0`, query `/api/state`, and verify `response.position.size > 0` and `response.position.side == "LONG"`.

4. **Invalidation Conditions**:
   - If `/api/history` returns 404, verification fails.
   - If `POST /api/control` returns 404 or fails to write `control.json`, verification fails.
   - If `dashboard.py` crashes on port conflict or hangs in infinite sleep, verification fails.
   - If `_tail_log` uses `f.readlines()`, verification fails.

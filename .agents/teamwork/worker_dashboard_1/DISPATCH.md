## 2026-11-13T07:31:37Z

You are the Frontend Dashboard UI Engineer worker for Milestone 5 of the trading bot project.

Working directory: C:\Users\Administrator\Desktop\v1\a
State folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\worker_dashboard_1
Original user request path: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
PROJECT document path: C:\Users\Administrator\Desktop\v1\a\PROJECT.md

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

EXCLUSIVE FILE OWNERSHIP:
You exclusively own and may modify:
- `C:\Users\Administrator\Desktop\v1\a\dashboard.py`
DO NOT modify `main.py`, `risk_manager.py`, `exchange.py`, `strategy.py`, `alerts.py`, `multi_coin_router.py`, `utils.py`, `scripts/*`, or `tests/*`.

INPUT DOCUMENTS TO READ:
1. `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md` (Specifically the M5 defect list and OKX cancellation instructions)
2. `C:\Users\Administrator\Desktop\v1\a\PROJECT.md`
3. Explorer M5-1 report: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m5_1\handoff.md` (Backend API & Server architecture)
4. Explorer M5-2 report: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m5_2\handoff.md` (Frontend UI & Chart rendering)
5. Explorer M5-3 report: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m5_3\handoff.md` (Controls & Edge cases)

REQUIREMENTS & DEFECTS TO REPAIR IN `dashboard.py`:

1. Core Architecture: 5-Tab Navigation Bar:
   - Add `<nav class="tabs-nav">` with 5 navigation tabs:
     1. 📊 首页大盘 (Overview)
     2. 🧠 信号研判 (AI & Signals)
     3. 🛡️ 持仓风控 (FSM & Risk)
     4. 📈 绩效分析 (Analytics)
     5. ⚙️ 运维监控 (Logs & System)
   - Divide the long 2500px page into 5 corresponding `.tab-pane` containers (`tab-overview`, `tab-signals`, `tab-risk`, `tab-analytics`, `tab-system`).
   - Implement `switchTab(tabId)` function: toggles `.active` class on button and pane, and calls `chartInstance.resize()` on visible charts with a 50ms delay to prevent 0-width collapsed rendering.

2. Critical Risk Control: Modal Dialog Confirm/Cancel Buttons Text:
   - In `#pauseModal` and `#emergencyModal`, ensure buttons have explicit, visible text labels and semantic IDs:
     - `#btnCancelPause`: "取消" (Cancel)
     - `#btnConfirmPause`: "确认暂停" (Confirm Pause)
     - `#btnCancelEmergency`: "取消" (Cancel)
     - `#btnConfirmEmergency`: "🛑 立即市价全平" (Emergency Close)
   - Ensure proper CSS classes (`.btn-secondary`, `.btn-primary`, `.btn-danger`) and minimum widths (e.g. `min-width: 96px`), with `# -*- coding: utf-8 -*-` at line 1 of `dashboard.py` so Chinese characters render crisply.

3. Chart Loading & Data Linkage:
   - Fix `/static/echarts.min.js` 404 issue: implement robust fallback script loading that checks `typeof echarts !== 'undefined'` with retry polling (`ensureChartInit()`) or embedded script fallback; eliminate uncaught console errors.
   - Net value trend timeframe toggle buttons (5m / 1h / 1d / All): ensure clicking filters the data points, requests `/api/equity_history?interval=...` or `/api/history?interval=...`, and updates the chart with `chart.setOption(..., notMerge: true)`.
   - Guard `yAxis.min` and `yAxis.max` against `NaN` or `-Infinity` when data points are sparse or empty.
   - Guard against width=0 collapse when initializing in hidden tabs.

4. Text Parsing & AI Stream Empty State:
   - Fix HTML entity unescaping in logs: unescape `&lt;` and `&gt;` so `streak<= -3` displays properly instead of `streak&lt;= -3`.
   - Strip string slice artifacts like `b'...'` or `B' ]` from error reasons.
   - Design a structured Empty State component for AI events when no trade entries have occurred yet.

5. Responsive Design & Sticky Header:
   - Make the top navigation bar / action buttons sticky (`position: sticky; top: 0; z-index: 100`) with high z-index and subtle backdrop-filter blur so that "⏸ 暂停交易" and "🛑 紧急全平" are always immediately accessible when scrolling through logs.
   - Add mobile responsive media queries (`@media (max-width: 768px)` and `@media (max-width: 480px)`) so KPI cards align cleanly and buttons do not overflow.

6. Metric & Data Alignment:
   - Equity Display: Read real Hyperliquid equity (current flat balance 858.67 USDC) via `_fetch_real_account_equity()` or `st.get("account_equity")`.
   - Daily PnL: Reflect rolled-over daily PnL (0.00 today) from `risk_state_aggressive.json` and `main.py`.
   - Historical metrics: Display calibrated 5 trades, 1 win, 4 losses, win rate 20%, max consecutive losses 3.
   - Multi-Coin Radar: Render dynamic scores from `state.json:radar_scores` (BTC, ETH, SUI, ARB).
   - ATR: Calibrate ETH ATR display so it does not render distorted 213.72 (clamp/sanitize to ~1.5 - 8.0 USDC or `cur_px * 0.0020`).
   - Position Data: Normalize position fields in `/api/state` (`sz_notional`/`filled_sz` -> `size`/`sz`, `direction` -> `side`, root `coin`) so the UI correctly detects and renders active positions when present.

7. Backend Routes & Cloud Compatibility:
   - Add alias `GET /api/history` pointing to equity history.
   - Implement `POST /api/control` reading JSON `{"action": "pause"|"resume"|"emergency_close"|"refresh_macro"}` and writing atomically to `control.json` via `.tmp` + `os.replace`.
   - Add `GET /health` and `GET /api/health` returning `{"status": "ok"}`.
   - Port resolution: read `os.getenv("PORT")` before defaulting to 8080 (crucial for Render container deployment).
   - Server threading: set `allow_reuse_address = True` and `daemon_threads = True` on `ThreadingHTTPServer`. Eliminate infinite `while True: time.sleep(3600)` on port collision (exit cleanly with error).
   - Memory safety: In `_tail_log`, read only the last 64KB from disk instead of `f.readlines()`.

8. OKX Requirement Fully Removed:
   - Per user scope update: completely remove any OKX references, endpoints, cards, or dummy code. Keep architecture minimal and clean.

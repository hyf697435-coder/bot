# Frontend Dashboard Diagnosis & Fix Investigation Report (Milestone 5)

## 1. Observation

### Obs 1.1: Missing Navigation Tab Bar (Single 2500px Scroll vs 5 Architectural Tiers)
- **Location**: `dashboard.py` lines 15-21 vs lines 1224-1420.
- **Direct Code Quote**:
  In file header (lines 15-21):
  ```python
  # 架构划分：
  #   1. 📊 首页大盘 (Overview)       : 核心资产大卡、4大专题穿透入口、实时净值迷你走势
  #   2. 🧠 信号研判 (AI & Signals)   : 4 币打分雷达、宏观 TRS 多周期动能、AI_Trade 意图审批全文
  #   3. 🛡️ 持仓风控 (FSM & Risk)     : 动态 R 轨进度条 (SL/BE/Trail/TP)、开仓风控模拟、30D 相关性热力图
  #   4. 📈 绩效分析 (Analytics)      : 真实日净值曲线、专业水下回撤图 (Underwater Plot)、冷启动友好绩效归因
  #   5. ⚙️ 运维监控 (Logs & System)  : 实时多级别过滤日志流、Hyperliquid 与 AI 节点延迟实测与鉴权诊断
  ```
  In `<header class="navbar">` (lines 1225-1241):
  ```html
  <header class="navbar">
    <div class="navbar-inner">
      <div class="nav-brand">
        <div class="brand-logo">QR</div>
        <div>
          <span class="brand-title">QUANT RADAR</span>
          <span style="font-size:11px; color:var(--text-dim); margin-left:6px;">Hyperliquid 智能量化阵列</span>
        </div>
      </div>

      <div class="nav-actions">
        <span class="badge-fsm active" id="fsmBadge">● 实盘就绪 IDLE</span>
        <button class="btn" id="btnPause" onclick="openModal('pauseModal')">⏸ 暂停交易</button>
        <button class="btn btn-danger" onclick="openModal('emergencyModal')">🛑 紧急全平</button>
      </div>
    </div>
  </header>
  ```
- **Observed Behavior**:
  There are NO navigation tabs, NO `<nav class="nav-tabs">`, NO tab container divs (`.tab-pane`), and NO tab switching JavaScript. All cards (KPI row, equity chart, position hero, 4-coin radar, AI stream, ops console) are stacked vertically inside `<main class="container">` spanning over 2500px in height.
  Furthermore, backend endpoints implemented for tabs — `/api/underwater` (`_make_underwater_drawdown_json()`, lines 356-375), `/api/correlation` (`_compute_correlation_matrix()`, lines 425-471), and `/api/fsm_status` (`_extract_fsm_status()`, lines 474-554) — are completely orphaned and never rendered in the frontend.

### Obs 1.2: Modal Confirm / Cancel Buttons Rendering Anomalies
- **Location**: `dashboard.py` lines 1425-1449.
- **Direct Code Quote**:
  ```html
  <div class="modal-overlay" id="pauseModal">
    <div class="modal-content">
      <h3 style="font-size:16px; font-weight:700; margin-bottom:8px;">确认切换交易状态？</h3>
      <p style="font-size:13px; color:var(--text-muted); line-height:1.5;">
        暂停后将阻止一切新开仓与 AI 决策触发。已持有仓位将继续由硬止损与动态保本逻辑保护。
      </p>
      <div class="modal-actions">
        <button class="btn" onclick="closeModal('pauseModal')">取消</button>
        <button class="btn" style="background:#0f172a; color:#fff;" onclick="confirmTogglePause()">确认执行</button>
      </div>
    </div>
  </div>

  <div class="modal-overlay" id="emergencyModal">
    <div class="modal-content">
      <h3 style="font-size:16px; font-weight:700; color:var(--color-red); margin-bottom:8px;">⚠️ 确认执行紧急全平？</h3>
      <p style="font-size:13px; color:var(--text-muted); line-height:1.5;">
        此操作将立即以市价撮合关闭所有币种持仓，撤销活动挂单，并将系统置为 HALT 状态！请谨慎确认。
      </p>
      <div class="modal-actions">
        <button class="btn" onclick="closeModal('emergencyModal')">取消</button>
        <button class="btn btn-danger" onclick="confirmEmergencyClose()">立即市价全平</button>
      </div>
    </div>
  </div>
  ```
- **Observed Behavior**:
  1. PEP 263 encoding header `# -*- coding: utf-8 -*-` was placed on line 10 rather than line 1 or 2 of `dashboard.py`. Under certain Windows default encodings (CP1252/GBK), Python can misread non-ASCII raw string literals.
  2. The cancel button has no class other than `.btn` with no semantic secondary styling, and lacks explicit DOM identifiers (`id="btnCancelPause"`, `id="btnConfirmPause"`, `id="btnCancelEmergency"`, `id="btnConfirmEmergency"`).
  3. The buttons lack explicit min-width and flex alignment safeguards. If `.btn` font rendering fails or is scaled down, buttons collapse into empty boxes without legible text.

### Obs 1.3: Charts & Interactions (404 Static Fallback Race, Filter Data Ignored, Width=0 Collapse)
- **Location**: `dashboard.py` lines 705-709, lines 1279-1284, lines 1794-1872, line 1880.
- **Direct Code Quote**:
  Lines 705-709:
  ```html
  <script src="/static/echarts.min.js"></script>
  <script>
    if (typeof echarts === 'undefined') {
      document.write('<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"><\/script>');
    }
  </script>
  ```
  Lines 1794-1798 & 1880:
  ```javascript
  async function initChart() {
    if (typeof echarts === 'undefined') return;
    const dom = document.getElementById('chartEquity');
    if (!chartEquityInstance && dom) chartEquityInstance = echarts.init(dom);
    loadEquity('1h');
  }
  ...
  setTimeout(initChart, 200);
  ```
  Lines 1803-1815:
  ```javascript
  async function loadEquity(interval, btnEl) {
    if (interval) currentInterval = interval;
    if (btnEl) {
      document.querySelectorAll('#chartTabs .btn').forEach(b => b.classList.remove('active'));
      btnEl.classList.add('active');
    }
    if (!chartEquityInstance) return;
    try {
      const res = await fetch('/api/equity_history?interval=' + currentInterval).then(r => r.json());
      const data = (res && (res.data || res.points)) || [];
      const times = data.map(d => d.time || '');
      const values = data.map(d => (d.equity !== undefined ? d.equity : 917.35));

      chartEquityInstance.setOption({
  ```
- **Observed Behavior**:
  1. `C:\Users\Administrator\Desktop\v1\a\static` does not exist on disk (`Test-Path` returned `False`). Requests to `/static/echarts.min.js` return HTTP 404.
  2. When falling back to the external CDN script via `document.write`, if CDN network latency exceeds 200ms, `initChart()` at line 1880 executes while `typeof echarts === 'undefined'`, returning immediately.
  3. When `chartEquityInstance` is null, clicking the timeframe buttons `5m`, `1h`, `1d`, `all` executes lines 1805-1808 (toggling the `.active` CSS class on the button), and then immediately hits `if (!chartEquityInstance) return;` at line 1809. The buttons visibly switch active state, but NO data is ever fetched or rendered.
  4. In ECharts, `setOption` merges data by default. Changing from 12 points to 4 points without `notMerge: true` causes previous x-axis categories and plot points to bleed into the new timeframe.
  5. When empty (`data = []`), `value.max - value.min` in y-axis `min`/`max` functions produces `-Infinity` or `NaN`, triggering uncaught ECharts internal math errors.
  6. When tabs are introduced, chart DOM elements inside hidden tabs (`display: none`) report `clientWidth = 0`. ECharts renders a collapsed 0px canvas unless an explicit `.resize()` listener is triggered on tab activation.

### Obs 1.4: Text Parsing & Rendering Anomalies
- **Location**: `dashboard.py` lines 1746-1770, lines 398-417, lines 1700-1722.
- **Direct Code Quote**:
  Lines 1768-1770:
  ```javascript
  function escapeHtml(str) {
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  ```
  Lines 1753-1761:
  ```javascript
  let html = '';
  filtered.forEach(line => {
    let cls = 'log-line';
    if (line.includes('ERROR')) cls += ' log-err';
    else if (line.includes('WARNING')) cls += ' log-warn';
    else if (line.includes('[AI') || line.includes('AI_')) cls += ' log-ai';
    html += `<div class="${cls}">${escapeHtml(line)}</div>`;
  });
  box.innerHTML = html || '<div style="color:var(--text-dim);">暂无日志数据</div>';
  ```
  In log file (`C:\Users\Administrator\Desktop\v1\log\trading_bot.log` line 208):
  `2026-11-13 10:58:47 | WARNING  | [RiskManager] 状态不一致: streak<= -3 但 cooldown_until 为空 | 自动补全冷却倒计时`
  In `renderAiStream` (lines 1700-1705, 1715):
  ```javascript
  const events = (res && res.events) || [];
  if (events.length === 0) {
    box.innerHTML = '<div style="padding:14px; text-align:center; color:var(--text-dim);">模型保持 5m 极值点与 1h 趋势共振监控中...</div>';
    return;
  }
  ...
  <span style="font-size:11px; color:var(--text-dim);">${e.time ? e.time.split(' ')[1] : '--'}</span>
  ```
- **Observed Behavior**:
  1. When logs containing unescaped HTML entities such as `streak<= -3` or double-encoded strings are rendered, users see literal `streak&lt;= -3` text artifacts.
  2. If Python log lines contain byte-string representations (e.g. `b'...'`), slicing extracts artifacts such as `B' ]` into `e.reason`.
  3. In `e.time ? e.time.split(' ')[1] : '--'`, if `e.time` is `"刚刚"` (default fallback in line 386) or an ISO timestamp (`2026-11-13T03:14:40`), `.split(' ')[1]` is `undefined`, literally printing `"undefined"` on the card.
  4. The AI event stream completely lacks a structured modern Empty State component (icon, header, explanation, status indicator) when no trades have triggered AI decisions.

### Obs 1.5: Responsiveness & UX (Sticky Header & Mobile KPI Alignment)
- **Location**: `dashboard.py` lines 758-778, lines 1216-1219.
- **Direct Code Quote**:
  ```css
  .navbar-inner {
    width: 100%;
    max-width: 1360px;
    margin: 0 auto;
    padding: 0 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  ...
  @media (max-width: 1024px) {
    .metrics-row { grid-template-columns: repeat(2, 1fr); }
    .hero-split, .secondary-split { grid-template-columns: 1fr; }
  }
  ```
- **Observed Behavior**:
  1. There is no mobile media query for `< 768px` or `< 640px`.
  2. On mobile screens (< 600px), `.navbar-inner` elements (`.nav-brand` + `.badge-fsm` + `btnPause` + `btnEmergency`) overflow horizontally, pushing the emergency close button offscreen.
  3. On mobile screens, 2-column KPI cards (`repeat(2, 1fr)`) with static text (`初始: $1,000.00 | 累计: -8.26%`) wrap into multiple ragged lines, breaking vertical height alignment.
  4. `.container` padding is fixed at `24px`, wasting 48px of valuable mobile viewport.

### Obs 1.6: Position Data Binding Disconnect (Active Positions Never Rendered)
- **Location**: `dashboard.py` lines 1522-1540 vs `main.py` lines 1730-1742.
- **Direct Code Quote**:
  `dashboard.py` lines 1522-1536:
  ```javascript
  const pos = resState.position;
  if (pos && (pos.size > 0 || pos.sz > 0)) {
    posBadge.className = 'hero-tag ' + (pos.side === 'LONG' ? 'green' : 'red');
    posBadge.innerText = (pos.coin || 'ETH') + ' ' + (pos.side || 'LONG');
    ...
    const pnl = pos.unrealized_pnl || pos.pnl || 0;
    const pnlPct = pos.return_pct || 0;
  ```
  `main.py` lines 1730-1742:
  ```python
  "position": None if self.position is None else {
      "entry_price": self.position.entry_price,
      "direction": self.position.direction,
      "sz_notional": self.position.sz_notional,
      "tp_price": self.position.tp_price,
      "sl_price": self.position.sl_price,
      "initial_risk_r": self.position.initial_risk_r,
      "peak_price": self.position.peak_price,
      "bars_held": self.position.bars_held,
      "opened_at": self.position.opened_at,
      "entry_score": getattr(self.position, "entry_score", 75.0),
      "filled_sz": getattr(self.position, "filled_sz", 1.0),
  }
  ```
- **Observed Behavior**:
  `pos.size` and `pos.sz` are both `undefined`. `pos.side` is `undefined` (field is `direction`). `pos.unrealized_pnl` is `undefined`.
  `pos && (pos.size > 0 || pos.sz > 0)` is ALWAYS `false`.
  Even when the trading bot holds an active position, the dashboard ALWAYS renders "无持仓敞口 / 目前空仓待机 (IDLE)"! Active positions never appear on screen.

### Obs 1.7: Missing API Endpoints (`/api/history` and `/api/control`)
- **Location**: `dashboard.py` lines 1906-2042 vs `PROJECT.md` line 94 and `SCOPE.md` lines 8-9.
- **Direct Code Quote**:
  `CleanPanelHandler.do_GET` (lines 1931-1985):
  Handles `/api/state`, `/api/risk`, `/api/heartbeat`, `/api/coins`, `/api/log`, `/api/equity_history`, `/api/underwater`, `/api/ai_events`, `/api/correlation`, `/api/fsm_status`, `/api/network`.
  `/api/history` is completely missing (returns 404).
  `CleanPanelHandler.do_POST` (lines 1990-2041):
  Handles `/api/action/toggle_pause`, `/api/action/emergency_close`, `/api/action/refresh_macro`.
  `/api/control` is completely missing (returns 404).
  `control.json` is written directly with `open()` without atomic replace (`os.replace`).

---

## 2. Logic Chain

1. **Tab Structure**:
   - From Obs 1.1, `dashboard.py` documents 5 core operational views and provides backend data for all 5 views, yet places every element in a single flat vertical flow exceeding 2500px.
   - Therefore, introducing a 5-tab navigation bar (`Overview`, `AI & Signals`, `FSM & Risk`, `Analytics`, `Logs & System`) with container pane switching directly aligns the UI with the architectural design, eliminates scrolling exhaustion, and enables proper display of previously hidden analytics (Underwater plot, Correlation matrix).

2. **Modal Buttons**:
   - From Obs 1.2, `#pauseModal` and `#emergencyModal` buttons rely on plain unstyled `.btn` classes and lack explicit IDs, explicit secondary styling, and UTF-8 encoding declaration at line 1.
   - Therefore, adding explicit button IDs, semantic CSS classes (`btn-secondary`, `btn-primary`, `btn-danger`), dedicated flex styling, and ensuring correct UTF-8 headers will guarantee that "确认" and "取消" labels render legibly in all browser environments.

3. **Chart Fallback, Interaction & Width=0**:
   - From Obs 1.3, the local static file does not exist, causing CDN fallback. The 200ms fixed timeout creates an initialization race condition. When `chartEquityInstance` is null, clicking timeframe buttons toggles CSS active state but returns early, creating the illusion of broken filters.
   - In ECharts, changing series length requires `notMerge: true`, and y-axis scale bounds on empty/singleton arrays must guard against `NaN`/`Infinity`.
   - Hidden tabs have `display: none` (0 width). Tab activation must trigger `.resize()` on visible charts.

4. **Text Sanitization & Empty State**:
   - From Obs 1.4, raw strings from log files and AI decision records can contain unescaped entities, byte prefixes (`b'`), and unexpected timestamp formats (`"刚刚"` or ISO).
   - Sanitizing input via an HTML entity resolver before display, stripping byte artifacts, and providing safe regex date formatters eliminates text defects.
   - Replacing the plain text div with a dedicated Empty State component ensures the UI communicates system health when trade signals are idle.

5. **Responsive Navbar & Layout**:
   - From Obs 1.5, lack of small-screen media queries causes navbar action buttons to clip and KPI cards to wrap unevenly.
   - Implementing a sticky, compact navbar with priority action buttons and fluid responsive grid layouts (`@media (max-width: 768px)` and `@media (max-width: 480px)`) ensures complete mobile and desktop usability.

6. **Position Data Contract**:
   - From Obs 1.6, the property names used in `dashboard.py` (`pos.size`, `pos.sz`, `pos.side`) contradict `main.py` (`pos.sz_notional`, `pos.filled_sz`, `pos.direction`).
   - Adapting the frontend to support both field names and deriving real-time unrealized PnL from `resCoins[coin]` and `pos.entry_price` restores full visibility of live trades.

7. **Contract Compliance**:
   - From Obs 1.7, alias routes `/api/history` (forwarding to `/api/equity_history`) and `/api/control` (processing control actions with atomic file replacement) are required to satisfy the project interface contract.

---

## 3. Caveats

1. **Live Network Latency Dependency**: The network probe thread in `dashboard.py` queries `https://api.hyperliquid-testnet.xyz/info` and the third-party AI gateway every 12 seconds. If outbound Internet access is restricted or behind a proxy, latency indicators will display fallback default values (`-- ms` or cached values).
2. **Chart.js vs ECharts**: The project currently uses Apache ECharts (v5.5.0) rather than Chart.js. Both libraries serve identical purposes, but ECharts is already deeply integrated in `dashboard.py`. Replacing ECharts with Chart.js is unnecessary and would introduce regressions; hardening the existing ECharts implementation is the most stable and risk-free approach.
3. **No Code Modification Undertaken**: In accordance with the Explorer role constraints, no source code files were modified during this investigation.

---

## 4. Conclusion & Recommended Action Plan

The 5 authoritative defects reported by the parent orchestrator, along with the critical position data binding disconnect and missing API contract routes, are fully diagnosed with exact root causes identified.

### Concrete Fix Specifications for Implementer:

#### 1. Navigation Tab Bar Architecture
- Add tab navigation in `<header>` or immediately below navbar:
  ```html
  <nav class="tabs-nav">
    <div class="tabs-inner">
      <button class="tab-btn active" onclick="switchTab('overview')">📊 首页大盘</button>
      <button class="tab-btn" onclick="switchTab('signals')">🧠 信号研判</button>
      <button class="tab-btn" onclick="switchTab('risk')">🛡️ 持仓风控</button>
      <button class="tab-btn" onclick="switchTab('analytics')">📈 绩效分析</button>
      <button class="tab-btn" onclick="switchTab('system')">⚙️ 运维监控</button>
    </div>
  </nav>
  ```
- Wrap page sections into `<div id="tab-overview" class="tab-pane active">`, `<div id="tab-signals" class="tab-pane">`, `<div id="tab-risk" class="tab-pane">`, `<div id="tab-analytics" class="tab-pane">`, `<div id="tab-system" class="tab-pane">`.
- In `switchTab(tabId)`:
  - Toggle `.active` class on `.tab-pane` and `.tab-btn`.
  - Execute `setTimeout(() => { if (chartEquityInstance) chartEquityInstance.resize(); if (chartUnderwaterInstance) chartUnderwaterInstance.resize(); }, 50);`.

#### 2. Modal Confirm / Cancel Button Hardening
- Update `#pauseModal` and `#emergencyModal` in lines 1425-1449:
  ```html
  <div class="modal-actions">
    <button class="btn btn-secondary" id="btnCancelPause" onclick="closeModal('pauseModal')">取消 (Cancel)</button>
    <button class="btn btn-primary" id="btnConfirmPause" onclick="confirmTogglePause()">确认暂停 (Confirm)</button>
  </div>
  ...
  <div class="modal-actions">
    <button class="btn btn-secondary" id="btnCancelEmergency" onclick="closeModal('emergencyModal')">取消 (Cancel)</button>
    <button class="btn btn-danger" id="btnConfirmEmergency" onclick="confirmEmergencyClose()">🛑 立即市价全平 (Emergency Close)</button>
  </div>
  ```
- Add explicit CSS:
  ```css
  .modal-actions .btn { min-width: 96px; justify-content: center; font-size: 13px; font-weight: 600; padding: 8px 16px; }
  .btn-secondary { background: #f1f5f9; color: var(--text-main); border: 1px solid var(--border-subtle); }
  .btn-secondary:hover { background: #e2e8f0; }
  .btn-primary { background: #0f172a; color: #ffffff; border: 1px solid #0f172a; }
  .btn-primary:hover { background: #1e293b; }
  ```

#### 3. Chart Loading, Interaction & Data Filtering Hardening
- Replace the fragile 200ms timeout with an intelligent loader loop that retries until `echarts` is loaded or falls back cleanly:
  ```javascript
  function ensureChartInit(retries = 20) {
    if (typeof echarts !== 'undefined') {
      initChart();
    } else if (retries > 0) {
      setTimeout(() => ensureChartInit(retries - 1), 250);
    } else {
      console.warn('ECharts library failed to load.');
    }
  }
  ```
- In `loadEquity(interval, btnEl)`:
  - Always call `ensureChartInit()` if `chartEquityInstance` is null.
  - Pass `{ notMerge: true }` to `chartEquityInstance.setOption(option, true)`.
  - Guard `yAxis.min` and `yAxis.max`:
    ```javascript
    min: function(val) {
      if (!isFinite(val.min) || !isFinite(val.max)) return 0;
      const diff = val.max - val.min;
      return Math.floor(val.min - (diff < 5 ? 5 : diff * 0.15));
    },
    max: function(val) {
      if (!isFinite(val.min) || !isFinite(val.max)) return 1000;
      const diff = val.max - val.min;
      return Math.ceil(val.max + (diff < 5 ? 5 : diff * 0.15));
    }
    ```

#### 4. Text Parsing, String Slicing & AI Stream Empty State
- Safe sanitization function:
  ```javascript
  function sanitizeLogLine(str) {
    if (!str) return '';
    return String(str)
      .replace(/&amp;/g, '&')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }
  ```
- Python `_extract_recent_ai_events` string cleanup:
  ```python
  reason = re.sub(r"^[bB]['\"]|['\"]$", "", reason).strip()
  ```
- AI Stream Empty State component:
  ```html
  <div class="empty-state-box">
    <div style="font-size:24px; margin-bottom:6px;">📡</div>
    <div style="font-weight:600; color:var(--text-main); font-size:13px;">暂无触发的 AI 决策事件</div>
    <div style="font-size:11px; color:var(--text-dim); margin-top:4px;">
      量化雷达持续巡检 5m 极值与 1h 趋势。当前未满足入场触发门槛 (Score < 70.0)，AI 代理保持待命。
    </div>
  </div>
  ```

#### 5. Responsive Design & Mobile Sticky Action Bar
- Media queries for `< 768px` and `< 480px`:
  ```css
  @media (max-width: 768px) {
    .navbar-inner { padding: 0 12px; gap: 8px; }
    .brand-title { font-size: 13px; }
    .badge-fsm { font-size: 10px; padding: 2px 6px; }
    .nav-actions .btn { padding: 4px 8px; font-size: 11px; }
    .container { padding: 0 12px; margin: 12px auto; gap: 14px; }
    .metrics-row { grid-template-columns: 1fr; gap: 10px; }
    .tabs-inner { overflow-x: auto; white-space: nowrap; -webkit-overflow-scrolling: touch; }
  }
  ```

#### 6. Position Property Name Alignment
- Update position checking logic in `pollData()`:
  ```javascript
  const hasPos = pos && ((pos.size && pos.size > 0) || (pos.sz && pos.sz > 0) || (pos.sz_notional && pos.sz_notional > 0) || (pos.filled_sz && pos.filled_sz > 0) || (pos.entry_price && pos.entry_price > 0));
  if (hasPos) {
    const side = (pos.direction || pos.side || 'LONG').toUpperCase();
    const coin = pos.coin || resState.coin || 'ETH';
    const entryPx = Number(pos.entry_price || 0);
    const curPx = (resCoins && resCoins[coin]) ? Number(resCoins[coin]) : entryPx;
    const size = Number(pos.size || pos.sz || pos.filled_sz || (entryPx > 0 ? pos.sz_notional / entryPx : 0));
    const pnl = pos.unrealized_pnl !== undefined ? Number(pos.unrealized_pnl) : (side === 'LONG' ? (curPx - entryPx) * size : (entryPx - curPx) * size);
    const pnlPct = entryPx > 0 ? ((curPx - entryPx) / entryPx * 100 * (side === 'LONG' ? 1 : -1)) : 0;
    ...
  }
  ```

#### 7. Interface Contract Route Additions
- In `CleanPanelHandler.do_GET`:
  ```python
  elif path in ("/api/history", "/api/equity_history"):
      interval = qs.get("interval", ["1h"])[0].lower()
      self._json(_make_equity_history_json(interval))
  ```
- In `CleanPanelHandler.do_POST`:
  Add handler for `/api/control` that reads JSON payload `{"action": "...", ...}`, writes atomically to `control.json` via `.tmp` + `os.replace`, and performs appropriate state mutations.

---

## 5. Verification Method

To independently verify the defects and validate the proposed fixes:

1. **Verify Static File 404 & Loading**:
   ```powershell
   python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/static/echarts.min.js').getcode())"
   ```
   *Expected Current*: HTTP 404.
   *Verification after fix*: CDN fallback loads with retry loop or embedded fallback; no blank chart.

2. **Verify Interface Contracts**:
   ```powershell
   python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/api/history').getcode())"
   python -c "import urllib.request, json; req = urllib.request.Request('http://127.0.0.1:8080/api/control', data=json.dumps({'action':'refresh_macro'}).encode(), headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(req).getcode())"
   ```
   *Expected Current*: Both return HTTP 404.
   *Verification after fix*: Both return HTTP 200 with valid JSON.

3. **Verify Position Data Binding**:
   Inject a mock open position into `state.json`:
   ```json
   "position": {
     "entry_price": 2315.5,
     "direction": "LONG",
     "sz_notional": 2315.5,
     "sl_price": 2280.0,
     "tp_price": 2400.0,
     "initial_risk_r": 35.5,
     "filled_sz": 1.0
   }
   ```
   Inspect DOM elements `#posActiveArea` and `#posBadge`:
   *Expected Current*: Hidden (`display: none`), badge says "无持仓敞口".
   *Verification after fix*: `#posActiveArea` is visible (`display: block`), badge shows "ETH LONG", and live PnL is computed and styled.

4. **Verify Tab Navigation & Responsive Resize**:
   In headless or interactive browser:
   - Navigate to `http://127.0.0.1:8080/`.
   - Click each of the 5 tabs (`Overview`, `AI & Signals`, `FSM & Risk`, `Analytics`, `Logs & System`).
   - Check that active panel displays, other panels hide, and chart `.resize()` is called with zero canvas width collapses.
   - Resize viewport to 375px (iPhone SE width) and verify no horizontal scrollbars appear in the navigation bar.

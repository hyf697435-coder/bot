# Synthesis: Milestone 5 Frontend Dashboard Diagnosis & Fix

## 1. Overview & Consensus
All 3 Explorers and the Authoritative Defect Inventory are in 100% agreement on root causes, line numbers, and remediation specifications across `dashboard.py`.

## 2. Complete Defect Remediation Catalog

### Defect 1: Navigation Tab Bar Missing (Core Architecture)
- **Problem**: UI header defines 5 tabs (`Overview`, `AI & Signals`, `FSM & Risk`, `Analytics`, `Logs & System`), but the page renders an unmanageable 2500px single scroll. Backend endpoints for tabs (`/api/underwater`, `/api/correlation`, `/api/fsm_status`) are orphaned.
- **Fix**:
  1. Add `<nav class="tabs-nav">` with 5 tab buttons below navbar.
  2. Partition the vertical container into 5 `<div id="tab-..." class="tab-pane">` sections.
  3. Implement `switchTab(tabId)` to toggle `.active` classes and trigger `resize()` on visible charts.

### Defect 2: Modal Confirm/Cancel Buttons Missing Text (Risk Control Safety)
- **Problem**: Modals for "⏸ 暂停交易" (#pauseModal) and "🛑 紧急全平" (#emergencyModal) have empty button labels rendered as blank boxes.
- **Fix**:
  1. Add explicit button IDs: `btnCancelPause`, `btnConfirmPause`, `btnCancelEmergency`, `btnConfirmEmergency`.
  2. Add explicit semantic CSS classes: `.btn-secondary` (styled cancel button), `.btn-primary` (dark confirm button), `.btn-danger` (emergency button).
  3. Add `min-width: 96px`, `justify-content: center`, clear legible text: "取消 (Cancel)", "确认暂停/确认恢复", "🛑 立即市价全平".
  4. Ensure UTF-8 declaration `# -*- coding: utf-8 -*-` is placed at line 1 of `dashboard.py`.
  5. Add button debounce and loading state (`sendControlAction` helper with toast feedback).

### Defect 3: Charts & Interactions
- **Problem**: Local `/static/echarts.min.js` 404 falling back to CDN; fixed 200ms timeout creates race condition; timeframe buttons only toggle CSS class without filtering data; chart width=0 on hidden tabs; stale October mock data ($917.35) used instead of active risk equity history.
- **Fix**:
  1. Replace 200ms timeout with retry loop (`ensureChartInit`).
  2. In `loadEquity(interval, btnEl)`, fetch from `/api/history?interval=` and pass `notMerge: true` to `chart.setOption(option, true)`.
  3. Guard y-axis `min`/`max` against empty array `NaN`/`Infinity`.
  4. Attach chart `.resize()` to tab activation and window resize.
  5. Update `_make_equity_history_json` to read active `equity_history_risk_state_aggressive.json` and support interval downsampling/filtering.

### Defect 4: Text Parsing & Rendering Anomalies
- **Problem**: Unescaped HTML entities (e.g. `streak&lt;= -3`), string slice artifacts (`B' ]`), timestamp `"刚刚"` printing `"undefined"`, AI event stream missing empty state.
- **Fix**:
  1. Safe single-escape sanitization for log lines.
  2. Regex strip byte-string prefixes `^[bB]['"]` in `_extract_recent_ai_events`.
  3. Safe timestamp formatter handling both `"刚刚"` and date strings without printing `"undefined"`.
  4. Structured Empty State component for AI decision stream when empty.

### Defect 5: Responsiveness & UX
- **Problem**: Fixed 56px navbar overflows on mobile (< 480px / 768px), hiding emergency buttons; metric cards wrap raggedly. Missing `refresh_macro` button.
- **Fix**:
  1. Media queries for `@media (max-width: 768px)`: flex-wrap navbar, compact text, ensure emergency button remains visible.
  2. Responsive grid for `.metrics-row`: 2 columns with neat spacing.
  3. Add `btnRefreshMacro` to navbar actions.

### Backend & Interface Contracts:
1. Support `GET /api/history` (alias to `/api/equity_history`).
2. Support `POST /api/control` accepting JSON `{"action": "pause" | "resume" | "emergency_close" | "refresh_macro"}`.
3. Atomic write `control.json` via `.tmp` + `os.replace`.
4. Remove direct `state.json` mutations by dashboard.
5. Support Render `$PORT` environment variable: `PORT = int(os.getenv("PORT", 8080))`.
6. High-performance O(1) binary reverse-seek log tailing in `_tail_log`.
7. Position data binding support for `sz_notional`, `filled_sz`, and `direction`.

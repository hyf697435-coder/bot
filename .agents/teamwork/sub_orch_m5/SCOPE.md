# Scope: Milestone 5 (Frontend Dashboard Diagnosis & Fix)

## Architecture
- Interactive Web Dashboard Service (`dashboard.py`):
  - Built-in lightweight HTTP server / web service hosting endpoints:
    - `/` (HTML/JS dashboard UI with charts, metrics, logs, controls)
    - `/api/state` (current bot state, positions, risk metrics, signals)
    - `/api/history` (equity history time-series)
    - `/api/control` (POST endpoint for manual controls: pause, resume, emergency_close, refresh_macro)
    - `/api/logs` (tailing `trading_bot.log`)
  - Integration with file-based state:
    - `state.json` (bot state and positions)
    - `risk_state_aggressive.json` (risk parameters)
    - `equity_history_risk_state_aggressive.json` (equity curve data points)
    - `control.json` (control triggers read by trading bot)
    - `trading_bot.log` (execution logs)

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 19 | Dashboard Inspection & Diagnostics | Diagnose all display, chart rendering, API endpoint, and interaction bugs in `dashboard.py` | M5 | User Follow-up |
| 20 | Dashboard Bug Fixes & Hardening | Fix data rendering, missing fields, chart layout, control triggers, and error resilience in `dashboard.py` | M5 | User Follow-up |
| 20.1 | Defect 1: Navigation Tab Bar Missing | Render proper tab switcher (5 tabs: Overview, AI & Signals, FSM & Risk, Analytics, Logs & System) and route views | M5 | Authoritative Inventory |
| 20.2 | Defect 2: Modal Buttons Missing Text | Add explicit, styled "确认" and "取消" labels with distinct color cues for #pauseModal and #emergencyModal | M5 | Authoritative Inventory |
| 20.3 | Defect 3: Charts & Interactions | Fix local echarts.min.js fallback, implement actual data filtering for timeframe buttons (5m/1h/1d/全部), add resize listeners on tab activate/window resize | M5 | Authoritative Inventory |
| 20.4 | Defect 4: Text Parsing & Rendering Anomalies | Fix unescaped HTML entities (`streak&lt;= -3`), string slice artifacts (`B' ]`), and add Empty State for AI event stream | M5 | Authoritative Inventory |
| 20.5 | Defect 5: Responsiveness & Sticky Controls | Add sticky/fixed header control bar for emergency buttons; fix mobile/narrow alignment of top KPI metric cards | M5 | Authoritative Inventory |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 5.1 | Comprehensive Inspection & Diagnostics | Audit `dashboard.py` code, endpoints, frontend template, CSS/JS chart rendering, missing field handling | none | IN_PROGRESS |
| 5.2 | Fix Bugs & Implement Hardening | Fix rendering bugs, missing fields, chart time formatting, API controls, error resilience on missing files | 5.1 | PLANNED |
| 5.3 | Review, Stress Challenge & Forensic Audit | Reviewer approvals, adversarial stress testing (empty/malformed files, rapid requests), forensic audit | 5.2 | PLANNED |

## Interface Contracts
### Dashboard Web Interface (`dashboard.py` ↔ State & Control Files)
- Reads `state.json`, `risk_state_aggressive.json`, `equity_history_risk_state_aggressive.json`, `trading_bot.log`. Must handle missing or corrupt files gracefully (fallback to default empty structures, no 500 crashes).
- Writes commands to `control.json` (`emergency_close`, `pause`, `resume`, `refresh_macro`) via atomic write/replace.
- Serves HTTP routes `/`, `/api/state`, `/api/history`, `/api/control`, `/api/logs`.

## Code Layout
- Exclusive File Ownership: `dashboard.py`

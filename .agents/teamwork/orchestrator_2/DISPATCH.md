## 2026-11-13T07:25:30Z

Continue executing and complete the trading bot bug fixes, frontend monitoring dashboard repairs, and free cloud 24/7 Render deployment in C:\Users\Administrator\Desktop\v1\a following the prior interrupted run.

Working directory: C:\Users\Administrator\Desktop\v1\a
Integrity mode: development

## Requirements

### R1. Fix Trading Bot Core Runtime Errors and Alert Storm (R1 & R2)
- Fix the `NameError: name 'pd' is not defined` scope and reference error in the core trading loop of `main.py`.
- Investigate and resolve the root cause of the "空仓期 API 连续异常 32 次" alert email storm:
  - Decouple the underlying exchange API from the LLM relay API ("中转站").
  - Handle LLM relay timeouts as normal expected occurrences during flat position periods by gracefully degrading and suppressing email alert storms (do not falsely report them as critical exchange failures).
  - Implement exponential backoff retry for actual exchange API exceptions.
  - Ensure alert notifications clearly and unambiguously distinguish between exchange API errors and LLM relay API timeouts.

### R2. Frontend Monitoring Dashboard Repairs (R5)
- Repair the 5 identified UI defects in `dashboard.py`:
  - Missing 5-tab navigation (Market Overview, Signal Analysis, Position Risk Control, Performance Analytics, Operations Monitoring).
  - Empty text on confirmation/cancel buttons in the "Pause Trading" and "Emergency Panic Close" modal dialogs.
  - Local `/static/echarts.min.js` 404 missing resource error.
  - Net value trend timeframe toggle buttons (5m / 1h / 1d / All) lack data linkage.
  - Action buttons are not sticky/floating.
- Repair underlying distorted metrics and data:
  - Connect to Hyperliquid on-chain real equity (current 858.67 USDC flat position).
  - Implement automatic daily PnL rollover at natural day cut (fix `risk_state_aggressive.json` stuck on 11-06 causing distorted `daily_pnl`).
  - Refactor win rate and historical trade calculation metrics.
  - Fix multi-coin radar score deadlock (stuck at 45.0).
  - Calibrate anomalous ETH 5m ATR distortion.
- Fully cancel and remove all OKX requirements, keeping the architecture minimal without developing any OKX modules.

### R3. Free Cloud 24/7 High-Availability Deployment (R3)
- Deploy the trading bot and dashboard for 24/7 continuous operation using Render (750 hours/month free tier) in a web container hosting both Dashboard and Bot, combined with UptimeRobot anti-sleep heartbeat pinging.
- Use the configured Render deployment credentials:
  - Workspace ID: tea-dbr81smtlv9c73fo1uk0
  - Render API Key: rnd_jb6f6LpJYzUK0F5GFiQ0MurWHHCD
- Deliverables must include: Dockerfile, render.yaml, automated Render REST API creation script (`scripts/deploy_render.py`), and UptimeRobot heartbeat setup guide. Keep the Instabox runner scheme as a secondary backup.

## Acceptance Criteria

### Trading Bot Stability & Alert Storm Fix
- [ ] Static syntax and import checks pass for `main.py` without unresolved variable references or `NameError: name 'pd' is not defined`.
- [ ] The trading bot core loop executes without crashing.
- [ ] API exceptions during flat positions distinguish LLM relay timeouts from exchange API errors; LLM relay timeouts do not trigger repeated email alarm storms.
- [ ] Exchange API communication includes retry with exponential backoff.

### Dashboard UI & Data Integrity
- [ ] All 5 navigation tabs render properly.
- [ ] Confirmation and cancel buttons in pause and emergency close modal dialogs display visible labels.
- [ ] ECharts script loads successfully without 404 errors.
- [ ] Timeframe toggle buttons (5m/1h/1d/All) update the chart data accordingly.
- [ ] Action buttons remain sticky/floating during scrolling.
- [ ] Displayed equity reflects Hyperliquid real account value (858.67 USDC when flat).
- [ ] Daily PnL rolls over across natural day transitions.
- [ ] Multi-coin radar scores update dynamically without getting stuck at 45.0.
- [ ] No OKX modules or references remain in the active codebase.

### Cloud Deployment
- [ ] Valid `Dockerfile` and `render.yaml` are present and configured for Render Web Service deployment.
- [ ] `scripts/deploy_render.py` successfully validates Render credentials and can initiate service creation via Render REST API.
- [ ] Complete 24/7 deployment and UptimeRobot heartbeat guide is documented.
- [ ] The team-selected verification tests pass cleanly.

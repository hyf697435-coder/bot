# Project: Hyperliquid Trading Bot Runtime Stability & 24/7 Cloud Rotation

## Architecture
The system consists of four architectural tiers:
1. **Trading Bot Core Execution Engine** (`main.py`, `strategy.py`, `exchange.py`, `risk_manager.py`, `alerts.py`, `multi_coin_router.py`, `utils.py`):
   - FSM States: `IDLE`, `TRADING`, `COOL_DOWN`, `SYSTEM_HALT`.
   - Local Quantitative Indicators + Dual AI Agents (`AI_macro` and `AI_Trade`).
   - Dynamic Multi-Coin Radar Scanner across BTC, ETH, SUI, ARB.
   - Robust Risk Engine: continuous Kelly sizing, daily PnL rollover, trade reconciliation, periodic equity history sampling.
   - Calibrated technical indicators (RMA ATR, fractal breakouts, Donchian).
2. **Alert & Fault Governance Engine** (`main.py`, `alerts.py`, `exchange.py`, `strategy.py`):
   - Unambiguous classification between Exchange API, LLM Relay API, and Bot Runtime Errors.
   - Resilient retry with exponential backoff on Exchange market data.
   - Advisory suppression and quant fallback for LLM relay timeouts during flat position.
   - Alert throttling (maximum 1 alert per 30 minutes for consecutive idle errors) to eliminate alarm storms.
3. **24/7 Free Cloud Deployment & Rotation Ring** (`Dockerfile`, `render.yaml`, `scripts/deploy_render.py`, `scripts/rotator.py`, `scripts/deploy_sandbox.sh`, `.github/workflows/rotation.yml`):
   - **Primary User Cloud Tier: Render Web Service** (750 free hours/month, Docker-based deployment running trading bot + dashboard + UptimeRobot keepalive probe for 24/7 continuous operation, managed via Render REST API).
   - **Secondary / Backup Tier: Instabox Multi-Account Rotation** (dual-account alternating 6-hour proactive shifts, pessimistic locking state handover, zero-naked-risk transitions).
4. **Interactive Web Dashboard Service** (`dashboard.py`):
   - 5-tab modular UI (Overview, AI & Signals, FSM & Risk, Analytics, Logs & System).
   - Real-time trading monitoring, dynamic radar signals, high-density equity curve, safe modal controls, and calibrated risk metrics.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Commit `pd` import in `main.py` | Ensure `import pandas as pd` is permanently committed at module level | M1 | Survey 1 (R1) |
| 2 | Internal Software Error Classification | Differentiate Python syntax/runtime errors (`NameError`, `TypeError`, etc.) from external API failures in `_handle_loop_exception` | M1 | Survey 1 (R1) |
| 3 | Full Traceback Logging | Include `exc_info=True` in `_handle_loop_exception` to preserve error tracebacks in logs | M1 | Survey 1 (R1) |
| 4 | Defensive Timestamp Extraction | Guard `pd.to_numeric` against missing globals or malformed series | M1 | Survey 1 (R1) |
| 5 | Exchange MarketData Retry & Backoff | Add retry with exponential backoff and jitter for Hyperliquid `/info` calls in `exchange.py` | M2 | Survey 2 (R2) |
| 6 | Defensive Wrap in `_idle_cycle` | Wrap market data calls (lines 525-528) to prevent transient network drops from aborting idle loop | M2 | Survey 2 (R2) |
| 7 | LLM Relay Timeout Decoupling | Suppress panic alert on `AI_macro` failure during flat position; fall back safely to `quant_state` | M2 | Survey 2 (R2) |
| 8 | LLM Cold-Start Cooldown | Prevent repeated immediate relay calls on cold start when relay is failing | M2 | Survey 2 (R2) |
| 9 | Alert Throttling & Deduplication | Enforce milestone/time-based alert throttling (max 1 per 30 min) in `_handle_loop_exception` | M2 | Survey 2 (R2) |
| 10 | Idle Error Counter Hygiene | Reset `idle_api_error_count` on clean idle iterations including between-candle polling | M2 | Survey 2 (R2) |
| 11 | Unambiguous Alert Categorization | Formulate distinct headers: `[交易所行情异常]`, `[交易所下单执行异常]`, `[AI中转站异常]`, `[交易程序运行时异常]` | M2 | Survey 2 (R2) |
| 12 | 6-Phase Handover Protocol | Automated standby preparation, freeze active, pull state, push state, verify health, decommission | M3 | Survey 3 (R3) |
| 13 | Rotation Orchestrator Script | Standalone Python script (`scripts/rotator.py`) managing 6-hour proactive rotation ring | M3 | Survey 3 (R3) |
| 14 | Free Sandbox Provisioning Script | Shell script (`scripts/deploy_sandbox.sh`) preparing dependencies on clean cloud instances | M3 | Survey 3 (R3) |
| 15 | GitHub Actions Automation | Workflow (`.github/workflows/rotation.yml`) executing scheduled handovers with zero host cost | M3 | Survey 3 (R3) |
| 16 | Complete 24/7 Deployment Guide | Detailed documentation (`DEPLOYMENT_24_7.md`) covering credentials, rotation, and failover | M3 | Survey 3 (R3) |
| 17 | E2E Regression & Stability Suite | 4-tier opaque-box test suite verifying dry-run, alert separation, retry, and handover | E2E | Survey 1/2/3 |
| 18 | Render Cloud 24/7 Deployment | Render Web Service configuration (`Dockerfile`, `render.yaml`, `scripts/deploy_render.py`, UptimeRobot keepalive probe setup) | M3 | User Follow-up 1/3 |
| 19 | Dashboard Inspection & Diagnostics | Diagnose all display, chart rendering, API endpoint, and interaction bugs in `dashboard.py` | M5 | User Follow-up 1 |
| 20 | Dashboard Bug Fixes & Hardening | Fix 5 core bugs: 5-tab navigation bar, modal button text, ECharts local/CDN fallback, timeframe data filter, text unescape, sticky header | M5 | User Follow-up 2 |
| 21 | Daily PnL Rollover & Date Sync | Reconstruct natural day rollover in `risk_manager.py` / `main.py` (reset daily PnL to 0 and advance date) | M1 | User Follow-up 3 |
| 22 | Trade Metrics Reconciliation | Auto-reconcile win rate, streak, and trade count from real trade fills into `risk_state_aggressive.json` | M1 | User Follow-up 3 |
| 23 | Dynamic Radar Scores Refresh | Fix frozen 45.0 scores in `multi_coin_router.py` / `main.py`; refresh dynamically with 5m candles and live prices | M1 | User Follow-up 3 |
| 24 | Regular Equity History Sampling | Periodic time-series persistence (each heartbeat/hour) so equity curve is dense and 5m/1h/1d switching works | M1 | User Follow-up 3 |
| 25 | ETH ATR Calculation Calibration | Calibrate ATR calculation / timeframe binding in `utils.py` / `strategy.py` to eliminate 213.72 distortion | M1 | User Follow-up 3 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Core Bot Runtime Stability & State Reconciliation | Commit `pd` import, internal error classification, full traceback, daily rollover, trade reconciliation, radar score refresh, dense equity sampling, ATR calibration | none | IN_PROGRESS |
| M2 | API Alert Decoupling & Disambiguation | Exchange retry/backoff, LLM timeout suppression & fallback, alert throttling, clear alert tags | M1 | PLANNED |
| M3 | 24/7 Free Cloud Deployment & Rotation | Render Web Service (Dockerfile, render.yaml, deploy_render.py, UptimeRobot probe), Instabox rotator, handover protocol, deploy guide | none | IN_PROGRESS |
| M5 | Frontend Dashboard Diagnosis & Hardening | 5-tab navigation bar, modal button labels, ECharts local/CDN fallback, timeframe data filter, text unescape, sticky bar, align with calibrated metrics | none | IN_PROGRESS |
| M4 | Final E2E Test Pass & Coverage Hardening | Run 100% of E2E test suite (Tiers 1-4) published in `TEST_READY.md`, adversarial verification | M1, M2, M3, M5, E2E | PLANNED |

## Interface Contracts

### Error Handling & Loop Resilience (`main.py` ↔ `alerts.py`)
- `_handle_loop_exception(e: Exception)`:
  - If `isinstance(e, (NameError, TypeError, AttributeError, SyntaxError, IndexError, KeyError, ZeroDivisionError))`:
    - Dispatches with category tag `[交易程序运行时异常]`
    - Logs with `logger.critical(..., exc_info=True)`
  - If `isinstance(e, (MarketDataError, requests.RequestException, httpx.HTTPError))`:
    - Dispatches with category tag `[交易所行情异常]`
    - Applies throttled alerting (first alert at threshold, subsequent alerts $\ge 1800$s apart)
  - If `isinstance(e, ExecutionError)`:
    - Dispatches with category tag `[交易所下单执行异常]` (HIGH priority)

### Risk Engine Day Rollover & Sampling (`risk_manager.py` ↔ `main.py`)
- `check_daily_reset(current_equity, current_date_utc)`:
  - If `current_date_utc > self.daily_loss_date`:
    - Resets `daily_loss_date = current_date_utc`, `daily_start_equity = current_equity`, `daily_loss_pnl = 0.0`.
- `record_periodic_equity_snapshot(current_equity, timestamp)`:
  - Appends to `equity_history_risk_state_aggressive.json` at minimum every 1h or on each candle closure.

### Dynamic Radar Scores (`multi_coin_router.py` ↔ `main.py`)
- `update_radar_scores(market_data_dict)`:
  - Computes TRS, RSI, ATR, and momentum for all candidate coins (`BTC`, `ETH`, `SUI`, `ARB`) on each 5m candle.
  - Updates `state.json:radar_scores` dynamically with varying scores, not static 45.0.

### Technical Indicator Calibration (`utils.py` ↔ `strategy.py`)
- `rma_atr(df, period=14)`:
  - Verified on 5m candles: typical ETH 5m ATR is ~1.5 to ~8.0 USDT (0.05% - 0.35%), not 213.72 USDT.

### Dashboard Web Interface (`dashboard.py` ↔ State & Control Files)
- Reads `state.json`, `risk_state_aggressive.json`, `equity_history_risk_state_aggressive.json`, `trading_bot.log`.
- Serves HTTP routes `/`, `/api/state`, `/api/history`, `/api/control`, `/api/logs`.
- Filters history data based on query parameter `?tf=5m|1h|1d|all`.

## Code Layout
- Exclusive File Ownership:
  - Milestone 1 (M1): `main.py`, `risk_manager.py`, `multi_coin_router.py`, `utils.py`
  - Milestone 2 (M2): `exchange.py` (market data retries), `strategy.py` (macro AI fallback), `alerts.py` (alert categories & throttling), `main.py` (integration in `_idle_cycle`)
  - Milestone 3 (M3): `Dockerfile`, `render.yaml`, `scripts/deploy_render.py`, `scripts/rotator.py`, `scripts/deploy_sandbox.sh`, `.github/workflows/rotation.yml`, `DEPLOYMENT_24_7.md`
  - Milestone 5 (M5): `dashboard.py`
  - E2E Testing Track: `tests/e2e/`, `TEST_INFRA.md`, `TEST_READY.md`

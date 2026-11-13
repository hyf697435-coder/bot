# Scope: Milestone 1 - Fix Trading Bot Runtime Errors & State Distortion Calibration

## Architecture
- Modules: `main.py`, `risk_manager.py`, `multi_coin_router.py`, `utils.py`, `strategy.py`
- Roles:
  - `main.py`: Core Execution Engine, FSM Controller, Loop Exception Handler, Signal Pipeline
  - `risk_manager.py`: Natural Day Rollover, Daily PnL Synchronization, Trade Metrics Reconciliation, Equity History Sampling
  - `multi_coin_router.py`: Dynamic Multi-Coin Radar Scoring & Dynamic Rotation
  - `utils.py` / `strategy.py`: ATR Calculation Calibration
- State Files: `state.json`, `risk_state_aggressive.json`, `equity_history_risk_state_aggressive.json`

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Commit `pd` import in `main.py` | Ensure `import pandas as pd` is permanently committed at module level | M1 | Survey 1 (R1) |
| 2 | Internal Software Error Classification | Differentiate Python syntax/runtime errors (`NameError`, `TypeError`, etc.) from external API failures in `_handle_loop_exception` | M1 | Survey 1 (R1) |
| 3 | Full Traceback Logging | Include `exc_info=True` in `_handle_loop_exception` to preserve error tracebacks in logs | M1 | Survey 1 (R1) |
| 4 | Defensive Timestamp Extraction | Guard `pd.to_numeric` against missing globals or malformed series in `_idle_cycle` | M1 | Survey 1 (R1) |
| 21 | Natural Day Rollover & Daily PnL Sync | Reset `daily_loss_date = now_utc.date()`, `daily_start_equity = current_equity`, `daily_loss_pnl = 0.0` when UTC day rolls over | M1 | User Request 03:25Z |
| 22 | Trade Metrics & Win Rate Reconciliation | Reconcile `metrics` (`total_trades`, `win_rate`, `streak_count`) with actual trade history/fills | M1 | User Request 03:25Z |
| 23 | Dynamic Multi-Coin Radar Scores | Dynamic radar scores with live prices and 5m K-lines on each cycle in `multi_coin_router.py` / `main.py` | M1 | User Request 03:25Z |
| 24 | Regular Equity History Sampling | Periodic hourly/candle sampling in `equity_history_risk_state_aggressive.json` for continuous equity curve | M1 | User Request 03:25Z |
| 25 | Calibrate ETH ATR Calculation | Calibrate 5m ATR calculation in `utils.py` / `strategy.py` to realistic range (~1.5 to ~8.0 USDC) | M1 | User Request 03:25Z |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Fix Trading Bot Runtime Errors & State Calibration | Features 1-4 (runtime fixes in `main.py`) + Features 21-25 (state & data distortions) | none | IN_PROGRESS |

## Interface Contracts
### Error Handling & Loop Resilience (`main.py` ↔ `alerts.py`)
- `_handle_loop_exception(e: Exception)`:
  - If `isinstance(e, (NameError, TypeError, AttributeError, SyntaxError, IndexError, KeyError, ZeroDivisionError, ImportError, UnboundLocalError))`:
    - Dispatches with category tag `[交易程序运行时异常]`
    - Logs with `logger.critical(..., exc_info=True)`
    - Does NOT increment `idle_api_error_count` or `api_error_count`
  - If `isinstance(e, (MarketDataError, requests.RequestException, httpx.HTTPError))`:
    - Dispatches with category tag `[交易所行情异常]`
  - If `isinstance(e, ExecutionError)`:
    - Dispatches with category tag `[交易所下单执行异常]`

### Risk State & Rollover Contract (`main.py` ↔ `risk_manager.py`)
- `check_daily_loss_limit(current_equity)`:
  - If `now_utc.date() > daily_loss_date`:
    - Updates `daily_loss_date = now_utc.strftime('%Y-%m-%d')`
    - Sets `daily_start_equity = current_equity`
    - Resets `daily_loss_pnl = 0.0`
    - Persists state immediately
- `sample_equity_history(current_equity, timestamp)`:
  - Appends `[timestamp, equity]` at regular intervals (hourly or on candle close), ensuring monotonic timestamps and avoiding duplicates.

### ATR Calibration Contract (`utils.py` ↔ `strategy.py`)
- 5m ATR must reflect true candle range in price units (e.g., $1.50 - $8.00 for ETH @ ~$2000-$3000), not multiplied inappropriately by 100 or using wrong period.

## Code Layout
- Exclusive File Ownership for M1:
  - `main.py`
  - `risk_manager.py`
  - `multi_coin_router.py`
  - `utils.py`
  - `strategy.py`
  - State JSON files (`state.json`, `risk_state_aggressive.json`, `equity_history_risk_state_aggressive.json`)
  - Test files: `tests/test_m1_runtime_fixes.py`, `tests/test_m1_state_calibrations.py`

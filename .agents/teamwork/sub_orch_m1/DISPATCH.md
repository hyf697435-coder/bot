# DISPATCH Log

## 2026-11-13T03:07:49Z
**Identity & Workspace**:
You are a sub-orchestrator for Milestone 1.
Working directory: C:\Users\Administrator\Desktop\v1\a
State folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m1
Parent conversation ID: d6979a11-ca31-400e-b3a7-34010c28c0cd (Send reports and updates back to this parent)
Original Request Path: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
Project File: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
Survey 1 Report: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_survey_1\handoff.md

**Your Scope**:
Milestone 1 (M1): Fix Trading Bot Runtime Errors
- Feature 1: Commit `import pandas as pd` at the module level in `main.py`.
- Feature 2: Internal Software Error Classification: Differentiate local Python software errors (`NameError`, `TypeError`, `KeyError`, etc.) from external API failures in `_handle_loop_exception` in `main.py`.
- Feature 3: Full Traceback Logging: Include `exc_info=True` in `_handle_loop_exception` to ensure tracebacks are recorded.
- Feature 4: Defensive Timestamp Extraction in `_idle_cycle` in `main.py`.
- File ownership: You exclusively own `main.py`.

**Procedure**:
Follow the Project Pattern sub-orchestrator procedure:
1. Initialize your state folder (`C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m1`), create `SCOPE.md` and `BRIEFING.md`.
2. Execute the iteration loop (2B):
   - Spawn Explorer(s) (`teamwork_explorer`) to plan exact edits based on Survey 1 report.
   - Spawn Worker (`teamwork_worker`) to implement edits in `main.py` and run build/test commands (`py_compile`, unit tests, simulation dry-run). Include the mandatory integrity warning.
   - Spawn Reviewers (`teamwork_reviewer`) to verify correctness, syntax, and interface conformance.
   - Spawn Challengers (`teamwork_challenger`) to stress test edge cases.
   - Spawn Forensic Auditor (`teamwork_auditor`) to verify implementation integrity.
   - Gate verdict in `GATE_STATUS.md`.
3. When Gate passes, write `handoff.md` and send a completion message with full details to the parent orchestrator.


## 2026-11-13T03:25:32Z
**Context**: Core Data Model & Risk State Distortion Fixes for Milestone 1
**Content**: The user has reported 5 critical underlying data distortions affecting the bot's state and calculations:
1. **Natural Day Rollover & Daily PnL Synchronization**:
   - Hyperliquid on-chain balance is 858.67 USDC (flat position).
   - `risk_state_aggressive.json` is stuck on 2026-11-06 with distorted `daily_pnl = -296.39`.
   - Implement robust UTC date rollover in `risk_manager.py` / `main.py`: when `now_utc.date() > daily_loss_date`, reset `daily_loss_date = now_utc.date()`, `daily_start_equity = current_equity`, `daily_loss_pnl = 0.0`.
2. **Trade Metrics & Win Rate Reconciliation**:
   - Win rate is showing 0% (18 losses in a row) stuck on Nov 1. Reconcile `metrics` (`total_trades`, `win_rate`, `streak_count`) with actual trade history/fills.
3. **Dynamic Multi-Coin Radar Scores**:
   - Radar scores in `state.json` are frozen at 45.0 across ETH, SUI, ARB. Ensure `multi_coin_router.py` / `main.py` recalculates dynamic radar scores with live prices and 5m K-lines on each cycle.
4. **Regular Equity History Sampling**:
   - `equity_history_risk_state_aggressive.json` only has 15 points across 20 days. Implement periodic sampling (e.g. record a point every hour or each signal candle) so the equity curve is dense and continuous.
5. **Calibrate ETH ATR Calculation**:
   - Recorded 5m ATR is distorted at 213.72 (>11%). Calibrate ATR period/calculation in `utils.py` / `strategy.py` to reflect realistic ETH 5m ATR (~1.5 to ~8.0 USDC).

**Action**: Implement Features 21-25 alongside the existing runtime error fixes in `main.py`, `risk_manager.py`, `multi_coin_router.py`, and `utils.py`.

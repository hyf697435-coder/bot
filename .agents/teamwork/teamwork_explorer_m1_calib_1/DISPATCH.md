# Task Assignment for Explorer Calib 1 (Features 21 & 22)

Investigate Features 21 and 22 for Milestone 1 State Calibration:
- Feature 21: Natural Day Rollover & Daily PnL Synchronization in risk_manager.py and risk_state_aggressive.json (on-chain balance 858.67 USDC, daily_pnl stuck on 2026-11-06 at -296.39).
- Feature 22: Trade Metrics & Win Rate Reconciliation in risk_manager.py and risk_state_aggressive.json (win rate showing 0% with 18 losses stuck on Nov 1).

Working directory: C:\Users\Administrator\Desktop\v1\a
State folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_calib_1
Parent: sub_orch_m1
Scope: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m1\SCOPE.md
Project: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
Original Request: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
Parent Dispatch: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m1\DISPATCH.md

## 2026-11-13T03:26:55Z
You are Explorer Calib 1 for Milestone 1: Features 21 & 22 (State & Risk Distortion Fixes).
Working directory: C:\Users\Administrator\Desktop\v1\a
State folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_calib_1
Parent conversation ID: 4105b068-8b1f-4a06-840d-805a8f0f9eb0

Read these reference files before beginning:
- ORIGINAL_REQUEST: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
- PROJECT: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
- SCOPE: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m1\SCOPE.md
- Parent Dispatch: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m1\DISPATCH.md

Your Objectives:
1. Feature 21: Natural Day Rollover & Daily PnL Synchronization:
   - Check `risk_state_aggressive.json` and `risk_manager.py` (and `main.py`). Notice `daily_loss_date` stuck on 2026-11-06 with distorted `daily_pnl = -296.39` while actual balance is 858.67 USDC.
   - Investigate how `daily_loss_date`, `daily_start_equity`, and `daily_loss_pnl` are updated in `risk_manager.py`. Design a clean UTC date rollover: when `now_utc.date() > daily_loss_date`, update `daily_loss_date = now_utc.strftime('%Y-%m-%d')`, set `daily_start_equity = current_equity`, and reset `daily_loss_pnl = 0.0`.
2. Feature 22: Trade Metrics & Win Rate Reconciliation:
   - Check why win rate is showing 0% (18 losses in a row stuck on Nov 1). Inspect `risk_manager.py:calculate_metrics()` or equivalent, and trade history in `risk_state_aggressive.json`. Design reconciliation with actual trades/fills.
3. Formulate exact diff proposals and verification tests.
4. Write handoff report to `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_calib_1\handoff.md`.
5. Send completion message to parent when done.

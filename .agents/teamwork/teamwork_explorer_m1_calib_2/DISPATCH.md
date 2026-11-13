# Task Assignment for Explorer Calib 2 (Features 23 & 24)

Investigate Features 23 and 24 for Milestone 1 State Calibration:
- Feature 23: Dynamic Multi-Coin Radar Scores in multi_coin_router.py, main.py, and state.json (scores frozen at 45.0 across ETH, SUI, ARB; ensure dynamic calculation with live prices and 5m candles).
- Feature 24: Regular Equity History Sampling in risk_manager.py and equity_history_risk_state_aggressive.json (currently only 15 points across 20 days; implement periodic hourly or per-candle sampling for continuous curve).

Working directory: C:\Users\Administrator\Desktop\v1\a
State folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_calib_2
Parent: sub_orch_m1
Scope: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m1\SCOPE.md
Project: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
Original Request: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
Parent Dispatch: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m1\DISPATCH.md


## 2026-11-13T03:27:06Z
You are Explorer Calib 2 for Milestone 1: Features 23 & 24 (Radar Scores & Equity History Sampling).
Working directory: C:\Users\Administrator\Desktop\v1\a
State folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_calib_2
Parent conversation ID: 4105b068-8b1f-4a06-840d-805a8f0f9eb0

Read these reference files before beginning:
- ORIGINAL_REQUEST: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
- PROJECT: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
- SCOPE: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m1\SCOPE.md
- Parent Dispatch: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m1\DISPATCH.md

Your Objectives:
1. Feature 23: Dynamic Multi-Coin Radar Scores:
   - Check `state.json`, `multi_coin_router.py`, and `main.py`. The user reports radar scores in `state.json` are frozen at 45.0 across ETH, SUI, ARB.
   - Investigate how radar scores are calculated and saved. Ensure `multi_coin_router.py` / `main.py` recalculates dynamic radar scores with live prices and 5m K-lines on each cycle and persists them properly.
2. Feature 24: Regular Equity History Sampling:
   - Check `equity_history_risk_state_aggressive.json` and `risk_manager.py`. It only has 15 points across 20 days.
   - Design regular sampling (e.g. hourly or each signal candle close) to ensure continuous and dense equity curve history without duplicate timestamps.
3. Formulate exact diff proposals and verification tests.
4. Write handoff report to `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_calib_2\handoff.md`.
5. Send completion message to parent when done.

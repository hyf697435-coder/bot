# Progress — Explorer Calib 2 (Features 23 & 24)

- **Status**: IN_PROGRESS (Investigation & Diff Formulation)
- **Last visited**: 2026-11-13T03:37:15Z
- **Current task**: Formulating exact diff proposals for Features 23 & 24 and unit verification tests.

## Completed Milestones
1. Read reference files (`ORIGINAL_REQUEST.md`, `PROJECT.md`, `SCOPE.md`, `DISPATCH.md`).
2. Deep dive into Feature 23 (Radar Scores frozen at 45.0):
   - Investigated `multi_coin_router.py`, `main.py`, `strategy.py`, and `state.json`.
   - Identified root causes:
     - Mathematical origin of 45.0: Base score 50 + rr_bonus 10 - counter_trend macro_bias 15 = 45.0 for oversold conditions under `LEAN_SHORT`.
     - Frozen state causes:
       a) Gate-locking in `_idle_cycle`: scans only once per 5m candle close node, ignoring cycles in between.
       b) Completely omitted during `STATE_TRADING`, `COOL_DOWN`, and `HALT`: `scan_all_coins` was NEVER called while holding a trade or cooling down.
       c) State persistence throttle: `_persist_state` only saved when ETH's candle timestamp changed.
       d) Mock tests historically overwrote `state.json` with static 2250.0 mock market data.
3. Deep dive into Feature 24 (Equity History Sampling):
   - Investigated `equity_history_risk_state_aggressive.json` and `risk_manager.py`.
   - Identified root causes:
     - `sync_equity` and `sample_equity_history` were NEVER called from `main.py` core loops.
     - Equity points were only saved on closed trades via `record_trade()`, resulting in only 17 points on Nov 6 and zero points during 7 days of flat idle.
     - `dashboard.py` reads `equity_history.json` while `risk_manager.py` writes to `equity_history_risk_state_aggressive.json`.
4. Designing diff proposals:
   - For `multi_coin_router.py`: Robust dynamic scan with fallback live price extraction, defensive dictionary persistence, and clean neutral reset.
   - For `main.py`: Periodic multi-coin radar and price refresh during both `_idle_cycle` and `_trading_cycle` with proper persistence to `state.json`.
   - For `risk_manager.py`: Implement `sample_equity_history(current_equity, timestamp)` adhering to the Interface Contract, with deduplication, monotonic enforcement, and dual persistence.
   - Historical backfill script to bridge the 20-day flat period up to 2026-11-13.

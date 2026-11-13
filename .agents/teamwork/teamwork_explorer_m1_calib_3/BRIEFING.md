# BRIEFING — 2026-11-13T03:36:45Z

## Mission
Investigate and calibrate ETH ATR calculation (Feature 25), resolving distorted 5m ATR (213.72, >11% of price) to accurately reflect true 5m candle range (~1.5 to ~8.0 USDC).

## 🔒 My Identity
- Archetype: explorer
- Roles: Teamwork explorer
- Working directory: C:\Users\Administrator\Desktop\v1\a
- State folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_m1_calib_3
- Original parent: 4105b068-8b1f-4a06-840d-805a8f0f9eb0
- Milestone: Milestone 1: Feature 25 (Calibrate ETH ATR Calculation)

## 🔒 Key Constraints
- Read-only investigation — do NOT implement directly into codebase source files
- Formulate exact diff proposals and verification tests
- All findings written to state folder files, and coordinate via messages

## Current Parent
- Conversation ID: 4105b068-8b1f-4a06-840d-805a8f0f9eb0
- Updated: not yet

## Investigation State
- **Explored paths**:
  - `utils.py` (Wilder RMA calculation, atr, atr_series)
  - `strategy.py` (_raw_sl_tp, calculate_position_size, update_position_fsm)
  - `main.py` (_idle_cycle, _switch_coin, _register_closed_position, _load_state, _refresh_hard_stop)
  - `multi_coin_router.py` (scan_all_coins, radar_scores)
  - `dashboard.py` (status ATR retrieval)
  - `state.json` (cached_atr_5m = 213.72628640898196)
  - `risk_state_aggressive.json` (recent_trades BTC entry at 76143.2)
  - Live Hyperliquid API tests on ETH vs BTC 5m ATR
- **Key findings**:
  1. The 213.72 ATR is NOT a math error in `utils.atr`. It is the exact 5m ATR of Bitcoin ($76k * 0.28% = 213.72).
  2. The bot traded BTC on 2026-11-06, populating `self.cached_atr_5m = 213.72`.
  3. `_register_closed_position` did not clear `cached_atr_5m`.
  4. `_idle_cycle` calculates `atr_14` for `self.coin` but never assigned it to `self.cached_atr_5m`.
  5. `_switch_coin` did not reset `cached_atr_5m`.
  6. `_load_state` blindly accepted 213.72 for ETH without sanity checking.
  7. `state.json` had `"coin": "ETH"` with `"cached_atr_5m": 213.72`, which `dashboard.py` displayed (>11% of ETH price $1927).
  8. Missing interface contracts `utils.rma_atr`, `utils.wilder_rma_atr`, and `strategy.calc_atr` with DataFrame support and sanity bounds.
- **Unexplored areas**: None, root cause and calibration chain fully proven.

## Key Decisions Made
- Formulate complete diffs for `utils.py`, `strategy.py`, `main.py`, `multi_coin_router.py`, `dashboard.py`, `state.json`, and regression tests for `tests/test_m1_state_calibrations.py`.

## Artifact Index
- DISPATCH.md — Parent task dispatch
- BRIEFING.md — Persistent context & state
- progress.md — Liveness heartbeat
- handoff.md — 5-component handoff report

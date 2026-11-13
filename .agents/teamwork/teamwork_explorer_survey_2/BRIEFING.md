# BRIEFING — 2026-11-13T03:05:00Z

## Mission
Investigate API alert mechanisms, flat position alert loops, and separation between Exchange API vs LLM relay API ("中转站") calls (R2 Focus).

## 🔒 My Identity
- Archetype: teamwork_explorer
- Roles: investigation, synthesis
- Working directory: C:\Users\Administrator\Desktop\v1\a
- State folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_survey_2
- Original parent: d6979a11-ca31-400e-b3a7-34010c28c0cd
- Milestone: survey_2_api_alert_investigation

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Do NOT modify any source code files
- Deliver findings in handoff.md and send_message to orchestrator

## Current Parent
- Conversation ID: d6979a11-ca31-400e-b3a7-34010c28c0cd
- Updated: not yet

## Investigation State
- **Explored paths**:
  - `C:\Users\Administrator\Desktop\v1\a\main.py` (lines 85-110, 430-850, 990-1150, 1460-1530)
  - `C:\Users\Administrator\Desktop\v1\a\exchange.py` (lines 180-410, 480-680)
  - `C:\Users\Administrator\Desktop\v1\a\strategy.py` (lines 90-240, 330-390, 630-680)
  - `C:\Users\Administrator\Desktop\v1\a\alerts.py` (lines 1-391)
  - `C:\Users\Administrator\Desktop\v1\a\multi_coin_router.py` (lines 220-265, 410-455)
  - `C:\Users\Administrator\Desktop\v1\a\.env` (API keys, endpoints, thresholds)
  - `C:\Users\Administrator\Desktop\v1\log\trading_bot.log.2026-W45` (1.9MB log file with historical exception evidence)
- **Key findings**:
  1. The code triggering the alert is `main.py:1473-1476` in `_handle_loop_exception`. `if self.idle_api_error_count >= IDLE_ALERT_THRESHOLD (5): send_alert(...)`. Because there is NO alert throttling, cooldown, or deduplication, an email is sent on EVERY loop iteration once count reaches 5 (iterations 5 through 32 triggered 28 emails).
  2. The counter `self.idle_api_error_count` is only reset inside `_reset_idle_counters()` during deep signal evaluation branches. It is NOT reset during normal 5s reconcile cycles between signal nodes (line 500 returns without reset), keeping the error count dirty.
  3. The 32 consecutive exceptions in the log were caused by Exchange REST API failures on `https://api.hyperliquid-testnet.xyz/info` (`[SSL: UNEXPECTED_EOF_WHILE_READING]`, `The read operation timed out`, proxy connection aborts), because `_idle_cycle()` lines 525-528 call `get_closed_ohlcv`, `get_ohlcv`, and `get_funding_stats` completely unwrapped by `try-except`, and `MarketData` in `exchange.py` has ZERO retries and ZERO exponential backoff.
  4. LLM relay API (`https://api.ludies.best/v1`) calls in `strategy.py` (`AI_macro` and `AI_Trade`) also have serious alert flaws: `fetch_ai_macro_state` catches `AIError` but calls `send_alert()` directly during flat position. Because `cached_ai_macro_state` is not populated on failure, the `cold_start` condition remains True, repeating AI calls and alert emails every 5 minutes.
  5. The alert message `"空仓期 API 连续异常 {count} 次，请人工检查网络/AI 通道。"` is completely ambiguous, conflating Exchange REST errors and LLM Relay timeouts and giving zero actionable diagnostic details.
- **Unexplored areas**: None for R2 scope.

## Key Decisions Made
- Completed deep dive across codebase and historical logs.
- Synthesized decoupling, suppression, retry/backoff, and disambiguation recommendations into handoff.md.

## Artifact Index
- DISPATCH.md — Initial dispatch message
- progress.md — Liveness heartbeat
- handoff.md — Final investigation report

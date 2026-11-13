# BRIEFING — 2026-11-13T03:02:30Z

## Mission
Investigate and design the Free Cloud Server 24/7 Rotation Scheme (R3) for the trading bot, including state persistence, instabox free tier capabilities, automated rotation architecture, and deployment procedures.

## 🔒 My Identity
- Archetype: teamwork_explorer
- Roles: investigator, synthesizer
- Working directory: C:\Users\Administrator\Desktop\v1\a
- State folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_survey_3
- Original parent: d6979a11-ca31-400e-b3a7-34010c28c0cd
- Milestone: Free Cloud Server 24/7 Rotation Scheme (R3 Focus)

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Modify only files within own state folder (.agents/teamwork/teamwork_explorer_survey_3)

## Current Parent
- Conversation ID: d6979a11-ca31-400e-b3a7-34010c28c0cd
- Updated: 2026-11-13T02:55:47Z

## Investigation State
- **Explored paths**:
  - Trading bot state persistence: `main.py` (`_persist_state`, `_load_state`), `risk_manager.py` (`_save_state`, `_append_equity_history`), `exchange.py` (`current_position`, `adopt_open_position`), `watchdog.py`, `alerts.py`.
  - State files: `state.json`, `risk_state_aggressive.json`, `equity_history_risk_state_aggressive.json`, `.heartbeat`, `control.json`, `.env`.
  - Free cloud tier: `instabox` CLI tested on Windows, `instabox whoami`, `usage`, `ls`, `get bot`. Examined SKILL.md.
- **Key findings**:
  1. Instabox Free Tier has `maxMachines: 1` per account, 4 vCPU / 8 GiB RAM / 20 GiB persistent `/data`, `maxRunMinutes: 480` (8h), and enforced cooldown (60-135 min) if it hits max run.
  2. Single free account cannot achieve 24/7 alone. Requires multi-account ring (e.g. 2 accounts running 6h shifts, resting 6h each to clear cooldown) or multi-provider ring.
  3. Trading bot state is atomically persisted (`os.replace`). Key files to sync: `state.json`, `risk_state_aggressive.json`, `equity_history_risk_state_aggressive.json`.
  4. On-chain orders on Hyperliquid protect open positions during the brief migration window; `main.py` adopts legacy positions smoothly upon restart.
- **Unexplored areas**: None. Ready for synthesis and handoff report creation.

## Key Decisions Made
- Finalized 2-account/3-account 6-hour shift rotation architecture with pessimistic handover protocol.
- Finalized design of Python daemon and GitHub Actions controller options.

## Artifact Index
- DISPATCH.md — incoming dispatch log
- BRIEFING.md — working memory and identity
- progress.md — liveness heartbeat
- handoff.md — final comprehensive report (in progress)

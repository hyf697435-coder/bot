# BRIEFING — 2026-11-13T03:18:00Z

## Mission
Analyze and provide complete technical architecture and design specifications for scripts/deploy_sandbox.sh and .github/workflows/rotation.yml (Features 14 & 15 of Milestone 3: 24/7 Rotation).

## 🔒 My Identity
- Archetype: explorer
- Roles: investigator, architect, specification author
- Working directory: C:\Users\Administrator\Desktop\v1\a
- State folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_2
- Original parent: 5319eb0f-eb30-49fe-b186-6e606f1e87fc
- Milestone: Milestone 3 (Free Cloud Server 24/7 Rotation Scheme)

## 🔒 Key Constraints
- Read-only investigation — do NOT modify production files
- Write only to state folder (.agents/teamwork/explorer_m3_2)
- Produce comprehensive handoff.md following 5-component protocol
- Use send_message when complete

## Current Parent
- Conversation ID: 5319eb0f-eb30-49fe-b186-6e606f1e87fc
- Updated: not yet

## Investigation State
- **Explored paths**:
  - `ORIGINAL_REQUEST.md`, `PROJECT.md`, `sub_orch_m3/SCOPE.md`, `survey_3/handoff.md`
  - Codebase imports across all 12 .py files in project root
  - `.env` configuration keys and alerts configuration
  - `instabox` CLI options, version `0.8.208`, quotas, exit codes, and execution flags
  - Coordinates with `explorer_m3_1` (`rotator.py`) and `explorer_m3_3` (`DEPLOYMENT_24_7.md`)
- **Key findings**:
  - Instabox free sandbox has 480 min max continuous run limit and 1 concurrent machine per account
  - Proactive 6-hour shift (360 min) prevents max-run penalties
  - Instabox persistent disk `/data` is at `/data/home/trading-bot`
  - Python dependencies: core (numpy, pandas, httpx, requests, python-dotenv, eth-account, hyperliquid-python-sdk) and extended (ccxt, aiohttp, rich, pydantic)
  - GitHub Actions cron jitter is comfortably absorbed by 120-minute safety buffer
  - Concurrency `cancel-in-progress: false` is strictly required to prevent split-brain state corruption
- **Unexplored areas**: None. Ready for complete specification synthesis.

## Key Decisions Made
- Structured `deploy_sandbox.sh` with 7 standard POSIX exit codes, self-contained Python validation, masked secret logging, and optional dependency handling
- Designed `.github/workflows/rotation.yml` with dual-account secret binding, `cancel-in-progress: false`, rich GitHub Step Summary, and instant Telegram/Webhook failure alerting

## Artifact Index
- DISPATCH.md — incoming instructions log
- BRIEFING.md — persistent working memory
- progress.md — liveness heartbeat
- handoff.md — final analysis and specifications

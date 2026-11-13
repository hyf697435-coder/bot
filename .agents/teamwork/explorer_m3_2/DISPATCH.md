# Explorer M3-2 Dispatch
Assigned task: Technical specification & detailed architecture design for `scripts/deploy_sandbox.sh` and `.github/workflows/rotation.yml`.


## 2026-11-13T03:09:20Z
You are Explorer 2 for Milestone 3 (Free Cloud Server 24/7 Rotation Scheme).

**Working Directory**: C:\Users\Administrator\Desktop\v1\a
**State Folder**: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_2

**Mandatory Inputs (Read before starting)**:
- Original Request: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
- Project Scope: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
- Milestone Scope: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_m3\SCOPE.md
- Survey 3 Report: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_survey_3\handoff.md

**Your Focus Area**:
Analyze and provide complete technical architecture and design specifications for `scripts/deploy_sandbox.sh` and `.github/workflows/rotation.yml` (Feature 14 & Feature 15):
1. `scripts/deploy_sandbox.sh` design:
   - Clean instance provisioning on Linux (Instabox, GitHub Actions runner, Debian/Ubuntu container)
   - Python virtualenv or package installation (`pip install -r requirements.txt`)
   - Verification of critical imports (ccxt, aiohttp, rich, pydantic, etc.)
   - Environment variable validation (API keys, Telegram bot token, secrets)
   - Directory initialization (`state/`, `logs/`)
   - Exit codes and error logging formatting
2. `.github/workflows/rotation.yml` design:
   - Scheduled cron rotation every 6 hours (`0 */6 * * *`)
   - Manual trigger (`workflow_dispatch`) with inputs (`force_rotate`, `target_account`)
   - Dual-account orchestration logic: managing Account A and Account B secrets / tokens
   - Handover trigger step: invoking `rotator.py` or trigger command
   - Failure alerting (GitHub step summary, webhook/telegram alert on rotation failure)
   - Concurrency limits (`concurrency: rotation-lock`) to prevent overlapping workflows
3. Write a comprehensive report in `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\explorer_m3_2\handoff.md`.
Remember: You are read-only. Do not modify production files. Use send_message when done.

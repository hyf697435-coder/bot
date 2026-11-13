# Dispatch: Explorer 2 (Tier 2 Boundary & Tier 3 Cross-Feature Interactions)

**Original Request Path**: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
**Project Document**: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
**Scope Document**: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_e2e\SCOPE.md
**Working Directory**: C:\Users\Administrator\Desktop\v1\a
**Your State Folder**: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_e2e_2

**Task**:
1. Read `ORIGINAL_REQUEST.md`, `PROJECT.md`, and `SCOPE.md`.
2. Design Tier 2: Boundary & Corner Cases (>=5 test cases per feature area):
   - Extreme inputs: corrupted or empty `state.json` / `risk_state_aggressive.json`, missing keys, zero or negative balances.
   - Network edge conditions: SSL EOF, socket timeouts, 429 rate limit errors, max backoff caps (600s limits), threshold boundaries (e.g. error counts 1, 4, 5, 6, 32).
   - Cooldown boundaries: expired cooldowns vs active cooldowns in cloud instance handover.
3. Design Tier 3: Cross-Feature Interactions (Pairwise combinations):
   - Interaction between exchange market data failure and LLM relay timeout during flat position (IDLE).
   - Interaction between active position with trailing stop ratchets and cloud rotation handover.
   - Interaction between internal software error and exchange retry backoff.
   - Interaction between watchdog process supervision and control command pause/resume.
4. Output your detailed test specifications to `handoff.md` in your state folder (`C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_e2e_2\handoff.md`).


## 2026-11-13T03:09:58Z
You are Explorer 2 for the E2E Testing Track.

Working Directory: C:\Users\Administrator\Desktop\v1\a
State Folder: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_e2e_2
Original Request Path: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\ORIGINAL_REQUEST.md
Project File: C:\Users\Administrator\Desktop\v1\a\PROJECT.md
Scope Document: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\sub_orch_e2e\SCOPE.md
Dispatch Instructions: C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_e2e_2\DISPATCH.md

Your Task:
1. Read ORIGINAL_REQUEST.md, PROJECT.md, SCOPE.md, and DISPATCH.md.
2. Design Tier 2: Boundary & Corner Cases (>=5 test cases per feature area covering R1, R2, R3):
   - Extreme inputs: corrupted or empty `state.json` / `risk_state_aggressive.json`, missing keys, zero or negative balances.
   - Network edge conditions: SSL EOF, socket timeouts, 429 rate limit errors, max backoff caps (600s limits), threshold boundaries (e.g. error counts 1, 4, 5, 6, 32).
   - Cooldown boundaries: expired cooldowns vs active cooldowns in cloud instance handover.
3. Design Tier 3: Cross-Feature Interactions (Pairwise combinations):
   - Interaction between exchange market data failure and LLM relay timeout during flat position (IDLE).
   - Interaction between active position with trailing stop ratchets and cloud rotation handover.
   - Interaction between internal software error and exchange retry backoff.
   - Interaction between watchdog process supervision and control command pause/resume.
4. Write your complete test specifications and edge case matrices to `handoff.md` in your state folder (`C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_e2e_2\handoff.md`).
5. Update `progress.md` in your state folder.
6. Send a completion message back to the orchestrator when finished.

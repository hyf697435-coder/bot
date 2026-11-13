# Investigation & Survey Report: API Alert Loops and Service Decoupling (R2)

**Report Author**: Teamwork Explorer (Survey 2)  
**Date**: 2026-11-13  
**Target Project**: `C:\Users\Administrator\Desktop\v1\a`  
**State Folder**: `C:\Users\Administrator\Desktop\v1\a\.agents\teamwork\teamwork_explorer_survey_2`  
**Core Objective**: Investigate API alert mechanisms, flat position alert loops, root causes of repeated "空仓期 API 连续异常 32 次" alerts, and formulate decoupling and disambiguation architectures for Crypto Exchange vs LLM Relay APIs.

---

## 1. Observation

### 1.1 Exact Alert Counter and Trigger in Code
- **File**: `C:\Users\Administrator\Desktop\v1\a\main.py`
  - **Lines 105**:
    ```python
    IDLE_ALERT_THRESHOLD = _env_int("IDLE_ALERT_THRESHOLD", 5)    # 空仓期连续异常告警阈值
    ```
  - **Line 230**:
    ```python
    self.idle_api_error_count = 0
    ```
  - **Lines 1462-1481** (`_handle_loop_exception`):
    ```python
    def _handle_loop_exception(self, e: Exception):
        """核心循环容错：持仓期连续异常触发保护兜底，空仓期指数退避。"""
        self.api_error_count += 1
        logger.error("[异常] 核心循环错误 | 计数=%s/%s | err=%s",
                     self.api_error_count, TRADING_ALERT_THRESHOLD, e)

        if self.state == STATE_TRADING:
            if self.api_error_count >= TRADING_ALERT_THRESHOLD:
                self._deploy_emergency_hard_stop()
            self._safe_sleep(10, phase="TRADING_EXC_BACKOFF", step=1.0)
        elif self.state == STATE_IDLE:
            self.idle_api_error_count += 1
            if self.idle_api_error_count >= IDLE_ALERT_THRESHOLD:
                send_alert(
                    f"空仓期 API 连续异常 {self.idle_api_error_count} 次，请人工检查网络/AI 通道。")
            logger.warning("[巡检] 空仓异常退避 | wait=%ss", self.idle_backoff)
            self._safe_sleep(self.idle_backoff, phase="IDLE_BACKOFF")
            self.idle_backoff = min(self.idle_backoff * 2, 600)
        else:
            self._safe_sleep(10, phase="LOOP_EXC_BACKOFF", step=1.0)
    ```
  - **Lines 825-830** (`_reset_idle_counters`):
    ```python
    def _reset_idle_counters(self):
        """正常 IDLE 迭代结束时清零异常计数。"""
        self.api_error_count = 0
        self.idle_api_error_count = 0
        self.idle_backoff = 60
    ```

### 1.2 Mailer Queue and Email Delivery Mechanics
- **File**: `C:\Users\Administrator\Desktop\v1\a\alerts.py`
  - **Lines 194-206** (`send_alert`):
    ```python
    def send_alert(message: str) -> None:
        """系统级熔断 / 风控拦截 / 黑天鹅断网告警。"""
        logger.warning("[告警] %s", message)
        if notifier is not None and notifier.ready:
            try:
                notifier.send("[交易机器人] 系统告警", message)
            except Exception as e:
                logger.error("[告警] 邮件转发失败 | err=%s", e)
    ```
  - **Lines 276-285** (`Mailer.send`):
    ```python
    def send(self, subject: str, body: str) -> bool:
        """入队一封邮件，立即返回；未配置时静默忽略。"""
        if not self.ready:
            return False
        try:
            self._queue.put((subject, body))
            return True
        except Exception:
            return False
    ```
  - **Lines 245-275** (`Mailer._run` and `_deliver`): Worker thread dequeues tuples and delivers emails via `smtplib.SMTP_SSL` to `yiyun0525@qq.com` with NO rate limiting, deduplication, or cooldown.

### 1.3 Asymmetry Between Flat Position (IDLE) vs Active Position (TRADING)
- **File**: `C:\Users\Administrator\Desktop\v1\a\main.py`
  - **In Active Position (`_trading_cycle`, lines 992-1150)**:
    - Every network interaction is wrapped in local defensive `try...except`:
      - Line 1009-1013: `self.exchange.current_position()` wrapped; on error logs `warning` and continues.
      - Line 1022-1027: `self.exchange.mark_price()` wrapped; on error logs `warning` and skips tick.
      - Line 1032-1047: `self.market.get_closed_ohlcv("5m", 120)` wrapped; on error logs `warning` and retains cached ATR.
    - Line 1150: Explicitly resets `self.api_error_count = 0` on every clean iteration.
  - **In Flat Position (`_idle_cycle`, lines 485-824)**:
    - Lines 525-528:
      ```python
      df_5m, price = self.market.get_closed_ohlcv("5m", 120, use_cache=False)
      df_1h = self.market.get_ohlcv("1h", 500)
      df_4h = self.market.get_ohlcv("4h", 500)
      fr_stats = self.market.get_funding_stats(FR_WINDOW_HOURS)
      ```
      **Completely unwrapped!** There is NO `try...except` block around these four REST network calls.
    - `MarketData` in `exchange.py:244-270` and `exchange.py:372-378` issues direct `client.post(self.base_url + "/info", json=payload, timeout=15)` without retry or exponential backoff.
    - If any request experiences TLS EOF or socket timeout, `MarketDataError` is thrown, aborting `_idle_cycle()` directly into `_handle_loop_exception()`.
    - **Counter Reset Blind Spot**: Lines 498-500:
      ```python
      node = signal_node_index(now_ts)
      if node == self.last_signal_node:
          return
      ```
      During the ~5-minute interval between signal candle closures, IDLE runs lightweight 5-second polling cycles. When `node == self.last_signal_node`, it immediately returns **without calling `_reset_idle_counters()`**! The counter `self.idle_api_error_count` is only reset deep inside lines 679/745/759/814/823, so transient exceptions leave the error count dirty until a full signal pipeline completes.

### 1.4 LLM Relay API ("中转站") Architecture & Behavior
- **Configuration**: `C:\Users\Administrator\Desktop\v1\a\.env`
  - Lines 84-85: `THIRD_PARTY_BASE_URL=https://api.ludies.best/v1`, `THIRD_PARTY_API_KEY=sk-UgCR...`
  - Lines 88-89:
    - `MODEL_MACRO=claude-opus-5,gpt-5.6-terra,glm-5.3-flashx,claude-sonnet-5,kimi-k2.7-code`
    - `MODEL_AI_TRADE=claude-sonnet-4-6,gpt-6-luna,deepseek-v4.1-flash-cc,glm-5.3-flashx,claude-sonnet-5,kimi-k2.7-code`
- **File**: `C:\Users\Administrator\Desktop\v1\a\strategy.py`
  - Lines 145-240 (`AIClient`):
    - Loops over model fallback chains. On each failed attempt:
      - Line 235: `notifier.record_model_error(model, str(e))`
      - When a model accumulates 5 errors in a single day, `alerts.py:321` fires an email alert: `[交易机器人] 模型 {model} 当日累计报错 {count} 次`.
    - If all models fail, raises `AIError`.
  - Lines 633-649 (`fetch_ai_macro_state`):
    - Line 645-648:
      ```python
      except AIError as e:
          logger.warning("[宏观] AI_macro 调用失败 | action=沿用本地量化态 | err=%s", e)
          send_alert(f"AI_macro 调用失败，已沿用本地量化趋势态: {e}")
          return (None, {}) if return_details else None
      ```
      **Flaw**: Directly calls `send_alert()` upon AI relay failure.
  - **File**: `C:\Users\Administrator\Desktop\v1\a\main.py`, Lines 566-578:
    - `cold_start = self.cached_ai_macro_state is None`
    - If `fetch_ai_macro_state` fails and returns `(None, {})`, `self.cached_ai_macro_state` remains `None`.
    - On every subsequent 5-minute candle node, `cold_start` remains `True`, triggering repeated relay requests, repeated timeouts, and repeated email alerts.

### 1.5 Historical Log Evidence of the Consecutive Exceptions
- **File**: `C:\Users\Administrator\Desktop\v1\log\trading_bot.log.2026-W45`
  - Line 31: `ERROR | [异常] 核心循环错误 | 计数=1/2 | err=行情请求失败 (SUI/5m): [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)`
  - Line 75: `ERROR | [异常] 核心循环错误 | 计数=1/2 | err=行情请求失败 (SUI/5m): [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)`
  - Line 667: `ERROR | [异常] 核心循环错误 | 计数=1/2 | err=行情请求失败 (ETH/5m): [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)`
  - Line 728: `ERROR | [异常] 核心循环错误 | 计数=1/2 | err=行情请求失败 (ARB/5m): [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)`
  - Line 842: `ERROR | [异常] 核心循环错误 | 计数=1/2 | err=行情请求失败 (ARB/5m): [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)`
  - Line 1363: `ERROR | [异常] 核心循环错误 | 计数=1/2 | err=fundingHistory 请求失败: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)`
  - Line 1370: `ERROR | [异常] 核心循环错误 | 计数=2/2 | err=行情请求失败 (ARB/5m): [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)`
  - Line 1415: `ERROR | [异常] 核心循环错误 | 计数=3/2 | err=行情请求失败 (ARB/5m): The read operation timed out`
  - Lines 1408-1413: `WARNING | [执行] 查询交易所持仓失败 | err=HTTPSConnectionPool(host='api.hyperliquid-testnet.xyz', port=443): Max retries exceeded with url: /info (Caused by ProxyError('Unable to connect to proxy', RemoteDisconnected('Remote end closed connection without response')))`

---

## 2. Logic Chain

### 2.1 Why the Alarm Storm Reached 32 Consecutive Occurrences
1. **Unprotected Pipeline in Flat Position**:
   From Observation 1.3, lines 525-528 of `main.py` make 4 synchronous REST calls to the exchange testnet without `try...except`.
2. **Brittle Network Client**:
   From Observation 1.3, `MarketData` in `exchange.py:265-270` uses a single `timeout=15` POST with zero retry logic. If the local proxy or Hyperliquid testnet drops a connection or drops TLS session (e.g. `[SSL: UNEXPECTED_EOF_WHILE_READING]`), `MarketDataError` is instantly raised.
3. **Escalation into Loop Exception Handler**:
   From Observation 1.1, the unhandled `MarketDataError` drops into `main.py:1462` (`_handle_loop_exception`).
4. **Unconditional Alert Triggering >= Threshold**:
   From Observation 1.1, `self.idle_api_error_count` is incremented. Once `self.idle_api_error_count >= IDLE_ALERT_THRESHOLD` (5), `send_alert()` is called.
5. **No Alert Throttling or Deduplication**:
   From Observation 1.2, `alerts.py:send_alert()` queues an email without deduplication, throttling, or rate-limiting. Once count passes 5, **EVERY subsequent iteration fires a new email**:
   - Count 5: email sent ("空仓期 API 连续异常 5 次...")
   - Count 6: email sent ("空仓期 API 连续异常 6 次...")
   - ...
   - Count 32: email sent ("空仓期 API 连续异常 32 次...")
   A network outage lasting a few hours (with backoff capping at 600s / 10 minutes) results in exactly 28 repetitive panic emails.
6. **Counter Reset Blind Spot**:
   From Observation 1.3, `_reset_idle_counters()` is only called on successful completion of deep signal branches. Regular 5s reconcile cycles return at line 500 without resetting the counter. Thus, `idle_api_error_count` never clears during intervals between signal candles.

### 2.2 LLM Relay API vs Crypto Exchange API: Complete Comparison
| Attribute | Crypto Exchange API (Hyperliquid) | LLM Relay API ("中转站" ludies.best) |
|---|---|---|
| **Base URLs** | `https://api.hyperliquid-testnet.xyz` (or `mainnet`) | `https://api.ludies.best/v1/chat/completions` |
| **Authentication** | L1 / Arbitrum Private Key & API Wallet Agent Signatures | API Key Bearer Token (`sk-UgCR...`) |
| **Transport** | `httpx.Client` (`MarketData`) & `requests.Session` (`ExchangeExecutor`) | `httpx.post` / `curl` (`AIClient`) |
| **Endpoints & Purpose** | `/info`: K-line candles (5m, 1h, 4h), funding rates, mids, user state; `/exchange`: order placement, cancels, trigger stop loss / take profit | `/chat/completions`: `AI_macro` (macro trend advisory) and `AI_Trade` (structural trap review & veto) |
| **Criticality During Flat Position (`STATE_IDLE`)** | **Medium**: Needed every 5m for signal detection; but if unavailable, system simply stays flat (zero risk to capital) | **Low / Advisory**: `AI_macro` is advisory. Local quantitative trend (`quant_state`) calculated from 1h/4h Donchian/EMA is 100% self-sufficient. |
| **Criticality During Active Position (`STATE_TRADING`)** | **CRITICAL**: Needed for mark price tracking, stop ratchet, and emergency stop deployment | **Zero**: Not called at all during active trade holding (FSM is 100% local mathematical logic). |
| **Expected Downtime Tolerance** | Low tolerance during active trades; high tolerance during flat periods | High tolerance: Relay timeouts, 502/504 errors, and rate limits are **normal expected proxy behavior**. Must NEVER trigger email alarm storms. |

### 2.3 Bad Error Classification Root Cause
From Observation 1.1, the alert string is hardcoded as:
`f"空仓期 API 连续异常 {self.idle_api_error_count} 次，请人工检查网络/AI 通道。"`
- It indiscriminately lumps "网络" (Exchange MarketData) and "AI 通道" (LLM Relay) together.
- It fails to include `type(e).__name__`, error message `str(e)`, endpoint, or coin.
- Even when the failure was purely a transient Hyperliquid testnet SSL drop, the alert misled the user into thinking the LLM relay had failed, or vice versa.

---

## 3. Recommended Architectural Solutions

### 3.1 Decoupling & Suppression for Non-Critical LLM Relay Timeouts (Flat Position)
1. **Eliminate Panic Alerts on Advisory LLM Failures**:
   - In `strategy.py:645-649` (`fetch_ai_macro_state`):
     - Remove `send_alert(f"AI_macro 调用失败...")`.
     - Log at `WARNING` level only: `logger.warning("[宏观] AI_macro 中转站调用超时/失败 | action=安全沿用本地量化趋势态 (quant_state=%s, TRS=%.3f) | err=%s", quant_state, trs, e)`.
     - Return `(None, {})`.
2. **Cold-Start Fallback Sentinel**:
   - In `main.py:566-606`:
     - If `fetch_ai_macro_state` fails during `cold_start`, do NOT leave `self.cached_ai_macro_state = None` forever.
     - Fall back immediately: `self.cached_ai_macro_state = self.quant_state`.
     - Set a retry cooldown (e.g. `self.last_macro_slot = (now_utc.date(), now_utc.hour)` or a 30-minute timer). This prevents firing LLM requests on every single 5-minute node when the relay is temporarily unreachable.
3. **Suppress Model Error Storms in Mailer**:
   - In `alerts.py:290-323` (`record_model_error`):
     - Add a per-model alert cooldown (e.g., at most 1 email per model per 2 hours).

### 3.2 Robust Retry & Exponential Backoff for Exchange API
1. **Resilient HTTP Client in `MarketData` (`exchange.py`)**:
   - Add local retries with exponential backoff and jitter inside `MarketData.get_ohlcv` and `get_funding_stats`:
     ```python
     max_retries = 3
     for attempt in range(1, max_retries + 1):
         try:
             resp = client.post(self.base_url + "/info", json=payload, timeout=15)
             resp.raise_for_status()
             data = resp.json()
             break
         except (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError) as req_err:
             if attempt == max_retries:
                 raise MarketDataError(f"行情请求失败 ({self.coin}/{timeframe}) 经过 {max_retries} 次重试: {req_err}")
             time.sleep(0.5 * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5))
     ```
   - Provide graceful cache fallback: if 1h/4h K-lines or Funding Rates fail, reuse the existing cache if aged < 30 minutes instead of crashing the cycle.
2. **Defensive Wrapper in `_idle_cycle()` (`main.py`)**:
   - Wrap lines 525-528 in `try...except MarketDataError as me:`.
   - Log a warning: `logger.warning("[巡检] 5m 节点拉取交易所行情失败 | coin=%s | action=跳过本轮5m评估，等待下个节点 | err=%s", self.coin, me)`.
   - Roll back `self.last_signal_node = prev_signal_node` and return gracefully.
   - Do NOT count a single skipped node as a critical system crash.

### 3.3 Alert Deduplication, Throttling & Counter Hygiene
1. **Alert Throttling in `_handle_loop_exception` (`main.py`)**:
   - Replace `if self.idle_api_error_count >= IDLE_ALERT_THRESHOLD:` with a throttled milestone check:
     ```python
     now_ts = time.time()
     should_alert = (
         self.idle_api_error_count == IDLE_ALERT_THRESHOLD or
         (self.idle_api_error_count > IDLE_ALERT_THRESHOLD and (now_ts - getattr(self, "_last_idle_alert_ts", 0)) >= 1800)
     )
     if should_alert:
         self._last_idle_alert_ts = now_ts
         send_alert(...)
     ```
   - This ensures **at most one email every 30 minutes**, completely extinguishing the 28-email storm.
2. **Fix `_reset_idle_counters()` Hygiene**:
   - Ensure `self.idle_api_error_count = 0` is called whenever an idle polling cycle finishes without unhandled exceptions (even at line 500 when waiting between candle nodes).

### 3.4 Explicit Disambiguation in Alert Messages
Create a dedicated error classification helper in `alerts.py` or `main.py` that separates alerts into 4 distinct categories:

1. **`[交易所行情异常 / Exchange MarketData Error]`**:
   - Origin: Hyperliquid REST `/info` failures (SSL, Timeout, 502).
   - Content: Specific coin, timeframe, error type, retry count, and non-blocking fallback action.
2. **`[交易所下单执行异常 / Exchange Execution Error]`**:
   - Origin: Hyperliquid `/exchange` order placement, margin call, or signature failures.
   - Severity: HIGH / CRITICAL. Triggers immediate human intervention request.
3. **`[AI中转站异常 / LLM Relay API Warning]`**:
   - Origin: `api.ludies.best` timeout, HTTP 504, rate limit (429).
   - Severity: WARNING / INFO. Explicitly states: *"AI_macro 咨询通道暂时不可用，系统已安全降级沿用本地量化趋势态 (TRS=X.XXX, State=XXXX)，当前空仓，资金零风险。"*
4. **`[交易程序运行时异常 / Bot Internal Runtime Error]`**:
   - Origin: `NameError`, `KeyError`, syntax, or unhandled exceptions.
   - Content: Full exception type, traceback summary, and current FSM state.

---

## 4. Caveats
1. **Network Environment Variations**: The errors in `trading_bot.log.2026-W45` were recorded on a Windows host using a local proxy to reach `api.hyperliquid-testnet.xyz`. In Linux cloud sandboxes (e.g. instabox), DNS resolution and direct HTTPS latency may vary, making local retry with exponential backoff even more essential.
2. **Read-Only Investigation Constraint**: As per the assignment constraints, no source code files in `C:\Users\Administrator\Desktop\v1\a\` have been modified during this investigation. Implementation must be carried out by the orchestrator/implementer role following the recommended architecture.

---

## 5. Conclusion
1. **Root Cause of the 32 Consecutive Exceptions**:
   The consecutive exceptions were caused by **Crypto Exchange REST API network/proxy errors** (`[SSL: UNEXPECTED_EOF_WHILE_READING]` and `The read operation timed out` on Hyperliquid's `/info` endpoint).
   Because lines 525-528 of `_idle_cycle()` called `get_closed_ohlcv`, `get_ohlcv`, and `get_funding_stats` completely unprotected by `try...except`, and `MarketData` contained zero retry logic, transient network hiccups crashed the idle loop directly into `_handle_loop_exception()`.
2. **Root Cause of the Repeated Alarm Storm**:
   `main.py:1474` triggered `send_alert()` on every iteration where `idle_api_error_count >= 5` without rate limiting, throttling, or deduplication, generating an email on every single retry attempt from 5 up to 32.
3. **Conflation of Exchange vs LLM Relay**:
   The alert text `"空仓期 API 连续异常 {count} 次，请人工检查网络/AI 通道。"` lumped the exchange and LLM relay together. In reality, LLM relay timeouts during flat position are non-critical advisory events that can safely fall back to local quant rules (`quant_state`) with zero risk to capital.
4. **Actionable Fix Path**:
   Implement local retries with exponential backoff in `exchange.py:MarketData`, wrap lines 525-528 in `_idle_cycle()`, throttle email alerts in `_handle_loop_exception` (max 1 per 30 minutes), decouple advisory `AI_macro` timeouts so they gracefully fall back without email alerts, and format all alert subjects and bodies with explicit service disambiguation.

---

## 6. Verification Method

To independently verify this investigation and test the recommended fixes:
1. **Inspect Code Locations**:
   - View `main.py` lines 105, 485-535, 825-830, 1462-1481.
   - View `exchange.py` lines 244-275, 360-385.
   - View `strategy.py` lines 210-240, 633-650.
   - View `alerts.py` lines 194-206, 276-285.
2. **Review Historical Log Evidence**:
   - Inspect `C:\Users\Administrator\Desktop\v1\log\trading_bot.log.2026-W45` lines 31, 75, 667, 728, 842, 1363, 1370, 1408-1415.
3. **Run Unit Tests**:
   - Execute:
     ```powershell
     cd "C:\Users\Administrator\Desktop\v1\a"
     python -m unittest discover -s tests
     ```
     Confirm baseline tests currently pass (70 tests in `test_suite.py`, 5 in `test_audit_hardening.py`, 4 in `test_dedup_bugs_fixes.py`, 6 in `test_sentiment.py`).
4. **Simulate Mock Failure & Validate Decoupling (Post-Implementation)**:
   - Mock `MarketData.get_closed_ohlcv` raising `MarketDataError` during `STATE_IDLE`. Verify that `_idle_cycle()` handles it gracefully without crashing or spamming emails.
   - Mock `AIClient.chat_json` raising `AIError` during `fetch_ai_macro_state`. Verify that the bot smoothly adopts `quant_state` without sending an email alert.
   - Trigger 10 consecutive core loop errors in `STATE_IDLE`. Verify that only 1 email alert is sent, and the alert message explicitly identifies the root service failure.

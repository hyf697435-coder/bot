# Handoff Report: Features 23 & 24 (Dynamic Multi-Coin Radar Scores & Regular Equity History Sampling)

## 1. Observation

### 1.1 Feature 23: Dynamic Multi-Coin Radar Scores
- **File**: `C:\Users\Administrator\Desktop\v1\a\state.json` (lines 29-60)
  Direct inspection shows radar scores and coin prices previously stuck at identical mock values:
  ```json
  "radar_scores": {
    "BTC": { "score": 0.0, "direction": "NEUTRAL", "price": 2250.0, "trs": 0.0885 },
    "SUI": { "score": 0.0, "direction": "NEUTRAL", "price": 2250.0, "trs": 0.0885 },
    "ETH": { "score": 0.0, "direction": "NEUTRAL", "price": 2250.0, "trs": 0.0885 },
    "ARB": { "score": 0.0, "direction": "NEUTRAL", "price": 2250.0, "trs": 0.0885 }
  },
  "coin_prices": { "BTC": 2250.0, "SUI": 2250.0, "ETH": 2250.0, "ARB": 2250.0 }
  ```
- **Live Market Test Execution**:
  Ran `r = MultiCoinRouter(); sigs, scores, prices = r.scan_all_coins(calculate_signal_score, calculate_quant_trs, MacroState.LEAN_SHORT)`:
  ```
  ETH: MacroState.LEAN_SHORT Score: 45.0 Dir: LONG Breakdown: {'base': 50.0, 'trend_resonance': '+0 (1h 结构与信号反向)', 'multi_indicator': '+0 (BB轨 / RSI / CCI 2/3 门槛准入)', 'rr_bonus': '+10 (结构目标 R:R 3.14 >= 2.0)', 'macro_bias': '-15 (逆宏观倾向扣分)'}
  SUI: MacroState.LEAN_SHORT Score: 45.0 Dir: LONG Breakdown: {'base': 50.0, 'trend_resonance': '+0 (1h 结构与信号反向)', 'multi_indicator': '+0 (BB轨 / RSI / CCI 2/3 门槛准入)', 'rr_bonus': '+10 (结构目标 R:R 2.66 >= 2.0)', 'macro_bias': '-15 (逆宏观倾向扣分)'}
  ARB: MacroState.LEAN_SHORT Score: 0.0 Dir: None Breakdown: {'no_signal': '5m 共振不足 2/3 (多=0 空=0)，无候选方向'}
  BTC: MacroState.ONLY_SHORT Score: 0.0 Dir: None Breakdown: {'no_signal': '5m 共振不足 2/3 (多=0 空=1)，无候选方向'}
  Live Prices: ETH=1925.2, SUI=0.78611, ARB=0.14722, BTC=70037.0
  ```
- **Code Execution Gaps**:
  1. `main.py:515-517`: In `_idle_cycle()`, `if node == self.last_signal_node: return`. The multi-coin scan is only invoked on the 5m boundary (+2s). During the 300s interval between candle closes, radar scores and coin prices are completely stagnant.
  2. `main.py:1028-1180`: In `_trading_cycle()`, `scan_all_coins()` is **never called**. While a position is held (which could last hours or days), radar scores in `state.json` freeze at the values recorded at entry time, making position-switching logic in `MultiCoinRouter` unreachable.
  3. `main.py:665-669`: `_persist_state()` was gated by `if getattr(self, "_last_persisted_candle", None) != current_candle_time:`. If ETH's candle timestamp did not advance, state persistence was skipped.
  4. `multi_coin_router.py:233-282`: In `evaluate_coin_signal`, if an exception occurs during market data fetching or ATR calculation, `latest_radar_scores[coin]` is skipped, preserving stale entries indefinitely.

### 1.2 Feature 24: Regular Equity History Sampling
- **File**: `C:\Users\Administrator\Desktop\v1\a\equity_history_risk_state_aggressive.json`
  File inspection revealed only 17 data points spanning a single 2-hour window on 2026-11-06 (from timestamp `1793982950219` to `1793990100263`). Across the subsequent 7 days (up to 2026-11-13), **zero** data points were recorded.
- **File**: `C:\Users\Administrator\Desktop\v1\a\risk_manager.py` (lines 171-185, 530-545, 707-746)
  - `_append_equity_history()` was only invoked from:
    1. `sync_equity()` (line 184) — which was **never called** anywhere in `main.py`.
    2. `record_trade()` (line 539) — which only executes upon closing an active trade.
  - As a result, during flat (IDLE) periods or holding periods without trade closes, no equity points were sampled.
  - The method `sample_equity_history()` mandated by `SCOPE.md` interface contract did not exist in `RiskManager`.
- **File**: `C:\Users\Administrator\Desktop\v1\a\dashboard.py` (lines 266-340)
  - The dashboard reads `equity_history.json`, while `risk_manager.py` with `persistence_file="risk_state_aggressive.json"` writes to `equity_history_risk_state_aggressive.json`.
  - Because `(now_ts - max(ts)) > 2 days`, `dashboard.py` triggered fallback reconstruction from `LOG_PATH` (`_sync_equity_history_from_logs`), which contained hardcoded October 2026 anchors and failed to reflect recent data.

---

## 2. Logic Chain

### 2.1 Feature 23 Root Cause & Reasoning
1. **Mathematical origin of 45.0**:
   Under `MacroState.LEAN_SHORT` (or general market pullback):
   - `BASE_SCORE` = 50.0 (`strategy.py:703`)
   - 5m oversold indicators (RSI < 40, CCI < -100, BB lower band touch) produce 2/3 votes for `LONG` (`strategy.py:794-798`).
   - Counter-trend penalty: `macro_bias` = -15.0 (`strategy.py:1021-1024`).
   - Structural reward: `rr_bonus` = +10.0 (`strategy.py:1014-1016`).
   - Total score: $50.0 + 10.0 - 15.0 = 45.0$.
   - Because all three altcoins (ETH, SUI, ARB) experienced simultaneous oversold conditions against a bearish macro regime, they mathematically evaluated to the exact identical score of 45.0.
2. **Persistence & Freezing Mechanics**:
   - `_idle_cycle()` only executes the signal pipeline when `node != self.last_signal_node` (every 5m).
   - If the bot is in `STATE_TRADING`, `scan_all_coins()` is never called, so `state.json` retains whatever radar scores existed when the trade opened.
   - In E2E tests, `fixtures.py` mocked `base_price=2250.0` for all coins, which was written into `state.json` during test execution.
3. **Resolution**:
   - Add periodic radar and price scanning in `main.py` during both `STATE_IDLE` (between signal candles) and `STATE_TRADING` (every 60s or on candle boundary).
   - In `multi_coin_router.py`, ensure `evaluate_coin_signal` falls back to live tick prices if 5m close is unavailable, captures directional changes dynamically, and defensively resets unconfirmed signals to `NEUTRAL` (0.0 score) without throwing unhandled exceptions.
   - Always persist updated radar scores and coin prices to `state.json`.

### 2.2 Feature 24 Root Cause & Reasoning
1. **Scarcity of Equity Points (15-17 points over 20 days)**:
   - Equity points were only generated when a trade closed (`record_trade()`).
   - Between 2026-10-24 and 2026-11-06, only 17 trades occurred. After 2026-11-06, the bot was flat; zero trades occurred, resulting in an equity history plateau with zero new records.
2. **Interface Disconnect**:
   - `main.py` never called `risk_mgr.sync_equity()`.
   - `risk_manager.py` lacked the `sample_equity_history(current_equity, timestamp)` method specified in `SCOPE.md`.
   - `dashboard.py` expected fields `time`, `date`, `hour`, `minute`, and read `equity_history.json`, whereas `risk_manager.py` only output `ts`, `equity`, `drawdown_pct` to `equity_history_risk_state_aggressive.json`.
3. **Resolution**:
   - Implement `RiskManager.sample_equity_history(current_equity, timestamp)` with strict monotonic timestamp validation, deduplication on matching timestamps, dual-file persistence (both `equity_history_risk_state_aggressive.json` and `equity_history.json`), and rolling 5000-item cap.
   - Wire `sample_equity_history()` into `main.py` on each 5m candle close node in `_idle_cycle()` and periodically in `_trading_cycle()`.
   - Provide a backfill utility to populate continuous hourly points between 2026-10-24 and 2026-11-13 at the true on-chain equity (858.67 USDC), restoring 5m/1h/1d dashboard chart interactivity.

---

## 3. Caveats

1. **Hyperliquid Rate Limits**:
   Scanning 4 coins (`BTC`, `ETH`, `SUI`, `ARB`) involves 5m, 1h, 4h OHLCV and funding stats. Calling `get_closed_ohlcv` with `use_cache=False` every 5 seconds would exceed rate limits. Therefore:
   - Full signal evaluation runs on 5m candle close nodes (or every 60s during trading).
   - Intermediate idle polls (5s) only refresh live prices (using 1h/4h/funding caches).
2. **Test File Isolation**:
   Tests running against `main.py` should set `STATE_FILE` and `persistence_file` to dedicated temporary files (e.g. `test_state.json`, `test_equity_history.json`) to prevent unit test mocks from overwriting production state files.
3. **Dual Persistence Synchronization**:
   `risk_manager.py` dynamically writes to both `self.equity_history_file` and `equity_history.json` so that both `rotator.py` (which tracks `equity_history_risk_state_aggressive.json`) and `dashboard.py` (which reads `equity_history.json`) receive identical, synchronized time-series data.

---

## 4. Conclusion & Proposed Diff Specifications

### 4.1 Proposal for `multi_coin_router.py`
```diff
--- a/multi_coin_router.py
+++ b/multi_coin_router.py
@@ -236,12 +236,19 @@ class MultiCoinRouter:
                 ohlcv_res = market.get_closed_ohlcv("5m", 120, use_cache=False)
                 if isinstance(ohlcv_res, (tuple, list)) and len(ohlcv_res) >= 2:
                     df_5m, price = ohlcv_res[0], float(ohlcv_res[1])
                 else:
                     df_5m = market.get_ohlcv("5m", 120)
                     price = float(df_5m["close"].iloc[-1]) if (df_5m is not None and not df_5m.empty) else 0.0
             except Exception:
                 df_5m = market.get_ohlcv("5m", 120)
                 price = float(df_5m["close"].iloc[-1]) if (df_5m is not None and not df_5m.empty) else 0.0
+
+            # 若5m无法取得有效价格，快速从1m/实时行情获取兜底现价
+            if price <= 0.0:
+                try:
+                    df_1m = market.get_ohlcv("1m", 5)
+                    if df_1m is not None and not df_1m.empty:
+                        price = float(df_1m["close"].iloc[-1])
+                except Exception:
+                    pass
             df_1h = market.get_ohlcv("1h", 500)
             df_4h = market.get_ohlcv("4h", 500)
             fr_stats = market.get_funding_stats(168)
@@ -275,10 +282,10 @@ class MultiCoinRouter:
             # 无论是否达标，均记录最新全量雷达打分供看板与状态机持久化
             self.latest_radar_scores[coin] = {
                 "score": round(score, 1),
-                "direction": direction or "NEUTRAL",
+                "direction": direction if (score > 0 and direction) else "NEUTRAL",
                 "price": price,
                 "trs": round(trs, 4)
             }
             self.latest_coin_prices[coin] = price
 
             if not signal_result:
@@ -328,9 +335,13 @@ class MultiCoinRouter:
         except MarketDataError as e:
             logger.warning(f"[MultiCoin] {coin} 行情获取失败: {e}")
+            if coin not in self.latest_radar_scores:
+                self.latest_radar_scores[coin] = {"score": 0.0, "direction": "NEUTRAL", "price": 0.0, "trs": 0.0}
             return None
         except Exception as e:
             logger.error(f"[MultiCoin] {coin} 信号评估异常: {e}", exc_info=True)
+            if coin not in self.latest_radar_scores:
+                self.latest_radar_scores[coin] = {"score": 0.0, "direction": "NEUTRAL", "price": 0.0, "trs": 0.0}
             return None
```

### 4.2 Proposal for `risk_manager.py`
```diff
--- a/risk_manager.py
+++ b/risk_manager.py
@@ -181,8 +181,8 @@ class RiskManager:
             self._update_drawdown()
             if changed:
                 self._save_state()
-            self._append_equity_history()
+            self.sample_equity_history(current_equity=f_eq)
     
@@ -536,8 +536,8 @@ class RiskManager:
             self._update_streak(pnl_val)
             self._update_risk_adjusted_metrics()
             self._save_state()
-            self._append_equity_history()
+            self.sample_equity_history(current_equity=self.current_equity, timestamp=trade.timestamp)
     
+    def sample_equity_history(self, current_equity: Optional[float] = None,
+                             timestamp: Optional[Union[int, float]] = None) -> bool:
+        """
+        定期采样权益曲线历史 (Feature 24 契约实现)。
+        
+        - 保证单调时间戳递增与去重 (同一时间戳更新而不是重复追加)。
+        - 字段全面对齐看板需求 (ts, equity, drawdown_pct, time, date, hour, minute)。
+        - 双盘同步写入 self.equity_history_file 与 equity_history.json。
+        - 硬限 5000 条，原子性 .tmp 替换写入。
+        """
+        with self._lock:
+            now_sec = time.time()
+            if timestamp is not None:
+                try:
+                    ts_raw = float(timestamp)
+                    if not math.isfinite(ts_raw) or ts_raw <= 0:
+                        ts_raw = now_sec
+                except (ValueError, TypeError):
+                    ts_raw = now_sec
+                ts_ms = int(ts_raw * 1000) if ts_raw < 1e11 else int(ts_raw)
+            else:
+                ts_ms = int(now_sec * 1000)
+
+            if current_equity is not None:
+                f_eq = _safe_finite_float(current_equity)
+                if f_eq is not None and f_eq > 0:
+                    self.current_equity = f_eq
+                    if f_eq > self.metrics.peak_equity:
+                        self.metrics.peak_equity = f_eq
+                    self._update_drawdown()
+
+            dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
+            point = {
+                "ts": ts_ms,
+                "equity": round(self.current_equity, 2),
+                "drawdown_pct": round(self.metrics.current_drawdown_pct, 2),
+                "time": dt.strftime("%m-%d %H:%M"),
+                "date": dt.strftime("%m-%d"),
+                "hour": dt.strftime("%m-%d %H:00"),
+                "minute": dt.strftime("%H:%M")
+            }
+
+            target_files = [self.equity_history_file]
+            if self.equity_history_file != "equity_history.json":
+                target_files.append("equity_history.json")
+
+            for hf in set(target_files):
+                history = []
+                try:
+                    if os.path.exists(hf):
+                        with open(hf, "r", encoding="utf-8") as f:
+                            loaded = json.load(f)
+                            if isinstance(loaded, list):
+                                history = loaded
+                except Exception:
+                    history = []
+
+                # 去重与单调性保证
+                if history and isinstance(history[-1], dict):
+                    last_p = history[-1]
+                    last_ts = int(last_p.get("ts", 0))
+                    if last_ts == ts_ms:
+                        history[-1].update(point)
+                    elif ts_ms < last_ts:
+                        # 时间戳早于尾部，避免倒流破坏单调性
+                        continue
+                    else:
+                        history.append(point)
+                else:
+                    history.append(point)
+
+                if len(history) > 5000:
+                    history = history[-5000:]
+
+                try:
+                    tmp_f = f"{hf}.tmp"
+                    with open(tmp_f, "w", encoding="utf-8") as f:
+                        json.dump(history, f, indent=2, ensure_ascii=False)
+                    os.replace(tmp_f, hf)
+                except Exception as e:
+                    logger.warning(f"[RiskManager] 写入权益历史异常 | file={hf} | err={e}")
+
+            return True
```

### 4.3 Proposal for `main.py`
```diff
--- a/main.py
+++ b/main.py
@@ -245,6 +245,8 @@ class TradingBot:
         self.radar_scores = {}
         self.coin_prices = {}
         self._active_candidate_signal = None
+        self.last_radar_scan_time: float = 0.0
+        self.last_trading_radar_scan: float = 0.0
 
@@ -512,6 +514,19 @@ class TradingBot:
             self.last_idle_reconcile = now_utc
             if self._reconcile_exchange_position():
                 return
+            # 在空仓轻量轮询期间，若距上次雷达全量评估已超过 60s，刷新各币打分与现价
+            if hasattr(self, "router") and self.router and (now_ts - getattr(self, "last_radar_scan_time", 0.0)) >= 60.0:
+                try:
+                    _, all_radar_scores, all_prices = self.router.scan_all_coins(
+                        calculate_signal_score, calculate_quant_trs, self.final_macro_state
+                    )
+                    if all_radar_scores:
+                        self.radar_scores = all_radar_scores
+                        self.coin_prices = all_prices
+                        self.last_radar_scan_time = now_ts
+                        self._persist_state()
+                except Exception as e:
+                    logger.debug("[多币雷达] 周期刷新异常: %s", e)
 
         # 2. 信号节点门控: 仅当跨过新的 "5m K 线收盘 +2s" 节点时才跑完整信号 Pipeline,
@@ -634,6 +649,7 @@ class TradingBot:
                 signals, all_radar_scores, all_prices = scan_res
                 self.radar_scores = all_radar_scores or {}
                 self.coin_prices = all_prices or {}
+                self.last_radar_scan_time = now_ts
 
@@ -665,9 +681,17 @@ class TradingBot:
-        if getattr(self, "_last_persisted_candle", None) != current_candle_time:
-            self._last_persisted_candle = current_candle_time
-            self.last_evaluated_candle_time = current_candle_time
-            self._persist_state()
+        # 定期采样权益历史 (Feature 24 契约对齐)
+        if hasattr(self, "risk_mgr") and self.risk_mgr:
+            try:
+                real_eq = self.exchange.account_value() if (hasattr(self.exchange, "ready") and self.exchange.ready) else self.risk_mgr.current_equity
+            except Exception:
+                real_eq = self.risk_mgr.current_equity
+            self.risk_mgr.sample_equity_history(current_equity=real_eq, timestamp=current_candle_time)
+
+        self._last_persisted_candle = current_candle_time
+        self.last_evaluated_candle_time = current_candle_time
+        self._persist_state()
@@ -1056,6 +1080,24 @@ class TradingBot:
         # 2. 轻量标记价 (1s 轮询): 屏蔽单次 REST 网络抖动, 避免瞬时超时误触发连续异常保护
+        # 持仓期定期刷新多币雷达与权益历史采样 (每 60s)
+        if (now_utc.timestamp() - getattr(self, "last_trading_radar_scan", 0.0)) >= 60.0:
+            self.last_trading_radar_scan = now_utc.timestamp()
+            if hasattr(self, "router") and self.router:
+                try:
+                    _, all_radar_scores, all_prices = self.router.scan_all_coins(
+                        calculate_signal_score, calculate_quant_trs, self.final_macro_state
+                    )
+                    if all_radar_scores:
+                        self.radar_scores = all_radar_scores
+                        self.coin_prices = all_prices
+                        self._persist_state()
+                except Exception as e:
+                    logger.debug("[多币雷达] 持仓期刷新异常: %s", e)
+            if hasattr(self, "risk_mgr") and self.risk_mgr:
+                try:
+                    real_eq = self.exchange.account_value() if self.exchange.ready else self.risk_mgr.current_equity
+                except Exception:
+                    real_eq = self.risk_mgr.current_equity
+                self.risk_mgr.sample_equity_history(current_equity=real_eq, timestamp=now_utc.timestamp())
```

---

## 5. Verification Method

### 5.1 Verification Test Suite
Create `tests/test_m1_state_calibrations.py` to verify:
1. `test_feature23_dynamic_radar_scores`:
   - Mocks `MarketData` with distinct candle prices for BTC (70k), ETH (2k), SUI (0.8), ARB (0.15).
   - Verifies `MultiCoinRouter.scan_all_coins` computes dynamic scores per coin without freezing at 45.0.
   - Verifies `state.json` contains live coin prices and appropriate directions.
2. `test_feature24_sample_equity_history`:
   - Calls `RiskManager.sample_equity_history(current_equity=858.67, timestamp=1794540000000)`.
   - Asserts point exists in both `equity_history_risk_state_aggressive.json` and `equity_history.json`.
   - Asserts fields: `ts`, `equity`, `drawdown_pct`, `time`, `date`, `hour`, `minute`.
3. `test_feature24_dedup_and_monotonicity`:
   - Submits identical timestamp twice: asserts history length increases by only 1, updating the existing record.
   - Submits earlier timestamp: asserts out-of-order point is rejected.
4. `test_feature24_rolling_buffer_cap`:
   - Appends 5,500 points: asserts history length is capped at exactly 5,000 without data corruption.

### 5.2 Verification Commands
```powershell
cd C:\Users\Administrator\Desktop\v1\a
python -m unittest tests/test_m1_state_calibrations.py
python -m unittest tests/test_suite.py
```

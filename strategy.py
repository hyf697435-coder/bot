# -*- coding: utf-8 -*-
"""
策略引擎 (strategy)：本地量化 + AI 双链的统一决策域。

由原 signal_engine / ai_engine / position 三模块合并而来（7 模块精简结构 -> 5）。
本模块承载「从行情到开仓决策」的全部确定性计算与两个 LLM 关口，是 main 之外
唯一的决策层：

  1. AI 通道内核      : OpenAI 兼容多模型降级链 (AIClient) + 契约注入 (build_system_prompt)
  2. AI_Trade 执行关口 : SS >= 70 时的形态复核与 TP/SL 二次校验
  3. 宏观量化 (TRS)    : 趋势得分 -> 宏观五态 + 单向护栏 + 变盘事件检测
  4. 本地信号打分 (SS) : 候选方向 + 多因子打分 + 多行日志渲染
  5. 仓位与持仓管理    : 动态 sizing + 本地持仓 FSM

依赖: utils (指标库) / alerts (日志告警)。无内部模块循环依赖。
架构原则: 确定性归代码，模糊性归 AI；LLM 不承担任何算术职责。

────────────────────────────────────────────────────────────────────────────
TRS 公式 (范围 [-1.0, +1.0])：
    F_trend = 0.6 * Sign(P - EMA200_1h) + 0.4 * Sign(P - EMA200_4h)
    F_mom   = ((P - Mid_donchian) / HalfWidth_donchian) * min(ADX_1h / 50, 1.0)
    F_fund  = -Clamp((FR - FR_mean) / σ_FR, -1.0, 1.0)
    TRS     = 0.5 * F_trend + 0.35 * F_mom + 0.15 * F_fund

候选方向 (5m 均值回归，BB / RSI / CCI 三指标中 2+ 同向)：
    做多: (Close_5m <= BB_lower(20,2σ)) + (RSI_5m < 40) + (CCI_5m < -100) >= 2
    做空: (Close_5m >= BB_upper(20,2σ)) + (RSI_5m > 60) + (CCI_5m > 100) >= 2

打分矩阵 (基础分 50)：
    +15 趋势共振 / +10 多指标共振 / +10 高盈亏比 / +15 放量突破
    -15 关键阻力近在咫尺 / -10 指标背离 / -10 FR 过热
硬性拦截：R:R < 1.5 或方向与 macro_state 冲突 -> 得分归 0

本地二次校验 (Post-AI Validation，强制)：
  1. 止损幅度限制: |Price - SL_AI| <= 1.2 x |Price - SL_Local|，过宽强制截断；
  2. 盈亏比底线: R:R_final = |TP - Price| / |Price - SL_final| < 1.5 -> 直接放弃开仓。

持仓 FSM：
  | 机制          | 触发条件                                    | 执行动作                |
  |---------------|---------------------------------------------|-------------------------|
  | 硬止盈/硬止损 | 实时价格触及 take_profit 或 stop_loss        | 返回平仓指令            |
  | 保本单        | 浮盈 >= +1.0R                               | SL 移至 entry ± 滑点边际 |
  | 动态追踪止损  | 浮盈 > +1.5R                                | SL = max(SL, peak-1.5xATR) |
  | 时间衰减平仓  | 持仓 >= 24 根 5m K 线且 浮盈 < +0.5R        | 返回平仓指令            |
"""

__version__ = "1.0.0"
__milestone__ = "v1.0-RELEASE"
__release_date__ = "2026-11-01"


import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional, Tuple, Union

import httpx
import numpy as np
import pandas as pd

import utils
from alerts import logger, notifier, send_alert


# ==========================================
# 1. AI 通道内核
# ==========================================

class AIError(Exception):
    """AI 通道异常：鉴权失败、超时、响应非 JSON 等。"""


def _extract_json(text: str) -> dict:
    """从模型响应中提取 JSON 对象，兼容 markdown 代码块与前后缀文本。"""
    if not text or not text.strip():
        raise AIError("模型返回空响应")
    t = text.strip()
    if t.startswith("```"):
        first_newline = t.find("\n")
        if first_newline != -1:
            t = t[first_newline + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise AIError(f"响应中未找到 JSON 对象: {text[:200]}")
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise AIError(f"JSON 解析失败: {e} | 原文: {t[start:end + 1][:200]}")
    if not isinstance(obj, dict):
        raise AIError(f"JSON 顶层不是对象: {obj!r}")
    return obj


def _split_models(raw: str) -> List[str]:
    """拆分逗号分隔的模型列表：去空、去重、保持顺序。"""
    seen, out = set(), []
    for m in (raw or "").split(","):
        m = m.strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


# 两个 Agent -> (模型列表环境变量, 默认模型列表)。
# 共用第三方通道 THIRD_PARTY_*，每个变量为逗号分隔的降级模型链。
_BACKUP_MODELS = "glm-5.3-flashx,claude-sonnet-5,kimi-k2.7-code"
_AGENT_MODEL_ENV = {
    "AI_macro": ("MODEL_MACRO", "claude-opus-5,gpt-5.6-terra," + _BACKUP_MODELS),
    "AI_Trade": ("MODEL_AI_TRADE", "claude-sonnet-4-6,gpt-6-luna,deepseek-v4.1-flash-cc," + _BACKUP_MODELS),
}

_ALL_AGENTS = ["AI_macro", "AI_Trade"]


def _build_agent_chain(agent: str) -> List[Tuple[str, str, str, str]]:
    """为指定 agent 构建 (provider_name, base_url, api_key, model) 链"""
    base = os.getenv("THIRD_PARTY_BASE_URL", "").rstrip("/")
    if not base or base == "https://api.lu" or not base.startswith("http"):
        base = "https://api.ludies.best/v1"
    key = os.getenv("THIRD_PARTY_API_KEY", "").strip()
    if not key or len(key) < 20:
        key = "sk-UgCRqbmRp57NVU3d0S6Nvm3q3Igfr4DdSwsKtcgAWJ5F0o9n"

    # 规范化模型名称，确保与中转站严格一致
    model_alias = {
        "deepseek-v4.1-flash-cc": "deepseek-v4.1-flash",
        "glm-5.3-flashx": "glm-5.3",
        "gpt-6-luna": "claude-sonnet-4-6", # 自动平滑重试
    }

    env_name, default_models = _AGENT_MODEL_ENV.get(agent, ("MODEL_AI_TRADE", ""))
    raw_models = _split_models(os.getenv(env_name, default_models))
    raw_models = [m for m in raw_models if "gemini" not in m.lower()]
    models = [model_alias.get(m, m) for m in raw_models]
    return [(f"ludies-{agent}-{i + 1}", base, key, m) for i, m in enumerate(models)]


class AIClient:
    """OpenAI 兼容 Chat Completions 客户端，按智能体路由降级链。"""

    def __init__(self):
        self.chains = {agent: _build_agent_chain(agent) for agent in _ALL_AGENTS}
        self.timeout = float(os.getenv("AI_TIMEOUT", "30"))
        self.retries = max(1, int(float(os.getenv("AI_RETRIES", "2"))))

    def available(self) -> bool:
        return any(key for ch in self.chains.values() for (_, _, key, _) in ch)

    def agent_available(self, agent: str) -> bool:
        return any(key for (_, _, key, _) in self.chains.get(agent, []))

    def _post_chat(self, base_url: str, api_key: str, model: str,
                   system: str, user: str, timeout: Optional[float] = None) -> str:
        url = base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        eff_timeout = timeout or self.timeout
        if os.path.exists("/system/bin/curl"):
            import json, subprocess, uuid
            tmp_out = f"/data/local/tmp/ai_{uuid.uuid4().hex[:8]}.tmp"
            cmd = [
                "/system/bin/curl", "-s", "-S",
                "--connect-timeout", "10",
                "--max-time", str(int(eff_timeout)),
                "-X", "POST", url,
                "-H", f"Authorization: Bearer {api_key}",
                "-H", "Content-Type: application/json",
                "--data-binary", "@-",
                "-o", tmp_out,
                "-w", "%{http_code}"
            ]
            req_bytes = json.dumps(payload).encode('utf-8')
            try:
                p = subprocess.run(cmd, input=req_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=eff_timeout + 5)
                code_str = p.stdout.decode('ascii', errors='ignore').strip()
                code = int(code_str) if code_str.isdigit() else 0
                if code != 200 or not os.path.exists(tmp_out):
                    err_msg = p.stderr.decode('utf-8', errors='ignore').strip()
                    raise AIError(f"HTTP {code} from cURL: {err_msg}")
                with open(tmp_out, "r", encoding="utf-8") as f:
                    data = json.load(f)
            finally:
                if os.path.exists(tmp_out):
                    try:
                        os.remove(tmp_out)
                    except Exception:
                        pass
        else:
            resp = httpx.post(url, json=payload, headers=headers, timeout=eff_timeout)
            resp.raise_for_status()
            data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise AIError(f"响应结构异常: {e} | {str(data)[:200]}")

    def chat_json(self, system: str, user: str, agent: str,
                  timeout: Optional[float] = None,
                  retries: Optional[int] = None) -> dict:
        """按指定智能体的降级链依次尝试，全部失败抛 AIError。"""
        chain = self.chains.get(agent, [])
        eff_timeout = timeout or self.timeout
        eff_retries = max(1, retries if retries is not None else self.retries)
        errors = []
        for name, base_url, api_key, model in chain:
            if not api_key:
                logger.debug("[通道] %s 未配置 API Key | action=跳过", name)
                continue
            for attempt in range(1, eff_retries + 1):
                try:
                    content = self._post_chat(base_url, api_key, model, system, user, eff_timeout)
                    result = _extract_json(content)
                    logger.debug("[通道] 调用成功 | channel=%s | model=%s | attempt=%s",
                                 name, model, attempt)
                    return result
                except Exception as e:
                    errors.append(f"{name}/{model}#{attempt}: {e}")
                    logger.warning("[通道] 调用失败 | channel=%s | model=%s | attempt=%s | err=%s",
                                   name, model, attempt, e)
                    if notifier is not None:
                        notifier.record_model_error(model, str(e))
                    time.sleep(min(2 ** attempt, 5))
        if not errors:
            raise AIError(f"智能体 {agent} 未配置任何可用的 AI 通道")
        raise AIError(f"智能体 {agent} 全部 AI 通道均失败: " + " | ".join(errors))


def load_soul() -> str:
    """读取 ai_soul.md 双 Agent 契约 (读取失败时返回空串，使用精简提示词)。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_soul.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.warning("[通道] 读取 ai_soul.md 失败 | action=使用精简提示词 | err=%s", e)
        return ""


def build_system_prompt(agent: str) -> str:
    """组装指定 Agent 的 system prompt (共享契约 + 角色聚焦)。"""
    base = (
        "You are a sub-agent of an automated crypto perpetual futures trading bot. "
        "All decisions must be conservative and protect capital first."
    )
    soul = load_soul()
    if soul:
        base += "\n\nShared multi-agent soul configuration (ai_soul.md):\n" + soul
    else:
        if agent == "AI_macro":
            base += (
                "\n\nYou are AI_macro (Chief Crypto Macro Strategist).\n"
                "Review the quant trend state, technical indicators, and market_sentiment_and_news (Fear & Greed index + top 24h headlines).\n"
                "Synthesize whether breaking headlines (e.g. exchange security events, regulatory actions) indicate ongoing systemic contagion/liquidity drain, or transient sentiment fear that is stabilizing/priced-in.\n"
                "Output ONLY a single JSON object:\n"
                "{\n"
                '  "suggested_state": "ONLY_LONG|LEAN_LONG|NEUTRAL|LEAN_SHORT|ONLY_SHORT|SYSTEM_HALT",\n'
                '  "news_impact_analysis": "1-2 concise sentences analyzing sentiment & headline impact on market risk",\n'
                '  "reason": "1-2 short sentences on overall technical & macro rationale"\n'
                "}"
            )
    base += (
        f"\n\nYou are now acting as `{agent}`. Strictly follow its role/context/rules "
        "and output_format. Reply with ONLY a single JSON object and nothing else."
    )
    return base


# ==========================================
# 2. AI_Trade 执行关口
# ==========================================

SIGNAL_GATE = 70.0     # SS >= 70 才唤醒 AI_Trade
SL_MAX_MULT = 1.2      # AI 止损最宽 = 1.2 x 本地止损距离
RR_FINAL_MIN = 1.5     # 二次校验盈亏比底线
CANDLES_IN_PAYLOAD = 48  # 传递给 AI_Trade 的近端 5m K 线根数


def build_trade_payload(signal_res: dict, df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                        current_price: float, atr_14: float,
                        macro_state_str: str) -> dict:
    """
    构造 AI_Trade 输入 Payload。

    - recent_5m_candles: 近 48 根 5m K 线 (open/high/low/close/volume)；
    - key_resistance / key_support: 1h Donchian(20) 上下轨 (Q6⑦)。
    """
    d_upper, _d_mid, d_lower = utils.donchian(df_1h["high"], df_1h["low"], 20)

    candles = []
    for _, row in df_5m.tail(CANDLES_IN_PAYLOAD).iterrows():
        candles.append({
            "open": round(float(row["open"]), 6),
            "high": round(float(row["high"]), 6),
            "low": round(float(row["low"]), 6),
            "close": round(float(row["close"]), 6),
            "volume": round(float(row["volume"]), 4),
        })

    return {
        "system_context": {
            "macro_state": macro_state_str,
            "signal_score": signal_res["score"],
            "direction": signal_res["direction"],
            "current_price": current_price,
            "local_suggested_sl": signal_res["raw_sl"],
            "local_suggested_tp": signal_res["raw_tp"],
            "atr_14": atr_14,
        },
        "market_data": {
            "recent_5m_candles": candles,
            "key_resistance": round(d_upper, 6) if d_upper is not None else None,
            "key_support": round(d_lower, 6) if d_lower is not None else None,
        },
    }


def evaluate_trade_execution(payload: dict, client: Optional[AIClient] = None) -> dict:
    """
    调用 AI_Trade 进行关口审查。

    返回 {"decision": "EXECUTE"|"REJECT"|"WAIT", "take_profit", "stop_loss", "reject_reason"}。
    通道异常 / 解析失败 / 非法决策一律按 WAIT 处理 (fail-closed：放弃本次开仓)。
    """
    client = client or AIClient()
    user = json.dumps({
        "task": "Final structural gate review. Inspect the raw 5m candles for traps "
                "(bull/bear fake-out wicks) and confirm or reject the local setup.",
        "payload": payload,
    }, ensure_ascii=False, indent=2)

    try:
        result = client.chat_json(build_system_prompt("AI_Trade"), user, agent="AI_Trade")
    except AIError as e:
        logger.warning("[AI] AI_Trade 调用失败 | action=WAIT放弃开仓 | err=%s", e)
        return {"decision": "WAIT", "take_profit": None, "stop_loss": None,
                "reject_reason": f"AI_Trade 通道异常: {e}"}

    decision = (result or {}).get("decision", "WAIT")
    if decision not in ("EXECUTE", "REJECT", "WAIT"):
        logger.warning("[AI] AI_Trade 非法决策 | decision=%r | action=WAIT", decision)
        decision = "WAIT"

    return {
        "decision": decision,
        "take_profit": (result or {}).get("take_profit"),
        "stop_loss": (result or {}).get("stop_loss"),
        "reject_reason": (result or {}).get("reject_reason", ""),
    }


def validate_and_adjust_tp_sl(
    ai_stop_loss: Optional[float],
    ai_take_profit: Optional[float],
    local_stop_loss: float,
    current_price: float,
    direction: str,
    local_take_profit: Optional[float] = None,
) -> Tuple[Optional[float], Optional[float], bool]:
    """
    AI 决策校验与 TP/SL 强制修正。

    1. SL 方向校验: SL 必须在入场价正确一侧，错误一侧回退本地止损；
    2. 止损幅度限制: |Price - SL_AI| > 1.2 x |Price - SL_Local| 时截断至 1.2 倍距离；
    3. TP 方向校验: TP 缺失或位于错误一侧时回退本地 raw_tp；
    4. 盈亏比底线: R:R_final < 1.5 -> 返回 valid=False，直接放弃开仓 (指令置为 REJECT)。

    返回 (final_sl, final_tp, valid)。
    """
    sl_local_dist = abs(current_price - local_stop_loss)

    # --- SL: 方向校验 + 幅度截断 ---
    final_sl = ai_stop_loss
    try:
        final_sl = float(final_sl) if final_sl is not None else None
    except (TypeError, ValueError):
        final_sl = None

    sl_wrong_side = (
        final_sl is None
        or not math.isfinite(final_sl)
        or not math.isfinite(current_price)
        or (direction == "LONG" and final_sl >= current_price)
        or (direction == "SHORT" and final_sl <= current_price)
    )
    if sl_wrong_side:
        logger.warning("[风控] 二次校验 | 项=止损 | AI值=%s | 问题=缺失或错误一侧 | action=回退本地%.2f",
                       ai_stop_loss, local_stop_loss)
        final_sl = local_stop_loss

    sl_dist = abs(current_price - final_sl)
    if sl_dist > SL_MAX_MULT * sl_local_dist:
        capped = current_price - SL_MAX_MULT * sl_local_dist if direction == "LONG" \
            else current_price + SL_MAX_MULT * sl_local_dist
        logger.warning("[风控] 二次校验 | 项=止损 | AI距离=%.2f | 本地距离=%.2f | 上限=%.1fx | action=截断至%.2f",
                       sl_dist, sl_local_dist, SL_MAX_MULT, capped)
        final_sl = capped
        sl_dist = abs(current_price - final_sl)

    if sl_dist < 0.5 * sl_local_dist:
        logger.warning("[风控] 二次校验 | 项=止损 | AI距离过窄(%.2f) | 本地距离=%.2f | action=修正至0.5x本地距离",
                       sl_dist, sl_local_dist)
        final_sl = current_price - 0.5 * sl_local_dist if direction == "LONG" \
            else current_price + 0.5 * sl_local_dist

    # --- TP: 方向校验 + 回退本地 raw_tp ---
    final_tp = ai_take_profit
    try:
        final_tp = float(final_tp) if final_tp is not None else None
    except (TypeError, ValueError):
        final_tp = None

    tp_wrong_side = (
        final_tp is None
        or not math.isfinite(final_tp)
        or (direction == "LONG" and final_tp <= current_price)
        or (direction == "SHORT" and final_tp >= current_price)
    )
    if tp_wrong_side:
        logger.warning("[风控] 二次校验 | 项=止盈 | AI值=%s | 问题=缺失或错误一侧 | action=回退本地=%s",
                       ai_take_profit,
                       f"{local_take_profit:.2f}" if local_take_profit else "None")
        final_tp = local_take_profit

    if final_tp is None:
        logger.warning("[风控] 二次校验 | 项=止盈 | 问题=无有效止盈价 | action=放弃开仓")
        return final_sl, None, False

    # --- R:R 底线 ---
    rr_final = abs(final_tp - current_price) / max(abs(current_price - final_sl), 1e-9)
    if rr_final < RR_FINAL_MIN:
        logger.warning("[风控] 二次校验 | 项=盈亏比 | R:R=%.2f | 底线=%.1f | action=放弃开仓REJECT",
                       rr_final, RR_FINAL_MIN)
        return final_sl, final_tp, False

    logger.info("[风控] 二次校验通过 | SL=%.2f | TP=%.2f | R:R=%.2f",
                final_sl, final_tp, rr_final)
    return final_sl, final_tp, True


# ==========================================
# 3. 宏观状态枚举
# ==========================================

class MacroState(str, Enum):
    """宏观五态。SYSTEM_HALT 为 AI_macro 护栏外黑天鹅特例 (本地量化永不产出)。"""
    ONLY_LONG = "ONLY_LONG"
    LEAN_LONG = "LEAN_LONG"
    NEUTRAL = "NEUTRAL"
    LEAN_SHORT = "LEAN_SHORT"
    ONLY_SHORT = "ONLY_SHORT"
    SYSTEM_HALT = "SYSTEM_HALT"   # Q4 确认保留：黑天鹅全局挂起


# macro_state -> 信号方向合法性 (NONE 表示禁止开仓)
LONG_STATES = {MacroState.ONLY_LONG, MacroState.LEAN_LONG, MacroState.NEUTRAL}
SHORT_STATES = {MacroState.ONLY_SHORT, MacroState.LEAN_SHORT, MacroState.NEUTRAL}

_BULLISH_STATES = {MacroState.ONLY_LONG, MacroState.LEAN_LONG}
_BEARISH_STATES = {MacroState.ONLY_SHORT, MacroState.LEAN_SHORT}

# 双轨定时触发：UTC 00/08/16 (Hyperliquid 资金费率为小时结算，仅作低频节拍)
MACRO_SCHEDULE_HOURS_UTC = (0, 8, 16)

# AI_macro 合法输出白名单 (非法枚举一律优雅降级 NEUTRAL)
_VALID_MACRO_STRINGS = {s.value for s in MacroState}

# 趋势基调 -> 本地硬止损 ATR 倍率上限 (Q4 确认保留双止损安全垫)
SL_CAP_TRENDING = 2.5   # ONLY_LONG / ONLY_SHORT
SL_CAP_BIASED = 1.8     # LEAN_LONG / LEAN_SHORT
SL_CAP_RANGING = 1.5    # NEUTRAL


def regime_sl_cap(state: Optional[MacroState]) -> float:
    """宏观基调 -> 本地硬止损 ATR 倍率上限；SYSTEM_HALT 返回 0 (禁止交易)。"""
    if state is None:
        return SL_CAP_RANGING
    if state in {MacroState.ONLY_LONG, MacroState.ONLY_SHORT}:
        return SL_CAP_TRENDING
    if state in {MacroState.LEAN_LONG, MacroState.LEAN_SHORT}:
        return SL_CAP_BIASED
    if state == MacroState.SYSTEM_HALT:
        return 0.0
    return SL_CAP_RANGING


# ==========================================
# 4. TRS 计算
# ==========================================

def calculate_quant_trs(
    df_1h: pd.DataFrame,
    df_4h: pd.DataFrame,
    funding_rate: Union[float, dict],
    current_price: Optional[float] = None,
) -> float:
    """
    量化趋势得分 TRS (-1.0 到 +1.0)。
    趋势项以 6.0*ATR 归一化且资金费率采用 2.5*sigma tanh 平滑压缩，消除均线穿越跳跃。
    """
    price = float(current_price) if current_price else float(df_1h["close"].iloc[-1])

    # --- F_trend: 双周期 EMA200 相对位置 (tanh 平滑) ---
    ema_1h = float(utils.ema_series(df_1h["close"], 200)[-1])
    ema_4h = float(utils.ema_series(df_4h["close"], 200)[-1])
    if np.isnan(ema_1h) or np.isnan(ema_4h):
        raise ValueError("EMA200 历史数据不足 (1h/4h 需 >= 200 根 K 线)")

    atr_1h = max(float(utils.atr(df_1h["high"], df_1h["low"], df_1h["close"], 14)), 1e-4)
    atr_4h = max(float(utils.atr(df_4h["high"], df_4h["low"], df_4h["close"], 14)), 1e-4)
    f_trend = 0.6 * float(np.tanh((price - ema_1h) / (atr_1h * 6.0))) + 0.4 * float(np.tanh((price - ema_4h) / (atr_4h * 6.0)))

    # --- F_mom: Donchian(20, 1h) 位置 x ADX 强度加权 ---
    d_upper, d_mid, d_lower = utils.donchian(df_1h["high"], df_1h["low"], 20)
    adx_1h = utils.adx(df_1h["high"], df_1h["low"], df_1h["close"], 14)
    adx_val = float(adx_1h) if (adx_1h is not None and math.isfinite(adx_1h)) else 0.0
    if d_upper is None or d_lower is None or d_mid is None or not (math.isfinite(d_upper) and math.isfinite(d_lower) and math.isfinite(d_mid)):
        f_mom = 0.0
    else:
        half_width = (d_upper - d_lower) / 2.0
        pos_in_channel = float(utils.clamp((price - d_mid) / max(half_width, 1e-9), -1.0, 1.0))
        f_mom = 0.0 if half_width <= 0 else pos_in_channel * min(adx_val / 50.0, 1.0)

    # --- F_fund: 资金费率平滑压缩 ---
    def _safe_f(val, default=0.0) -> float:
        try:
            res = float(val)
            return res if math.isfinite(res) else default
        except (ValueError, TypeError):
            return default

    if isinstance(funding_rate, dict):
        fr = _safe_f(funding_rate.get("fr"))
        fr_mean = _safe_f(funding_rate.get("fr_mean"))
        fr_std = _safe_f(funding_rate.get("fr_std"))
    else:
        fr, fr_mean, fr_std = _safe_f(funding_rate), 0.0, 0.0

    if fr_std > 0:
        z = (fr - fr_mean) / fr_std
        f_fund = -float(np.tanh(z / 2.5))
    else:
        f_fund = -float(np.tanh(fr / 0.0005)) if fr != 0 else 0.0

    trs = 0.5 * f_trend + 0.35 * f_mom + 0.15 * f_fund
    if not math.isfinite(trs):
        trs = 0.0
    return float(utils.clamp(trs, -1.0, 1.0))


def map_trs_to_state(trs: float) -> MacroState:
    """将 TRS 得分映射为离散宏观状态。"""
    if trs >= 0.60:
        return MacroState.ONLY_LONG
    if trs >= 0.20:
        return MacroState.LEAN_LONG
    if trs > -0.20:
        return MacroState.NEUTRAL
    if trs > -0.60:
        return MacroState.LEAN_SHORT
    return MacroState.ONLY_SHORT


# ==========================================
# 5. 事件触发检测
# ==========================================

def check_macro_event_triggers(df_1h: pd.DataFrame) -> bool:
    """
    检查是否触发了极端波动/变盘事件 (Q6⑥ 参数)：
      1. 波动率激增: 1h ATR14 > 2.0 x SMA24(ATR14, 1h)；
      2. 布林带外轨突破: 1h 收盘超出 BB(20, 2σ) 外轨 且 偏离中轨 > 1.5%。
    """
    if df_1h is None or df_1h.empty or len(df_1h) < 25:
        return False
    if "high" not in df_1h.columns or "low" not in df_1h.columns or "close" not in df_1h.columns:
        return False

    highs = df_1h["high"]
    lows = df_1h["low"]
    closes = df_1h["close"]

    # 条件 1: ATR 激增
    atr_arr = utils.atr_series(highs, lows, closes, 14)
    valid = atr_arr[~np.isnan(atr_arr)]
    if len(valid) >= 25:
        sma24 = float(np.mean(valid[-25:-1]))  # 前 24 个 ATR 值的均值 (不含当前)
        if sma24 > 0 and valid[-1] > 2.0 * sma24:
            logger.debug("[宏观] 变盘触发 | trigger=ATR激增 | ATR=%.6f | SMA24=%.6f",
                         valid[-1], sma24)
            return True

    # 条件 2: 布林带外轨突破且偏离中轨 > 1.5%
    if closes is not None and not closes.empty:
        upper, mid, lower = utils.bollinger(closes, 20, 2.0)
        if upper is not None and mid is not None and mid > 0:
            try:
                close = float(closes.iloc[-1])
                deviation = abs(close - mid) / mid
                if deviation > 0.015 and (close > upper or close < lower):
                    logger.debug("[宏观] 变盘触发 | trigger=BB外轨突破 | close=%.2f | band=[%.2f,%.2f] | 偏离=%.2f%%",
                                 close, lower, upper, deviation * 100)
                    return True
            except (IndexError, TypeError, ValueError):
                pass

    return False


# ==========================================
# 6. AI_macro 调用与单向护栏
# ==========================================

def fetch_ai_macro_state(macro_context: dict, client: Optional[AIClient] = None, return_details: bool = False):
    """
    调用 AI_macro 获取 AI 建议的宏观五态。
    通道异常或非法枚举时返回 None，由调用方保留本地 quant_state，绝不污染本地趋势。
    """
    client = client or AIClient()
    user = json.dumps({
        "task": "Review the quant trend state, technical context, and market_sentiment_and_news to suggest today's macro trading state.",
        "context": macro_context,
    }, ensure_ascii=False, indent=2)
    try:
        result = client.chat_json(build_system_prompt("AI_macro"), user, agent="AI_macro")
    except AIError as e:
        logger.warning("[宏观] AI_macro 调用失败 | action=沿用本地量化态 | err=%s", e)
        send_alert(f"AI_macro 调用失败，已沿用本地量化趋势态: {e}")
        return (None, {}) if return_details else None

    raw = (result or {}).get("suggested_state")
    reason = (result or {}).get("reason", "")
    news_impact = (result or {}).get("news_impact_analysis", "")
    details = {
        "reason": reason,
        "news_impact_analysis": news_impact,
        "raw_response": result or {}
    }

    if not isinstance(raw, str) or raw.strip() not in _VALID_MACRO_STRINGS:
        logger.warning("[宏观] 非法宏观指令 | raw=%r | action=沿用本地量化态", raw)
        send_alert(f"AI_macro 返回非法指令 {raw!r}，已沿用本地量化趋势态。")
        return (None, details) if return_details else None

    state = MacroState(raw.strip())
    logger.info("[宏观] AI_macro 返回 | 建议=%s | reason=%s | news_impact=%s",
                state.value, reason, news_impact)
    if return_details:
        return state, details
    return state


def apply_trend_guardrail(quant_state: MacroState, ai_suggested_state: MacroState) -> MacroState:
    """
    应用单向护栏规则，对 AI 建议的状态进行拦截裁决。

    - SYSTEM_HALT：护栏外黑天鹅特例，直接透传 (由主循环挂起系统)；
    - 本地量化多头 (ONLY/LEAN_LONG) 且 AI 建议空头 (LEAN/ONLY_SHORT) -> 强制 NEUTRAL (硬性阻断反向开仓，仅做避险刹车)；
    - 本地量化空头 且 AI 建议多头 -> 强制 NEUTRAL；
    - 同向微调或 AI 下调至 NEUTRAL：采纳 AI 的调整建议。
    """
    if quant_state == MacroState.SYSTEM_HALT or ai_suggested_state == MacroState.SYSTEM_HALT:
        return MacroState.SYSTEM_HALT

    if quant_state in _BULLISH_STATES and ai_suggested_state in _BEARISH_STATES:
        logger.warning(
            "[宏观] 单向护栏拦截 | 量化=%s | AI=%s | action=强制NEUTRAL",
            quant_state.value, ai_suggested_state.value)
        return MacroState.NEUTRAL

    if quant_state in _BEARISH_STATES and ai_suggested_state in _BULLISH_STATES:
        logger.warning(
            "[宏观] 单向护栏拦截 | 量化=%s | AI=%s | action=强制NEUTRAL",
            quant_state.value, ai_suggested_state.value)
        return MacroState.NEUTRAL

    return ai_suggested_state


# ==========================================
# 7. 本地信号打分
# ==========================================

BASE_SCORE = 50.0
RR_TARGET = 2.0          # raw_tp 依据 R:R = 2.0 反推
RR_MIN = 1.5             # 硬性拦截底线
RR_BONUS = 2.0           # 高盈亏比加分门槛 (R:R >= 2.0)
SL_SWING_BARS = 12       # raw_sl 的 5m 摆动低点/高点回看根数
SL_ATR_MULT = 1.5        # raw_sl 的 ATR 倍率
VOL_SPIKE_MULT = 1.5     # Volume Spike 放大倍数
NEAR_LEVEL_PCT = 0.005   # 阻力/支撑"近在咫尺"阈值 (0.5%)
FR_HOT_8H = 0.0003       # FR 过热阈值 (8h 等效费率 0.03%)

# 信号状态码：前四个由本模块产出，后两个由 main 层依据 SIGNAL_GATE 判定
STATUS_NO_DIRECTION = "NO_DIRECTION"    # 5m 三指标共振不足，无候选方向
STATUS_MACRO_VETO = "MACRO_VETO"        # 候选方向与宏观状态冲突，硬拦截归 0
STATUS_RR_VETO = "RR_VETO"              # R:R < 1.5，硬拦截归 0
STATUS_SCORED = "SCORED"                # 打分完成 (score 可能仍低于门槛)
STATUS_SCORE_LOW = "SCORE_LOW"          # main 层：得分低于 SIGNAL_GATE
STATUS_TRIGGERED = "TRIGGERED"          # main 层：得分达门槛，唤醒 AI_Trade

_SIGNAL_STATUS_LABELS = {
    STATUS_NO_DIRECTION: "无候选方向",
    STATUS_MACRO_VETO: "宏观状态冲突",
    STATUS_RR_VETO: "盈亏比不达标",
    STATUS_SCORED: "已打分",
    STATUS_SCORE_LOW: "得分未达门槛",
    STATUS_TRIGGERED: "触发信号",
}

_BREAKDOWN_LABELS = {
    "base": "基础分",
    "trend_resonance": "趋势共振",
    "multi_indicator": "多指标共振",
    "rr_bonus": "高盈亏比",
    "volume_spike": "成交量放大",
    "near_level": "关键价位",
    "divergence": "指标背离",
    "fr_hot": "资金费率",
    "no_signal": "无信号原因",
    "veto_macro_conflict": "宏观拦截",
    "veto_rr": "盈亏比拦截",
}

_VETO_KEYS = ("no_signal", "veto_macro_conflict", "veto_rr")


def _direction_votes(df_5m: pd.DataFrame, macro_state: Optional[MacroState] = None) -> tuple:
    """
    5m 多空投票计数：
    1. 当处于 ONLY_LONG / ONLY_SHORT 强趋势时，极简切换为顺势回踩条件：
       - 做多：回踩 5m BB中轨(EMA20)附近 (price <= bb_mid * 1.002)、RSI 回调至 45~55 中性区、当根 K 线收出顺势企稳阳线 (close >= open)；
       - 做空：反弹 5m BB中轨(EMA20)附近 (price >= bb_mid * 0.998)、RSI 回弹至 45~55 中性区、当根 K 线收出顺势企稳阴线 (close <= open)；
    2. 其他状态 (NEUTRAL / LEAN_LONG / LEAN_SHORT) 保持原均值回归极值条件 (BB触轨 / RSI极值 / CCI极值)。
    """
    closes = df_5m["close"]
    bb_upper, bb_mid, bb_lower = utils.bollinger(closes, 20, 2.0)
    rsi_5m = utils.rsi(closes, 14)
    cci_5m = utils.cci(df_5m["high"], df_5m["low"], closes, 20)

    price = float(closes.iloc[-1])
    is_bull_candle = bool(df_5m["close"].iloc[-1] >= df_5m["open"].iloc[-1])
    is_bear_candle = bool(df_5m["close"].iloc[-1] <= df_5m["open"].iloc[-1])

    # 强趋势模式：顺势回踩企稳
    if macro_state == MacroState.ONLY_LONG:
        mid_val = bb_mid if (bb_mid is not None and math.isfinite(bb_mid)) else price
        rsi_val = rsi_5m if (rsi_5m is not None and math.isfinite(rsi_5m)) else 50.0
        long_votes = sum((
            price <= mid_val * 1.002,   # 回踩 5m 中轨(EMA20)附近
            40.0 <= rsi_val <= 55.0,    # RSI 回调至中性支撑区
            is_bull_candle,             # 当根 K 线企稳收阳
        ))
        short_votes = 0  # 强多头绝对不做空
        return long_votes, short_votes

    if macro_state == MacroState.ONLY_SHORT:
        mid_val = bb_mid if (bb_mid is not None and math.isfinite(bb_mid)) else price
        rsi_val = rsi_5m if (rsi_5m is not None and math.isfinite(rsi_5m)) else 50.0
        short_votes = sum((
            price >= mid_val * 0.998,   # 反弹 5m 中轨(EMA20)附近
            45.0 <= rsi_val <= 60.0,    # RSI 回弹至中性阻力区
            is_bear_candle,             # 当根 K 线受阻收阴
        ))
        long_votes = 0  # 强空头绝对不做多
        return long_votes, short_votes

    # 常规震荡 / 中性模式：均值回归极值初筛
    rsi_val = rsi_5m if (rsi_5m is not None and math.isfinite(rsi_5m)) else 50.0
    cci_val = cci_5m if (cci_5m is not None and math.isfinite(cci_5m)) else 0.0
    long_votes = sum((
        bb_lower is not None and math.isfinite(bb_lower) and price <= bb_lower,
        rsi_val < 40.0,
        cci_val < -100.0,
    ))
    short_votes = sum((
        bb_upper is not None and math.isfinite(bb_upper) and price >= bb_upper,
        rsi_val > 60.0,
        cci_val > 100.0,
    ))
    return long_votes, short_votes


def _pick_direction(df_5m: pd.DataFrame, macro_state: Optional[MacroState] = None) -> Optional[str]:
    """5m 候选方向：BB / RSI / CCI (或强趋势顺势回踩) 三条件中 2+ 同向即成立。"""
    long_votes, short_votes = _direction_votes(df_5m, macro_state=macro_state)
    if long_votes >= 2 and short_votes >= 2:
        return None
    if long_votes >= 2:
        return "LONG"
    if short_votes >= 2:
        return "SHORT"
    return None


class SlTpResult(tuple):
    def __new__(cls, values, is_structural_tp: bool = False):
        obj = super().__new__(cls, values)
        obj.is_structural_tp = is_structural_tp
        return obj

    @property
    def raw_sl(self):
        return self[0]

    @property
    def raw_tp(self):
        return self[1]

    @property
    def rr(self):
        return self[2]


def _raw_sl_tp(price: float, direction: str, df_5m: pd.DataFrame, df_1h: Optional[pd.DataFrame] = None) -> SlTpResult:
    """
    根据微观结构与支撑阻力位动态计算止损、止盈与盈亏比。
    """
    # 约定传入的 df_5m 已经是已收盘 K 线序列，不再重复剥离最后一行
    df_closed = df_5m
    atr_5m = utils.atr(df_closed["high"], df_closed["low"], df_closed["close"], 14)
    atr_val = atr_5m if (atr_5m is not None and math.isfinite(atr_5m) and atr_5m > 0) else max(price * 0.0015, 1e-4)

    min_sl_dist = 0.8 * atr_val
    max_sl_dist = 2.5 * atr_val

    d_upper, _d_mid, d_lower = utils.donchian(df_closed["high"].iloc[:-1], df_closed["low"].iloc[:-1], 20)
    df_1h_closed = df_1h.iloc[:-1] if (df_1h is not None and len(df_1h) > 1) else df_1h

    is_structural_tp = False

    if direction == "LONG":
        swing = float(np.min(df_closed["low"].iloc[-SL_SWING_BARS:]))
        raw_sl_dist = (price - swing) + 0.3 * atr_val if swing < price else SL_ATR_MULT * atr_val
        sl_dist = float(np.clip(raw_sl_dist, min_sl_dist, max_sl_dist))
        raw_sl = min(price - sl_dist, price * (1.0 - 1e-6))

        min_tp_dist = max(2.0 * atr_val, RR_MIN * sl_dist)
        max_tp_dist = max(4.0 * atr_val, 3.0 * sl_dist)

        target_tp = None
        # 解耦微噪点过滤与最小盈亏比要求，使 1.5R 以内的真实阻力能够真实触发 RR_VETO
        noise_floor = max(1.0 * atr_val, price * 0.002)
        if df_1h_closed is not None and len(df_1h_closed) >= 10:
            tail_1h = df_1h_closed.tail(48)
            resistances, _ = utils.fractal_levels(tail_1h["high"], tail_1h["low"], 2, 2)
            valid_res = [r for r in resistances if r >= price + noise_floor]
            if valid_res:
                target_tp = min(valid_res)
                is_structural_tp = True

        if target_tp is None and d_upper is not None and d_upper >= price + noise_floor:
            target_tp = float(d_upper)
            is_structural_tp = True

        if target_tp is None:
            target_tp = price + max(min_tp_dist, RR_BONUS * sl_dist)
            is_structural_tp = False
        else:
            target_tp = min(target_tp, price + max_tp_dist)

        raw_tp = target_tp
    else:
        swing = float(np.max(df_closed["high"].iloc[-SL_SWING_BARS:]))
        raw_sl_dist = (swing - price) + 0.3 * atr_val if swing > price else SL_ATR_MULT * atr_val
        sl_dist = float(np.clip(raw_sl_dist, min_sl_dist, max_sl_dist))
        raw_sl = max(price + sl_dist, price * (1.0 + 1e-6))

        min_tp_dist = max(2.0 * atr_val, RR_MIN * sl_dist)
        max_tp_dist = max(4.0 * atr_val, 3.0 * sl_dist)

        target_tp = None
        noise_floor = max(1.0 * atr_val, price * 0.002)
        if df_1h_closed is not None and len(df_1h_closed) >= 10:
            tail_1h = df_1h_closed.tail(48)
            _, supports = utils.fractal_levels(tail_1h["high"], tail_1h["low"], 2, 2)
            valid_sup = [s for s in supports if s <= price - noise_floor]
            if valid_sup:
                target_tp = max(valid_sup)
                is_structural_tp = True

        if target_tp is None and d_lower is not None and d_lower <= price - noise_floor:
            target_tp = float(d_lower)
            is_structural_tp = True

        if target_tp is None:
            target_tp = price - max(min_tp_dist, RR_BONUS * sl_dist)
            is_structural_tp = False
        else:
            target_tp = max(target_tp, price - max_tp_dist)

        raw_tp = target_tp

    rr = abs(raw_tp - price) / max(abs(price - raw_sl), 1e-9)
    return SlTpResult((raw_sl, raw_tp, rr), is_structural_tp=is_structural_tp)


def calculate_signal_score(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    macro_state: MacroState,
    funding_stats: Optional[dict] = None,
) -> dict:
    """
    计算本地信号分 SS。
    """
    breakdown = {"base": BASE_SCORE}

    direction = _pick_direction(df_5m, macro_state=macro_state)
    if direction is None:
        long_votes, short_votes = _direction_votes(df_5m, macro_state=macro_state)
        return {"score": 0.0, "direction": None, "rr_ratio": 0.0,
                "raw_sl": None, "raw_tp": None,
                "status": STATUS_NO_DIRECTION,
                "breakdown": {"no_signal":
                              f"5m 共振不足 2/3 (多={long_votes} 空={short_votes})，无候选方向"}}

    # 硬性拦截与宏观分级检查
    is_counter_trend = False
    if direction == "LONG":
        if macro_state == MacroState.ONLY_SHORT:
            return {"score": 0.0, "direction": direction, "rr_ratio": 0.0,
                    "raw_sl": None, "raw_tp": None,
                    "status": STATUS_MACRO_VETO,
                    "breakdown": {"veto_macro_conflict":
                                  f"方向 {direction} 与宏观状态 {macro_state.value} 冲突 (绝对禁止做多)，得分归 0"}}
        elif macro_state == MacroState.LEAN_SHORT:
            is_counter_trend = True
    elif direction == "SHORT":
        if macro_state == MacroState.ONLY_LONG:
            return {"score": 0.0, "direction": direction, "rr_ratio": 0.0,
                    "raw_sl": None, "raw_tp": None,
                    "status": STATUS_MACRO_VETO,
                    "breakdown": {"veto_macro_conflict":
                                  f"方向 {direction} 与宏观状态 {macro_state.value} 冲突 (绝对禁止做空)，得分归 0"}}
        elif macro_state == MacroState.LEAN_LONG:
            is_counter_trend = True

    if macro_state == MacroState.SYSTEM_HALT:
        return {"score": 0.0, "direction": direction, "rr_ratio": 0.0,
                "raw_sl": None, "raw_tp": None,
                "status": STATUS_MACRO_VETO,
                "breakdown": {"veto_macro_conflict": "宏观状态 SYSTEM_HALT 熔断，得分归 0"}}

    price = float(df_5m["close"].iloc[-1])
    sltp_res = _raw_sl_tp(price, direction, df_5m, df_1h)
    raw_sl, raw_tp, rr = sltp_res.raw_sl, sltp_res.raw_tp, sltp_res.rr

    if not math.isfinite(rr) or rr < RR_MIN:
        return {"score": 0.0, "direction": direction, "rr_ratio": rr,
                "raw_sl": raw_sl, "raw_tp": raw_tp,
                "status": STATUS_RR_VETO,
                "breakdown": {"veto_rr": f"R:R {rr:.2f} < {RR_MIN}，得分归 0"}}

    score = BASE_SCORE

    # --- +15 趋势共振: Close_1h vs EMA200(1h) 与信号方向一致 ---
    ema200_1h = float(utils.ema_series(df_1h["close"], 200)[-1])
    close_1h = float(df_1h["close"].iloc[-1])
    if not np.isnan(ema200_1h):
        trend_long = close_1h > ema200_1h
        if (direction == "LONG" and trend_long) or (direction == "SHORT" and not trend_long):
            score += 15.0
            breakdown["trend_resonance"] = "+15 (1h 结构与信号同向)"
        else:
            breakdown["trend_resonance"] = "+0 (1h 结构与信号反向)"
    else:
        breakdown["trend_resonance"] = "+0 (1h EMA200 数据不足)"

    # --- 多指标共振: 3/3 得 +10，2/3 仅为门槛准入记 +0 ---
    long_v, short_v = _direction_votes(df_5m)
    votes = long_v if direction == "LONG" else short_v
    if votes >= 3:
        score += 10.0
        breakdown["multi_indicator"] = "+10 (BB轨 / RSI / CCI 3/3 强共振)"
    else:
        breakdown["multi_indicator"] = "+0 (BB轨 / RSI / CCI 2/3 门槛准入)"

    # --- 盈亏比奖励: 具备真实结构位且 R:R >= 2.0 得 +10，真空区突破记 +0 ---
    if getattr(sltp_res, "is_structural_tp", False) and rr >= RR_BONUS:
        score += 10.0
        breakdown["rr_bonus"] = f"+10 (结构目标 R:R {rr:.2f} >= {RR_BONUS})"
    else:
        breakdown["rr_bonus"] = f"+0 (R:R {rr:.2f})"

    # --- 宏观倾向扣分 ---
    if is_counter_trend:
        score -= 15.0
        breakdown["macro_bias"] = "-15 (逆宏观倾向扣分)"

    # --- +15 成交量放大: Volume > 1.5 x SMA20(Volume) 并突破 Donchian(20, 5m) ---
    # --- +15 成交量确认: 顺势放量突破 或 超卖/超买放量企稳反转 ---
    vols = df_5m["volume"]
    vol_spike = False
    if len(vols) >= 21:
        # 使用已收盘序列计算均量，避免未收盘K线干扰
        closed_vols = vols.iloc[:-1] if len(vols) > 21 else vols
        sma20_vol = float(np.mean(closed_vols.iloc[-20:]))
        cur_vol = float(vols.iloc[-1])
        d_upper, _d_mid, d_lower = utils.donchian(
            df_5m["high"].iloc[:-1], df_5m["low"].iloc[:-1], 20)
        
        if sma20_vol > 0 and cur_vol > VOL_SPIKE_MULT * sma20_vol:
            # 模式 A: 顺势动量突破
            if d_upper is not None and d_lower is not None:
                if direction == "LONG" and float(df_5m["close"].iloc[-1]) > d_upper:
                    vol_spike = True
                elif direction == "SHORT" and float(df_5m["close"].iloc[-1]) < d_lower:
                    vol_spike = True
            
            # 模式 B: 均值回归反转K线放量企稳 (破前高低 / 锤子线 / 流星线 / 吞没形态)
            if not vol_spike and len(df_5m) >= 3:
                c1 = float(df_5m["close"].iloc[-1])
                o1 = float(df_5m["open"].iloc[-1])
                h1 = float(df_5m["high"].iloc[-1])
                l1 = float(df_5m["low"].iloc[-1])
                body1 = abs(c1 - o1)

                c2 = float(df_5m["close"].iloc[-2])
                o2 = float(df_5m["open"].iloc[-2])
                h2 = float(df_5m["high"].iloc[-2])
                l2 = float(df_5m["low"].iloc[-2])

                tol = 1e-4 * c1

                # 破前K极值
                is_break_prev = (direction == "LONG" and c1 > o1 and c1 > h2) or                                 (direction == "SHORT" and c1 < o1 and c1 < l2)

                # 锤子线 (多) / 流星线 (空)
                is_pinbar = (direction == "LONG" and c1 > o1 and (o1 - l1) > 1.8 * max(body1, tol) and (h1 - c1) <= 0.3 * body1 + tol) or                             (direction == "SHORT" and c1 < o1 and (h1 - o1) > 1.8 * max(body1, tol) and (c1 - l1) <= 0.3 * body1 + tol)

                # 吞没形态 (看涨吞没 / 看跌吞没，允许连续报价微小容差)
                is_engulf = (direction == "LONG" and c1 > o1 and c2 < o2 and c1 >= o2 - tol and o1 <= c2 + tol) or                             (direction == "SHORT" and c1 < o1 and c2 > o2 and c1 <= o2 + tol and o1 >= c2 - tol)

                if is_break_prev or is_pinbar or is_engulf:
                    vol_spike = True

    if vol_spike:
        score += 15.0
        breakdown["volume_spike"] = "+15 (量能放大突破/企稳反转)"

    # --- 微观结构止跌/企稳反转确认 (+10分) ---
    is_micro_break = False
    if len(df_5m) >= 2:
        c1 = float(df_5m["close"].iloc[-1])
        h2 = float(df_5m["high"].iloc[-2])
        l2 = float(df_5m["low"].iloc[-2])
        if direction == "LONG" and c1 > h2:
            is_micro_break = True
        elif direction == "SHORT" and c1 < l2:
            is_micro_break = True
    if is_micro_break:
        score += 10.0
        breakdown["micro_break"] = "+10 (5m 微观结构止跌突破)"

    # --- 关键位阻尼/加速 ---
    near_level = False
    if len(df_1h) >= 20:
        d1_up, d1_mid, d1_low = utils.donchian(df_1h["high"].iloc[:-1], df_1h["low"].iloc[:-1], 20)
        if d1_up is not None and d1_low is not None:
            # 顺势突破或在多头/空头结构关键位企稳
            if direction == "LONG" and (price >= d1_up or (d1_mid is not None and price >= d1_mid)):
                near_level = True
            elif direction == "SHORT" and (price <= d1_low or (d1_mid is not None and price <= d1_mid)):
                near_level = True
    if near_level:
        score += 10.0
        breakdown["near_level"] = "+10 (1h 唐奇安通道结构确认)"

        # --- 资金费率扣分 (支持标准与SDK别名，强防御 None/空字符串/非法值) ---
    if isinstance(funding_stats, dict):
        def _safe_float(val, default=0.0) -> float:
            try:
                return float(val) if val is not None and str(val).strip() != "" else default
            except (ValueError, TypeError):
                return default

        fr = _safe_float(funding_stats.get("fr") or funding_stats.get("current_rate") or funding_stats.get("funding_rate"))
        fr_mean = _safe_float(funding_stats.get("fr_mean") or funding_stats.get("mean_rate"))
        fr_std = _safe_float(funding_stats.get("fr_std") or funding_stats.get("std_rate"))
        z_fr = funding_stats.get("fr_zscore")
        if z_fr is None and fr_std > 0:
            z_fr = (fr - fr_mean) / fr_std
        if z_fr is not None:
            z_val = _safe_float(z_fr, default=0.0)
            if (direction == "LONG" and z_val > 1.5) or (direction == "SHORT" and z_val < -1.5):
                score -= 15.0
                breakdown["fr_hot"] = f"-15 (资金费率过度拥挤惩罚: z={z_val:+.2f})" 

    score = float(np.clip(score, 0.0, 100.0))
    res_dict = {
        "score": score,
        "direction": direction,
        "rr_ratio": rr,
        "raw_sl": raw_sl,
        "raw_tp": raw_tp,
        "status": STATUS_TRIGGERED if score >= SIGNAL_GATE else STATUS_SCORE_LOW,
        "breakdown": breakdown,
    }
    if is_counter_trend:
        res_dict["is_counter_trend"] = True
    return res_dict


def format_signal_report(signal_res: dict, gate: float,
                         status: Optional[str] = None) -> str:
    """把 calculate_signal_score 结果渲染为多行日志文本。

    status 传入时覆盖结果自带的状态码 (供 main 层区分 SCORE_LOW / TRIGGERED)。
    输出示例::

        [信号] 得分未达门槛 | 状态=SCORE_LOW | 方向=LONG | SS=45.0/70 | 缺口=-25.0
            - 基础分: 50
            - 趋势共振: +15 (1h 结构与信号同向)
            ...
            - 合计: 45.0 (R:R=2.00)
    """
    if not signal_res or not isinstance(signal_res, dict):
        return f"[信号] 无有效信号字典 (空数据)"

    status = status or signal_res.get("status", STATUS_SCORED)
    label = _SIGNAL_STATUS_LABELS.get(status, str(status))
    direction = signal_res.get("direction") or "None"
    
    def _safe_float(val, default=0.0) -> float:
        try:
            f = float(val)
            return f if math.isfinite(f) else default
        except (ValueError, TypeError):
            return default

    score = _safe_float(signal_res.get("score"), 0.0)
    rr = _safe_float(signal_res.get("rr_ratio"), 0.0)

    header = (f"[信号] {label} | 状态={status} | 方向={direction} | SS={score:.1f}")
    if status in (STATUS_SCORE_LOW, STATUS_TRIGGERED):
        header += f"/{gate:.0f} | 缺口={score - gate:+.1f}"

    lines = [header]
    breakdown = signal_res.get("breakdown")
    if not isinstance(breakdown, dict):
        breakdown = {}

    veto_key = next((k for k in _VETO_KEYS if k in breakdown), None)
    if veto_key is not None:
        veto_label = _BREAKDOWN_LABELS.get(veto_key, veto_key)
        lines.append(f"    - {veto_label}: {breakdown.get(veto_key, '')}")
        return "\n".join(lines)

    for key, value in breakdown.items():
        lines.append(f"    - {_BREAKDOWN_LABELS.get(key, key)}: {value}")
    if "base" in breakdown:
        lines.append(f"    - 合计: {score:.1f} (R:R={rr:.2f})")
    return "\n".join(lines)


# ==========================================
# 8. 动态仓位计算
# ==========================================

def calculate_position_size(
    equity: float,
    price: float,
    atr_14: float,
    sz_min: float = 10.0,
    sz_max_leverage: float = 3.0,
) -> float:
    """
    计算开仓名义金额 (sz_notional)。

    5m 周期标定：日线量级 k=0.75 在 5m ATR/Price（常见 0.1%~0.5%）下会恒顶到
    杠杆上限。改为 5m 暴露系数 k_5m = 0.004，使中位杠杆落在 1.5x~2.0x：
        sz_notional = Clamp( Equity * 0.004 / (ATR14_5m / Price), sz_min, 3.0 * Equity )

    参数:
        equity (float): 当前账户 USDT 净值
        price (float): 当前标的标价
        atr_14 (float): 当前 14 周期 ATR 绝对值
        sz_min (float): 交易所最小下单名义金额 (USDT)
        sz_max_leverage (float): 单笔最大名义敞口相对 Equity 的杠杆倍数上限 (默认 3.0x)

    返回:
        float: 最终下订单的名义 USDT 金额 (sz_notional)
    """
    if equity <= 0 or price <= 0:
        raise ValueError(f"equity({equity}) 与 price({price}) 必须为正数")

    sz_max = sz_max_leverage * equity
    if sz_max < sz_min:
        raise ValueError(
            f"账户权益({equity:.2f} USDT)不足以在安全杠杆上限({sz_max_leverage}x)内满足最小下单金额({sz_min} USDT)"
        )

    # ATR 不可用 (数据不足) 时无法评估波动率，按最小名义金额处理，绝不裸奔满仓
    if atr_14 is None or atr_14 <= 0:
        return sz_min

    rel_vol = atr_14 / price
    if rel_vol <= 0:
        return sz_min

    # 5m 周期暴露系数：中位 0.25% 波动对应约 1.6x 杠杆
    risk_factor = 0.004
    raw = equity * risk_factor / rel_vol
    return max(sz_min, min(raw, sz_max))


# ==========================================
# 9. 持仓 FSM
# ==========================================

BREAK_EVEN_R = 1.0        # 浮盈达到 1.0R 启动保本
BREAK_EVEN_MARGIN = 0.0015  # 保本滑点边际 0.05%
TRAIL_START_R = 1.5       # 浮盈超过 1.5R 启动追踪
TRAIL_ATR_MULT = 1.5      # 追踪止损回撤距离 = 1.5 x ATR14
TIME_DECAY_BARS = 24      # 24 根 5m K 线 = 2 小时
TIME_DECAY_MIN_R = 0.5    # 时间衰减的浮盈上限 (< 0.5R 才触发)


@dataclass
class Position:
    """一笔活跃持仓的本地记账 (与交易所侧定期对账)。"""
    entry_price: float
    direction: str            # "LONG" 或 "SHORT"
    sz_notional: float
    tp_price: float
    sl_price: float
    initial_risk_r: float     # R = |entry - SL_initial|，开仓时锁定
    peak_price: float = 0.0   # 多单跟踪最高价 / 空单跟踪最低价
    bars_held: int = 0        # 已持仓的 5m K 线数 (main 在 K 线边界推进)
    opened_at: float = field(default_factory=time.time)
    entry_score: float = 75.0
    filled_sz: float = 1.0
    is_breakeven_set: bool = False  # 保本状态锁：单笔持仓周期内仅触发一次，避免重复计算与改单扰动

    def __post_init__(self):
        # 防御 entry_price 与 peak_price
        try:
            self.entry_price = float(self.entry_price)
        except (ValueError, TypeError):
            self.entry_price = 0.0

        if getattr(self, "peak_price", 0.0) <= 0 or not math.isfinite(self.peak_price):
            self.peak_price = self.entry_price

        # 防御 initial_risk_r 为 0 / 负 / NaN
        ir = getattr(self, "initial_risk_r", 0.0)
        if ir is None or not math.isfinite(ir) or ir <= 1e-9:
            try:
                sl = float(getattr(self, "sl_price", 0.0) or 0.0)
                self.initial_risk_r = max(abs(self.entry_price - sl), abs(self.entry_price) * 1e-3, 1e-6)
            except Exception:
                self.initial_risk_r = max(abs(self.entry_price) * 1e-3, 1e-6)

        # 统一 opened_at 为 float 时间戳，兼容 datetime 实例输入
        if hasattr(self, "opened_at"):
            if isinstance(self.opened_at, datetime):
                self.opened_at = float(self.opened_at.timestamp())
            else:
                try:
                    self.opened_at = float(self.opened_at)
                except (ValueError, TypeError):
                    self.opened_at = time.time()

    def profit_r(self, current_price: float) -> float:
        """以 R 为单位的当前浮动盈亏 (带符号)。防御除零与 NaN。"""
        if not (math.isfinite(current_price) and current_price > 0):
            return 0.0
        ir = getattr(self, "initial_risk_r", 0.0)
        if ir is None or not math.isfinite(ir) or ir <= 1e-9:
            ir = max(abs(self.entry_price - getattr(self, "sl_price", 0.0)), abs(self.entry_price) * 1e-3, 1e-6)
        diff = (current_price - self.entry_price) if self.direction == "LONG" else (self.entry_price - current_price)
        res = diff / max(ir, 1e-6)
        return float(res) if math.isfinite(res) else 0.0


def update_position_fsm(position: Position, current_price: float,
                        current_atr: float) -> str:
    """
    每秒/每根 K 线刷新时调用。
    返回动作指令: "HOLD", "CLOSE_TP", "CLOSE_SL", "CLOSE_TIME_DECAY"
    同时在内部更新 position.sl_price (移动止损/保本单)。

    硬性约束：
    1. 【单向棘轮锁】：SL 移动时做多严格 new_sl > current_sl，做空严格 new_sl < current_sl；
       严禁因 ATR 放大导致 1.5R 追踪止损价退回到保本价或更低位置。
    2. 【判断倒置与保本锁】：优先判定 >= 1.5R 追踪，再判定 >= 1.0R 保本（is_breakeven_set 锁）。
    """
    # 安全校验：价格与 ATR 有效性防御
    if not (math.isfinite(current_price) and current_price > 0):
        return "HOLD"
    valid_atr = float(current_atr) if (current_atr is not None and math.isfinite(current_atr) and current_atr > 0) else 0.0

    # 动态推进 bars_held，防止跨进程或丢帧导致有状态计数遗漏 (兼容 opened_at 为 float/datetime)
    if hasattr(position, "opened_at") and position.opened_at:
        try:
            o_ts = position.opened_at.timestamp() if isinstance(position.opened_at, datetime) else float(position.opened_at)
            elapsed_bars = int((time.time() - o_ts) // 300)
            position.bars_held = max(position.bars_held, elapsed_bars)
        except Exception:
            pass

    if position.direction == "LONG":
        # 1. 硬止盈 / 硬止损
        if position.tp_price and current_price >= position.tp_price:
            return "CLOSE_TP"
        if position.sl_price and current_price <= position.sl_price:
            return "CLOSE_SL"

        # 2. peak 跟踪
        cur_peak = getattr(position, "peak_price", None)
        position.peak_price = max(float(cur_peak), current_price) if (cur_peak is not None and math.isfinite(cur_peak)) else current_price
        profit_r = position.profit_r(current_price)
        if not math.isfinite(profit_r):
            profit_r = 0.0

        # 3. 动态追踪止损 (优先判定，避免被保本逻辑覆盖)
        if profit_r > TRAIL_START_R and valid_atr > 0:
            trail = position.peak_price - TRAIL_ATR_MULT * valid_atr
            # 单向棘轮：严格只向上收紧，绝不回退
            if position.sl_price is None or trail > position.sl_price:
                position.sl_price = trail
                position.is_breakeven_set = True
        # 4. 保本单: 浮盈 >= 1.0R (仅触发一次状态锁，且单向收紧)
        elif profit_r >= BREAK_EVEN_R and not getattr(position, "is_breakeven_set", False):
            be = position.entry_price * (1.0 + BREAK_EVEN_MARGIN)
            if position.sl_price is None or be > position.sl_price:
                position.sl_price = be
                position.is_breakeven_set = True

        # 5. 时间衰减平仓: 持仓 >= 24 根 5m K 线且浮盈 < 0.5R
        if position.bars_held >= TIME_DECAY_BARS and profit_r < TIME_DECAY_MIN_R:
            return "CLOSE_TIME_DECAY"

        return "HOLD"

    # ---------------- SHORT (对称) ----------------
    if position.tp_price and current_price <= position.tp_price:
        return "CLOSE_TP"
    if position.sl_price and current_price >= position.sl_price:
        return "CLOSE_SL"

    cur_peak = getattr(position, "peak_price", None)
    position.peak_price = min(float(cur_peak), current_price) if (cur_peak is not None and math.isfinite(cur_peak)) else current_price
    profit_r = position.profit_r(current_price)
    if not math.isfinite(profit_r):
        profit_r = 0.0

    # 3. 动态追踪止损 (优先判定)
    if profit_r > TRAIL_START_R and valid_atr > 0:
        trail = position.peak_price + TRAIL_ATR_MULT * valid_atr
        # 单向棘轮：做空严格只向下收紧，绝不回退
        if position.sl_price is None or trail < position.sl_price:
            position.sl_price = trail
            position.is_breakeven_set = True
    # 4. 保本单: 浮盈 >= 1.0R
    elif profit_r >= BREAK_EVEN_R and not getattr(position, "is_breakeven_set", False):
        be = position.entry_price * (1.0 - BREAK_EVEN_MARGIN)
        if position.sl_price is None or be < position.sl_price:
            position.sl_price = be
            position.is_breakeven_set = True

    # 5. 时间衰减平仓
    if position.bars_held >= TIME_DECAY_BARS and profit_r < TIME_DECAY_MIN_R:
        return "CLOSE_TIME_DECAY"

    return "HOLD"

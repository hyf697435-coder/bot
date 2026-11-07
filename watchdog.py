# -*- coding: utf-8 -*-
"""
交易机器人看门狗与自愈守护进程 (Watchdog)
═══════════════════════════════════════════════════════════════════════

核心功能：
1. 监控主交易程序 (main.py) 的进程存活性
2. 基于 .heartbeat 心跳时间戳监控假死 (默认 150 秒超时)
3. 进程异常退出或假死时自动撤单保护并重启自愈
4. 峰值回撤 (30%) 或单日亏损 (15%) 硬触达时停止重启，进入安全停机
5. 提供平滑重启与日志告警
"""

__version__ = "1.0.0"
__milestone__ = "v1.0-RELEASE"
__release_date__ = "2026-11-01"


import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from alerts import logger, send_alert

HEARTBEAT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".heartbeat")
HEARTBEAT_TIMEOUT_SECONDS = 150.0
CHECK_INTERVAL_SECONDS = 10.0
MAX_DRAWDOWN_LIMIT_PCT = 30.0
DAILY_LOSS_LIMIT_PCT = 15.0


def emergency_cancel_orders():
    """紧急保护：仅清理普通未成交限价挂单，始终保留远端止损/止盈 reduce-only 触发保护单，严防持仓裸奔"""
    try:
        from exchange import HyperliquidExecutor
        ex = HyperliquidExecutor()
        if ex.ready:
            logger.warning("[看门狗] 启动紧急保护：仅清理非触发普通限价挂单，保留远端止损/止盈触发保护单！")
            ex.cancel_non_trigger_orders()
            logger.info("[看门狗] 紧急保护挂单清理完成 (触发保护单完好)")
    except Exception as e:
        logger.error(f"[看门狗] 紧急撤单/保护检查失败: {e}")


def read_heartbeat() -> dict:
    """读取最新心跳文件"""
    if not os.path.exists(HEARTBEAT_FILE):
        return {}
    try:
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def run_watchdog():
    logger.info("[看门狗] 交易自愈守护进程已启动 | 检查间隔=%ss | 心跳超时=%ss",
                CHECK_INTERVAL_SECONDS, HEARTBEAT_TIMEOUT_SECONDS)
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(script_dir, "main.py")
    cmd = [sys.executable, script_path]
    
    while True:
        logger.info("[看门狗] 正在启动交易机器人: %s", script_path)
        # 清理旧心跳
        if os.path.exists(HEARTBEAT_FILE):
            try:
                os.remove(HEARTBEAT_FILE)
            except Exception:
                pass
                
        proc = subprocess.Popen(cmd, cwd=script_dir)
        start_time = time.time()
        
        while True:
            time.sleep(CHECK_INTERVAL_SECONDS)

            # 1. 检查子进程是否已退出
            retcode = proc.poll()
            if retcode is not None:
                msg = f"交易机器人主进程异常退出 | 退出码={retcode} | 准备自愈重启"
                logger.error("[看门狗] %s", msg)
                send_alert(msg)
                emergency_cancel_orders()
                time.sleep(3)
                break

            # 2. 启动宽限期 (前 30 秒等待初始化)
            uptime = time.time() - start_time
            if uptime < 30.0:
                continue

            hb = read_heartbeat()
            # 若超过超时阈值仍未生成心跳文件，同样判定为初始化假死
            hb_ts = float(hb.get("timestamp", start_time)) if hb else start_time
            delay = time.time() - hb_ts

            # 3. 检查硬熔断条件（单日亏损或最大回撤触及红线时安全停机，禁止无限重启）
            if hb:
                daily_loss = float(hb.get("daily_loss_pnl", 0.0))
                peak_equity = float(hb.get("peak_equity", 1000.0))
                if peak_equity > 0 and daily_loss < 0:
                    daily_loss_pct = abs(daily_loss) / peak_equity * 100.0
                    if daily_loss_pct >= DAILY_LOSS_LIMIT_PCT:
                        msg = f"触发单日亏损硬熔断 ({daily_loss_pct:.2f}% >= {DAILY_LOSS_LIMIT_PCT}%)，看门狗停止拉起并安全停机"
                        logger.critical("[看门狗] %s", msg)
                        send_alert(msg)
                        proc.terminate()
                        emergency_cancel_orders()
                        return

            # 4. 心跳假死判定 (状态与阶段感知动态阈值)
            hb_state = hb.get("state", "UNKNOWN") if hb else "UNKNOWN"
            hb_phase = hb.get("phase", "") if hb else ""
            if hb_state in ("COOL_DOWN", "SYSTEM_HALT"):
                timeout_limit = 600.0
            elif hb_phase in ("AI_MACRO", "AI_TRADE", "FETCH_MARKET"):
                timeout_limit = 180.0
            else:
                timeout_limit = HEARTBEAT_TIMEOUT_SECONDS

            if delay > timeout_limit:
                msg = f"交易机器人假死检测：state={hb_state} phase={hb_phase} 心跳超过 {delay:.1f}s (阈值 {timeout_limit:.0f}s) 未更新，执行安全自愈重启"
                logger.critical("[看门狗] %s", msg)
                send_alert(msg)
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
                except Exception as e:
                    logger.error(f"[看门狗] 终止僵死进程异常: {e}")
                emergency_cancel_orders()
                time.sleep(5)
                break

if __name__ == "__main__":
    try:
        run_watchdog()
    except KeyboardInterrupt:
        logger.info("[看门狗] 收到终止信号，安全退出")

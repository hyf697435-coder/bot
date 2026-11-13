# -*- coding: utf-8 -*-
"""
公共告警底座：日志双写 + SMTP 邮件通知 (Mailer)。

旧 core.py 中的 setup_logging / send_alert / Mailer 装配收拢于此，
供 main / exchange / strategy 等模块统一引用；
通知类故障一律吞掉并降级为日志，绝不阻塞交易主流程。

Mailer 职责 (后台守护线程 + 队列，非阻塞)：
  1. 单个模型当日累计报错达到阈值 (默认 5 次) —— 邮件含模型名与每次报错时间；
  2. 平仓后 —— 简要汇报该笔盈亏；
  3. 触发应急接管 —— 汇报触发原因与动作。

注意：本文件内使用 `from email.mime.text import MIMEText` 导入标准库 email 包，
工作区必须始终保持有 alerts.py 这一文件名 (历史上的独立 email.py 曾连带破坏
smtplib 对 email.mime / email.utils 的导入)。
"""

__version__ = "1.0.0"
__milestone__ = "v1.0-RELEASE"
__release_date__ = "2026-11-01"


import logging
import os
import queue
import smtplib
import sys
import threading
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from typing import Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


def _resolve_log_path() -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    parent_log_dir = os.path.join(os.path.dirname(base_dir), "log")
    env_log = os.getenv("LOG_FILE")
    if env_log:
        if os.path.isabs(env_log):
            resolved = env_log
        else:
            resolved = os.path.abspath(os.path.join(base_dir, env_log))
        try:
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            return resolved
        except Exception:
            pass
    try:
        os.makedirs(parent_log_dir, exist_ok=True)
        return os.path.join(parent_log_dir, "trading_bot.log")
    except Exception:
        return os.path.join(base_dir, "trading_bot.log")

LOG_FILE = _resolve_log_path()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# 日志保留策略: 0 = 每周清零（仅保留当周）；>0 = 额外保留最近 N 周历史备份
LOG_KEEP_WEEKS = max(0, _env_int("LOG_KEEP_WEEKS", 0))


class WeeklyResetFileHandler(logging.FileHandler):
    """
    按自然周自动清零的日志文件处理器（以周一 00:00 为界）。

    跨周首次写入时:
      - LOG_KEEP_WEEKS=0 (默认): 直接截断当前文件，仅保留当周日志；
      - LOG_KEEP_WEEKS>0: 先把上周日志改名为 <LOG_FILE>.<年-W周>，再新建当周文件，
        并清理超出保留数量的历史备份。
    """

    def __init__(self, filename: str, encoding: str = "utf-8", keep_weeks: int = 0):
        super().__init__(filename, encoding=encoding)
        self.keep_weeks = max(0, int(keep_weeks))
        self._week = self._current_week()
        self._reset_if_stale()

    @staticmethod
    def _current_week() -> str:
        year, week, _ = datetime.now().isocalendar()
        return f"{year}-W{week:02d}"

    @staticmethod
    def _week_of(timestamp: float) -> str:
        year, week, _ = datetime.fromtimestamp(timestamp).isocalendar()
        return f"{year}-W{week:02d}"

    def _reset_if_stale(self):
        """进程启动时文件若属于上周，立即清零/归档，避免跨周停机残留旧日志。"""
        try:
            file_week = self._week_of(os.path.getmtime(self.baseFilename))
        except OSError:
            return
        if file_week != self._week:
            try:
                self._rotate(file_week)
            except Exception:
                pass

    def emit(self, record):
        week = self._current_week()
        if week != self._week:
            try:
                self._rotate(self._week)
            except Exception:
                self.handleError(record)
            self._week = week
        super().emit(record)

    def _rotate(self, old_week: str):
        self.acquire()
        try:
            if self.stream:
                self.stream.flush()
                self.stream.close()
                self.stream = None
            if self.keep_weeks > 0 and os.path.exists(self.baseFilename):
                # 归档上周日志后新建当周文件（原文件已改名，append 模式即为空文件）
                os.replace(self.baseFilename, f"{self.baseFilename}.{old_week}")
                self._prune_backups()
                self.stream = self._open()
            else:
                # 仅保留当周: 覆盖模式重开，直接截断历史内容
                self.stream = open(self.baseFilename, "w", encoding=self.encoding)
        finally:
            self.release()

    def _prune_backups(self):
        base = os.path.basename(self.baseFilename)
        prefix = base + "."
        directory = os.path.dirname(self.baseFilename) or "."
        try:
            backups = sorted(
                f for f in os.listdir(directory)
                if f.startswith(prefix) and len(f) > len(prefix)
            )
        except OSError:
            return
        for stale in backups[:max(0, len(backups) - self.keep_weeks)]:
            try:
                os.remove(os.path.join(directory, stale))
            except OSError:
                pass


def setup_logging() -> logging.Logger:
    """配置控制台 + 文件双写 logger（日志文件按自然周清零），全模块统一使用。"""
    logger = logging.getLogger("trading_bot")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    logger.propagate = False
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        file_handler = WeeklyResetFileHandler(LOG_FILE, keep_weeks=LOG_KEEP_WEEKS)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except Exception as e:  # 文件不可写时至少保留控制台输出
        logger.warning("[告警] 日志文件不可写 | file=%s | err=%s", LOG_FILE, e)

    return logger


logger = setup_logging()


def categorize_error(e: Exception) -> str:
    """
    错误归类判别器 (Feature 11 契约实现)：
    严格区分交易所行情、交易所执行、AI中转站与程序内部运行时异常四类，杜绝模糊指代。
    """
    try:
        from exchange import MarketDataError, ExecutionError
    except ImportError:
        MarketDataError = ExecutionError = ()
    try:
        from strategy import AIError
    except ImportError:
        AIError = ()
    try:
        import requests
        req_err = (requests.RequestException,)
    except ImportError:
        req_err = ()
    try:
        import httpx
        httpx_err = (httpx.HTTPError,)
    except ImportError:
        httpx_err = ()

    internal_errs = (
        NameError, TypeError, AttributeError, SyntaxError,
        IndexError, KeyError, ZeroDivisionError, ImportError, UnboundLocalError
    )
    if isinstance(e, internal_errs):
        return "[交易程序运行时异常]"
    if ExecutionError and isinstance(e, ExecutionError):
        return "[交易所下单执行异常]"
    if (MarketDataError and isinstance(e, MarketDataError)) or (req_err and isinstance(e, req_err)) or (httpx_err and isinstance(e, httpx_err)):
        return "[交易所行情异常]"
    if AIError and isinstance(e, AIError):
        return "[AI中转站异常]"
    return "[交易程序运行时异常]"


def send_alert(message: str, subject: Optional[str] = None) -> None:
    """系统级熔断 / 风控拦截 / 黑天鹅断网告警。

    统一记录日志，并在邮件通道就绪时转发一封邮件；未配置邮件时仅落日志。
    通知失败一律吞掉，绝不阻塞交易主流程。
    """
    logger.warning("[告警] %s", message)
    if notifier is not None and notifier.ready:
        try:
            mail_subject = subject or "[交易机器人] 系统告警"
            notifier.send(mail_subject, message)
        except Exception as e:  # noqa: BLE001
            logger.error("[告警] 邮件转发失败 | err=%s", e)


# ==========================================
# SMTP 邮件通道
# ==========================================

class Mailer:
    """轻量 SMTP 通知器。线程安全，非阻塞。"""

    def __init__(self):
        self.enabled = _env_bool("MAIL_ENABLED", False)
        self.host = (os.getenv("SMTP_SERVER", "").strip() or os.getenv("SMTP_HOST", "smtp.qq.com").strip())
        self.port = _env_int("SMTP_PORT", 465)
        self.use_ssl = _env_bool("SMTP_SSL", True)          # 465 用 SSL，587 用 STARTTLS 时置 false
        self.user = os.getenv("SMTP_USER", "").strip()
        self.password = os.getenv("SMTP_PASSWORD", "").strip()
        self.sender = os.getenv("SMTP_FROM", "").strip() or self.user
        self.to: List[str] = [
            x.strip() for x in os.getenv("MAIL_TO", "").split(",") if x.strip()
        ]
        self.error_threshold = max(1, _env_int("MODEL_ERROR_ALERT_THRESHOLD", 5))

        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._lock = threading.Lock()
        self._model_errors: Dict[str, List[str]] = {}   # "YYYY-MM-DD|model" -> [HH:MM:SS, ...]
        self._worker: Optional[threading.Thread] = None

        if self.ready:
            self._worker = threading.Thread(target=self._run, name="mailer", daemon=True)
            self._worker.start()

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.user and self.password and self.host and self.to)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    def _run(self):
        while True:
            subject, body = self._queue.get()
            try:
                self._deliver(subject, body)
            except Exception:
                # 通知失败不得影响交易流程，丢弃该封邮件
                pass
            finally:
                self._queue.task_done()

    def _deliver(self, subject: str, body: str):
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = self.sender
        msg["To"] = ",".join(self.to)

        if self.use_ssl:
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=20)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=20)
            server.starttls()
        try:
            server.login(self.user, self.password)
            server.sendmail(self.sender, self.to, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass

    def send(self, subject: str, body: str) -> bool:
        """入队一封邮件，立即返回；未配置时静默忽略。"""
        if not self.ready:
            return False
        try:
            self._queue.put((subject, body))
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 触发点 1：单个模型当日累计报错
    # ------------------------------------------------------------------

    def record_model_error(self, model: str, error: str) -> None:
        """记录一次模型报错；某模型当日累计达到阈值时发信 (跨过 5/10/15... 各发一次)。"""
        if not self.ready:
            return
        try:
            now = datetime.now()
            day = now.strftime("%Y-%m-%d")
            stamp = now.strftime("%H:%M:%S")
            key = f"{day}|{model}"
            with self._lock:
                bucket = self._model_errors.setdefault(key, [])
                bucket.append(stamp)
                count = len(bucket)
                times = list(bucket)
                # 跨日清理旧计数，避免长期运行内存无界增长
                stale = [k for k in self._model_errors if not k.startswith(day + "|")]
                for k in stale:
                    del self._model_errors[k]

            if count % self.error_threshold != 0:
                return

            lines = [
                f"模型: {model}",
                f"日期: {day}",
                f"当日累计报错: {count} 次 (达到阈值 {self.error_threshold})",
                "",
                "报错时间:",
            ]
            lines += [f"  - {t}" for t in times]
            lines += ["", "最近一次错误信息:", f"  {error}"]
            self.send(f"[交易机器人] 模型 {model} 当日累计报错 {count} 次", "\n".join(lines))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 触发点 2：平仓盈亏汇报
    # ------------------------------------------------------------------

    def notify_position_closed(self, reason: str, direction: Optional[str],
                               entry_price: Optional[float], exit_price: Optional[float],
                               pnl_ratio: float,
                               extra: Optional[dict] = None) -> None:
        if not self.ready:
            return
        try:
            pnl_pct = (pnl_ratio or 0.0) * 100.0
            body = [
                "平仓汇报",
                "",
                f"时间      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"平仓原因  : {reason}",
                f"方向      : {direction or 'N/A'}",
                f"开仓价    : {entry_price if entry_price is not None else 'N/A'}",
                f"平仓价    : {exit_price if exit_price is not None else 'N/A'}",
                f"已实现盈亏: {pnl_pct:+.2f}% (含手续费/滑点估算)",
            ]
            if extra:
                body.append("")
                for k, v in extra.items():
                    body.append(f"{k}: {v}")
            self.send(f"[交易机器人] 平仓 {direction or ''} PnL {pnl_pct:+.2f}%",
                      "\n".join(body))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 触发点 3：应急接管汇报
    # ------------------------------------------------------------------

    def notify_aid_triggered(self, context: Optional[dict], action: Optional[str],
                             detail: str = "") -> None:
        if not self.ready:
            return
        try:
            body = [
                "应急接管已触发",
                "",
                f"时间  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"动作  : {action or 'N/A'}",
            ]
            if detail:
                body.append(f"说明  : {detail}")
            if context:
                body += ["", "上下文:"]
                for k, v in context.items():
                    body.append(f"  {k}: {v}")
            self.send(f"[交易机器人] 应急接管 (action={action or 'N/A'})", "\n".join(body))
        except Exception:
            pass


# 邮件通知器 (可选)。未配置 SMTP 时静默降级；实例化失败也不影响交易主流程。
try:
    notifier: Optional[Mailer] = Mailer()
    if notifier.ready:
        logger.info("[告警] 邮件通知已启用 | to=%s", ", ".join(notifier.to))
    else:
        logger.info("[告警] 邮件通知未启用 | reason=缺少 MAIL_ENABLED/SMTP_USER/SMTP_PASSWORD")
except Exception as _mailer_err:  # noqa: BLE001
    notifier = None
    logger.warning("[告警] 邮件模块初始化失败 | 邮件功能=禁用 | err=%s", _mailer_err)

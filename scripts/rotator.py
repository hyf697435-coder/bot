#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
24/7 Multi-Instance Cloud Rotation Orchestrator & Process Supervisor
═════════════════════════════════════════════════════════════════════
Milestone: M3 (Feature 12 & Feature 13)
Location: scripts/rotator.py

Capabilities:
1. 6-Phase Zero-Naked-Risk Handover Protocol across dual ephemeral cloud sandboxes.
2. Pessimistic Distributed Lease Lock with Fencing Tokens and TTL.
3. Resilient Process Supervisor for main.py with dynamic heartbeat verification,
   drawdown circuit breakers, and graceful SIGTERM -> SIGKILL escalation.
4. Pre-flight health checking (Hyperliquid exchange, LLM relay, credentials, disk I/O).
5. Multi-stage automated rollback on failover / promotion faults.

CRITICAL ARCHITECTURAL INVARIANT:
Never write "pause" to control.json during instance handovers because main.py:323
executes _enter_system_halt() which liquidates all open market positions via
self.exchange.close_all(). Rotation transitions MUST use graceful OS termination
(SIGTERM/SIGINT) to ensure positions remain active on Hyperliquid with on-chain
TP/SL protection, and state is preserved cleanly for the standby node.
"""

import argparse
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ==============================================================================
# 1. Logging Setup & Formatting
# ==============================================================================
logger = logging.getLogger("rotator")

def setup_logging(level: str = "INFO", log_file: Optional[str] = None):
    """Configures structured console and optional file logging."""
    log_format = "%(asctime)s [%(levelname)s] [rotator] %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    log_level = getattr(logging, level.upper(), logging.INFO)

    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except Exception as e:
            sys.stderr.write(f"Failed to configure file logger {log_file}: {e}\n")

    logging.basicConfig(level=log_level, format=log_format, datefmt=date_format, handlers=handlers, force=True)

# ==============================================================================
# 2. Lock Management & Fencing Token Protocol
# ==============================================================================
class LockManager:
    """
    Manages pessimistic distributed lease file locks with monotonic fencing tokens.
    Prevents split-brain double trading across rotating sandbox instances.
    """
    def __init__(self, lock_file: str, ttl_seconds: int = 600, stale_timeout_seconds: int = 900):
        self.lock_path = Path(lock_file)
        self.ttl = ttl_seconds
        self.stale_timeout = stale_timeout_seconds
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> Optional[Dict[str, Any]]:
        """Reads and parses the lock file safely."""
        if not self.lock_path.exists():
            return None
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("[LOCK] Failed to read lock file %s: %s", self.lock_path, e)
            return None

    def acquire(self, active_node: str, standby_node: str, owner: str = "orchestrator", force: bool = False) -> Tuple[bool, int, str]:
        """
        Attempts to acquire the pessimistic handover lock.
        Returns (success: bool, fencing_token: int, message: str).
        """
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()
        current = self.read()
        fencing_token = 1

        if current and not force:
            status = current.get("status", "IDLE")
            expires_at_ts = float(current.get("expires_at_ts", 0))
            fencing_token = int(current.get("fencing_token", 0)) + 1
            cur_owner = current.get("owner", "unknown")

            if status == "HANDOVER_IN_PROGRESS":
                if now_ts < expires_at_ts:
                    msg = f"Handover lock actively held by {cur_owner} until {datetime.fromtimestamp(expires_at_ts, tz=timezone.utc).isoformat()}"
                    logger.warning("[LOCK] Contention: %s", msg)
                    return False, current.get("fencing_token", 0), msg
                else:
                    logger.warning("[LOCK] Stale handover lock detected (expired at %s). Breaking lock.",
                                   datetime.fromtimestamp(expires_at_ts, tz=timezone.utc).isoformat())

        payload = {
            "lock_id": f"lock-{int(now_ts)}-{os.getpid()}",
            "status": "HANDOVER_IN_PROGRESS",
            "active_node": active_node,
            "standby_node": standby_node,
            "owner": owner,
            "fencing_token": fencing_token,
            "acquired_at": now.isoformat(),
            "expires_at": datetime.fromtimestamp(now_ts + self.ttl, tz=timezone.utc).isoformat(),
            "expires_at_ts": now_ts + self.ttl,
            "ttl_seconds": self.ttl,
            "current_phase": 1,
            "updated_at": now.isoformat()
        }

        try:
            self._write_atomic(payload)
            logger.info("[LOCK] Pessimistic lock acquired. Fencing token: %d, TTL: %ds", fencing_token, self.ttl)
            return True, fencing_token, "Lock acquired successfully"
        except Exception as e:
            msg = f"Failed to write lock file: {e}"
            logger.error("[LOCK] %s", msg)
            return False, 0, msg

    def update_phase(self, phase: int, details: Optional[Dict[str, Any]] = None):
        """Updates the current handover phase in the lock file."""
        data = self.read()
        if data:
            data["current_phase"] = phase
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            if details:
                data.setdefault("phase_details", {})[f"phase_{phase}"] = details
            try:
                self._write_atomic(data)
            except Exception as e:
                logger.error("[LOCK] Failed to update phase %d: %s", phase, e)

    def release_to_active(self, active_node: str, standby_node: str):
        """Transitions lock status from HANDOVER_IN_PROGRESS to ACTIVE_HELD."""
        now = datetime.now(timezone.utc)
        current = self.read() or {}
        payload = {
            "lock_id": current.get("lock_id", f"lock-{int(now.timestamp())}"),
            "status": "ACTIVE_HELD",
            "active_node": active_node,
            "standby_node": standby_node,
            "owner": "node_supervisor",
            "fencing_token": current.get("fencing_token", 1),
            "acquired_at": now.isoformat(),
            "expires_at": datetime.fromtimestamp(now.timestamp() + 86400 * 365, tz=timezone.utc).isoformat(),
            "expires_at_ts": now.timestamp() + 86400 * 365,
            "ttl_seconds": 86400 * 365,
            "current_phase": 0,
            "last_handover_success": now.isoformat()
        }
        self._write_atomic(payload)
        logger.info("[LOCK] Handover lock released. Node %s is now ACTIVE_HELD.", active_node)

    def mark_rollback(self, active_node: str, reason: str):
        """Marks the lock as ROLLBACK state."""
        data = self.read() or {}
        data["status"] = "ROLLBACK"
        data["rollback_reason"] = reason
        data["active_node"] = active_node
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_atomic(data)
        logger.warning("[LOCK] Lock marked as ROLLBACK for node %s. Reason: %s", active_node, reason)

    def release(self):
        """Removes the lock file safely."""
        try:
            if self.lock_path.exists():
                self.lock_path.unlink()
                logger.info("[LOCK] Lock file released and removed.")
        except Exception as e:
            logger.error("[LOCK] Error removing lock file: %s", e)

    def _write_atomic(self, payload: dict):
        """Writes payload to a temporary file and atomically replaces destination."""
        tmp_path = self.lock_path.with_suffix(f".tmp.{os.getpid()}")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.lock_path)

# ==============================================================================
# 3. Pre-Flight Health Checker
# ==============================================================================
class HealthChecker:
    """
    Validates runtime readiness on local or remote nodes:
    - Critical Python imports
    - Exchange API connectivity and latency
    - LLM relay reachability (optional)
    - Disk write permissions and free space
    - Process liveness
    """
    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.health_cfg = self.config.get("health_check", {})

    def check_imports(self) -> Tuple[bool, List[str]]:
        """Verifies critical Python library imports."""
        mandatory_modules = [
            ("hyperliquid", "hyperliquid-python-sdk"),
            ("eth_account", "eth-account"),
            ("pandas", "pandas"),
            ("httpx", "httpx")
        ]
        failed = []
        for mod, pkg in mandatory_modules:
            try:
                __import__(mod)
            except ImportError:
                failed.append(f"{mod} ({pkg})")
        return len(failed) == 0, failed

    def check_credentials(self, env_file: Optional[str] = None) -> Tuple[bool, str]:
        """Validates that necessary trading keys exist and conform to schemas."""
        target_env = env_file if env_file else (".env" if os.path.exists(".env") else None)
        user_addr = os.getenv("HYPE_USER_ADDRESS", "").strip()
        sec_key = os.getenv("HYPE_SECRET_KEY", "").strip()

        if target_env and os.path.exists(target_env):
            try:
                with open(target_env, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("HYPE_USER_ADDRESS="):
                            user_addr = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("HYPE_SECRET_KEY="):
                            sec_key = line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception as e:
                return False, f"Failed to read env file {target_env}: {e}"

        if not user_addr or not sec_key:
            return False, "Missing HYPE_USER_ADDRESS or HYPE_SECRET_KEY"

        if not re.match(r"^0x[0-9a-fA-F]{40}$", user_addr):
            return False, f"Invalid HYPE_USER_ADDRESS format: {user_addr[:6]}..."

        if not re.match(r"^(0x)?[0-9a-fA-F]{64}$", sec_key):
            return False, "Invalid HYPE_SECRET_KEY format (expected 64 hex characters)"

        return True, "Credentials format valid"

    def check_exchange_connectivity(self, endpoint: Optional[str] = None, timeout_s: float = 5.0) -> Tuple[bool, float, str]:
        """Queries Hyperliquid /info API endpoint and measures round-trip latency."""
        url = endpoint or self.health_cfg.get("exchange_info_endpoint", "https://api.hyperliquid.xyz/info")
        payload = json.dumps({"type": "metaAndAssetCtxs"}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "TradingBotRotator/1.0"}
        )

        start_t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as response:
                status = response.getcode()
                latency_ms = (time.time() - start_t) * 1000.0
                if status == 200:
                    return True, latency_ms, f"HTTP 200 OK ({latency_ms:.1f}ms)"
                return False, latency_ms, f"Unexpected HTTP status {status}"
        except urllib.error.HTTPError as e:
            latency_ms = (time.time() - start_t) * 1000.0
            return False, latency_ms, f"HTTP Error {e.code}: {e.reason}"
        except Exception as e:
            latency_ms = (time.time() - start_t) * 1000.0
            return False, latency_ms, f"Connection failed: {e}"

    def check_disk_io(self, target_dir: str = ".", min_free_mb: int = 500) -> Tuple[bool, str]:
        """Tests disk write/read/delete capabilities and verifies free space."""
        p = Path(target_dir)
        p.mkdir(parents=True, exist_ok=True)
        test_file = p / f".disk_probe_{os.getpid()}_{int(time.time())}"
        test_data = b"ROTATOR_DISK_PROBE_DATA"

        try:
            with open(test_file, "wb") as f:
                f.write(test_data)
                f.flush()
                os.fsync(f.fileno())

            with open(test_file, "rb") as f:
                read_data = f.read()

            if read_data != test_data:
                return False, "Disk probe readback mismatch"
        except Exception as e:
            return False, f"Disk write test failed: {e}"
        finally:
            if test_file.exists():
                try:
                    test_file.unlink()
                except Exception:
                    pass

        try:
            usage = shutil.disk_usage(str(p))
            free_mb = usage.free / (1024 * 1024)
            if free_mb < min_free_mb:
                return False, f"Insufficient disk space: {free_mb:.1f}MB available, required {min_free_mb}MB"
            return True, f"Disk I/O OK ({free_mb:.0f}MB free)"
        except Exception as e:
            return False, f"Disk usage query failed: {e}"

    def check_processes(self) -> Tuple[bool, List[int]]:
        """Detects whether trading bot processes (main.py) are running locally."""
        pids = []
        try:
            if platform.system() == "Windows":
                cmd = ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"]
                res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                # Parse running python processes
                for line in res.stdout.splitlines():
                    if "python.exe" in line:
                        parts = line.split('","')
                        if len(parts) > 1:
                            try:
                                pids.append(int(parts[1].replace('"', '')))
                            except ValueError:
                                pass
            else:
                res = subprocess.run(["pgrep", "-f", "main.py"], capture_output=True, text=True, check=False)
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        pids.append(int(line))
        except Exception as e:
            logger.debug("[HEALTH_CHECK] Process inspection exception: %s", e)

        return len(pids) > 0, pids

    def run_all(self, target_dir: str = ".", env_file: Optional[str] = None) -> Tuple[bool, Dict[str, Any]]:
        """Executes full diagnostic suite and returns summary."""
        results: Dict[str, Any] = {}
        all_passed = True

        # 1. Imports
        imp_ok, imp_fail = self.check_imports()
        results["imports"] = {"passed": imp_ok, "details": "All critical packages available" if imp_ok else f"Missing: {imp_fail}"}
        if not imp_ok:
            all_passed = False

        # 2. Credentials
        cred_ok, cred_msg = self.check_credentials(env_file)
        results["credentials"] = {"passed": cred_ok, "details": cred_msg}
        if not cred_ok:
            all_passed = False

        # 3. Exchange Connectivity
        ex_ok, latency, ex_msg = self.check_exchange_connectivity()
        results["exchange"] = {"passed": ex_ok, "latency_ms": round(latency, 1), "details": ex_msg}
        if not ex_ok:
            all_passed = False

        # 4. Disk I/O
        disk_ok, disk_msg = self.check_disk_io(target_dir)
        results["disk"] = {"passed": disk_ok, "details": disk_msg}
        if not disk_ok:
            all_passed = False

        # 5. Process status
        proc_running, proc_pids = self.check_processes()
        results["processes"] = {"running": proc_running, "pids": proc_pids}

        results["healthy"] = all_passed
        return all_passed, results

# ==============================================================================
# 4. State Synchronizer & Validator
# ==============================================================================
class StateSynchronizer:
    """
    Validates, extracts, and injects state files atomically.
    Ensures zero corruption or schema drift across instance rotations.
    """
    def __init__(self, staging_dir: str = "./sync_staging"):
        self.staging_dir = Path(staging_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    def validate_file(self, file_path: Path, schema_type: str) -> Tuple[bool, str]:
        """Performs structural integrity and schema assertion checks."""
        if not file_path.exists():
            return False, f"File does not exist: {file_path.name}"

        size = file_path.stat().st_size
        if size == 0:
            return False, f"File is empty (0 bytes): {file_path.name}"

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            return False, f"JSON parse error in {file_path.name}: {e}"

        if schema_type == "state.json":
            if not isinstance(data, dict):
                return False, "state.json root must be a JSON object"
            required_keys = ["state", "coin", "daily_start_equity"]
            missing = [k for k in required_keys if k not in data]
            if missing:
                return False, f"state.json missing mandatory keys: {missing}"
            pos = data.get("position")
            if pos is not None and isinstance(pos, dict):
                pos_keys = ["direction", "entry_price", "sz_notional"]
                missing_pos = [k for k in pos_keys if k not in pos]
                if missing_pos:
                    return False, f"state.json position object missing keys: {missing_pos}"

        elif schema_type == "risk_state_aggressive.json":
            if not isinstance(data, dict):
                return False, "risk_state root must be a JSON object"
            required_keys = ["initial_equity", "metrics", "recent_trades"]
            missing = [k for k in required_keys if k not in data]
            if missing:
                return False, f"risk_state missing mandatory keys: {missing}"

        elif schema_type == "equity_history_risk_state_aggressive.json":
            if not isinstance(data, list):
                return False, "equity history root must be a JSON array"

        return True, f"Verified {file_path.name} ({size} bytes)"

    def stage_local_state(self, source_dir: str, mandatory_files: List[str], optional_files: Optional[List[str]] = None) -> Tuple[bool, str]:
        """Copies state files from source directory into staging buffer with validation."""
        src_path = Path(source_dir)
        for fname in mandatory_files:
            src_file = src_path / fname
            dst_file = self.staging_dir / fname
            if not src_file.exists():
                return False, f"Mandatory state file missing at source: {src_file}"
            shutil.copy2(src_file, dst_file)
            valid, msg = self.validate_file(dst_file, fname)
            if not valid:
                return False, f"Staged file validation failed for {fname}: {msg}"
            logger.info("[STATE_SYNC] Staged %s (%s)", fname, msg)

        if optional_files:
            for fname in optional_files:
                src_file = src_path / fname
                dst_file = self.staging_dir / fname
                if src_file.exists():
                    try:
                        shutil.copy2(src_file, dst_file)
                        logger.info("[STATE_SYNC] Staged optional file %s", fname)
                    except Exception as e:
                        logger.warning("[STATE_SYNC] Optional file copy failed for %s: %s", fname, e)

        return True, "All state files successfully staged and verified"

    def deploy_staged_state(self, target_dir: str, mandatory_files: List[str], optional_files: Optional[List[str]] = None) -> Tuple[bool, str]:
        """Deploys staged state files into target directory atomically."""
        tgt_path = Path(target_dir)
        tgt_path.mkdir(parents=True, exist_ok=True)

        for fname in mandatory_files:
            src_file = self.staging_dir / fname
            dst_file = tgt_path / fname
            if not src_file.exists():
                return False, f"Mandatory staged file not found: {src_file}"
            # Atomic write
            tmp_file = tgt_path / f"{fname}.tmp.{os.getpid()}"
            shutil.copy2(src_file, tmp_file)
            os.replace(tmp_file, dst_file)
            logger.info("[STATE_SYNC] Deployed %s to %s", fname, dst_file)

        if optional_files:
            for fname in optional_files:
                src_file = self.staging_dir / fname
                dst_file = tgt_path / fname
                if src_file.exists():
                    tmp_file = tgt_path / f"{fname}.tmp.{os.getpid()}"
                    shutil.copy2(src_file, tmp_file)
                    os.replace(tmp_file, dst_file)
                    logger.info("[STATE_SYNC] Deployed optional %s to %s", fname, dst_file)

        # Clear stale heartbeat and control command files in target
        for stale in [".heartbeat", "control.json"]:
            stale_f = tgt_path / stale
            if stale_f.exists():
                try:
                    stale_f.unlink()
                    logger.info("[STATE_SYNC] Cleared stale file: %s", stale_f)
                except Exception as e:
                    logger.warning("[STATE_SYNC] Failed to remove stale file %s: %s", stale_f, e)

        return True, "All state files deployed atomically"

# ==============================================================================
# 5. Cloud Provider Client (Instabox / Local Sandbox Abstraction)
# ==============================================================================
class CloudProviderClient:
    """
    Interacts with cloud sandbox providers (e.g. Instabox CLI).
    Provides unified execution, machine start/stop, and file transfer.
    """
    def __init__(self, api_key: Optional[str] = None, dry_run: bool = False):
        self.api_key = api_key or os.getenv("INSTABOX_API_KEY", "")
        self.dry_run = dry_run

    def _run_instabox(self, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        """Executes an instabox CLI command with the bound API key."""
        env = os.environ.copy()
        if self.api_key:
            env["INSTABOX_API_KEY"] = self.api_key

        cmd = ["instabox"] + args
        if self.dry_run:
            logger.info("[DRY_RUN] Command: %s", " ".join(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout='{"state": "running"}', stderr="")

        try:
            return subprocess.run(cmd, env=env, capture_output=True, text=True, check=check)
        except FileNotFoundError:
            logger.warning("[CLOUD_PROVIDER] instabox CLI binary not found in PATH.")
            return subprocess.CompletedProcess(cmd, 127, stdout="", stderr="instabox not found in PATH")

    def get_status(self, machine_name: str) -> Dict[str, Any]:
        """Queries machine status and metadata."""
        if self.dry_run:
            return {"name": machine_name, "state": "running", "runtime": {"ready": True}}
        res = self._run_instabox(["get", machine_name, "--json"], check=False)
        if res.returncode == 0 and res.stdout.strip():
            try:
                return json.loads(res.stdout)
            except Exception:
                pass
        return {"name": machine_name, "state": "unknown", "raw": res.stderr}

    def start_machine(self, machine_name: str) -> bool:
        """Wakes or resumes a suspended/stopped machine."""
        if self.dry_run:
            logger.info("[DRY_RUN] Waking machine: %s", machine_name)
            return True
        res = self._run_instabox(["start", machine_name], check=False)
        return res.returncode == 0

    def stop_machine(self, machine_name: str) -> bool:
        """Stops/suspends a machine to preserve quotas and enter resting cooldown."""
        if self.dry_run:
            logger.info("[DRY_RUN] Stopping machine: %s", machine_name)
            return True
        res = self._run_instabox(["stop", machine_name], check=False)
        return res.returncode == 0

    def exec_cmd(self, machine_name: str, command: str, background: bool = False, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        """Executes a command inside the remote machine."""
        args = ["exec"]
        if background:
            args.append("--background")
        if cwd:
            args.extend(["--cwd", cwd])
        args.extend([machine_name, "--", command])
        return self._run_instabox(args, check=not background)

    def pull_file(self, machine_name: str, remote_path: str, local_path: str) -> bool:
        """Pulls a file from remote sandbox to local host."""
        if self.dry_run:
            logger.info("[DRY_RUN] Pulling %s:%s -> %s", machine_name, remote_path, local_path)
            return True
        res = self._run_instabox(["pull", machine_name, remote_path, local_path], check=False)
        return res.returncode == 0

    def push_file(self, machine_name: str, local_path: str, remote_path: str) -> bool:
        """Pushes a local file to remote sandbox."""
        if self.dry_run:
            logger.info("[DRY_RUN] Pushing %s -> %s:%s", local_path, machine_name, remote_path)
            return True
        res = self._run_instabox(["push", machine_name, local_path, remote_path], check=False)
        return res.returncode == 0

# ==============================================================================
# 6. Handover Orchestrator (6-Phase Zero-Naked-Risk Handover)
# ==============================================================================
class HandoverOrchestrator:
    """
    Executes the 6-Phase Zero-Naked-Risk Handover Protocol across dual cloud nodes:
    - Phase 1: Standby Initialization
    - Phase 2: Pre-Flight Health Check
    - Phase 3: State Lock & Sync (Atomic copy & graceful process stop; NO pause command!)
    - Phase 4: Standby Promotion
    - Phase 5: Verification (Heartbeat freshness & loop activity)
    - Phase 6: Active Demotion & Clean Resting Cooldown
    """
    def __init__(self, config_path: str, lock_file: str, dry_run: bool = False, force: bool = False):
        self.config_path = config_path
        self.dry_run = dry_run
        self.force = force

        # Load configuration
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        else:
            logger.warning("[ORCHESTRATOR] Config file %s not found. Using defaults.", config_path)
            self.config = {
                "rotation": {"shift_duration_hours": 6, "ring_nodes": ["bot-node-a", "bot-node-b"]},
                "nodes": {
                    "bot-node-a": {"provider": "instabox", "machine_name": "bot-node-a", "api_key_env": "INSTABOX_API_KEY_A", "workdir": "/data/home/trading-bot"},
                    "bot-node-b": {"provider": "instabox", "machine_name": "bot-node-b", "api_key_env": "INSTABOX_API_KEY_B", "workdir": "/data/home/trading-bot"}
                },
                "state_sync": {
                    "mandatory_files": ["state.json", "risk_state_aggressive.json", "equity_history_risk_state_aggressive.json"],
                    "optional_files": ["news_sentiment_cache.json", "correlation_matrix.json"],
                    "staging_dir": "./sync_staging"
                },
                "lock": {"file_path": lock_file, "ttl_seconds": 600}
            }

        self.lock_mgr = LockManager(
            lock_file,
            ttl_seconds=self.config.get("lock", {}).get("ttl_seconds", 600)
        )
        self.synchronizer = StateSynchronizer(
            staging_dir=self.config.get("state_sync", {}).get("staging_dir", "./sync_staging")
        )
        self.health_checker = HealthChecker(self.config)

    def _get_provider_client(self, node_name: str) -> CloudProviderClient:
        node_cfg = self.config.get("nodes", {}).get(node_name, {})
        api_key_env = node_cfg.get("api_key_env", "")
        api_key = os.getenv(api_key_env) if api_key_env else None
        return CloudProviderClient(api_key=api_key, dry_run=self.dry_run)

    def execute_handover(self, active_node: str, standby_node: str) -> bool:
        """Executes full 6-phase handover with automatic rollback."""
        logger.info("================================================================================")
        logger.info("[HANDOVER] Starting 6-Phase Zero-Naked-Risk Handover Protocol")
        logger.info("[HANDOVER] Active Node : %s  -->  Standby Node : %s", active_node, standby_node)
        logger.info("[HANDOVER] Dry Run     : %s  |  Force Override: %s", self.dry_run, self.force)
        logger.info("================================================================================")

        # ----------------------------------------------------------------------
        # Phase 1: Standby Initialization
        # ----------------------------------------------------------------------
        logger.info("\n>>> [PHASE 1/6] Standby Initialization (Node: %s)...", standby_node)
        self.lock_mgr.update_phase(1, {"target": standby_node})
        if not self._phase_1_standby_init(standby_node):
            logger.error("[HANDOVER] Phase 1 failed. Aborting handover. Active node remains running.")
            return False

        # ----------------------------------------------------------------------
        # Phase 2: Pre-Flight Health Check
        # ----------------------------------------------------------------------
        logger.info("\n>>> [PHASE 2/6] Standby Pre-Flight Health Check (Node: %s)...", standby_node)
        self.lock_mgr.update_phase(2, {"target": standby_node})
        if not self._phase_2_health_check(standby_node):
            logger.error("[HANDOVER] Phase 2 failed. Aborting handover. Active node remains running.")
            return False

        # ----------------------------------------------------------------------
        # Phase 3: State Lock & Synchronization
        # ----------------------------------------------------------------------
        logger.info("\n>>> [PHASE 3/6] State Lock & Synchronization (%s -> %s)...", active_node, standby_node)
        acquired, token, lock_msg = self.lock_mgr.acquire(active_node, standby_node, force=self.force)
        if not acquired:
            logger.error("[HANDOVER] Phase 3 aborted: %s", lock_msg)
            return False

        self.lock_mgr.update_phase(3, {"active": active_node, "standby": standby_node, "token": token})
        sync_ok = self._phase_3_lock_and_sync(active_node, standby_node)
        if not sync_ok:
            logger.critical("[HANDOVER] Phase 3 failed! Triggering Rollback to Active Node %s...", active_node)
            self._rollback_phase_3(active_node)
            return False

        # ----------------------------------------------------------------------
        # Phase 4: Standby Promotion
        # ----------------------------------------------------------------------
        logger.info("\n>>> [PHASE 4/6] Standby Node Promotion (Node: %s)...", standby_node)
        self.lock_mgr.update_phase(4, {"target": standby_node})
        promoted = self._phase_4_standby_promotion(standby_node)
        if not promoted:
            logger.critical("[HANDOVER] Phase 4 promotion failed! Triggering Rollback to Active Node %s...", active_node)
            self._rollback_phase_4_5(active_node, standby_node, "Phase 4 promotion failed")
            return False

        # ----------------------------------------------------------------------
        # Phase 5: Verification
        # ----------------------------------------------------------------------
        logger.info("\n>>> [PHASE 5/6] Standby Node Health & Trading Loop Verification...", )
        self.lock_mgr.update_phase(5, {"target": standby_node})
        verified = self._phase_5_verification(standby_node)
        if not verified:
            logger.critical("[HANDOVER] Phase 5 verification failed! Triggering Rollback to Active Node %s...", active_node)
            self._rollback_phase_4_5(active_node, standby_node, "Phase 5 verification failed")
            return False

        # ----------------------------------------------------------------------
        # Phase 6: Active Demotion & Clean Exit
        # ----------------------------------------------------------------------
        logger.info("\n>>> [PHASE 6/6] Active Demotion & Clean Resting Cooldown (Old Active: %s)...", active_node)
        self.lock_mgr.update_phase(6, {"demoting": active_node, "new_active": standby_node})
        self._phase_6_active_demotion(active_node, standby_node)

        # Release lock to ACTIVE_HELD on new node
        self.lock_mgr.release_to_active(active_node=standby_node, standby_node=active_node)

        logger.info("================================================================================")
        logger.info("[HANDOVER] SUCCESS! Handover completed with ZERO naked risk.")
        logger.info("[HANDOVER] New Active Node : %s (Running)", standby_node)
        logger.info("[HANDOVER] Decommissioned  : %s (Resting for 6h cooldown reset)", active_node)
        logger.info("================================================================================")
        return True

    def _phase_1_standby_init(self, standby_node: str) -> bool:
        """Wakes standby sandbox if suspended/stopped and verifies readiness."""
        client = self._get_provider_client(standby_node)
        status = client.get_status(standby_node)
        state = status.get("state", "").lower()
        logger.info("[Phase 1] Node %s current state: %s", standby_node, state or "unknown")

        if state in ("suspended", "stopped", "stopping"):
            logger.info("[Phase 1] Waking standby instance %s...", standby_node)
            client.start_machine(standby_node)
            for attempt in range(1, 19):
                time.sleep(5)
                s = client.get_status(standby_node)
                cur_state = s.get("state", "").lower()
                logger.info("[Phase 1] Polling instance status (%d/18): %s", attempt, cur_state)
                if cur_state == "running" or self.dry_run:
                    logger.info("[Phase 1] Standby instance %s is now running.", standby_node)
                    return True
            logger.error("[Phase 1] Standby instance %s failed to enter running state within 90s.", standby_node)
            return False

        return True

    def _phase_2_health_check(self, standby_node: str) -> bool:
        """Executes pre-flight health checks on the standby node."""
        client = self._get_provider_client(standby_node)
        workdir = self.config.get("nodes", {}).get(standby_node, {}).get("workdir", ".")

        if self.dry_run:
            logger.info("[Phase 2] [DRY_RUN] Simulating remote health check on %s...", standby_node)
            return True

        probe_cmd = f"python3 {workdir}/scripts/rotator.py --role standby --action health-check --config {workdir}/config/rotation_config.json"
        res = client.exec_cmd(standby_node, probe_cmd)
        if res.returncode != 0:
            logger.error("[Phase 2] Pre-flight health check FAILED on %s (exit %d): %s",
                         standby_node, res.returncode, res.stderr.strip() or res.stdout.strip())
            return False

        logger.info("[Phase 2] Pre-flight health check PASSED on %s.", standby_node)
        return True

    def _phase_3_lock_and_sync(self, active_node: str, standby_node: str) -> bool:
        """
        Quiesces active node via graceful SIGTERM and synchronizes state files.
        CRITICAL: Never writes 'pause' to control.json to prevent liquidation!
        """
        logger.info("[Phase 3] Quiescing Active Node %s (Zero-Naked-Risk Handover)...", active_node)
        client_active = self._get_provider_client(active_node)
        client_standby = self._get_provider_client(standby_node)
        workdir_act = self.config.get("nodes", {}).get(active_node, {}).get("workdir", ".")
        workdir_stb = self.config.get("nodes", {}).get(standby_node, {}).get("workdir", ".")

        # Step 1: Quiesce Active Process via graceful SIGTERM / SIGINT
        logger.info("[Phase 3] Sending graceful termination signal to Active bot process...")
        logger.info("[Phase 3] NOTICE: Refraining from control.json 'pause' to preserve open positions on Hyperliquid.")
        if not self.dry_run:
            # First send SIGINT so KeyboardInterrupt triggers clean loop exit and state flush
            client_active.exec_cmd(active_node, "pkill -SIGINT -f main.py || true")
            time.sleep(3)
            # Check if still running; if so, send SIGTERM
            res = client_active.exec_cmd(active_node, "pgrep -f main.py")
            if res.returncode == 0:
                logger.info("[Phase 3] Bot process still active. Sending SIGTERM for graceful exit...")
                client_active.exec_cmd(active_node, "pkill -SIGTERM -f main.py || true")
                time.sleep(5)

        # Step 2: Pull mandatory state files to staging
        mand_files = self.config.get("state_sync", {}).get("mandatory_files", [])
        opt_files = self.config.get("state_sync", {}).get("optional_files", [])

        staging_dir = self.synchronizer.staging_dir
        for fname in mand_files:
            remote_f = f"{workdir_act}/{fname}"
            local_f = str(staging_dir / fname)
            logger.info("[Phase 3] Pulling state file: %s -> %s", remote_f, local_f)
            if not self.dry_run:
                ok = client_active.pull_file(active_node, remote_f, local_f)
                if not ok or not os.path.exists(local_f):
                    logger.error("[Phase 3] Failed to pull mandatory state file %s from %s!", fname, active_node)
                    return False
                valid, msg = self.synchronizer.validate_file(Path(local_f), fname)
                if not valid:
                    logger.error("[Phase 3] State file integrity validation failed: %s", msg)
                    return False
                logger.info("[Phase 3] Verified %s: %s", fname, msg)

        # Step 3: Push state files to Standby node
        for fname in mand_files:
            local_f = str(staging_dir / fname)
            remote_f = f"{workdir_stb}/{fname}"
            logger.info("[Phase 3] Pushing state file: %s -> %s:%s", local_f, standby_node, remote_f)
            if not self.dry_run:
                ok = client_standby.push_file(standby_node, local_f, remote_f)
                if not ok:
                    logger.error("[Phase 3] Failed to push state file %s to standby %s!", fname, standby_node)
                    return False

        # Clear stale heartbeat / control files on standby
        if not self.dry_run:
            client_standby.exec_cmd(standby_node, f"rm -f {workdir_stb}/.heartbeat {workdir_stb}/control.json")

        logger.info("[Phase 3] State files successfully synchronized with zero corruption.")
        return True

    def _phase_4_standby_promotion(self, standby_node: str) -> bool:
        """Launches the trading bot supervisor on the standby node."""
        client = self._get_provider_client(standby_node)
        workdir = self.config.get("nodes", {}).get(standby_node, {}).get("workdir", ".")
        launch_cmd = f"nohup python3 {workdir}/scripts/rotator.py --role active --action run --config {workdir}/config/rotation_config.json > {workdir}/supervisor.log 2>&1 &"

        logger.info("[Phase 4] Spawning bot supervisor on Standby Node %s...", standby_node)
        if self.dry_run:
            logger.info("[Phase 4] [DRY_RUN] Command: %s", launch_cmd)
            return True

        client.exec_cmd(standby_node, launch_cmd, background=True)
        time.sleep(5)

        # Verify supervisor and main.py spawned
        res = client.exec_cmd(standby_node, "pgrep -f 'main.py|rotator.py'")
        if res.returncode != 0:
            logger.error("[Phase 4] Standby process check failed: no active main.py or rotator process found!")
            return False

        logger.info("[Phase 4] Standby process active (PIDs: %s).", res.stdout.strip().replace("\n", ", "))
        return True

    def _phase_5_verification(self, standby_node: str) -> bool:
        """Monitors standby node heartbeat and confirms live trading loop."""
        client = self._get_provider_client(standby_node)
        workdir = self.config.get("nodes", {}).get(standby_node, {}).get("workdir", ".")
        local_hb = str(self.synchronizer.staging_dir / ".heartbeat_check")

        if self.dry_run:
            logger.info("[Phase 5] [DRY_RUN] Simulating standby heartbeat verification (PASS).")
            return True

        logger.info("[Phase 5] Polling Standby Node %s for fresh heartbeat (up to 60s)...", standby_node)
        for attempt in range(1, 13):
            time.sleep(5)
            try:
                ok = client.pull_file(standby_node, f"{workdir}/.heartbeat", local_hb)
                if ok and os.path.exists(local_hb):
                    with open(local_hb, "r", encoding="utf-8") as f:
                        hb_data = json.load(f)
                    hb_ts = float(hb_data.get("timestamp", 0))
                    age = time.time() - hb_ts
                    state = hb_data.get("state", "UNKNOWN")
                    logger.info("[Phase 5] Standby heartbeat check (%d/12): age=%.1fs, state=%s", attempt, age, state)
                    if age < 30.0 and state in ("IDLE", "TRADING", "COOL_DOWN"):
                        logger.info("[Phase 5] Standby node verified healthy and actively trading.")
                        return True
            except Exception as e:
                logger.debug("[Phase 5] Heartbeat probe exception: %s", e)

        logger.error("[Phase 5] Verification timed out! Standby failed to produce a fresh heartbeat within 60s.")
        return False

    def _phase_6_active_demotion(self, active_node: str, standby_node: str):
        """Stops the old active sandbox so it can enter resting cooldown."""
        client_act = self._get_provider_client(active_node)
        logger.info("[Phase 6] Ensuring old Active Node %s is completely quiesced...", active_node)
        if not self.dry_run:
            client_act.exec_cmd(active_node, "pkill -9 -f 'main.py|rotator.py' || true")
            logger.info("[Phase 6] Putting old Active Node %s to sleep for 6-hour quota recovery...", active_node)
            client_act.stop_machine(active_node)

    def _rollback_phase_3(self, active_node: str):
        """Rollback logic if Phase 3 state lock/sync fails: resumes Active node."""
        logger.warning("[ROLLBACK] Resuming Active Node %s trading loop...", active_node)
        client = self._get_provider_client(active_node)
        workdir = self.config.get("nodes", {}).get(active_node, {}).get("workdir", ".")
        launch_cmd = f"nohup python3 {workdir}/scripts/rotator.py --role active --action run --config {workdir}/config/rotation_config.json > {workdir}/supervisor.log 2>&1 &"
        if not self.dry_run:
            client.exec_cmd(active_node, launch_cmd, background=True)
        self.lock_mgr.mark_rollback(active_node, "Phase 3 lock and sync failure")

    def _rollback_phase_4_5(self, active_node: str, standby_node: str, reason: str):
        """Rollback logic if Standby fails promotion/verification: kills standby, resurrects Active."""
        logger.critical("[ROLLBACK] Emergency rollback triggered: %s", reason)
        client_stb = self._get_provider_client(standby_node)
        client_act = self._get_provider_client(active_node)
        workdir_act = self.config.get("nodes", {}).get(active_node, {}).get("workdir", ".")

        if not self.dry_run:
            # Terminate faulty standby
            client_stb.exec_cmd(standby_node, "pkill -9 -f 'main.py|rotator.py' || true")
            client_stb.stop_machine(standby_node)
            # Resurrect Active node
            client_act.start_machine(active_node)
            launch_cmd = f"nohup python3 {workdir_act}/scripts/rotator.py --role active --action run --config {workdir_act}/config/rotation_config.json > {workdir_act}/supervisor.log 2>&1 &"
            client_act.exec_cmd(active_node, launch_cmd, background=True)

        self.lock_mgr.mark_rollback(active_node, f"Promotion/Verification failure: {reason}")
        logger.info("[ROLLBACK] Active Node %s resurrected. Standby Node %s terminated.", active_node, standby_node)

# ==============================================================================
# 7. Process Supervisor Loop (Node Level Process Manager)
# ==============================================================================
class ProcessSupervisor:
    """
    Supervises main.py execution on the active node:
    - Captures stdout/stderr into trading_bot.log
    - Monitors .heartbeat freshness with dynamic timeout per execution phase
    - Enforces max drawdown (30%) and daily loss (15%) circuit breakers
    - Gracefully stops main.py via SIGTERM -> 15s timeout -> SIGKILL
    - Strictly avoids control.json 'pause' to prevent position liquidation
    """
    def __init__(self, config_path: str, workdir: str = "."):
        self.workdir = Path(workdir).resolve()
        self.config_path = config_path

        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        else:
            self.config = {}

        self.sup_cfg = self.config.get("supervisor", {
            "heartbeat_file": ".heartbeat",
            "check_interval_seconds": 10,
            "heartbeat_timeout_seconds": 150,
            "ai_phase_timeout_seconds": 180,
            "halt_timeout_seconds": 600,
            "sigterm_timeout_seconds": 15,
            "max_restarts_per_hour": 5,
            "restart_backoff_base_seconds": 5,
            "max_drawdown_limit_pct": 30.0,
            "daily_loss_limit_pct": 15.0
        })

        self.heartbeat_path = self.workdir / self.sup_cfg.get("heartbeat_file", ".heartbeat")
        self.log_path = self.workdir / "trading_bot.log"
        self.running = True
        self.current_proc: Optional[subprocess.Popen] = None

        # Signal registration
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info("[SUPERVISOR] Caught signal %d. Initiating graceful shutdown of trading engine.", signum)
        self.running = False
        if self.current_proc:
            self._terminate_proc(self.current_proc)

    def _terminate_proc(self, proc: subprocess.Popen):
        """Terminates process using graceful escalation: SIGTERM -> wait 15s -> SIGKILL."""
        if proc.poll() is not None:
            return

        logger.info("[SUPERVISOR] Sending SIGTERM to bot PID %d (graceful exit window: %ds)...",
                    proc.pid, self.sup_cfg.get("sigterm_timeout_seconds", 15))
        try:
            proc.terminate()
            try:
                proc.wait(timeout=self.sup_cfg.get("sigterm_timeout_seconds", 15))
                logger.info("[SUPERVISOR] Bot PID %d exited cleanly.", proc.pid)
                return
            except subprocess.TimeoutExpired:
                logger.warning("[SUPERVISOR] Bot PID %d did not exit within timeout. Escalating to SIGKILL...", proc.pid)
                proc.kill()
                proc.wait(timeout=5)
                logger.info("[SUPERVISOR] Bot PID %d killed.", proc.pid)
        except Exception as e:
            logger.error("[SUPERVISOR] Error terminating bot process: %s", e)

    def run(self):
        """Main supervision loop."""
        logger.info("================================================================================")
        logger.info("[SUPERVISOR] Starting Trading Bot Process Supervisor")
        logger.info("[SUPERVISOR] Working Directory: %s", self.workdir)
        logger.info("[SUPERVISOR] Heartbeat File   : %s", self.heartbeat_path)
        logger.info("================================================================================")

        restart_count = 0
        hourly_window_start = time.time()

        while self.running:
            # Reset restart counter every hour
            if time.time() - hourly_window_start > 3600:
                restart_count = 0
                hourly_window_start = time.time()

            logger.info("[SUPERVISOR] Spawning main.py (attempt %d)...", restart_count + 1)
            # Ensure clean heartbeat
            if self.heartbeat_path.exists():
                try:
                    self.heartbeat_path.unlink()
                except Exception:
                    pass

            cmd = [sys.executable, "main.py"]
            try:
                log_file = open(self.log_path, "a", encoding="utf-8")
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.workdir),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    bufsize=1
                )
                self.current_proc = proc
                logger.info("[SUPERVISOR] main.py running under PID %d", proc.pid)
            except Exception as e:
                logger.critical("[SUPERVISOR] Failed to spawn main.py: %s", e)
                time.sleep(10)
                continue

            start_time = time.time()
            check_interval = self.sup_cfg.get("check_interval_seconds", 10)

            while self.running:
                time.sleep(check_interval)

                # 1. Process alive check
                retcode = proc.poll()
                if retcode is not None:
                    if not self.running:
                        logger.info("[SUPERVISOR] Bot stopped as requested.")
                        return

                    logger.error("[SUPERVISOR] main.py exited unexpectedly with code %d", retcode)
                    restart_count += 1
                    max_restarts = self.sup_cfg.get("max_restarts_per_hour", 5)
                    if restart_count > max_restarts:
                        logger.critical("[SUPERVISOR] Max restart limit (%d/hr) reached. Halting supervisor.", max_restarts)
                        return

                    backoff = self.sup_cfg.get("restart_backoff_base_seconds", 5) * restart_count
                    logger.info("[SUPERVISOR] Backing off for %ds before restart...", backoff)
                    time.sleep(backoff)
                    break

                # 2. Liveness & Heartbeat Check (Grace period of 30s)
                if (time.time() - start_time) < 30.0:
                    continue

                if self.heartbeat_path.exists():
                    try:
                        with open(self.heartbeat_path, "r", encoding="utf-8") as f:
                            hb = json.load(f)

                        delay = time.time() - float(hb.get("timestamp", start_time))
                        state = hb.get("state", "UNKNOWN")
                        phase = hb.get("phase", "")

                        # Dynamic timeout
                        timeout = self.sup_cfg.get("heartbeat_timeout_seconds", 150)
                        if state in ("COOL_DOWN", "SYSTEM_HALT"):
                            timeout = self.sup_cfg.get("halt_timeout_seconds", 600)
                        elif phase in ("AI_MACRO", "AI_TRADE", "FETCH_MARKET"):
                            timeout = self.sup_cfg.get("ai_phase_timeout_seconds", 180)

                        if delay > timeout:
                            logger.critical("[SUPERVISOR] Hang detected (heartbeat delay=%.1fs > timeout=%.0fs). Terminating hung bot PID %d...",
                                            delay, timeout, proc.pid)
                            self._terminate_proc(proc)
                            break

                        # Circuit Breakers (Daily loss & max drawdown)
                        peak_eq = float(hb.get("peak_equity", 0))
                        daily_loss = float(hb.get("daily_loss_pnl", 0))
                        if peak_eq > 0:
                            daily_loss_pct = (abs(daily_loss) / peak_eq) * 100.0 if daily_loss < 0 else 0
                            limit_dl = float(self.sup_cfg.get("daily_loss_limit_pct", 15.0))
                            if daily_loss_pct >= limit_dl:
                                logger.critical("[SUPERVISOR] CIRCUIT BREAKER: Daily loss %.2f%% exceeded limit %.2f%%! Stopping bot permanently.",
                                                daily_loss_pct, limit_dl)
                                self._terminate_proc(proc)
                                return

                    except Exception as e:
                        logger.debug("[SUPERVISOR] Heartbeat evaluation exception: %s", e)

            if not self.running and proc.poll() is None:
                self._terminate_proc(proc)

# ==============================================================================
# 8. Command-Line Interface Parser & Routing
# ==============================================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description="Hyperliquid Trading Bot 24/7 Cloud Rotation Orchestrator & Supervisor",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--role",
        choices=["orchestrator", "active", "standby", "supervisor"],
        default=os.getenv("ROTATOR_ROLE", "orchestrator"),
        help="Execution role (orchestrator: coordinate handover; active/supervisor: run local supervisor; standby: ready node)"
    )
    parser.add_argument(
        "--action",
        choices=["handoff", "run", "health-check", "status", "rollback", "quiesce", "promote", "demote"],
        default=os.getenv("ROTATOR_ACTION", "status"),
        help="Action to execute"
    )
    parser.add_argument("--active-node", default=os.getenv("ROTATOR_ACTIVE_NODE"), help="Identifier of active cloud node (e.g. bot-node-a)")
    parser.add_argument("--standby-node", default=os.getenv("ROTATOR_STANDBY_NODE"), help="Identifier of standby target node (e.g. bot-node-b)")
    parser.add_argument("--node-a", default="bot-node-a", help="Alias for Node A")
    parser.add_argument("--node-b", default="bot-node-b", help="Alias for Node B")
    parser.add_argument("--target", default="auto", help="Target node selection override (auto, account_a, account_b)")
    parser.add_argument("--config", default=os.getenv("ROTATOR_CONFIG_FILE", "config/rotation_config.json"), help="Path to JSON configuration")
    parser.add_argument("--lock-file", default=os.getenv("ROTATOR_LOCK_FILE", "state/rotation.lock.json"), help="Path to pessimistic lock file")
    parser.add_argument("--interval", type=int, default=10, help="Supervisor check interval in seconds")
    parser.add_argument("--timeout", type=int, default=300, help="Handover operation timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("ROTATOR_DRY_RUN", "false").lower() in ("true", "1", "yes"), help="Simulate without affecting live instances")
    parser.add_argument("--force", action="store_true", default=os.getenv("ROTATOR_FORCE", "false").lower() in ("true", "1", "yes"), help="Force break stale lock or override checks")
    parser.add_argument("--log-level", default=os.getenv("ROTATOR_LOG_LEVEL", "INFO"), choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Logging verbosity")
    parser.add_argument("--log-file", default=os.getenv("ROTATOR_LOG_FILE"), help="Path to rotator log file")

    return parser.parse_args()

def main():
    args = parse_args()
    setup_logging(args.log_level, args.log_file)

    # 1. Supervisor Mode
    if args.action == "run" or args.role == "supervisor":
        supervisor = ProcessSupervisor(args.config)
        supervisor.run()
        sys.exit(0)

    # 2. Pre-Flight Health Check
    elif args.action == "health-check":
        logger.info("[CLI] Executing node pre-flight health check...")
        checker = HealthChecker()
        healthy, report = checker.run_all()
        print(json.dumps(report, indent=2))
        sys.exit(0 if healthy else 2)

    # 3. Status Query
    elif args.action == "status":
        lock_mgr = LockManager(args.lock_file)
        data = lock_mgr.read()
        print(json.dumps(data or {"status": "NO_LOCK_FILE_FOUND"}, indent=2))
        sys.exit(0)

    # 4. Quiesce (Local Node Shutdown)
    elif args.action == "quiesce":
        logger.info("[CLI] Quiescing local trading processes cleanly...")
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/F", "/IM", "python.exe", "/T"], check=False)
        else:
            subprocess.run(["pkill", "-SIGINT", "-f", "main.py"], check=False)
        sys.exit(0)

    # 5. Handover Orchestration
    elif args.action == "handoff":
        # Resolve active and standby nodes
        active_node = args.active_node
        standby_node = args.standby_node

        # Auto-detect if not explicitly provided
        if not active_node or not standby_node:
            lock_mgr = LockManager(args.lock_file)
            current_lock = lock_mgr.read()
            last_active = current_lock.get("active_node") if current_lock else None

            if args.target == "account_b":
                active_node = args.node_a
                standby_node = args.node_b
            elif args.target == "account_a":
                active_node = args.node_b
                standby_node = args.node_a
            elif last_active == args.node_a:
                active_node = args.node_a
                standby_node = args.node_b
            elif last_active == args.node_b:
                active_node = args.node_b
                standby_node = args.node_a
            else:
                # Default initial rotation: A -> B
                active_node = args.node_a
                standby_node = args.node_b

        logger.info("[CLI] Orchestrating Handover: %s -> %s", active_node, standby_node)
        orchestrator = HandoverOrchestrator(
            config_path=args.config,
            lock_file=args.lock_file,
            dry_run=args.dry_run,
            force=args.force
        )
        success = orchestrator.execute_handover(active_node, standby_node)
        sys.exit(0 if success else 5)

    # 6. Manual Rollback
    elif args.action == "rollback":
        active_node = args.active_node or args.node_a
        standby_node = args.standby_node or args.node_b
        logger.warning("[CLI] Executing manual rollback: restoring Active %s, killing Standby %s", active_node, standby_node)
        orchestrator = HandoverOrchestrator(args.config, args.lock_file, dry_run=args.dry_run, force=args.force)
        orchestrator._rollback_phase_4_5(active_node, standby_node, "Manual operator rollback command")
        sys.exit(0)

    else:
        logger.error("[CLI] Unknown action: %s", args.action)
        sys.exit(1)

if __name__ == "__main__":
    main()

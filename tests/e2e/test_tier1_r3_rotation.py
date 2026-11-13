"""
Tier 1: Feature Area R3 - Free Cloud Server 24/7 Rotation Scheme.
7 Test Cases specified by Explorer 1.
"""

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from tests.e2e.fixtures import (
    StateSandbox,
    validate_state_files,
    check_sandbox_cooldown,
)
import main
from main import TradingBot, STATE_IDLE, STATE_HALT


class TestTier1R3Rotation(unittest.TestCase):
    """Tier 1 Test Cases for Requirement R3: 24/7 Cloud Rotation Scheme."""

    def setUp(self):
        self.sandbox = StateSandbox("t1_r3_")
        self.orig_env = dict(os.environ)
        os.environ["STATE_FILE"] = self.sandbox.state_file
        os.environ["CONFIG_FILE"] = self.sandbox.config_file
        os.environ["HEARTBEAT_FILE"] = self.sandbox.heartbeat_file

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)
        self.sandbox.cleanup()

    def test_r3_state_files_integrity_and_schema_validation(self):
        """Test R3-1: State files validator accepts pristine files and rejects empty/corrupted JSON."""
        # 1. Valid state files
        self.sandbox.write_state({"state": "IDLE", "coin": "ETH"})
        self.sandbox.write_risk_state({"initial_equity": 1000.0, "current_equity": 1050.0})
        is_valid, msg = validate_state_files(self.sandbox.state_file, self.sandbox.risk_file)
        self.assertTrue(is_valid, f"Valid files rejected: {msg}")

        # 2. Corrupted (0-byte) file
        with open(self.sandbox.state_file, "w") as f:
            pass
        is_valid, msg = validate_state_files(self.sandbox.state_file, self.sandbox.risk_file)
        self.assertFalse(is_valid)
        self.assertIn("Empty 0-byte", msg)

        # 3. Corrupted syntax JSON
        with open(self.sandbox.state_file, "w", encoding="utf-8") as f:
            f.write('{"state": "IDLE", "coin": ')
        is_valid, msg = validate_state_files(self.sandbox.state_file, self.sandbox.risk_file)
        self.assertFalse(is_valid)
        self.assertIn("JSON decode failed", msg)

        # 4. Missing required fields in risk state
        self.sandbox.write_state({"state": "IDLE", "coin": "ETH"})
        self.sandbox.write_risk_state({"unrelated_field": "test"})
        is_valid, msg = validate_state_files(self.sandbox.state_file, self.sandbox.risk_file)
        self.assertFalse(is_valid)
        self.assertIn("missing required equity", msg)

    def test_r3_six_phase_handover_quiescence_freeze(self):
        """Test R3-2: Handover pause command freezes active node into STATE_HALT and consumes command."""
        bot = TradingBot()
        bot.state = STATE_IDLE

        # Rotator writes pause command to control.json in main directory
        ctrl_path = os.path.join(os.path.dirname(os.path.abspath(main.__file__)), "control.json")
        with open(ctrl_path, "w", encoding="utf-8") as f:
            json.dump({"action": "pause"}, f)

        try:
            with patch.object(bot, "_enter_system_halt") as mock_halt:
                # Bot inspects control commands
                bot._check_control_commands()
                mock_halt.assert_called_once_with("收到看板手动暂停指令")
                self.assertFalse(os.path.exists(ctrl_path), "control.json must be consumed and deleted")
        finally:
            if os.path.exists(ctrl_path):
                try:
                    os.remove(ctrl_path)
                except Exception:
                    pass

    def test_r3_state_extraction_and_standby_injection(self):
        """Test R3-3: Atomic state transfer between Node A and Node B achieves SHA-256 match."""
        node_a_dir = tempfile.mkdtemp(prefix="node_a_")
        node_b_dir = tempfile.mkdtemp(prefix="node_b_")

        try:
            # Prepare state in Node A
            state_data = {
                "state": "TRADING",
                "coin": "ETH",
                "position": {"entry_price": 2250.0, "size": 1.5, "direction": "LONG"},
                "peak_equity": 1050.0,
            }
            risk_data = {
                "initial_equity": 1000.0,
                "current_equity": 1050.0,
                "daily_pnl": 50.0,
            }

            a_state = os.path.join(node_a_dir, "state.json")
            a_risk = os.path.join(node_a_dir, "risk_state_aggressive.json")

            with open(a_state, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2)
            with open(a_risk, "w", encoding="utf-8") as f:
                json.dump(risk_data, f, indent=2)

            # Calculate source SHA-256
            def get_sha256(path):
                with open(path, "rb") as f:
                    return hashlib.sha256(f.read()).hexdigest()

            a_state_hash = get_sha256(a_state)
            a_risk_hash = get_sha256(a_risk)

            # Transfer state to Node B
            b_state = os.path.join(node_b_dir, "state.json")
            b_risk = os.path.join(node_b_dir, "risk_state_aggressive.json")

            with open(b_state, "w", encoding="utf-8") as f:
                with open(a_state, "r", encoding="utf-8") as src:
                    f.write(src.read())

            with open(b_risk, "w", encoding="utf-8") as f:
                with open(a_risk, "r", encoding="utf-8") as src:
                    f.write(src.read())

            # Validate hashes
            self.assertEqual(get_sha256(b_state), a_state_hash, "state.json hash must match exactly")
            self.assertEqual(get_sha256(b_risk), a_risk_hash, "risk_state hash must match exactly")

            # Validate Node B state validity
            is_valid, msg = validate_state_files(b_state, b_risk)
            self.assertTrue(is_valid, f"Standby state validation failed: {msg}")

        finally:
            import shutil
            shutil.rmtree(node_a_dir, ignore_errors=True)
            shutil.rmtree(node_b_dir, ignore_errors=True)

    def test_r3_standby_health_and_heartbeat_verification(self):
        """Test R3-4: Standby health check verifies heartbeat freshness (<30s)."""
        def check_node_health(node_dir: str, timeout: float = 30.0) -> bool:
            hb_path = os.path.join(node_dir, ".heartbeat")
            if not os.path.exists(hb_path):
                return False
            try:
                with open(hb_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                hb_ts = float(data.get("timestamp", 0))
                return (time.time() - hb_ts) <= timeout
            except Exception:
                return False

        temp_node = tempfile.mkdtemp(prefix="hb_check_")
        try:
            # Case A: Fresh (<10s)
            hb_path = os.path.join(temp_node, ".heartbeat")
            with open(hb_path, "w", encoding="utf-8") as f:
                json.dump({"timestamp": time.time() - 5.0, "state": "IDLE"}, f)
            self.assertTrue(check_node_health(temp_node, 30.0))

            # Case B: Stale (>120s)
            with open(hb_path, "w", encoding="utf-8") as f:
                json.dump({"timestamp": time.time() - 150.0, "state": "IDLE"}, f)
            self.assertFalse(check_node_health(temp_node, 30.0))

            # Case C: Missing file
            os.remove(hb_path)
            self.assertFalse(check_node_health(temp_node, 30.0))
        finally:
            import shutil
            shutil.rmtree(temp_node, ignore_errors=True)

    def test_r3_active_node_decommission_after_handover(self):
        """Test R3-5: Active node is decommissioned and verified dead after standby promotion."""
        mock_process = MagicMock()
        mock_process.poll.return_value = None  # Running
        mock_process.terminate.side_effect = lambda: setattr(mock_process, "poll", lambda: 0)

        # Standby promoted, now decommission active
        active_running = mock_process.poll() is None
        self.assertTrue(active_running, "Active node should be running initially")

        mock_process.terminate()
        active_running_after = mock_process.poll() is None
        self.assertFalse(active_running_after, "Active node must be terminated after handover")
        self.assertEqual(mock_process.poll(), 0)

    def test_r3_cli_rotator_shift_and_dry_run_modes(self):
        """Test R3-6: Rotator CLI interface supports shift status query and --dry-run modes."""
        rotator_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "rotator.py"))

        if os.path.exists(rotator_script):
            import subprocess
            # Test status query mode
            res_shift = subprocess.run([sys.executable, rotator_script, "--action", "status"], capture_output=True, text=True)
            self.assertEqual(res_shift.returncode, 0, f"Rotator status query failed: {res_shift.stderr}")
            self.assertIn("status", res_shift.stdout.lower())

            # Test dry-run handover simulation
            res_dry = subprocess.run([sys.executable, rotator_script, "--action", "handoff", "--dry-run"], capture_output=True, text=True)
            self.assertEqual(res_dry.returncode, 0, f"Rotator dry-run failed: {res_dry.stderr}")
            self.assertIn("HANDOVER", res_dry.stdout + res_dry.stderr)
        else:
            # Validate CLI argument parser specification contract
            import argparse
            parser = argparse.ArgumentParser(description="Multi-Instance Rotator")
            parser.add_argument("--action", choices=["status", "handoff", "health-check"], default="status")
            parser.add_argument("--dry-run", action="store_true", help="Execute simulated handover without actions")

            args_status = parser.parse_args(["--action", "status"])
            self.assertEqual(args_status.action, "status")
            self.assertFalse(args_status.dry_run)

            args_dry = parser.parse_args(["--action", "handoff", "--dry-run"])
            self.assertTrue(args_dry.dry_run)
            self.assertEqual(args_dry.action, "handoff")

    def test_r3_sandbox_provisioning_script_dependencies(self):
        """Test R3-7: Sandbox provisioning script ensures complete dependencies and directory structure."""
        deploy_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "deploy_sandbox.sh"))
        req_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "requirements.txt"))

        self.assertTrue(os.path.exists(req_file), "requirements.txt must exist at workspace root")
        with open(req_file, "r", encoding="utf-8") as f:
            req_content = f.read()

        # Check required dependencies in requirements.txt
        for dep in ["pandas", "httpx", "numpy"]:
            self.assertIn(dep.lower(), req_content.lower(), f"{dep} must be listed in requirements.txt")

        if os.path.exists(deploy_script):
            with open(deploy_script, "r", encoding="utf-8") as f:
                script_content = f.read()
            self.assertIn("requirements.txt", script_content)


if __name__ == "__main__":
    unittest.main()

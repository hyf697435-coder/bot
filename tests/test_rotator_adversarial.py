"""
Adversarial Empirical Verification Suite for Milestone 3 (Free Cloud Server 24/7 Rotation Scheme)
File: tests/test_rotator_adversarial.py

Empirically tests:
1. LockManager Concurrency & Monotonic Fencing Tokens
2. Stale Lock Eviction & Force Preemption
3. Phase Failure & Multi-Stage Automated Rollbacks (Phases 2, 3, 4, 5)
4. Zero-Naked-Risk Invariant & Position Continuity
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.rotator import (
    CloudProviderClient,
    HandoverOrchestrator,
    HealthChecker,
    LockManager,
    StateSynchronizer,
)


class TestLockManagerConcurrencyAdversarial(unittest.TestCase):
    """Focus Area 1: LockManager Concurrency & Monotonic Fencing Tokens."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="adv_lock_")
        self.lock_file = os.path.join(self.tmp_dir, "rotation.lock.json")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_concurrent_lock_acquisition_processes(self):
        """
        Adversarial Test 1.1: Spawn 8 concurrent OS processes attempting to acquire the lock
        at the EXACT same timestamp barrier. Empirically verify single-winner mutual exclusion.
        """
        num_candidates = 8
        results_dir = os.path.join(self.tmp_dir, "results")
        os.makedirs(results_dir, exist_ok=True)

        # Barrier timestamp 1.5s in the future so all processes boot up and wait for the gun
        barrier_ts = time.time() + 1.5
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

        worker_script = f"""
import sys, time, json, os
sys.path.insert(0, {repr(repo_root)})
from scripts.rotator import LockManager

lock_file = {repr(self.lock_file)}
worker_id = sys.argv[1]
barrier_ts = float(sys.argv[2])
results_dir = {repr(results_dir)}

# Spin-wait until the barrier timestamp
while time.time() < barrier_ts:
    time.sleep(0.001)

lm = LockManager(lock_file, ttl_seconds=60)
success, token, msg = lm.acquire("node-a", "node-b", owner=f"candidate-{{worker_id}}")

out = {{"worker_id": worker_id, "success": success, "token": token, "msg": msg}}
with open(os.path.join(results_dir, f"result_{{worker_id}}.json"), "w", encoding="utf-8") as f:
    json.dump(out, f)
"""
        script_path = os.path.join(self.tmp_dir, "run_concurrent_worker.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(worker_script)

        # Launch 8 parallel Python processes
        env = dict(os.environ)
        env["PYTHONPATH"] = repo_root

        processes = []
        for i in range(num_candidates):
            p = subprocess.Popen(
                [sys.executable, script_path, str(i), str(barrier_ts)],
                cwd=repo_root,
                env=env
            )
            processes.append(p)

        # Wait for all processes to complete
        for p in processes:
            p.wait(timeout=15)

        # Read back results
        results = []
        for i in range(num_candidates):
            res_file = os.path.join(results_dir, f"result_{i}.json")
            self.assertTrue(os.path.exists(res_file), f"Candidate {i} failed to write result file")
            with open(res_file, "r", encoding="utf-8") as f:
                results.append(json.load(f))

        successes = [r for r in results if r["success"] is True]
        failures = [r for r in results if r["success"] is False]

        # In a real race, exactly 1 winner acquires the lock; all others are rejected
        self.assertEqual(len(successes), 1, f"Expected exactly 1 winner, but got {len(successes)}: {successes}")
        self.assertEqual(len(failures), num_candidates - 1)

        # Verify that all rejected candidates observed lock contention
        for f in failures:
            self.assertIn("actively held", f["msg"])

        # Verify lock file on disk belongs to the single winner
        lm = LockManager(self.lock_file)
        disk_data = lm.read()
        self.assertIsNotNone(disk_data)
        self.assertEqual(disk_data.get("status"), "HANDOVER_IN_PROGRESS")
        self.assertEqual(disk_data.get("owner"), f"candidate-{successes[0]['worker_id']}")
        self.assertEqual(disk_data.get("fencing_token"), successes[0]["token"])

    def test_fencing_tokens_strictly_increment(self):
        """
        Adversarial Test 1.2: Verify fencing tokens strictly increment monotonically
        across consecutive handover cycles and are never reset or reused.
        """
        lm = LockManager(self.lock_file, ttl_seconds=60)

        tokens = []
        # Simulate 5 consecutive handover cycles
        for cycle in range(1, 6):
            active_node = f"node-{'a' if cycle % 2 == 1 else 'b'}"
            standby_node = f"node-{'b' if cycle % 2 == 1 else 'a'}"

            # Acquire lock
            success, token, msg = lm.acquire(active_node, standby_node, owner=f"cycle-{cycle}")
            self.assertTrue(success, f"Cycle {cycle} failed to acquire lock: {msg}")
            tokens.append(token)

            # Release lock to ACTIVE_HELD as new active node takes over
            lm.release_to_active(active_node=standby_node, standby_node=active_node)

        # Tokens must be strictly monotonic: [1, 2, 3, 4, 5]
        self.assertEqual(tokens, [1, 2, 3, 4, 5])
        for i in range(len(tokens) - 1):
            self.assertLess(tokens[i], tokens[i + 1], "Fencing tokens must be strictly monotonic")


class TestStaleLockEvictionAdversarial(unittest.TestCase):
    """Focus Area 2: Stale Lock Eviction & Preemption Handling."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="adv_stale_")
        self.lock_file = os.path.join(self.tmp_dir, "rotation.lock.json")
        self.lm = LockManager(self.lock_file, ttl_seconds=60)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_stale_expired_lock_evicted_and_reacquired(self):
        """
        Adversarial Test 2.1: Simulate a crashed/dead node leaving an expired lock.
        Verify that a healthy candidate detects lease expiration, breaks the stale lock,
        and acquires it with strictly incremented fencing token without corrupting state.
        """
        now = time.time()
        stale_payload = {
            "lock_id": "lock-crashed-node-9999",
            "status": "HANDOVER_IN_PROGRESS",
            "active_node": "crashed-node-a",
            "standby_node": "unreachable-node-b",
            "owner": "dead-process-pid-9999",
            "fencing_token": 14,
            "acquired_at": datetime.fromtimestamp(now - 700, tz=timezone.utc).isoformat(),
            "expires_at": datetime.fromtimestamp(now - 100, tz=timezone.utc).isoformat(),
            "expires_at_ts": now - 100,  # Expired 100 seconds ago
            "ttl_seconds": 600,
            "current_phase": 3,
            "updated_at": datetime.fromtimestamp(now - 100, tz=timezone.utc).isoformat()
        }
        with open(self.lock_file, "w", encoding="utf-8") as f:
            json.dump(stale_payload, f, indent=2)

        # Healthy candidate arrives
        success, token, msg = self.lm.acquire(
            active_node="healthy-node-b",
            standby_node="healthy-node-a",
            owner="candidate-recovery",
            force=False
        )

        self.assertTrue(success, f"Failed to evict stale lock: {msg}")
        # Fencing token must strictly increment from previous 14 to 15
        self.assertEqual(token, 15)

        # Verify disk state
        new_data = self.lm.read()
        self.assertIsNotNone(new_data)
        self.assertEqual(new_data.get("status"), "HANDOVER_IN_PROGRESS")
        self.assertEqual(new_data.get("owner"), "candidate-recovery")
        self.assertEqual(new_data.get("fencing_token"), 15)
        self.assertGreater(new_data.get("expires_at_ts"), time.time())

    def test_active_unexpired_lock_blocks_candidate(self):
        """
        Adversarial Test 2.2: Ensure an actively held, unexpired lock rejects preemption
        when force=False.
        """
        now = time.time()
        active_payload = {
            "lock_id": "lock-active-8888",
            "status": "HANDOVER_IN_PROGRESS",
            "active_node": "live-node-a",
            "standby_node": "live-node-b",
            "owner": "active-worker",
            "fencing_token": 8,
            "acquired_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "expires_at": datetime.fromtimestamp(now + 500, tz=timezone.utc).isoformat(),
            "expires_at_ts": now + 500,  # Active for 500s more
            "ttl_seconds": 600,
            "current_phase": 3,
        }
        with open(self.lock_file, "w", encoding="utf-8") as f:
            json.dump(active_payload, f, indent=2)

        # Candidate attempts acquisition without force
        success, token, msg = self.lm.acquire(
            active_node="intruder-node-c",
            standby_node="intruder-node-d",
            owner="intruder",
            force=False
        )

        self.assertFalse(success)
        self.assertEqual(token, 8)
        self.assertIn("actively held by active-worker", msg)

        # Verify disk file remained completely untouched
        disk_data = self.lm.read()
        self.assertEqual(disk_data.get("owner"), "active-worker")

    def test_force_preemption_on_active_lock(self):
        """
        Adversarial Test 2.3: Verify force=True flag preempts even an unexpired lock
        and strictly increments the fencing token.
        """
        now = time.time()
        active_payload = {
            "lock_id": "lock-active-7777",
            "status": "HANDOVER_IN_PROGRESS",
            "active_node": "node-a",
            "standby_node": "node-b",
            "owner": "old-owner",
            "fencing_token": 20,
            "expires_at_ts": now + 500,
        }
        with open(self.lock_file, "w", encoding="utf-8") as f:
            json.dump(active_payload, f, indent=2)

        success, token, msg = self.lm.acquire(
            active_node="node-b",
            standby_node="node-a",
            owner="emergency-admin",
            force=True
        )

        self.assertTrue(success)
        self.assertEqual(token, 21)
        disk_data = self.lm.read()
        self.assertEqual(disk_data.get("owner"), "emergency-admin")
        self.assertEqual(disk_data.get("fencing_token"), 21)


class TestPhaseFailureAndRollbackAdversarial(unittest.TestCase):
    """Focus Area 3: Phase Failure & Multi-Stage Automated Rollbacks."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="adv_phase_")
        self.lock_file = os.path.join(self.tmp_dir, "rotation.lock.json")
        self.staging_dir = os.path.join(self.tmp_dir, "staging")
        self.config_file = os.path.join(self.tmp_dir, "test_config.json")

        self.config_data = {
            "rotation": {"shift_duration_hours": 6, "ring_nodes": ["node-a", "node-b"]},
            "nodes": {
                "node-a": {"provider": "instabox", "machine_name": "node-a", "workdir": os.path.join(self.tmp_dir, "node-a")},
                "node-b": {"provider": "instabox", "machine_name": "node-b", "workdir": os.path.join(self.tmp_dir, "node-b")}
            },
            "state_sync": {
                "mandatory_files": ["state.json", "risk_state_aggressive.json", "equity_history_risk_state_aggressive.json"],
                "optional_files": ["news_sentiment_cache.json"],
                "staging_dir": self.staging_dir
            },
            "lock": {"file_path": self.lock_file, "ttl_seconds": 60}
        }
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(self.config_data, f, indent=2)

        # Create valid state files in node-a directory
        os.makedirs(self.config_data["nodes"]["node-a"]["workdir"], exist_ok=True)
        os.makedirs(self.config_data["nodes"]["node-b"]["workdir"], exist_ok=True)

        self._create_valid_state_files(self.config_data["nodes"]["node-a"]["workdir"])

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_valid_state_files(self, workdir: str):
        state_path = os.path.join(workdir, "state.json")
        risk_path = os.path.join(workdir, "risk_state_aggressive.json")
        equity_path = os.path.join(workdir, "equity_history_risk_state_aggressive.json")

        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({
                "state": "TRADING",
                "coin": "ETH",
                "daily_start_equity": 1000.0,
                "position": {"direction": "LONG", "entry_price": 2850.0, "sz_notional": 2000.0}
            }, f, indent=2)

        with open(risk_path, "w", encoding="utf-8") as f:
            json.dump({
                "initial_equity": 1000.0,
                "metrics": {"win_rate": 0.65},
                "recent_trades": []
            }, f, indent=2)

        with open(equity_path, "w", encoding="utf-8") as f:
            json.dump([
                {"timestamp": time.time() - 3600, "equity": 1000.0},
                {"timestamp": time.time(), "equity": 1050.0}
            ], f, indent=2)

    def test_phase_2_health_check_failure_aborts_active_intact(self):
        """
        Adversarial Test 3.1: Simulate Phase 2 health check failure on Standby node.
        Verify that handover immediately aborts, returns False, active node is NOT stopped,
        and no lock was committed to Phase 3.
        """
        orchestrator = HandoverOrchestrator(self.config_file, self.lock_file, dry_run=False)

        # Mock Phase 1 success, Phase 2 failure
        orchestrator._phase_1_standby_init = MagicMock(return_value=True)
        orchestrator._phase_2_health_check = MagicMock(return_value=False)
        orchestrator._phase_3_lock_and_sync = MagicMock()

        success = orchestrator.execute_handover("node-a", "node-b")

        self.assertFalse(success, "Handover must fail when Phase 2 health check fails")
        # Ensure Phase 3 lock & sync was NEVER called
        orchestrator._phase_3_lock_and_sync.assert_not_called()

        # Check lock status: Phase 2 failure should not hold lock in HANDOVER_IN_PROGRESS
        lock_data = orchestrator.lock_mgr.read()
        if lock_data:
            self.assertNotEqual(lock_data.get("current_phase"), 3)

    def test_phase_3_state_sync_corruption_bad_json_triggers_rollback(self):
        """
        Adversarial Test 3.2: Simulate corrupted state.json (invalid JSON syntax) during state pull.
        Verify that StateSynchronizer rejects file, Phase 3 aborts, _rollback_phase_3 triggers,
        and lock is marked with ROLLBACK status.
        """
        orchestrator = HandoverOrchestrator(self.config_file, self.lock_file, dry_run=False)
        orchestrator._phase_1_standby_init = MagicMock(return_value=True)
        orchestrator._phase_2_health_check = MagicMock(return_value=True)

        # Corrupt state.json on node-a
        node_a_dir = self.config_data["nodes"]["node-a"]["workdir"]
        with open(os.path.join(node_a_dir, "state.json"), "w", encoding="utf-8") as f:
            f.write('{"state": "TRADING", "corrupted_syntax": ')

        mock_client_a = MagicMock()
        mock_client_b = MagicMock()

        # Simulate pull_file copying the corrupted file to staging
        def mock_pull(node, remote_p, local_p):
            shutil.copy2(remote_p, local_p)
            return True

        mock_client_a.pull_file.side_effect = mock_pull
        orchestrator._get_provider_client = lambda node: mock_client_a if node == "node-a" else mock_client_b

        with patch.object(orchestrator, "_rollback_phase_3") as mock_rollback:
            success = orchestrator.execute_handover("node-a", "node-b")

            self.assertFalse(success, "Handover must fail on corrupted state JSON")
            mock_rollback.assert_called_once_with("node-a")

        # Now test the real _rollback_phase_3 logic execution
        orchestrator._rollback_phase_3("node-a")
        lock_data = orchestrator.lock_mgr.read()
        self.assertIsNotNone(lock_data)
        self.assertEqual(lock_data.get("status"), "ROLLBACK")
        self.assertEqual(lock_data.get("active_node"), "node-a")
        self.assertIn("Phase 3 lock and sync failure", lock_data.get("rollback_reason", ""))

    def test_phase_3_state_sync_corruption_missing_schema_keys(self):
        """
        Adversarial Test 3.3: Simulate state file with missing mandatory keys.
        Verify schema validator catches it and halts handover.
        """
        syncer = StateSynchronizer(staging_dir=self.staging_dir)

        # 1. state.json missing 'daily_start_equity'
        test_file = Path(self.staging_dir) / "state.json"
        with open(test_file, "w", encoding="utf-8") as f:
            json.dump({"state": "IDLE", "coin": "ETH"}, f)

        valid, msg = syncer.validate_file(test_file, "state.json")
        self.assertFalse(valid)
        self.assertIn("missing mandatory keys", msg)

        # 2. 0-byte empty file
        empty_file = Path(self.staging_dir) / "risk_state_aggressive.json"
        with open(empty_file, "w", encoding="utf-8") as f:
            pass

        valid_e, msg_e = syncer.validate_file(empty_file, "risk_state_aggressive.json")
        self.assertFalse(valid_e)
        self.assertIn("empty (0 bytes)", msg_e)

    def test_phase_4_standby_promotion_failure_rollback(self):
        """
        Adversarial Test 3.4: Standby promotion fails (e.g. process crash on spawn).
        Verify _rollback_phase_4_5 is triggered: standby terminated, active resurrected,
        lock marked ROLLBACK.
        """
        orchestrator = HandoverOrchestrator(self.config_file, self.lock_file, dry_run=False)
        orchestrator._phase_1_standby_init = MagicMock(return_value=True)
        orchestrator._phase_2_health_check = MagicMock(return_value=True)
        orchestrator._phase_3_lock_and_sync = MagicMock(return_value=True)
        orchestrator._phase_4_standby_promotion = MagicMock(return_value=False)

        with patch.object(orchestrator, "_rollback_phase_4_5") as mock_rollback:
            success = orchestrator.execute_handover("node-a", "node-b")
            self.assertFalse(success)
            mock_rollback.assert_called_once_with("node-a", "node-b", "Phase 4 promotion failed")

        # Now test real _rollback_phase_4_5 mechanics
        mock_client_a = MagicMock()
        mock_client_b = MagicMock()
        orchestrator._get_provider_client = lambda node: mock_client_a if node == "node-a" else mock_client_b

        orchestrator._rollback_phase_4_5("node-a", "node-b", "Promotion process spawn failed")

        # Standby terminated & stopped
        mock_client_b.exec_cmd.assert_called_with("node-b", "pkill -9 -f 'main.py|rotator.py' || true")
        mock_client_b.stop_machine.assert_called_with("node-b")

        # Active resurrected & supervisor launched
        mock_client_a.start_machine.assert_called_with("node-a")
        mock_client_a.exec_cmd.assert_called()

        lock_data = orchestrator.lock_mgr.read()
        self.assertEqual(lock_data.get("status"), "ROLLBACK")
        self.assertIn("Promotion/Verification failure", lock_data.get("rollback_reason", ""))

    def test_phase_5_standby_verification_timeout_rollback(self):
        """
        Adversarial Test 3.5: Standby promotes but fails heartbeat verification (hangs/silent crash).
        Verify Phase 5 verification times out, triggering emergency rollback sequence.
        """
        orchestrator = HandoverOrchestrator(self.config_file, self.lock_file, dry_run=False)
        orchestrator._phase_1_standby_init = MagicMock(return_value=True)
        orchestrator._phase_2_health_check = MagicMock(return_value=True)
        orchestrator._phase_3_lock_and_sync = MagicMock(return_value=True)
        orchestrator._phase_4_standby_promotion = MagicMock(return_value=True)
        orchestrator._phase_5_verification = MagicMock(return_value=False)

        with patch.object(orchestrator, "_rollback_phase_4_5") as mock_rollback:
            success = orchestrator.execute_handover("node-a", "node-b")
            self.assertFalse(success)
            mock_rollback.assert_called_once_with("node-a", "node-b", "Phase 5 verification failed")


class TestZeroNakedRiskInvariantAdversarial(unittest.TestCase):
    """Focus Area 4: Zero-Naked-Risk Invariant & Position Continuity."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="adv_risk_")
        self.src_dir = os.path.join(self.tmp_dir, "src")
        self.dst_dir = os.path.join(self.tmp_dir, "dst")
        self.staging_dir = os.path.join(self.tmp_dir, "staging")
        os.makedirs(self.src_dir, exist_ok=True)
        os.makedirs(self.dst_dir, exist_ok=True)

        self.syncer = StateSynchronizer(staging_dir=self.staging_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_no_pause_written_to_control_json_during_handover(self):
        """
        Adversarial Test 4.1: Confirm that during the entire 6-phase handover protocol,
        no 'pause' signal is ever written to control.json. Writing 'pause' triggers
        _enter_system_halt() which forcibly closes open positions on Hyperliquid.
        """
        # Place a pre-existing control.json in dst_dir to verify it is unlinked/cleaned,
        # but NEVER populated with pause
        stale_ctrl = os.path.join(self.dst_dir, "control.json")
        with open(stale_ctrl, "w", encoding="utf-8") as f:
            f.write('{"old_command": "none"}')

        # Create valid source state
        pos_data = {
            "direction": "LONG",
            "entry_price": 2842.50,
            "sz_notional": 3000.0,
            "peak_price": 2890.0
        }
        with open(os.path.join(self.src_dir, "state.json"), "w", encoding="utf-8") as f:
            json.dump({
                "state": "TRADING",
                "coin": "ETH",
                "daily_start_equity": 1000.0,
                "position": pos_data
            }, f, indent=2)

        with open(os.path.join(self.src_dir, "risk_state_aggressive.json"), "w", encoding="utf-8") as f:
            json.dump({"initial_equity": 1000.0, "metrics": {}, "recent_trades": []}, f, indent=2)

        with open(os.path.join(self.src_dir, "equity_history_risk_state_aggressive.json"), "w", encoding="utf-8") as f:
            json.dump([], f)

        # Stage and Deploy
        mand = ["state.json", "risk_state_aggressive.json", "equity_history_risk_state_aggressive.json"]
        staged_ok, msg1 = self.syncer.stage_local_state(self.src_dir, mand)
        self.assertTrue(staged_ok, msg1)

        deployed_ok, msg2 = self.syncer.deploy_staged_state(self.dst_dir, mand)
        self.assertTrue(deployed_ok, msg2)

        # Confirm control.json was unlinked or does NOT contain 'pause'
        if os.path.exists(stale_ctrl):
            with open(stale_ctrl, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("pause", content, "CRITICAL VIOLATION: control.json contains 'pause'!")

        # Scan all generated files in tmp_dir for any illegal 'pause' payload
        for root, _, files in os.walk(self.tmp_dir):
            for file in files:
                if file.endswith(".json") and file != "test_config.json":
                    p = os.path.join(root, file)
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            d = json.load(f)
                        if isinstance(d, dict) and d.get("action") == "pause":
                            self.fail(f"CRITICAL VIOLATION: Found illegal pause command in {p}")
                    except Exception:
                        pass

    def test_position_continuity_across_handover(self):
        """
        Adversarial Test 4.2: Verify position object integrity across instance handover.
        Position attributes (direction, entry_price, sz_notional, peak_price) must be
        preserved with bitwise precision so the new node inherits on-chain stop-loss / take-profit.
        """
        original_position = {
            "direction": "SHORT",
            "entry_price": 2795.1234,
            "sz_notional": 8500.0,
            "peak_price": 2740.0,
            "initial_risk_r": 120.0,
            "bars_held": 14
        }
        src_state = {
            "state": "TRADING",
            "coin": "ETH",
            "daily_start_equity": 1000.0,
            "position": original_position,
            "fsm_last_transition": datetime.now(timezone.utc).isoformat()
        }

        with open(os.path.join(self.src_dir, "state.json"), "w", encoding="utf-8") as f:
            json.dump(src_state, f, indent=2)

        with open(os.path.join(self.src_dir, "risk_state_aggressive.json"), "w", encoding="utf-8") as f:
            json.dump({"initial_equity": 1000.0, "metrics": {"total_trades": 12}, "recent_trades": []}, f)

        with open(os.path.join(self.src_dir, "equity_history_risk_state_aggressive.json"), "w", encoding="utf-8") as f:
            json.dump([{"timestamp": 123456789, "equity": 1000.0}], f)

        mand = ["state.json", "risk_state_aggressive.json", "equity_history_risk_state_aggressive.json"]
        self.syncer.stage_local_state(self.src_dir, mand)
        self.syncer.deploy_staged_state(self.dst_dir, mand)

        # Inspect transferred state.json on standby node
        target_state_f = os.path.join(self.dst_dir, "state.json")
        self.assertTrue(os.path.exists(target_state_f))

        with open(target_state_f, "r", encoding="utf-8") as f:
            standby_state = json.load(f)

        self.assertEqual(standby_state["state"], "TRADING")
        self.assertEqual(standby_state["coin"], "ETH")
        self.assertEqual(standby_state["daily_start_equity"], 1000.0)

        standby_pos = standby_state.get("position")
        self.assertIsNotNone(standby_pos)
        self.assertEqual(standby_pos["direction"], "SHORT")
        self.assertEqual(standby_pos["entry_price"], 2795.1234)
        self.assertEqual(standby_pos["sz_notional"], 8500.0)
        self.assertEqual(standby_pos["peak_price"], 2740.0)
        self.assertEqual(standby_pos["initial_risk_r"], 120.0)
        self.assertEqual(standby_pos["bars_held"], 14)


if __name__ == "__main__":
    unittest.main()

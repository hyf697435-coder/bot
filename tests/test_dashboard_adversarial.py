# -*- coding: utf-8 -*-
"""
Adversarial Stress Test for Dashboard Service:
1. Launch dashboard.py on test port 8877 in subprocess
2. Query /health, /api/state, /api/history, /api/risk, POST /api/control
3. Validate DOM elements: 5 tabs, button text labels in modals, sticky nav, 0 OKX occurrences
4. Clean process teardown
"""

import json
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.request
import urllib.error


PORT = 8877
BASE_URL = f"http://127.0.0.1:{PORT}"


class TestDashboardAdversarial(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        project_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        cmd = [sys.executable, "dashboard.py", "--port", str(PORT)]
        cls.proc = subprocess.Popen(
            cmd,
            cwd=project_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Poll up to 10 seconds for server startup
        started = False
        for _ in range(20):
            time.sleep(0.5)
            try:
                req = urllib.request.Request(f"{BASE_URL}/health")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        started = True
                        break
            except Exception:
                pass
        
        if not started:
            cls.proc.kill()
            out, err = cls.proc.communicate()
            raise RuntimeError(f"Dashboard failed to start on port {PORT}. Stdout: {out}\nStderr: {err}")

    @classmethod
    def tearDownClass(cls):
        if cls.proc and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=2)

    def test_01_health_endpoint(self):
        """Verify GET /health returns 200 and status ok."""
        req = urllib.request.Request(f"{BASE_URL}/health")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("status"), "ok")
            self.assertIn("timestamp", data)

    def test_02_api_state_endpoint(self):
        """Verify GET /api/state returns 200 and sane state fields."""
        req = urllib.request.Request(f"{BASE_URL}/api/state")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("account_equity", data)
            self.assertGreater(data["account_equity"], 0)
            self.assertIn("cached_atr_5m", data)
            # ATR sanity bounds (should not be 213.72)
            self.assertLess(data["cached_atr_5m"], 20.0, "cached_atr_5m must be calibrated, not BTC scale")

    def test_03_api_history_endpoint(self):
        """Verify GET /api/history returns 200 and valid timeseries array."""
        req = urllib.request.Request(f"{BASE_URL}/api/history?interval=1h")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("data", data)
            self.assertIsInstance(data["data"], list)
            self.assertGreater(len(data["data"]), 0)
            self.assertEqual(data.get("interval"), "1h")

    def test_04_api_risk_endpoint(self):
        """Verify GET /api/risk returns 200 and rolled-over daily PnL and trade metrics."""
        req = urllib.request.Request(f"{BASE_URL}/api/risk")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("daily_pnl", data)
            # Verify daily_pnl has rolled over to 0.00
            self.assertEqual(float(data["daily_pnl"]), 0.0)
            self.assertIn("metrics", data)
            metrics = data["metrics"]
            self.assertGreaterEqual(metrics.get("total_trades", 0), 5)
            self.assertEqual(metrics.get("winning_trades", 0), 1)
            self.assertEqual(metrics.get("losing_trades", 0), 4)

    def test_05_post_control_endpoint(self):
        """Verify POST /api/control writes atomically to control.json."""
        payload = json.dumps({"action": "refresh_macro"}).encode("utf-8")
        req = urllib.request.Request(
            f"{BASE_URL}/api/control",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("status"), "ok")
            self.assertEqual(data.get("action"), "refresh_macro")
        
        # Verify on-disk control.json
        project_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        ctrl_path = os.path.join(project_dir, "control.json")
        self.assertTrue(os.path.exists(ctrl_path))
        with open(ctrl_path, "r", encoding="utf-8") as f:
            disk_ctrl = json.load(f)
        self.assertEqual(disk_ctrl.get("action"), "refresh_macro")

    def test_06_dom_elements_and_tab_structure(self):
        """Verify DOM elements: 5 tabs, modal button texts, sticky nav, 0 OKX."""
        req = urllib.request.Request(f"{BASE_URL}/")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode("utf-8")

        # 1. Zero OKX occurrences in entire rendered HTML
        self.assertNotIn("okx", html.lower(), "HTML must contain 0 occurrences of OKX")

        # 2. 5 Tabs verification
        expected_tabs = [
            ("tabBtnOverview", "首页大盘", "tab-overview"),
            ("tabBtnSignals", "信号研判", "tab-signals"),
            ("tabBtnRisk", "持仓风控", "tab-risk"),
            ("tabBtnAnalytics", "绩效分析", "tab-analytics"),
            ("tabBtnSystem", "运维监控", "tab-system"),
        ]
        for btn_id, label, pane_id in expected_tabs:
            self.assertIn(btn_id, html, f"Tab button ID '{btn_id}' missing in DOM")
            self.assertIn(label, html, f"Tab label '{label}' missing in DOM")
            self.assertIn(pane_id, html, f"Tab pane ID '{pane_id}' missing in DOM")

        # 3. Modal Dialog buttons text labels
        # Pause modal
        self.assertIn('id="btnCancelPause"', html)
        self.assertIn('id="btnConfirmPause"', html)
        self.assertRegex(html, r'id="btnCancelPause"[^>]*>\s*取消\s*</button>')
        self.assertRegex(html, r'id="btnConfirmPause"[^>]*>\s*确认暂停\s*</button>')

        # Emergency modal
        self.assertIn('id="btnCancelEmergency"', html)
        self.assertIn('id="btnConfirmEmergency"', html)
        self.assertRegex(html, r'id="btnCancelEmergency"[^>]*>\s*取消\s*</button>')
        self.assertRegex(html, r'id="btnConfirmEmergency"[^>]*>\s*🛑\s*立即市价全平\s*</button>')

        # 4. Sticky nav bar styling
        sticky_match = re.search(r'(\.tabs-nav|\.navbar)\s*\{[^}]*position:\s*sticky[^}]*top:\s*0', html, re.DOTALL)
        self.assertIsNotNone(sticky_match, "Must have sticky navbar styling (position: sticky; top: 0)")


if __name__ == "__main__":
    unittest.main()

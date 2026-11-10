"""End-to-end tests: starts a real server and talks to it like the dashboard and an agent would.
Run:  python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import server as srv  # noqa: E402


class CafeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.cafe = srv.Cafe(os.path.join(cls.tmp, "cafe.db"), os.path.join(cls.tmp, "config.json"))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(cls.cafe))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.token = cls.call("/api/login", {"password": "admin123"})["token"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    @classmethod
    def call(cls, path, body=None, method=None, token=None):
        req = urllib.request.Request(cls.base + path, method=method or ("POST" if body is not None else "GET"),
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              "X-Admin-Token": token or getattr(cls, "token", "")})
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"_status": e.code, **json.loads(e.read())}

    def agent(self, pc_id, action="heartbeat", **extra):
        return self.call(f"/api/agent/{action}", {"key": self.cafe.cfg["agent_key"], "pc_id": pc_id,
                                                  "name": pc_id, **extra})

    def test_auth_required(self):
        self.assertEqual(self.call("/api/state", token="nope")["_status"], 401)
        self.assertEqual(self.call("/api/login", {"password": "wrong"})["_status"], 401)
        self.assertEqual(self.call("/api/agent/heartbeat", {"key": "bad", "pc_id": "x"})["_status"], 403)

    def test_pc_autoregisters_and_prepaid_flow(self):
        hb = self.agent("PC-01")
        self.assertEqual(hb["state"], "locked")
        state = self.call("/api/state")
        pc = next(p for p in state["pcs"] if p["id"] == "PC-01")
        self.assertTrue(pc["online"])
        self.assertEqual(pc["status"], "idle")

        r = self.call("/api/pc/PC-01/start", {"mode": "prepaid", "minutes": 60})
        self.assertTrue(r["ok"])
        hb = self.agent("PC-01")
        self.assertEqual(hb["state"], "active")
        self.assertAlmostEqual(hb["session"]["remaining"], 3600, delta=3)
        self.assertEqual(hb["session"]["cost"], 60)  # Standard = 60/hr

        self.assertEqual(self.call("/api/pc/PC-01/start", {"mode": "prepaid", "minutes": 10})["_status"], 409)
        self.call("/api/pc/PC-01/addtime", {"minutes": 30})
        hb = self.agent("PC-01")
        self.assertAlmostEqual(hb["session"]["remaining"], 5400, delta=3)
        self.assertEqual(hb["session"]["cost"], 90)

        bill = self.call("/api/pc/PC-01/end", {})["bill"]
        self.assertEqual(bill["amount"], 90)
        self.assertEqual(bill["due"], 0)
        hb = self.agent("PC-01")
        self.assertEqual(hb["state"], "locked")
        self.assertIn("lock", [c["type"] for c in hb["commands"]])

    def test_time_up_auto_locks(self):
        self.agent("PC-02")
        self.call("/api/pc/PC-02/start", {"mode": "prepaid", "minutes": 1})
        with self.cafe.lock:  # fast-forward: pretend the session ends now
            self.cafe.db.execute("UPDATE sessions SET ends=? WHERE pc_id='PC-02' AND ended IS NULL",
                                 (time.time() - 1,))
        self.cafe.tick()
        hb = self.agent("PC-02")
        self.assertEqual(hb["state"], "locked")
        rep = self.call("/api/report")
        s = [x for x in rep["sessions"] if x["pc_id"] == "PC-02"][-1]
        self.assertEqual(s["end_reason"], "time_up")

    def test_postpaid_minimum_charge(self):
        self.agent("PC-03")
        self.call("/api/pc/PC-03/start", {"mode": "postpaid"})
        bill = self.call("/api/pc/PC-03/end", {})["bill"]
        self.assertEqual(bill["billed_minutes"], 15)   # min_minutes
        self.assertEqual(bill["due"], 15.0)            # 60/hr * 15 min

    def test_member_login_balance_and_logout(self):
        self.agent("PC-04")
        mid = self.call("/api/members", {"username": "rahul", "password": "pass1", "balance": 120})["id"]
        bad = self.agent("PC-04", "login", username="rahul", password="wrong")
        self.assertEqual(bad["_status"], 401)
        self.assertTrue(self.agent("PC-04", "login", username="rahul", password="pass1")["ok"])
        hb = self.agent("PC-04")
        self.assertEqual(hb["state"], "active")
        self.assertAlmostEqual(hb["session"]["remaining"], 7200, delta=3)  # 120 / 60 per hr = 2 h

        # same member can't log in twice
        self.agent("PC-05")
        self.assertEqual(self.agent("PC-05", "login", username="rahul", password="pass1")["_status"], 409)

        # top-up mid-session extends time
        self.call(f"/api/members/{mid}/topup", {"amount": 60})
        self.assertAlmostEqual(self.agent("PC-04")["session"]["remaining"], 10800, delta=3)

        out = self.agent("PC-04", "logout")
        self.assertEqual(out["bill"]["amount"], 15.0)
        self.assertEqual(out["bill"]["balance_left"], 165.0)
        m = next(x for x in self.call("/api/members") if x["id"] == mid)
        self.assertEqual(m["balance"], 165.0)

    def test_member_insufficient_balance(self):
        self.agent("PC-06")
        self.call("/api/members", {"username": "broke", "password": "pass1"})
        self.assertEqual(self.agent("PC-06", "login", username="broke", password="pass1")["_status"], 402)

    def test_commands_and_bulk(self):
        self.agent("PC-07")
        self.agent("PC-08")
        self.call("/api/pc/PC-07/command", {"type": "message", "text": "Closing in 10 min"})
        cmds = self.agent("PC-07")["commands"]
        self.assertEqual(cmds[0]["text"], "Closing in 10 min")
        self.assertEqual(self.agent("PC-07")["commands"], [])  # delivered once
        self.call("/api/pc/PC-08/start", {"mode": "prepaid", "minutes": 30})
        self.call("/api/bulk", {"type": "shutdown", "ids": ["PC-07", "PC-08"]})
        self.assertIn("shutdown", [c["type"] for c in self.agent("PC-08")["commands"]])
        self.assertEqual(self.agent("PC-08")["state"], "locked")
        self.assertEqual(self.call("/api/pc/PC-07/command", {"type": "rm -rf"})["_status"], 400)

    def test_settings_and_vip_rate(self):
        self.agent("PC-09")
        self.call("/api/pc/PC-09/update", {"group": "VIP", "name": "VIP-1"})
        self.call("/api/pc/PC-09/start", {"mode": "prepaid", "minutes": 60})
        self.assertEqual(self.agent("PC-09")["session"]["cost"], 100)
        self.call("/api/pc/PC-09/end", {})
        r = self.call("/api/settings", {"rates": {"Standard": 50, "VIP": 90}})
        self.assertEqual(r["rates"]["VIP"], 90)

    def test_report_and_csv(self):
        rep = self.call("/api/report")
        self.assertIn("cash_collected", rep["totals"])
        req = urllib.request.Request(self.base + "/api/report.csv", headers={"X-Admin-Token": self.token})
        with urllib.request.urlopen(req) as r:
            self.assertIn("Session ID", r.read().decode("utf-8-sig"))

    def test_dashboard_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"CafeMaster", r.read())

    def test_restart_restores_running_sessions(self):
        self.agent("PC-10")
        self.call("/api/pc/PC-10/start", {"mode": "prepaid", "minutes": 45})
        cafe2 = srv.Cafe(os.path.join(self.tmp, "cafe.db"), os.path.join(self.tmp, "config.json"))
        self.assertEqual(cafe2.heartbeat("PC-10", "PC-10", "127.0.0.1", {})["state"], "active")


if __name__ == "__main__":
    unittest.main()

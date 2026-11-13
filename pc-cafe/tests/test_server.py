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
        handler = srv.make_handler(cls.cafe)
        handler.log_message = lambda *a: None
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
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


    # ---- v2: games, rig register, packages, group start ----------------------
    def test_games_catalogue_and_rig_register(self):
        games = self.call("/api/games")
        f1 = next(g for g in games if g["name"] == "F1 25")          # seeded starter entry
        self.call("/api/games", {"id": f1["id"], "name": "F1 25", "launch": "steam://rungameid/123",
                                 "category": "Racing", "max_players": 20})
        acc = self.call("/api/games", {"name": "Assetto Corsa", "launch": r"C:\\Games\\ac.exe"})["id"]
        self.agent("RIG-1")
        self.call("/api/pc/RIG-1/update", {"account": "Steam: rig1_cafe", "notes": "Fanatec wheel",
                                           "games": [f1["id"], acc, 9999]})
        pc = next(p for p in self.call("/api/state")["pcs"] if p["id"] == "RIG-1")
        self.assertEqual(sorted(pc["games"]), sorted([f1["id"], acc]))   # unknown id dropped
        self.assertEqual(pc["account"], "Steam: rig1_cafe")
        # agent launcher only receives installed games that have a launch target
        names = [g["name"] for g in self.agent("RIG-1")["games"]]
        self.assertEqual(sorted(names), ["Assetto Corsa", "F1 25"])
        # deleting a game removes it from rigs
        self.call(f"/api/games/{acc}", method="DELETE")
        self.assertNotIn(acc, next(p for p in self.call("/api/state")["pcs"] if p["id"] == "RIG-1")["games"])

    def test_shared_account_flagged(self):
        self.agent("RIG-A"); self.agent("RIG-B")
        self.call("/api/pc/RIG-A/update", {"account": "EA: same@x"})
        self.call("/api/pc/RIG-B/update", {"account": "ea: SAME@x "})
        pcs = {p["id"]: p for p in self.call("/api/state")["pcs"]}
        self.assertEqual(pcs["RIG-A"]["shares_account_with"], ["RIG-B"])
        r = self.call("/api/group/start", {"ids": ["RIG-A", "RIG-B"], "minutes": 30})
        self.assertTrue(any("same game account" in w for w in r["warnings"]))
        self.call(f"/api/group/{r['group_id']}/end", {})

    def test_race_package_group_start_split_and_end(self):
        gid = self.call("/api/games", {"name": "Race Sim", "launch": "steam://rungameid/1"})["id"]
        ids = ["R1", "R2", "R3"]
        for i in ids:
            self.agent(i)
        self.call("/api/pc/R1/update", {"games": [gid]})
        self.call("/api/pc/R2/update", {"games": [gid]})        # R3 lacks the game
        pkg = self.call("/api/packages", {"name": "Race night 3x1h", "minutes": 60, "price": 500,
                                          "rigs": 3, "game_id": gid})["id"]
        # wrong rig count is rejected
        self.assertEqual(self.call("/api/group/start", {"ids": ids[:2], "package_id": pkg})["_status"], 400)
        r = self.call("/api/group/start", {"ids": ids, "package_id": pkg, "label": "Team Apex"})
        self.assertEqual(r["total"], 500)
        self.assertTrue(any("R3" in w for w in r["warnings"]))
        sessions = [self.agent(i) for i in ids]
        self.assertEqual(round(sum(h["session"]["cost"] for h in sessions), 2), 500)   # split exactly
        self.assertEqual({h["session"]["group_id"] for h in sessions}, {r["group_id"]})
        self.assertEqual(sessions[0]["session"]["package"], "Race night 3x1h")
        self.assertAlmostEqual(sessions[0]["session"]["remaining"], 3600, delta=3)
        # every rig was told to launch the game (delivered with the heartbeat above)
        self.assertTrue(all(any(c["type"] == "launch" and c["game_id"] == gid for c in h["commands"])
                            for h in sessions))
        # agent reports launches -> top games report
        self.agent("R1", "launch", game_id=gid)
        self.agent("R2", "launch", game_id=gid)
        top = self.call("/api/report")["top_games"]
        self.assertEqual(next(g for g in top if g["name"] == "Race Sim")["rigs"], 2)
        end = self.call(f"/api/group/{r['group_id']}/end", {})
        self.assertEqual(end["rigs"], 3)
        self.assertEqual(end["amount"], 500)
        self.assertTrue(all(self.agent(i)["state"] == "locked" for i in ids))

    def test_group_start_is_all_or_nothing(self):
        for i in ("G1", "G2"):
            self.agent(i)
        self.call("/api/pc/G2/start", {"mode": "prepaid", "minutes": 10})
        before = self.call("/api/report")["totals"]["cash_collected"]
        r = self.call("/api/group/start", {"ids": ["G1", "G2"], "minutes": 60})
        self.assertEqual(r["_status"], 409)
        self.assertEqual(self.agent("G1")["state"], "locked")
        self.assertEqual(self.call("/api/report")["totals"]["cash_collected"], before)
        self.call("/api/pc/G2/end", {})

    def test_per_rig_package_and_remote_launch(self):
        gid = self.call("/api/games", {"name": "Kart", "launch": "kart.exe"})["id"]
        nolaunch = self.call("/api/games", {"name": "Not set up"})["id"]
        pkg = self.call("/api/packages", {"name": "5 hours", "minutes": 300, "price": 250})["id"]
        self.agent("K1")
        self.assertEqual(self.call("/api/pc/K1/command", {"type": "launch", "game_id": gid})["_status"], 409)
        r = self.call("/api/pc/K1/start", {"mode": "package", "package_id": pkg})
        self.assertIsNone(r["group_id"])
        hb = self.agent("K1")
        self.assertEqual(hb["session"]["cost"], 250)
        self.assertAlmostEqual(hb["session"]["remaining"], 18000, delta=3)
        self.call("/api/pc/K1/command", {"type": "launch", "game_id": gid})
        self.assertEqual(self.agent("K1")["commands"][0]["type"], "launch")
        self.assertEqual(self.call("/api/pc/K1/command", {"type": "launch", "game_id": nolaunch})["_status"], 400)
        self.call("/api/pc/K1/end", {})
        self.call(f"/api/packages/{pkg}", method="DELETE")
        self.assertNotIn(pkg, [p["id"] for p in self.call("/api/packages")])

    def test_v1_database_migrates(self):
        import sqlite3
        path = os.path.join(self.tmp, "old.db")
        db = sqlite3.connect(path)
        db.executescript("CREATE TABLE pcs (id TEXT PRIMARY KEY, name TEXT, grp TEXT, created REAL);"
                         "CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, pc_id TEXT, pc_name TEXT,"
                         " member_id INTEGER, user_label TEXT, mode TEXT, rate REAL, started REAL, ends REAL,"
                         " ended REAL, minutes REAL, amount REAL DEFAULT 0, prepaid REAL DEFAULT 0, end_reason TEXT);"
                         "INSERT INTO pcs VALUES ('OLD','Old PC','Standard',0);")
        db.commit(); db.close()
        c = srv.Cafe(path, os.path.join(self.tmp, "config.json"))
        self.assertEqual(c.heartbeat("OLD", "Old PC", "1.1.1.1", {})["games"], [])
        self.assertEqual(next(p for p in c.state()["pcs"] if p["id"] == "OLD")["account"], "")


    # ---- v3: monitoring -------------------------------------------------------
    def raw_screen(self, pc_id, data=b"\x89PNGfake", mime="image/png", key=None):
        req = urllib.request.Request(self.base + "/api/agent/screen", data=data, method="POST", headers={
            "Content-Type": mime, "X-Agent-Key": key or self.cafe.cfg["agent_key"], "X-PC-Id": pc_id})
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"_status": e.code}

    def get_raw(self, path):
        req = urllib.request.Request(self.base + path, headers={"X-Admin-Token": self.token})
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def test_screen_view_flow(self):
        self.agent("MON-1")
        self.assertIsNone(self.agent("MON-1")["watch"])               # nobody watching -> no streaming
        status, _, _ = self.get_raw("/api/pc/MON-1/screen?mode=thumb")
        self.assertEqual(status, 204)                                 # no frame yet
        self.assertEqual(self.agent("MON-1")["watch"], "thumb")       # agent is now asked to stream
        self.get_raw("/api/pc/MON-1/screen?mode=full")
        self.assertEqual(self.agent("MON-1")["watch"], "full")
        self.assertEqual(self.raw_screen("MON-1", b"jpegbytes", "image/jpeg")["watch"], "full")
        status, ctype, body = self.get_raw("/api/pc/MON-1/screen?mode=full")
        self.assertEqual((status, ctype, body), (200, "image/jpeg", b"jpegbytes"))
        self.assertEqual(self.raw_screen("MON-1", key="wrong")["_status"], 403)
        self.assertEqual(self.raw_screen("MON-1", mime="text/html")["_status"], 400)
        # watching expires
        self.cafe.live["MON-1"]["watch"] = {"full": 0, "thumb": 0}
        self.assertIsNone(self.agent("MON-1")["watch"])
        # owner can turn screen view off entirely
        self.call("/api/settings", {"monitoring": {"screen_view": False}})
        self.assertEqual(self.call("/api/pc/MON-1/screen")["_status"], 403)
        self.assertFalse(self.agent("MON-1")["policy"]["screen_view"])
        self.call("/api/settings", {"monitoring": {"screen_view": True}})

    def test_activity_and_processes(self):
        self.call("/api/agent/heartbeat", {"key": self.cafe.cfg["agent_key"], "pc_id": "MON-2", "name": "MON-2",
                                           "info": {"active": {"title": "YouTube - Chrome", "app": "chrome.exe"}}})
        pc = next(p for p in self.call("/api/state")["pcs"] if p["id"] == "MON-2")
        self.assertEqual(pc["activity"]["title"], "YouTube - Chrome")
        self.call("/api/pc/MON-2/processes?refresh=1")
        self.assertIn("processes", [c["type"] for c in self.agent("MON-2")["commands"]])
        self.agent("MON-2", "processes", list=[{"name": "chrome.exe", "pid": 7, "mem_mb": 300}])
        self.assertEqual(self.call("/api/pc/MON-2/processes")["list"][0]["name"], "chrome.exe")
        # closing an app
        self.call("/api/pc/MON-2/command", {"type": "kill", "text": "chrome.exe"})
        cmd = self.agent("MON-2")["commands"][0]
        self.assertEqual((cmd["type"], cmd["text"]), ("kill", "chrome.exe"))
        self.assertEqual(self.call("/api/pc/MON-2/command", {"type": "kill", "text": "lsass.exe"})["_status"], 400)
        self.assertEqual(self.call("/api/pc/MON-2/command", {"type": "kill", "text": "a.exe;del"})["_status"], 400)

    def test_policy_settings_and_delivery(self):
        r = self.call("/api/settings", {"monitoring": {"blocked_apps": "qbittorrent.exe\n\n  tor.exe ",
                                                       "blocked_keywords": ["casino"],
                                                       "blocked_sites": "bad-site.com"}})
        self.assertEqual(r["monitoring"]["blocked_apps"], ["qbittorrent.exe", "tor.exe"])
        pol = self.agent("MON-3")["policy"]
        self.assertEqual(pol["blocked_sites"], ["bad-site.com"])
        self.assertEqual(pol["blocked_keywords"], ["casino"])
        self.assertEqual(self.call("/api/settings", {"monitoring": {"on_violation": "nuke"}})["_status"], 400)

    def test_alerts_alert_only_and_auto_lock(self):
        import base64
        self.agent("MON-4")
        self.call("/api/settings", {"monitoring": {"on_violation": "alert"}})
        self.call("/api/pc/MON-4/start", {"mode": "prepaid", "minutes": 60, "label": "Guest"})
        r = self.agent("MON-4", "alert", kind="blocked_word", detail='"casino" on screen',
                       image=base64.b64encode(b"img").decode(), mime="image/jpeg")
        self.assertEqual(self.agent("MON-4")["state"], "active")        # alert only: keeps playing
        st = self.call("/api/state")
        self.assertGreaterEqual(st["alerts_open"], 1)
        self.assertEqual(next(p for p in st["pcs"] if p["id"] == "MON-4")["alert"]["id"], r["id"])
        a = next(x for x in self.call("/api/alerts") if x["id"] == r["id"])
        self.assertEqual((a["user_label"], a["action"], a["has_image"]), ("Guest", "alert", 1))
        self.assertEqual(self.get_raw(f"/api/alerts/{r['id']}/image")[2], b"img")
        self.call("/api/alerts/ack", {"ids": [r["id"]]})
        self.assertNotIn(r["id"], [x["id"] for x in self.call("/api/alerts?open=1")])

        # auto-lock mode ends the session and tells the customer
        self.call("/api/settings", {"monitoring": {"on_violation": "lock"}})
        self.agent("MON-4", "alert", kind="blocked_app", detail="Blocked app running: tor.exe")
        hb = self.agent("MON-4")
        self.assertEqual(hb["state"], "locked")
        self.assertTrue(any(c["type"] == "message" and "cafe rules" in c["text"] for c in hb["commands"]))
        rep = self.call("/api/report")
        self.assertEqual([s for s in rep["sessions"] if s["pc_id"] == "MON-4"][-1]["end_reason"], "policy_violation")
        self.call("/api/settings", {"monitoring": {"on_violation": "alert"}})
        self.call("/api/alerts/ack", {})
        self.assertEqual(self.call("/api/state")["alerts_open"], 0)

    # ---- v4: time-up warnings, top-ups with UPI QR --------------------------------
    def test_timeup_settings_delivered(self):
        r = self.call("/api/settings", {"timeup": {"warn_minutes": "10, 5 1", "close_game": False}})
        self.assertEqual(r["timeup"], {"warn_minutes": [10, 5, 1], "close_game": False})
        hb = self.agent("TU-1")
        self.assertEqual(hb["timeup"]["warn_minutes"], [10, 5, 1])
        self.assertFalse(hb["timeup"]["close_game"])
        self.call("/api/settings", {"timeup": {"close_game": True}})

    def test_payment_settings_validation(self):
        self.assertEqual(self.call("/api/settings", {"payments": {"upi_id": "not a upi"}})["_status"], 400)
        self.assertEqual(self.call("/api/settings", {"payments": {"topup_options": "0 -5"}})["_status"], 400)
        r = self.call("/api/settings", {"payments": {"upi_id": "mycafe@okhdfcbank", "payee_name": "My Cafe",
                                                     "topup_options": "120 30, 60"}})
        self.assertEqual(r["payments"]["topup_options"], [30, 60, 120])

    def test_prepaid_topup_upi_approve(self):
        self.call("/api/settings", {"payments": {"upi_id": "mycafe@okhdfcbank", "payee_name": "My Cafe"}})
        self.agent("TU-2")
        self.assertIsNone(self.agent("TU-2")["topup"]["kind"])              # nothing running: no top-up
        self.call("/api/pc/TU-2/start", {"mode": "prepaid", "minutes": 30, "label": "Vikram"})
        hb = self.agent("TU-2")
        self.assertEqual(hb["topup"]["kind"], "time")
        self.assertTrue(hb["topup"]["upi"])
        req = self.agent("TU-2", "topup", minutes=60, method="upi")
        self.assertEqual((req["amount"], req["upi_id"], req["ref"]), (60.0, "mycafe@okhdfcbank", f"T{req['id']}"))
        self.assertIn("+60 min", req["note"])
        pend = self.call("/api/state")["topups_pending"]
        self.assertEqual([(t["id"], t["user_label"]) for t in pend if t["pc_id"] == "TU-2"], [(req["id"], "Vikram")])
        self.call(f"/api/topups/{req['id']}/approve", {})
        hb = self.agent("TU-2")
        self.assertAlmostEqual(hb["session"]["remaining"], 90 * 60, delta=3)
        self.assertEqual(hb["session"]["cost"], 90)
        self.assertEqual(hb["topup"]["last"]["status"], "approved")
        self.assertTrue(any("Payment received" in c.get("text", "") for c in hb["commands"]))
        self.assertEqual(self.call(f"/api/topups/{req['id']}/approve", {})["_status"], 409)   # no double credit
        upi = [t for t in self.call("/api/report")["transactions"] if t["pc_id"] == "TU-2" and t["method"] == "upi"]
        self.assertEqual(upi[0]["amount"], 60)
        self.call("/api/pc/TU-2/end", {})

    def test_topup_after_time_up_unlocks_pc(self):
        self.agent("TU-3")
        self.call("/api/pc/TU-3/start", {"mode": "prepaid", "minutes": 30, "label": "Neha"})
        with self.cafe.lock:
            self.cafe.db.execute("UPDATE sessions SET ends=? WHERE pc_id='TU-3' AND ended IS NULL", (time.time() - 1,))
        self.cafe.tick()
        hb = self.agent("TU-3")
        self.assertEqual((hb["state"], hb["topup"]["kind"]), ("locked", "extend"))
        req = self.agent("TU-3", "topup", minutes=30, method="counter")
        self.assertEqual(req["kind"], "extend")
        self.call(f"/api/topups/{req['id']}/approve", {})
        hb = self.agent("TU-3")
        self.assertEqual(hb["state"], "active")
        self.assertEqual(hb["session"]["user"], "Neha")
        self.assertAlmostEqual(hb["session"]["remaining"], 1800, delta=3)
        self.call("/api/pc/TU-3/end", {})
        # the extend window closes: a walk-in can't unlock a PC long after their time ended
        with self.cafe.lock:
            self.cafe.db.execute("UPDATE sessions SET ended=?, end_reason='time_up' WHERE pc_id='TU-3'",
                                 (time.time() - 3600,))
        self.assertIsNone(self.agent("TU-3")["topup"]["kind"])
        self.assertEqual(self.agent("TU-3", "topup", minutes=30, method="counter")["_status"], 409)

    def test_member_topup_reject_cancel_expire(self):
        self.agent("TU-4")
        mid = self.call("/api/members", {"username": "aditi", "password": "pass1", "balance": 60})["id"]
        self.agent("TU-4", "login", username="aditi", password="pass1")
        self.assertEqual(self.agent("TU-4")["topup"]["kind"], "balance")
        r1 = self.agent("TU-4", "topup", minutes=60, method="counter")
        r2 = self.agent("TU-4", "topup", minutes=120, method="counter")      # replaces the first
        statuses = {t["id"]: t["status"] for t in self.call("/api/topups")}
        self.assertEqual((statuses[r1["id"]], statuses[r2["id"]]), ("cancelled", "pending"))
        self.call(f"/api/topups/{r2['id']}/reject", {})
        self.assertEqual(self.agent("TU-4")["topup"]["last"]["status"], "rejected")
        r3 = self.agent("TU-4", "topup", minutes=60, method="counter")
        self.call(f"/api/topups/{r3['id']}/approve", {})
        m = next(x for x in self.call("/api/members") if x["id"] == mid)
        self.assertEqual(m["balance"], 120)
        r4 = self.agent("TU-4", "topup", minutes=30, method="counter")
        self.agent("TU-4", "topup", cancel=True)
        self.assertEqual(next(t for t in self.call("/api/topups") if t["id"] == r4["id"])["status"], "cancelled")
        r5 = self.agent("TU-4", "topup", minutes=30, method="counter")
        with self.cafe.lock:
            self.cafe.db.execute("UPDATE topups SET ts=? WHERE id=?", (time.time() - 3600, r5["id"]))
        self.cafe.tick()
        self.assertEqual(next(t for t in self.call("/api/topups") if t["id"] == r5["id"])["status"], "expired")
        self.agent("TU-4", "logout")

    def test_upi_needs_setup_and_postpaid_has_no_topup(self):
        self.call("/api/settings", {"payments": {"upi_id": ""}})
        self.agent("TU-5")
        self.call("/api/pc/TU-5/start", {"mode": "prepaid", "minutes": 30})
        self.assertEqual(self.agent("TU-5", "topup", minutes=30, method="upi")["_status"], 400)
        self.call("/api/pc/TU-5/end", {})
        self.call("/api/pc/TU-5/start", {"mode": "postpaid"})
        self.assertIsNone(self.agent("TU-5")["topup"]["kind"])
        self.call("/api/pc/TU-5/end", {})

    def test_database_never_left_locked(self):
        import sqlite3
        self.agent("DB-1")
        self.call("/api/pc/DB-1/update", {"name": "New name", "group": "NoSuchGroup"})   # fails half-way
        self.cafe.tick()
        other = sqlite3.connect(os.path.join(self.tmp, "cafe.db"), timeout=0.5)
        other.execute("UPDATE pcs SET notes='x' WHERE id='DB-1'")    # another program can write
        other.commit()
        other.close()
        self.assertNotEqual(next(p for p in self.call("/api/state")["pcs"] if p["id"] == "DB-1")["name"], "New name")

if __name__ == "__main__":
    unittest.main()

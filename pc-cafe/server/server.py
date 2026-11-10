#!/usr/bin/env python3
"""
CafeMaster - master server for a PC cafe.

Runs on the counter / admin PC. Every gaming PC runs `client/agent.py`, which
checks in with this server every couple of seconds. Staff control everything
from the web dashboard at http://<server-ip>:8080.

Standard library only. Python 3.9+.
"""
import argparse
import csv
import datetime as dt
import hashlib
import hmac
import io
import json
import math
import os
import secrets
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
OFFLINE_AFTER = 10          # seconds without heartbeat => PC shown offline
VALID_COMMANDS = {"message", "shutdown", "restart", "lock", "logoff"}

DEFAULT_CONFIG = {
    "cafe_name": "CafeMaster Gaming",
    "port": 8080,
    "currency": "₹",
    "admin_password": "admin123",
    "agent_key": "",                       # generated on first run
    "rates": {"Standard": 60, "VIP": 100},  # per hour
    "min_minutes": 15,                      # minimum billable minutes (postpaid/member)
    "round_to_minutes": 1,                  # bill in blocks of N minutes
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def now():
    return time.time()


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${digest}"


def verify_password(password, stored):
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    changed = not os.path.exists(path)
    if not cfg.get("agent_key"):
        cfg["agent_key"] = secrets.token_urlsafe(18)
        changed = True
    if changed:
        save_config(path, cfg)
    return cfg


def save_config(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# Core: state + billing. One lock guards both memory and the DB.
# --------------------------------------------------------------------------- #
class Cafe:
    def __init__(self, db_path, config_path):
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init_db()
        self.live = {}       # pc_id -> {"last_seen", "ip", "commands": [], "info": {}}
        self.admin_tokens = {}  # token -> expiry

    # ---- schema -------------------------------------------------------------
    def _init_db(self):
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS pcs (
                id TEXT PRIMARY KEY, name TEXT, grp TEXT DEFAULT 'Standard',
                created REAL);
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE COLLATE NOCASE,
                full_name TEXT, phone TEXT, password TEXT, balance REAL DEFAULT 0,
                created REAL);
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, pc_id TEXT, pc_name TEXT,
                member_id INTEGER, user_label TEXT, mode TEXT, rate REAL,
                started REAL, ends REAL, ended REAL, minutes REAL,
                amount REAL DEFAULT 0, prepaid REAL DEFAULT 0, end_reason TEXT);
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT,
                member_id INTEGER, session_id INTEGER, pc_id TEXT, amount REAL,
                method TEXT, note TEXT);
            """
        )
        self.db.commit()

    # ---- config -------------------------------------------------------------
    def rate_for(self, grp):
        rates = self.cfg["rates"]
        return float(rates.get(grp, next(iter(rates.values()), 60)))

    def public_settings(self):
        c = self.cfg
        return {k: c[k] for k in ("cafe_name", "currency", "rates", "min_minutes", "round_to_minutes")}

    def update_settings(self, data):
        with self.lock:
            if "cafe_name" in data:
                self.cfg["cafe_name"] = str(data["cafe_name"])[:60] or "CafeMaster"
            if "currency" in data:
                self.cfg["currency"] = str(data["currency"])[:5]
            if "rates" in data:
                rates = {str(k)[:30]: float(v) for k, v in data["rates"].items() if str(k).strip()}
                if not rates or any(v < 0 for v in rates.values()):
                    raise ApiError(400, "Rates must be non-negative and at least one group is needed")
                self.cfg["rates"] = rates
            for key in ("min_minutes", "round_to_minutes"):
                if key in data:
                    self.cfg[key] = max(0 if key == "min_minutes" else 1, int(data[key]))
            if data.get("admin_password"):
                if len(data["admin_password"]) < 6:
                    raise ApiError(400, "Admin password must be at least 6 characters")
                self.cfg["admin_password"] = data["admin_password"]
                self.admin_tokens.clear()
            save_config(self.config_path, self.cfg)
            return self.public_settings()

    # ---- admin auth ---------------------------------------------------------
    def admin_login(self, password):
        if not hmac.compare_digest(str(password), str(self.cfg["admin_password"])):
            raise ApiError(401, "Wrong password")
        token = secrets.token_urlsafe(24)
        self.admin_tokens[token] = now() + 12 * 3600
        return token

    def check_admin(self, token):
        exp = self.admin_tokens.get(token or "")
        if not exp or exp < now():
            raise ApiError(401, "Login required")

    # ---- billing maths ------------------------------------------------------
    def billable_minutes(self, seconds):
        minutes = seconds / 60
        block = max(1, int(self.cfg["round_to_minutes"]))
        minutes = math.ceil(minutes / block - 1e-9) * block
        return max(minutes, int(self.cfg["min_minutes"]))

    @staticmethod
    def cost(rate, minutes):
        return round(rate * minutes / 60, 2)

    # ---- sessions -----------------------------------------------------------
    def active_session(self, pc_id):
        return self.db.execute(
            "SELECT * FROM sessions WHERE pc_id=? AND ended IS NULL ORDER BY id DESC LIMIT 1",
            (pc_id,)).fetchone()

    def _pc(self, pc_id):
        row = self.db.execute("SELECT * FROM pcs WHERE id=?", (pc_id,)).fetchone()
        if not row:
            raise ApiError(404, "Unknown PC")
        return row

    def start_session(self, pc_id, mode, minutes=None, member_id=None, label=None, method="cash"):
        with self.lock:
            pc = self._pc(pc_id)
            if self.active_session(pc_id):
                raise ApiError(409, f"{pc['name']} already has a running session")
            rate = self.rate_for(pc["grp"])
            t = now()
            ends, prepaid = None, 0.0
            if mode == "prepaid":
                minutes = int(minutes or 0)
                if minutes <= 0:
                    raise ApiError(400, "Enter the number of minutes")
                ends = t + minutes * 60
                prepaid = self.cost(rate, minutes)
                label = label or "Walk-in"
            elif mode == "postpaid":
                label = label or "Walk-in"
            elif mode == "member":
                m = self.db.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
                if not m:
                    raise ApiError(404, "Member not found")
                if self.db.execute("SELECT 1 FROM sessions WHERE member_id=? AND ended IS NULL",
                                   (member_id,)).fetchone():
                    raise ApiError(409, "This member is already logged in on another PC")
                if rate > 0 and m["balance"] < self.cost(rate, int(self.cfg["min_minutes"])):
                    raise ApiError(402, "Not enough balance. Please top up at the counter.")
                ends = t + (m["balance"] / rate * 3600 if rate > 0 else 365 * 86400)
                label = m["username"]
            elif mode == "free":
                label = label or "Staff / Free"
                rate = 0.0
            else:
                raise ApiError(400, "Unknown session type")
            cur = self.db.execute(
                "INSERT INTO sessions (pc_id, pc_name, member_id, user_label, mode, rate, started, ends, prepaid)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (pc_id, pc["name"], member_id, label, mode, rate, t, ends, prepaid))
            if prepaid:
                self._txn("session_prepaid", prepaid, session_id=cur.lastrowid, pc_id=pc_id,
                          method=method, note=f"{minutes} min on {pc['name']}")
            self.db.commit()
            return cur.lastrowid

    def add_time(self, pc_id, minutes, method="cash"):
        with self.lock:
            s = self.active_session(pc_id)
            if not s:
                raise ApiError(409, "No running session")
            if s["mode"] != "prepaid":
                raise ApiError(400, "Add time only applies to prepaid sessions "
                                    "(members: top up balance instead)")
            minutes = int(minutes)
            if minutes <= 0:
                raise ApiError(400, "Enter minutes to add")
            amt = self.cost(s["rate"], minutes)
            self.db.execute("UPDATE sessions SET ends=?, prepaid=prepaid+? WHERE id=?",
                            (max(now(), s["ends"]) + minutes * 60, amt, s["id"]))
            self._txn("session_prepaid", amt, session_id=s["id"], pc_id=pc_id, method=method,
                      note=f"+{minutes} min")
            self.db.commit()

    def end_session(self, pc_id, reason="staff"):
        """Close the running session and return a bill summary."""
        with self.lock:
            s = self.active_session(pc_id)
            if not s:
                raise ApiError(409, "No running session")
            t = now()
            if s["mode"] == "prepaid":
                end_t = min(t, s["ends"])
            else:
                end_t = t
            used_sec = max(0, end_t - s["started"])
            bill = {"session_id": s["id"], "mode": s["mode"], "user": s["user_label"],
                    "minutes_used": round(used_sec / 60, 1)}
            if s["mode"] == "prepaid":
                amount = s["prepaid"]
                bill.update(amount=amount, due=0)
            elif s["mode"] == "free":
                amount = 0.0
                bill.update(amount=0, due=0)
            else:
                billed_min = self.billable_minutes(used_sec)
                amount = self.cost(s["rate"], billed_min)
                bill["billed_minutes"] = billed_min
                if s["mode"] == "member":
                    m = self.db.execute("SELECT balance FROM members WHERE id=?",
                                        (s["member_id"],)).fetchone()
                    bal = m["balance"] if m else 0
                    amount = min(amount, max(bal, 0))  # never push a member negative
                    self.db.execute("UPDATE members SET balance=balance-? WHERE id=?",
                                    (amount, s["member_id"]))
                    self._txn("member_usage", amount, member_id=s["member_id"], session_id=s["id"],
                              pc_id=pc_id, method="balance", note=f"{billed_min} min")
                    bill.update(amount=amount, due=0, balance_left=round(bal - amount, 2))
                else:  # postpaid -> collect at counter
                    self._txn("session_postpaid", amount, session_id=s["id"], pc_id=pc_id,
                              method="cash", note=f"{billed_min} min")
                    bill.update(amount=amount, due=amount)
            self.db.execute("UPDATE sessions SET ended=?, minutes=?, amount=?, end_reason=? WHERE id=?",
                            (t, round(used_sec / 60, 2), amount, reason, s["id"]))
            self.db.commit()
            self.queue_command(pc_id, {"type": "lock"})
            if bill.get("due"):
                self.queue_command(pc_id, {"type": "message",
                                           "text": f"Session ended. Please pay {self.cfg['currency']}"
                                                   f"{amount:.0f} at the counter."})
            return bill

    def tick(self):
        """Expire sessions whose time ran out. Called every second."""
        with self.lock:
            t = now()
            for s in self.db.execute(
                    "SELECT pc_id FROM sessions WHERE ended IS NULL AND ends IS NOT NULL AND ends<=?",
                    (t,)).fetchall():
                try:
                    self.end_session(s["pc_id"], reason="time_up")
                except ApiError:
                    pass

    def session_view(self, s):
        if not s:
            return None
        t = now()
        elapsed = t - s["started"]
        out = {"id": s["id"], "mode": s["mode"], "user": s["user_label"], "rate": s["rate"],
               "started": s["started"], "ends": s["ends"], "elapsed": int(elapsed),
               "remaining": int(s["ends"] - t) if s["ends"] else None}
        if s["mode"] == "prepaid":
            out["cost"] = s["prepaid"]
        elif s["mode"] == "free":
            out["cost"] = 0
        else:
            out["cost"] = self.cost(s["rate"], self.billable_minutes(elapsed))
        if s["mode"] == "member":
            m = self.db.execute("SELECT balance FROM members WHERE id=?", (s["member_id"],)).fetchone()
            out["balance"] = m["balance"] if m else 0
        return out

    # ---- transactions -------------------------------------------------------
    def _txn(self, kind, amount, member_id=None, session_id=None, pc_id=None, method=None, note=None):
        self.db.execute(
            "INSERT INTO transactions (ts, kind, member_id, session_id, pc_id, amount, method, note)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (now(), kind, member_id, session_id, pc_id, amount, method, note))

    # ---- PCs ----------------------------------------------------------------
    def queue_command(self, pc_id, cmd):
        live = self.live.setdefault(pc_id, {"last_seen": 0, "ip": "", "commands": [], "info": {}})
        cmd = dict(cmd, id=secrets.token_hex(4), ts=now())
        live["commands"].append(cmd)
        live["commands"] = live["commands"][-20:]

    def command(self, pc_id, ctype, text=None):
        if ctype not in VALID_COMMANDS:
            raise ApiError(400, "Unknown command")
        with self.lock:
            self._pc(pc_id)
            if ctype == "lock":
                if self.active_session(pc_id):
                    return self.end_session(pc_id, reason="locked_by_staff")
                self.queue_command(pc_id, {"type": "lock"})
                return None
            if ctype in ("shutdown", "restart", "logoff") and self.active_session(pc_id):
                self.end_session(pc_id, reason=ctype)
            if ctype == "message":
                text = (text or "").strip()[:500]
                if not text:
                    raise ApiError(400, "Message is empty")
            self.queue_command(pc_id, {"type": ctype, "text": text})
            return None

    def heartbeat(self, pc_id, name, ip, info):
        with self.lock:
            row = self.db.execute("SELECT * FROM pcs WHERE id=?", (pc_id,)).fetchone()
            if not row:
                grp = next(iter(self.cfg["rates"]))
                self.db.execute("INSERT INTO pcs (id, name, grp, created) VALUES (?,?,?,?)",
                                (pc_id, name or pc_id, grp, now()))
                self.db.commit()
                row = self._pc(pc_id)
            live = self.live.setdefault(pc_id, {"last_seen": 0, "ip": "", "commands": [], "info": {}})
            live.update(last_seen=now(), ip=ip, info=info or {})
            commands, live["commands"] = live["commands"], []
            s = self.session_view(self.active_session(pc_id))
            return {
                "pc": {"id": pc_id, "name": row["name"], "group": row["grp"]},
                "cafe_name": self.cfg["cafe_name"], "currency": self.cfg["currency"],
                "rate": self.rate_for(row["grp"]),
                "server_time": now(), "state": "active" if s else "locked",
                "session": s, "commands": commands,
            }

    def update_pc(self, pc_id, data):
        with self.lock:
            self._pc(pc_id)
            if "name" in data and str(data["name"]).strip():
                self.db.execute("UPDATE pcs SET name=? WHERE id=?", (str(data["name"]).strip()[:40], pc_id))
            if "group" in data:
                if data["group"] not in self.cfg["rates"]:
                    raise ApiError(400, "Unknown rate group")
                if self.active_session(pc_id):
                    raise ApiError(409, "End the running session before changing the group")
                self.db.execute("UPDATE pcs SET grp=? WHERE id=?", (data["group"], pc_id))
            self.db.commit()

    def delete_pc(self, pc_id):
        with self.lock:
            if self.active_session(pc_id):
                raise ApiError(409, "End the running session first")
            self.db.execute("DELETE FROM pcs WHERE id=?", (pc_id,))
            self.db.commit()
            self.live.pop(pc_id, None)

    def state(self):
        with self.lock:
            t = now()
            pcs = []
            for row in self.db.execute("SELECT * FROM pcs ORDER BY name COLLATE NOCASE"):
                live = self.live.get(row["id"], {})
                online = t - live.get("last_seen", 0) < OFFLINE_AFTER
                s = self.session_view(self.active_session(row["id"]))
                pcs.append({
                    "id": row["id"], "name": row["name"], "group": row["grp"],
                    "rate": self.rate_for(row["grp"]), "online": online, "ip": live.get("ip", ""),
                    "info": live.get("info", {}), "last_seen": live.get("last_seen"),
                    "status": ("in_use" if s else "idle") if online else ("in_use_offline" if s else "offline"),
                    "session": s,
                })
            return {"server_time": t, "settings": self.public_settings(), "pcs": pcs,
                    "today": self.report(dt.date.today().isoformat())["totals"]}

    # ---- members ------------------------------------------------------------
    def members(self, q=""):
        with self.lock:
            rows = self.db.execute(
                "SELECT m.id, m.username, m.full_name, m.phone, m.balance, m.created,"
                " (SELECT pc_name FROM sessions s WHERE s.member_id=m.id AND s.ended IS NULL) AS on_pc"
                " FROM members m WHERE m.username LIKE ? OR m.full_name LIKE ? OR m.phone LIKE ?"
                " ORDER BY m.username COLLATE NOCASE", (f"%{q}%",) * 3).fetchall()
            return [dict(r) for r in rows]

    def add_member(self, data):
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        if not username or len(username) > 30 or not username.replace("_", "").replace(".", "").isalnum():
            raise ApiError(400, "Username: letters, numbers, dot or underscore (max 30)")
        if len(password) < 4:
            raise ApiError(400, "Password must be at least 4 characters")
        with self.lock:
            try:
                cur = self.db.execute(
                    "INSERT INTO members (username, full_name, phone, password, balance, created)"
                    " VALUES (?,?,?,?,0,?)",
                    (username, str(data.get("full_name", ""))[:60], str(data.get("phone", ""))[:20],
                     hash_password(password), now()))
            except sqlite3.IntegrityError:
                raise ApiError(409, "Username already taken")
            mid = cur.lastrowid
            self.db.commit()
            opening = float(data.get("balance") or 0)
            if opening > 0:
                self.topup(mid, opening, data.get("method", "cash"))
            return mid

    def topup(self, member_id, amount, method="cash"):
        amount = round(float(amount), 2)
        if amount == 0:
            raise ApiError(400, "Enter an amount")
        with self.lock:
            m = self.db.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
            if not m:
                raise ApiError(404, "Member not found")
            if m["balance"] + amount < 0:
                raise ApiError(400, "Balance cannot go below zero")
            self.db.execute("UPDATE members SET balance=balance+? WHERE id=?", (amount, member_id))
            self._txn("topup" if amount > 0 else "refund", amount, member_id=member_id,
                      method=method, note=m["username"])
            # extend an active member session to match the new balance
            s = self.db.execute("SELECT * FROM sessions WHERE member_id=? AND ended IS NULL",
                                (member_id,)).fetchone()
            if s and s["rate"] > 0:
                new_bal = m["balance"] + amount
                self.db.execute("UPDATE sessions SET ends=? WHERE id=?",
                                (s["started"] + new_bal / s["rate"] * 3600, s["id"]))
            self.db.commit()

    def reset_member_password(self, member_id, password):
        if len(str(password)) < 4:
            raise ApiError(400, "Password must be at least 4 characters")
        with self.lock:
            self.db.execute("UPDATE members SET password=? WHERE id=?", (hash_password(password), member_id))
            self.db.commit()

    def delete_member(self, member_id):
        with self.lock:
            if self.db.execute("SELECT 1 FROM sessions WHERE member_id=? AND ended IS NULL",
                               (member_id,)).fetchone():
                raise ApiError(409, "Member is logged in right now")
            self.db.execute("DELETE FROM members WHERE id=?", (member_id,))
            self.db.commit()

    def member_login(self, pc_id, username, password):
        with self.lock:
            m = self.db.execute("SELECT * FROM members WHERE username=?", (username.strip(),)).fetchone()
            if not m or not verify_password(password, m["password"]):
                raise ApiError(401, "Wrong username or password")
            return self.start_session(pc_id, "member", member_id=m["id"])

    # ---- reports ------------------------------------------------------------
    def report(self, day):
        try:
            d = dt.date.fromisoformat(day)
        except ValueError:
            raise ApiError(400, "Date must be YYYY-MM-DD")
        start = time.mktime(d.timetuple())
        end = start + 86400
        with self.lock:
            txns = [dict(r) for r in self.db.execute(
                "SELECT * FROM transactions WHERE ts>=? AND ts<? ORDER BY ts", (start, end))]
            sessions = [dict(r) for r in self.db.execute(
                "SELECT * FROM sessions WHERE started>=? AND started<? ORDER BY started", (start, end))]
        cash_in = sum(t["amount"] for t in txns if t["kind"] in ("session_prepaid", "session_postpaid", "topup"))
        refunds = sum(t["amount"] for t in txns if t["kind"] == "refund")
        usage = sum(t["amount"] for t in txns if t["kind"] == "member_usage")
        totals = {
            "date": day,
            "cash_collected": round(cash_in + refunds, 2),
            "walkin_sales": round(sum(t["amount"] for t in txns if t["kind"].startswith("session_")), 2),
            "member_topups": round(sum(t["amount"] for t in txns if t["kind"] == "topup") + refunds, 2),
            "member_usage": round(usage, 2),
            "sessions": len(sessions),
            "hours_played": round(sum((s["minutes"] or (now() - s["started"]) / 60) for s in sessions) / 60, 1),
        }
        return {"totals": totals, "transactions": txns, "sessions": sessions}

    def report_csv(self, day):
        rep = self.report(day)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Session ID", "PC", "User", "Type", "Rate/hr", "Start", "End",
                    "Minutes", "Amount", "End reason"])
        fmt = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
        for s in rep["sessions"]:
            w.writerow([s["id"], s["pc_name"], s["user_label"], s["mode"], s["rate"], fmt(s["started"]),
                        fmt(s["ended"]), s["minutes"] if s["minutes"] is not None else "running",
                        s["amount"] if s["ended"] else s["prepaid"], s["end_reason"] or ""])
        w.writerow([])
        w.writerow(["Transactions"])
        w.writerow(["Time", "Type", "PC", "Amount", "Method", "Note"])
        for t in rep["transactions"]:
            w.writerow([fmt(t["ts"]), t["kind"], t["pc_id"] or "", t["amount"], t["method"] or "", t["note"] or ""])
        w.writerow([])
        for k, v in rep["totals"].items():
            w.writerow([k, v])
        return buf.getvalue()


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
def make_handler(cafe):
    dashboard_path = os.path.join(HERE, "dashboard.html")

    class Handler(BaseHTTPRequestHandler):
        server_version = "CafeMaster/1.0"

        def log_message(self, fmt, *args):
            if "/api/agent/" not in (args[0] if args else ""):
                super().log_message(fmt, *args)

        # -- plumbing --
        def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, status=200):
            self._send(status, json.dumps(obj, ensure_ascii=False))

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ApiError(413, "Request too large")
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except json.JSONDecodeError:
                raise ApiError(400, "Invalid JSON")

        def _admin(self):
            cafe.check_admin(self.headers.get("X-Admin-Token"))

        def _agent(self, body):
            if not hmac.compare_digest(str(body.get("key", "")), cafe.cfg["agent_key"]):
                raise ApiError(403, "Bad agent key")
            pc_id = str(body.get("pc_id", "")).strip()[:64]
            if not pc_id or not all(c.isalnum() or c in "-_." for c in pc_id):
                raise ApiError(400, "pc_id missing")
            return pc_id

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _dispatch(self, method):
            try:
                self._route(method)
            except ApiError as e:
                self._json({"error": e.message}, e.status)
            except (ValueError, TypeError, KeyError) as e:
                self._json({"error": f"Bad request: {e}"}, 400)
            except Exception as e:  # pragma: no cover
                self.log_error("Unhandled: %r", e)
                self._json({"error": "Server error"}, 500)

        # -- routes --
        def _route(self, method):
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            qs = parse_qs(url.query)

            if method == "GET" and url.path in ("/", "/index.html"):
                with open(dashboard_path, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if not parts or parts[0] != "api":
                raise ApiError(404, "Not found")
            route = parts[1:]

            # agent endpoints (gaming PCs)
            if route[:1] == ["agent"] and method == "POST":
                body = self._body()
                pc_id = self._agent(body)
                action = route[1] if len(route) > 1 else ""
                if action == "heartbeat":
                    return self._json(cafe.heartbeat(pc_id, body.get("name"), self.client_address[0],
                                                     body.get("info")))
                if action == "login":
                    cafe.member_login(pc_id, str(body.get("username", "")), str(body.get("password", "")))
                    return self._json({"ok": True})
                if action == "logout":
                    with cafe.lock:
                        s = cafe.active_session(pc_id)
                        if s and s["mode"] not in ("member", "postpaid"):
                            raise ApiError(403, "Ask staff to end this session")
                        bill = cafe.end_session(pc_id, reason="user_logout")
                    return self._json({"ok": True, "bill": bill})
                raise ApiError(404, "Not found")

            # admin login
            if route == ["login"] and method == "POST":
                return self._json({"token": cafe.admin_login(self._body().get("password", ""))})

            self._admin()
            body = self._body() if method == "POST" else {}

            if route == ["state"] and method == "GET":
                return self._json(cafe.state())
            if route == ["settings"]:
                if method == "GET":
                    s = cafe.public_settings()
                    s["agent_key"] = cafe.cfg["agent_key"]
                    return self._json(s)
                if method == "POST":
                    return self._json(cafe.update_settings(body))

            if route[:1] == ["pc"] and len(route) >= 2:
                pc_id, action = route[1], (route[2] if len(route) > 2 else "")
                if method == "DELETE" and not action:
                    cafe.delete_pc(pc_id)
                    return self._json({"ok": True})
                if method == "POST":
                    if action == "start":
                        mode = body.get("mode", "prepaid")
                        member_id = None
                        if mode == "member":
                            with cafe.lock:
                                m = cafe.db.execute("SELECT id FROM members WHERE username=?",
                                                    (str(body.get("username", "")).strip(),)).fetchone()
                            if not m:
                                raise ApiError(404, "Member not found")
                            member_id = m["id"]
                        sid = cafe.start_session(pc_id, mode, body.get("minutes"), member_id,
                                                 (body.get("label") or "").strip()[:40] or None,
                                                 body.get("method", "cash"))
                        return self._json({"ok": True, "session_id": sid})
                    if action == "addtime":
                        cafe.add_time(pc_id, body.get("minutes", 0), body.get("method", "cash"))
                        return self._json({"ok": True})
                    if action == "end":
                        return self._json({"ok": True, "bill": cafe.end_session(pc_id)})
                    if action == "command":
                        bill = cafe.command(pc_id, body.get("type"), body.get("text"))
                        return self._json({"ok": True, "bill": bill})
                    if action == "update":
                        cafe.update_pc(pc_id, body)
                        return self._json({"ok": True})

            if route == ["bulk"] and method == "POST":
                ids = body.get("ids") or [p["id"] for p in cafe.state()["pcs"] if p["online"]]
                results = {}
                for pc_id in ids:
                    try:
                        cafe.command(pc_id, body.get("type"), body.get("text"))
                        results[pc_id] = "ok"
                    except ApiError as e:
                        results[pc_id] = e.message
                return self._json({"ok": True, "results": results})

            if route == ["members"]:
                if method == "GET":
                    return self._json(cafe.members(qs.get("q", [""])[0]))
                if method == "POST":
                    return self._json({"ok": True, "id": cafe.add_member(body)})
            if route[:1] == ["members"] and len(route) >= 2:
                mid = int(route[1])
                action = route[2] if len(route) > 2 else ""
                if method == "DELETE" and not action:
                    cafe.delete_member(mid)
                    return self._json({"ok": True})
                if method == "POST" and action == "topup":
                    cafe.topup(mid, body.get("amount", 0), body.get("method", "cash"))
                    return self._json({"ok": True})
                if method == "POST" and action == "password":
                    cafe.reset_member_password(mid, body.get("password", ""))
                    return self._json({"ok": True})

            if route == ["report"] and method == "GET":
                return self._json(cafe.report(qs.get("date", [dt.date.today().isoformat()])[0]))
            if route == ["report.csv"] and method == "GET":
                day = qs.get("date", [dt.date.today().isoformat()])[0]
                return self._send(200, "﻿" + cafe.report_csv(day), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="cafe-report-{day}.csv"'})

            raise ApiError(404, "Not found")

    return Handler


def run(host, port, data_dir):
    os.makedirs(data_dir, exist_ok=True)
    cafe = Cafe(os.path.join(data_dir, "cafe.db"), os.path.join(data_dir, "config.json"))
    port = port or int(cafe.cfg["port"])

    def ticker():
        while True:
            try:
                cafe.tick()
            except Exception as e:  # pragma: no cover
                print("tick error:", e)
            time.sleep(1)

    threading.Thread(target=ticker, daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(cafe))
    print(f"""
  CafeMaster server running
  Dashboard : http://localhost:{port}   (admin password in {os.path.join(data_dir, 'config.json')})
  Agent key : {cafe.cfg['agent_key']}
  Point each gaming PC's agent at  http://<this-pc-ip>:{port}
""")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    return cafe


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CafeMaster master server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=0, help="default: from config.json (8080)")
    ap.add_argument("--data", default=os.path.join(HERE, "data"), help="folder for cafe.db and config.json")
    a = ap.parse_args()
    run(a.host, a.port, a.data)

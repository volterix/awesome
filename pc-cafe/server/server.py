#!/usr/bin/env python3
"""
CafeMaster - master server for a PC cafe.

Runs on the counter / admin PC. Every gaming PC runs `client/agent.py`, which
checks in with this server every couple of seconds. Staff control everything
from the web dashboard at http://<server-ip>:8080.

Standard library only. Python 3.9+.
"""
import argparse
import base64
import csv
import datetime as dt
import hashlib
import hmac
import io
import json
import math
import re
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
VALID_COMMANDS = {"message", "shutdown", "restart", "lock", "logoff", "launch", "kill", "processes"}
WATCH_SECONDS = 12          # a PC keeps streaming its screen this long after the dashboard last asked
ALERT_IMAGE_DAYS = 7
EXTEND_WINDOW = 30 * 60     # a walk-in can pay to extend from the lock screen this long after time ran out
TOPUP_EXPIRES = 15 * 60     # unanswered top-up requests expire after this
UPI_RE = r"^[\w.\-]{2,256}@[A-Za-z][\w.]{1,64}$"        # alert screenshots are deleted after this many days
PROTECTED_PROCS = {"system", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
                   "lsass.exe", "svchost.exe", "dwm.exe", "python.exe", "pythonw.exe", "explorer.exe"}

DEFAULT_CONFIG = {
    "cafe_name": "CafeMaster Gaming",
    "port": 8080,
    "currency": "₹",
    "admin_password": "admin123",
    "agent_key": "",                       # generated on first run
    "rates": {"Standard": 60, "VIP": 100},  # per hour
    "min_minutes": 15,                      # minimum billable minutes (postpaid/member)
    "round_to_minutes": 1,                  # bill in blocks of N minutes
    "timeup": {
        "warn_minutes": [10, 5, 1],         # pop-ups on the PC this many minutes before time runs out
        "close_game": True,                 # close the game in front when time is up, so the lock shows
    },
    "payments": {
        "upi_id": "",                       # e.g. cafename@okhdfcbank - enables the QR code on PCs
        "payee_name": "",
        "topup_options": [30, 60, 120],     # minutes offered on the PC's top-up screen
    },
    "monitoring": {
        "screen_view": True,
        "blocked_apps": ["qbittorrent.exe", "utorrent.exe", "bittorrent.exe", "tor.exe", "vuze.exe"],
        "blocked_keywords": ["torrent", "magnet:"],
        "blocked_sites": [],
        "on_violation": "alert",            # alert | lock
    },
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
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, launch TEXT,
                category TEXT, max_players INTEGER, sort INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, minutes INTEGER,
                price REAL, rigs INTEGER DEFAULT 1, game_id INTEGER, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS stock_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, unit TEXT DEFAULT 'pcs',
                qty REAL DEFAULT 0, low_level REAL DEFAULT 0, cost REAL DEFAULT 0, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS stock_moves (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, stock_id INTEGER, change REAL,
                kind TEXT, ref TEXT, note TEXT, cost_total REAL DEFAULT 0, qty_after REAL);
            CREATE TABLE IF NOT EXISTS menu_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, category TEXT, price REAL,
                active INTEGER DEFAULT 1, sort INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS recipes (
                menu_id INTEGER, stock_id INTEGER, qty REAL, PRIMARY KEY (menu_id, stock_id));
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, pc_id TEXT, pc_name TEXT, session_id INTEGER,
                member_id INTEGER, customer TEXT, source TEXT, status TEXT DEFAULT 'new', total REAL,
                cost REAL, pay_method TEXT, paid INTEGER DEFAULT 0, paid_ts REAL, note TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS order_items (
                order_id INTEGER, menu_id INTEGER, name TEXT, qty INTEGER, price REAL, cost REAL);
            CREATE TABLE IF NOT EXISTS topups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, pc_id TEXT, pc_name TEXT,
                session_id INTEGER, member_id INTEGER, user_label TEXT, kind TEXT, minutes INTEGER,
                amount REAL, method TEXT, status TEXT DEFAULT 'pending', handled REAL);
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, pc_id TEXT, pc_name TEXT,
                user_label TEXT, session_id INTEGER, kind TEXT, detail TEXT, action TEXT,
                image BLOB, mime TEXT, acked INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS game_launches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, pc_id TEXT,
                game_id INTEGER, game_name TEXT, session_id INTEGER);
            """
        )
        # migrations for databases created by v1
        for table, col, decl in (("pcs", "account", "TEXT DEFAULT ''"),
                                 ("pcs", "notes", "TEXT DEFAULT ''"),
                                 ("pcs", "games", "TEXT DEFAULT '[]'"),
                                 ("sessions", "group_id", "TEXT"),
                                 ("sessions", "package", "TEXT")):
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        if not self.db.execute("SELECT 1 FROM games LIMIT 1").fetchone():
            # starter entry; staff fill in the launch target from the Games tab
            self.db.execute("INSERT INTO games (name, launch, category, max_players) VALUES (?,?,?,?)",
                            ("F1 25", "", "Racing", 20))
        self.db.commit()

    # ---- config -------------------------------------------------------------
    def rate_for(self, grp):
        rates = self.cfg["rates"]
        return float(rates.get(grp, next(iter(rates.values()), 60)))

    def public_settings(self):
        c = self.cfg
        return {k: c.get(k, DEFAULT_CONFIG.get(k)) for k in (
            "cafe_name", "currency", "rates", "min_minutes", "round_to_minutes", "monitoring", "timeup", "payments")}

    def _section(self, name):
        return dict(DEFAULT_CONFIG[name], **self.cfg.get(name, {}))

    def policy(self):
        m = dict(DEFAULT_CONFIG["monitoring"], **self.cfg.get("monitoring", {}))
        return {k: m[k] for k in ("blocked_apps", "blocked_keywords", "blocked_sites", "screen_view")}

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
            if "timeup" in data:
                t, cur = data["timeup"], self._section("timeup")
                if "warn_minutes" in t:
                    raw = t["warn_minutes"]
                    raw = raw.replace(",", " ").split() if isinstance(raw, str) else raw
                    mins = sorted({int(x) for x in raw if 0 < int(x) <= 120}, reverse=True)[:6]
                    cur["warn_minutes"] = mins
                if "close_game" in t:
                    cur["close_game"] = bool(t["close_game"])
                self.cfg["timeup"] = cur
            if "payments" in data:
                pay, cur = data["payments"], self._section("payments")
                if "upi_id" in pay:
                    upi = str(pay["upi_id"]).strip()
                    if upi and not re.match(UPI_RE, upi):
                        raise ApiError(400, "UPI ID looks wrong. It should look like name@bank")
                    cur["upi_id"] = upi
                if "payee_name" in pay:
                    cur["payee_name"] = str(pay["payee_name"]).strip()[:50]
                if "topup_options" in pay:
                    raw = pay["topup_options"]
                    raw = raw.replace(",", " ").split() if isinstance(raw, str) else raw
                    opts = sorted({int(x) for x in raw if 0 < int(x) <= 600})[:6]
                    if not opts:
                        raise ApiError(400, "Give at least one top-up option in minutes")
                    cur["topup_options"] = opts
                self.cfg["payments"] = cur
            if "monitoring" in data:
                m, cur = data["monitoring"], dict(DEFAULT_CONFIG["monitoring"], **self.cfg.get("monitoring", {}))
                for key in ("blocked_apps", "blocked_keywords", "blocked_sites"):
                    if key in m:
                        items = m[key].splitlines() if isinstance(m[key], str) else m[key]
                        cur[key] = [str(x).strip()[:120] for x in items if str(x).strip()][:200]
                if "on_violation" in m:
                    if m["on_violation"] not in ("alert", "lock"):
                        raise ApiError(400, "on_violation must be alert or lock")
                    cur["on_violation"] = m["on_violation"]
                if "screen_view" in m:
                    cur["screen_view"] = bool(m["screen_view"])
                self.cfg["monitoring"] = cur
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

    def start_session(self, pc_id, mode, minutes=None, member_id=None, label=None, method="cash",
                      price=None, group_id=None, package=None, commit=True):
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
                prepaid = self.cost(rate, minutes) if price is None else round(float(price), 2)
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
                "INSERT INTO sessions (pc_id, pc_name, member_id, user_label, mode, rate, started, ends,"
                " prepaid, group_id, package) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pc_id, pc["name"], member_id, label, mode, rate, t, ends, prepaid, group_id, package))
            if prepaid:
                self._txn("session_prepaid", prepaid, session_id=cur.lastrowid, pc_id=pc_id, method=method,
                          note=f"{package + ' · ' if package else ''}{minutes} min on {pc['name']}")
            if commit:
                self.db.commit()
            return cur.lastrowid

    def start_group(self, pc_ids, package_id=None, minutes=None, label=None, method="cash", game_id=None):
        """Start prepaid sessions on several rigs at once (race night, team booking).

        With a package: rigs>1 means the package price covers exactly that many rigs and is
        split across them; rigs==1 means the package price is charged per rig.
        Without a package: per-rig hourly rate for `minutes`.
        All-or-nothing: if any rig is busy or unknown, nothing is started or charged.
        """
        pc_ids = list(dict.fromkeys(pc_ids or []))
        if not pc_ids:
            raise ApiError(400, "Select at least one PC")
        with self.lock:
            pkg = None
            if package_id:
                pkg = self.db.execute("SELECT * FROM packages WHERE id=? AND active=1",
                                      (package_id,)).fetchone()
                if not pkg:
                    raise ApiError(404, "Package not found")
                if pkg["rigs"] > 1 and len(pc_ids) != pkg["rigs"]:
                    raise ApiError(400, f"'{pkg['name']}' is for exactly {pkg['rigs']} rigs; "
                                        f"{len(pc_ids)} selected")
                minutes = pkg["minutes"]
                game_id = game_id or pkg["game_id"]
            minutes = int(minutes or 0)
            if minutes <= 0:
                raise ApiError(400, "Enter the number of minutes")
            busy = [self._pc(p)["name"] for p in pc_ids if self.active_session(p)]
            if busy:
                raise ApiError(409, "Already in use: " + ", ".join(busy))
            game = self._game(game_id) if game_id else None
            prices = [None] * len(pc_ids)
            if pkg:
                if pkg["rigs"] > 1:   # split total, put rounding remainder on the first rig
                    share = math.floor(pkg["price"] / len(pc_ids) * 100) / 100
                    prices = [share] * len(pc_ids)
                    prices[0] = round(pkg["price"] - share * (len(pc_ids) - 1), 2)
                else:
                    prices = [pkg["price"]] * len(pc_ids)
            group_id = secrets.token_hex(4) if len(pc_ids) > 1 else None
            label = (label or "").strip()[:40] or ("Group" if group_id else "Walk-in")
            try:
                for pc_id, price in zip(pc_ids, prices):
                    self.start_session(pc_id, "prepaid", minutes, label=label, method=method, price=price,
                                       group_id=group_id, package=pkg["name"] if pkg else None, commit=False)
            except Exception:
                self.db.rollback()
                raise
            self.db.commit()
            warnings = []
            if game:
                missing = [self._pc(p)["name"] for p in pc_ids if game["id"] not in self.pc_games(p)]
                if missing:
                    warnings.append(f"{game['name']} is not marked installed on: {', '.join(missing)}")
                if game["launch"]:
                    for p in pc_ids:
                        self.queue_command(p, {"type": "launch", "game_id": game["id"]})
                else:
                    warnings.append(f"{game['name']} has no launch target yet - set it in the Games tab")
            warnings += self.account_conflicts(pc_ids)
            total = sum(p if p is not None else self.cost(self.rate_for(self._pc(i)["grp"]), minutes)
                        for i, p in zip(pc_ids, prices))
            return {"group_id": group_id, "total": round(total, 2), "warnings": warnings}

    def end_group(self, group_id):
        with self.lock:
            rows = self.db.execute("SELECT pc_id FROM sessions WHERE group_id=? AND ended IS NULL",
                                   (group_id,)).fetchall()
            if not rows:
                raise ApiError(409, "No running sessions in this group")
            bills = [self.end_session(r["pc_id"]) for r in rows]
            return {"rigs": len(bills), "amount": round(sum(b["amount"] for b in bills), 2),
                    "due": round(sum(b.get("due", 0) for b in bills), 2)}

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
            food = self._settle_food_tab(s["id"])
            if food:
                bill["food"] = food
                bill["due"] = round(bill.get("due", 0) + food, 2)
            self.db.commit()
            self.queue_command(pc_id, {"type": "lock"})
            if bill.get("due"):
                self.queue_command(pc_id, {"type": "message",
                                           "text": f"Session ended. Please pay {self.cfg['currency']}"
                                                   f"{bill['due']:.0f} at the counter."})
            return bill

    def tick(self):
        """Expire sessions whose time ran out. Called every second."""
        with self.lock:
            t = now()
            self.db.execute("UPDATE topups SET status='expired', handled=? WHERE status='pending' AND ts<?",
                            (t, t - TOPUP_EXPIRES))
            self.db.commit()   # always commit: an open transaction would lock the database file
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
               "remaining": int(s["ends"] - t) if s["ends"] else None,
               "group_id": s["group_id"], "package": s["package"]}
        if s["mode"] == "prepaid":
            out["cost"] = s["prepaid"]
        elif s["mode"] == "free":
            out["cost"] = 0
        else:
            out["cost"] = self.cost(s["rate"], self.billable_minutes(elapsed))
        if s["mode"] == "member":
            m = self.db.execute("SELECT balance FROM members WHERE id=?", (s["member_id"],)).fetchone()
            out["balance"] = m["balance"] if m else 0
        tab = self.db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE session_id=? AND pay_method='tab'"
                              " AND paid=0 AND status!='cancelled'", (s["id"],)).fetchone()[0]
        out["food_tab"] = round(tab, 2)
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

    def command(self, pc_id, ctype, text=None, game_id=None):
        if ctype not in VALID_COMMANDS:
            raise ApiError(400, "Unknown command")
        with self.lock:
            self._pc(pc_id)
            if ctype == "kill":
                name = (text or "").strip()
                if not name or len(name) > 100 or not re.match(r"^[\w .\-()+]+$", name):
                    raise ApiError(400, "Invalid program name")
                if name.lower() in PROTECTED_PROCS:
                    raise ApiError(400, f"{name} is a Windows system process and can't be closed")
                self.queue_command(pc_id, {"type": "kill", "text": name})
                return None
            if ctype == "processes":
                self.queue_command(pc_id, {"type": "processes"})
                return None
            if ctype == "launch":
                game = self._game(game_id)
                if not self.active_session(pc_id):
                    raise ApiError(409, "Start a session before launching a game")
                if not game["launch"]:
                    raise ApiError(400, f"Set a launch target for {game['name']} in the Games tab")
                self.queue_command(pc_id, {"type": "launch", "game_id": game["id"]})
                return None
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
                "games": self.games_for_pc(pc_id),
                "watch": self.watch_mode(pc_id), "policy": self.policy(),
                "timeup": self._section("timeup"), "topup": self.topup_info(pc_id),
                "food": {"enabled": self.food_enabled(), "orders": self.pc_orders(pc_id)},
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
            if "account" in data:
                self.db.execute("UPDATE pcs SET account=? WHERE id=?", (str(data["account"]).strip()[:80], pc_id))
            if "notes" in data:
                self.db.execute("UPDATE pcs SET notes=? WHERE id=?", (str(data["notes"]).strip()[:300], pc_id))
            if "games" in data:
                valid = {r["id"] for r in self.db.execute("SELECT id FROM games")}
                ids = sorted({int(g) for g in data["games"] if int(g) in valid})
                self.db.execute("UPDATE pcs SET games=? WHERE id=?", (json.dumps(ids), pc_id))
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
            open_alerts = {}
            for a in self.db.execute("SELECT id, pc_id, detail, ts FROM alerts WHERE acked=0 ORDER BY id"):
                open_alerts[a["pc_id"]] = dict(a)
            for row in self.db.execute("SELECT * FROM pcs ORDER BY name COLLATE NOCASE"):
                live = self.live.get(row["id"], {})
                online = t - live.get("last_seen", 0) < OFFLINE_AFTER
                s = self.session_view(self.active_session(row["id"]))
                pcs.append({
                    "id": row["id"], "name": row["name"], "group": row["grp"],
                    "rate": self.rate_for(row["grp"]), "online": online, "ip": live.get("ip", ""),
                    "info": live.get("info", {}), "last_seen": live.get("last_seen"),
                    "status": ("in_use" if s else "idle") if online else ("in_use_offline" if s else "offline"),
                    "session": s, "account": row["account"] or "", "notes": row["notes"] or "",
                    "games": self.pc_games(row["id"]),
                    "activity": (live.get("info") or {}).get("active") or {},
                    "has_screen": bool(live.get("screen")),
                    "alert": open_alerts.get(row["id"]),
                })
            by_account = {}
            for p in pcs:
                key = p["account"].strip().lower()
                if key:
                    by_account.setdefault(key, []).append(p["name"])
            for p in pcs:
                others = [n for n in by_account.get(p["account"].strip().lower(), []) if n != p["name"]]
                p["shares_account_with"] = others
            return {"server_time": t, "settings": self.public_settings(), "pcs": pcs,
                    "games": self.list_games(), "packages": self.list_packages(),
                    "alerts_open": self.db.execute("SELECT COUNT(*) FROM alerts WHERE acked=0").fetchone()[0],
                    "topups_pending": self.list_topups("pending"),
                    "orders_active": self.list_orders(active=True),
                    "low_stock": [dict(r) for r in self.db.execute(
                        "SELECT id, name, unit, qty, low_level FROM stock_items"
                        " WHERE active=1 AND qty<=low_level ORDER BY name")],
                    "today": self.report(dt.date.today().isoformat())["totals"]}

    # ---- F&B: inventory -------------------------------------------------------------
    def _stock(self, stock_id):
        r = self.db.execute("SELECT * FROM stock_items WHERE id=?", (int(stock_id),)).fetchone()
        if not r:
            raise ApiError(404, "Stock item not found")
        return r

    def _move(self, stock_id, change, kind, ref="", note="", cost_total=0.0):
        self.db.execute("UPDATE stock_items SET qty=ROUND(qty+?, 3) WHERE id=?", (change, stock_id))
        after = self.db.execute("SELECT qty FROM stock_items WHERE id=?", (stock_id,)).fetchone()[0]
        self.db.execute("INSERT INTO stock_moves (ts, stock_id, change, kind, ref, note, cost_total, qty_after)"
                        " VALUES (?,?,?,?,?,?,?,?)", (now(), stock_id, change, kind, ref, note[:200],
                                                      round(cost_total, 2), after))

    def list_stock(self):
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM stock_items WHERE active=1 ORDER BY name")]
            for r in rows:
                r["low"] = r["qty"] <= r["low_level"]
                r["value"] = round(r["qty"] * r["cost"], 2)
            return rows

    def save_stock(self, data):
        name = str(data.get("name", "")).strip()[:60]
        if not name:
            raise ApiError(400, "Name is required")
        unit = str(data.get("unit") or "pcs").strip()[:12]
        low, cost = float(data.get("low_level") or 0), float(data.get("cost") or 0)
        if low < 0 or cost < 0:
            raise ApiError(400, "Values can't be negative")
        with self.lock:
            if data.get("id"):
                self._stock(data["id"])
                self.db.execute("UPDATE stock_items SET name=?, unit=?, low_level=?, cost=? WHERE id=?",
                                (name, unit, low, cost, int(data["id"])))
                sid = int(data["id"])
            else:
                sid = self.db.execute("INSERT INTO stock_items (name, unit, qty, low_level, cost) VALUES (?,?,0,?,?)",
                                      (name, unit, low, cost)).lastrowid
                opening = float(data.get("qty") or 0)
                if opening:
                    self._move(sid, opening, "opening", note="Opening stock", cost_total=opening * cost)
            self.db.commit()
            return sid

    def stock_in(self, stock_id, qty, cost_total=None, note=""):
        """Record a purchase. Updates the average cost per unit when a total cost is given."""
        qty = float(qty or 0)
        if qty <= 0:
            raise ApiError(400, "Quantity must be more than zero")
        with self.lock:
            st = self._stock(stock_id)
            if cost_total not in (None, ""):
                cost_total = float(cost_total)
                if cost_total < 0:
                    raise ApiError(400, "Cost can't be negative")
                held = max(st["qty"], 0)
                avg = (held * st["cost"] + cost_total) / (held + qty)
                self.db.execute("UPDATE stock_items SET cost=? WHERE id=?", (round(avg, 4), st["id"]))
            else:
                cost_total = qty * st["cost"]
            self._move(st["id"], qty, "purchase", note=note or "Stock in", cost_total=cost_total)
            self.db.commit()

    def adjust_stock(self, stock_id, counted=None, change=None, reason="adjust"):
        """Stock count correction (counted) or wastage/breakage (negative change)."""
        if reason not in ("adjust", "waste"):
            raise ApiError(400, "Reason must be adjust or waste")
        with self.lock:
            st = self._stock(stock_id)
            delta = float(counted) - st["qty"] if counted not in (None, "") else float(change or 0)
            if delta == 0:
                return
            self._move(st["id"], delta, reason, note="Stock count" if counted not in (None, "") else "Wastage",
                       cost_total=abs(delta) * st["cost"])
            self.db.commit()

    def delete_stock(self, stock_id):
        with self.lock:
            self._stock(stock_id)
            if self.db.execute("SELECT 1 FROM recipes r JOIN menu_items m ON m.id=r.menu_id"
                               " WHERE r.stock_id=? AND m.active=1", (int(stock_id),)).fetchone():
                raise ApiError(409, "A menu item still uses this. Remove it from the menu item first.")
            self.db.execute("UPDATE stock_items SET active=0 WHERE id=?", (int(stock_id),))
            self.db.commit()

    def stock_moves(self, stock_id=None, limit=200):
        with self.lock:
            q = ("SELECT m.*, s.name, s.unit FROM stock_moves m JOIN stock_items s ON s.id=m.stock_id")
            args = []
            if stock_id:
                q += " WHERE m.stock_id=?"
                args.append(int(stock_id))
            return [dict(r) for r in self.db.execute(q + " ORDER BY m.id DESC LIMIT ?", args + [int(limit)])]

    # ---- F&B: menu --------------------------------------------------------------------
    def food_enabled(self):
        return bool(self.db.execute("SELECT 1 FROM menu_items WHERE active=1 LIMIT 1").fetchone())

    def _recipe(self, menu_id):
        return [dict(r) for r in self.db.execute(
            "SELECT r.stock_id, r.qty, s.name, s.unit, s.qty AS in_stock, s.cost FROM recipes r"
            " JOIN stock_items s ON s.id=r.stock_id WHERE r.menu_id=?", (menu_id,))]

    def list_menu(self, include_inactive=False):
        with self.lock:
            q = "SELECT * FROM menu_items" + ("" if include_inactive else " WHERE active=1")
            items = [dict(r) for r in self.db.execute(q + " ORDER BY category COLLATE NOCASE, sort, name")]
            for it in items:
                rec = self._recipe(it["id"])
                it["recipe"] = [{"stock_id": r["stock_id"], "qty": r["qty"], "name": r["name"], "unit": r["unit"]}
                                for r in rec]
                # how many can still be made from current stock (None = not stock-tracked)
                it["can_make"] = (min(int(r["in_stock"] // r["qty"]) for r in rec if r["qty"] > 0)
                                  if rec else None)
                it["available"] = it["can_make"] is None or it["can_make"] > 0
                it["cost"] = round(sum(r["qty"] * r["cost"] for r in rec), 2)
            return items

    def save_menu(self, data):
        name = str(data.get("name", "")).strip()[:60]
        price = float(data.get("price") or 0)
        if not name or price < 0:
            raise ApiError(400, "Menu item needs a name and a price")
        category = str(data.get("category") or "Snacks").strip()[:30]
        recipe = data.get("recipe") or []
        with self.lock:
            if data.get("track_as_stock"):      # packaged item: create a stock item with the same name
                sid = self.save_stock({"name": name, "unit": data.get("unit") or "pcs",
                                       "qty": data.get("opening_qty") or 0,
                                       "low_level": data.get("low_level") or 0, "cost": data.get("unit_cost") or 0})
                recipe = [{"stock_id": sid, "qty": 1}]
            clean = {}
            for r in recipe:
                q = float(r.get("qty") or 0)
                if q > 0:
                    self._stock(r["stock_id"])
                    clean[int(r["stock_id"])] = q
            if data.get("id"):
                mid = int(data["id"])
                if not self.db.execute("SELECT 1 FROM menu_items WHERE id=?", (mid,)).fetchone():
                    raise ApiError(404, "Menu item not found")
                self.db.execute("UPDATE menu_items SET name=?, category=?, price=?, active=1 WHERE id=?",
                                (name, category, round(price, 2), mid))
            else:
                mid = self.db.execute("INSERT INTO menu_items (name, category, price) VALUES (?,?,?)",
                                      (name, category, round(price, 2))).lastrowid
            if "recipe" in data or data.get("track_as_stock"):
                self.db.execute("DELETE FROM recipes WHERE menu_id=?", (mid,))
                self.db.executemany("INSERT INTO recipes (menu_id, stock_id, qty) VALUES (?,?,?)",
                                    [(mid, k, v) for k, v in clean.items()])
            self.db.commit()
            return mid

    def delete_menu(self, menu_id):
        with self.lock:
            self.db.execute("UPDATE menu_items SET active=0 WHERE id=?", (int(menu_id),))
            self.db.commit()

    # ---- F&B: orders ------------------------------------------------------------------
    PAY_METHODS = ("tab", "balance", "upi", "cash")

    def place_order(self, items, pay_method, pc_id=None, source="pc", note="", customer="", paid_now=False):
        """Create an order and take the ingredients out of stock.

        pay_method: tab (added to the PC bill, paid at End & bill), balance (member balance, paid now),
                    upi / cash (paid to staff; paid_now=True when the counter takes the money immediately).
        """
        if pay_method not in self.PAY_METHODS:
            raise ApiError(400, "Unknown payment method")
        want = {}
        for it in items or []:
            q = int(it.get("qty") or 0)
            if q > 0:
                want[int(it["menu_id"])] = want.get(int(it["menu_id"]), 0) + q
        if not want or sum(want.values()) > 50:
            raise ApiError(400, "Add between 1 and 50 items")
        with self.lock:
            s, pc_name = None, "Counter"
            if pc_id:
                pc_name = self._pc(pc_id)["name"]
                s = self.active_session(pc_id)
                if source == "pc" and not s:
                    raise ApiError(409, "Ordering is available during a session")
            if pay_method in ("tab", "balance") and not s:
                raise ApiError(400, "Add-to-bill and balance payment need a running session on a PC")
            if pay_method == "balance" and s["mode"] != "member":
                raise ApiError(400, "Balance payment is for member logins")
            lines, need, total, cost = [], {}, 0.0, 0.0
            for mid, q in want.items():
                m = self.db.execute("SELECT * FROM menu_items WHERE id=? AND active=1", (mid,)).fetchone()
                if not m:
                    raise ApiError(404, "A menu item is no longer available")
                rec = self._recipe(mid)
                unit_cost = sum(r["qty"] * r["cost"] for r in rec)
                for r in rec:
                    need[r["stock_id"]] = need.get(r["stock_id"], 0) + r["qty"] * q
                lines.append((mid, m["name"], q, m["price"], round(unit_cost, 2)))
                total += m["price"] * q
                cost += unit_cost * q
            short = [self._stock(sid)["name"] for sid, q in need.items() if self._stock(sid)["qty"] + 1e-9 < q]
            if short:
                raise ApiError(409, "Sold out: " + ", ".join(short))
            total, cost = round(total, 2), round(cost, 2)
            if pay_method == "balance":
                bal = self.db.execute("SELECT balance FROM members WHERE id=?", (s["member_id"],)).fetchone()[0]
                playing = self.cost(s["rate"], self.billable_minutes(now() - s["started"]))
                if bal - playing < total:
                    raise ApiError(402, f"Not enough balance (available {self.cfg['currency']}"
                                        f"{max(bal - playing, 0):.0f}). Choose another way to pay.")
            paid = pay_method == "balance" or (paid_now and pay_method in ("cash", "upi"))
            cur = self.db.execute(
                "INSERT INTO orders (ts, pc_id, pc_name, session_id, member_id, customer, source, total, cost,"
                " pay_method, paid, paid_ts, note, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now(), pc_id, pc_name, s["id"] if s else None, s["member_id"] if s else None,
                 (customer or (s["user_label"] if s else "Walk-in"))[:40], source, total, cost, pay_method,
                 int(paid), now() if paid else None, str(note or "")[:200], now()))
            oid = cur.lastrowid
            self.db.executemany("INSERT INTO order_items (order_id, menu_id, name, qty, price, cost) VALUES (?,?,?,?,?,?)",
                                [(oid,) + ln for ln in lines])
            for sid, q in need.items():
                self._move(sid, -q, "sale", ref=f"Order #{oid}")
            if pay_method == "balance":
                self.db.execute("UPDATE members SET balance=balance-? WHERE id=?", (total, s["member_id"]))
                self._txn("food_balance", total, member_id=s["member_id"], session_id=s["id"], pc_id=pc_id,
                          method="balance", note=f"Order #{oid}")
                self._sync_member_end(s["member_id"])
            elif paid:
                self._txn("food", total, session_id=s["id"] if s else None, pc_id=pc_id, method=pay_method,
                          note=f"Order #{oid}")
            self.db.commit()
            return self.get_order(oid)

    def get_order(self, oid):
        o = self.db.execute("SELECT * FROM orders WHERE id=?", (int(oid),)).fetchone()
        if not o:
            raise ApiError(404, "Order not found")
        out = dict(o)
        out["items"] = [dict(r) for r in self.db.execute(
            "SELECT menu_id, name, qty, price FROM order_items WHERE order_id=?", (o["id"],))]
        return out

    def list_orders(self, active=False, day=None, limit=100):
        q, args = "SELECT id FROM orders", []
        if active:
            q += " WHERE status IN ('new','preparing') OR (pay_method IN ('cash','upi') AND paid=0" \
                 " AND status!='cancelled')"
        elif day:
            start = time.mktime(dt.date.fromisoformat(day).timetuple())
            q += " WHERE ts>=? AND ts<?"
            args += [start, start + 86400]
        return [self.get_order(r[0]) for r in self.db.execute(q + " ORDER BY id DESC LIMIT ?", args + [int(limit)])]

    def pc_orders(self, pc_id):
        """Orders the customer at this PC should still see (open, or finished in the last 10 minutes)."""
        rows = self.db.execute("SELECT id FROM orders WHERE pc_id=? AND source='pc' AND (status IN ('new','preparing')"
                               " OR updated>?) ORDER BY id DESC LIMIT 5", (pc_id, now() - 600)).fetchall()
        return [{k: o[k] for k in ("id", "status", "total", "pay_method", "paid")}
                for o in (self.get_order(r[0]) for r in rows)]

    def set_order_status(self, oid, status):
        flow = {"new": ("preparing", "delivered", "cancelled"), "preparing": ("delivered", "cancelled"),
                "delivered": ("cancelled",)}
        with self.lock:
            o = self.get_order(oid)
            if status not in flow.get(o["status"], ()):
                raise ApiError(409, f"Order is {o['status']}; can't change it to {status}")
            if status == "cancelled":
                self._cancel_order(o)
            self.db.execute("UPDATE orders SET status=?, updated=? WHERE id=?", (status, now(), o["id"]))
            self.db.commit()
            if o["pc_id"] and o["source"] == "pc":
                msg = {"preparing": f"Your order #{o['id']} is being prepared.",
                       "cancelled": f"Sorry, your order #{o['id']} was cancelled. Please see staff."}.get(status)
                if msg:
                    self.queue_command(o["pc_id"], {"type": "message", "text": msg})
            return self.get_order(o["id"])

    def _cancel_order(self, o):
        for r in self.db.execute("SELECT menu_id, qty FROM order_items WHERE order_id=?", (o["id"],)).fetchall():
            for rec in self.db.execute("SELECT stock_id, qty FROM recipes WHERE menu_id=?", (r["menu_id"],)).fetchall():
                self._move(rec["stock_id"], rec["qty"] * r["qty"], "cancel", ref=f"Order #{o['id']}")
        if o["paid"] and o["pay_method"] == "balance":
            self.db.execute("UPDATE members SET balance=balance+? WHERE id=?", (o["total"], o["member_id"]))
            self._txn("food_balance", -o["total"], member_id=o["member_id"], pc_id=o["pc_id"], method="balance",
                      note=f"Refund order #{o['id']}")
            self._sync_member_end(o["member_id"])
        elif o["paid"]:
            self._txn("food_refund", -o["total"], pc_id=o["pc_id"], method=o["pay_method"],
                      note=f"Refund order #{o['id']}")

    def mark_order_paid(self, oid, method="cash"):
        if method not in ("cash", "upi"):
            raise ApiError(400, "Method must be cash or upi")
        with self.lock:
            o = self.get_order(oid)
            if o["paid"] or o["status"] == "cancelled":
                raise ApiError(409, "Order is already paid or cancelled")
            self.db.execute("UPDATE orders SET paid=1, paid_ts=?, pay_method=?, updated=? WHERE id=?",
                            (now(), method, now(), o["id"]))
            self._txn("food", o["total"], session_id=o["session_id"], pc_id=o["pc_id"], method=method,
                      note=f"Order #{o['id']}")
            self.db.commit()

    def _settle_food_tab(self, session_id):
        """At End & bill: unpaid 'add to bill' orders become due at the counter."""
        rows = self.db.execute("SELECT id, total, pc_id FROM orders WHERE session_id=? AND pay_method='tab'"
                               " AND paid=0 AND status!='cancelled'", (session_id,)).fetchall()
        for r in rows:
            self.db.execute("UPDATE orders SET paid=1, paid_ts=?, updated=? WHERE id=?", (now(), now(), r["id"]))
            self._txn("food", r["total"], session_id=session_id, pc_id=r["pc_id"], method="cash",
                      note=f"Order #{r['id']} (on bill)")
        return round(sum(r["total"] for r in rows), 2)

    def pc_menu(self, pc_id):
        """Menu + payment choices for the customer at this PC."""
        with self.lock:
            s = self.active_session(pc_id)
            pay = self._section("payments")
            methods = []
            if s:
                methods.append("tab")
                if s["mode"] == "member":
                    methods.append("balance")
            if pay["upi_id"]:
                methods.append("upi")
            methods.append("cash")
            items = [{k: it[k] for k in ("id", "name", "category", "price", "available", "can_make")}
                     for it in self.list_menu()]
            return {"items": items, "methods": methods, "currency": self.cfg["currency"]}

    def upi_details(self, amount, note):
        pay = self._section("payments")
        if not pay["upi_id"]:
            return {}
        return {"upi_id": pay["upi_id"], "payee": pay["payee_name"] or self.cfg["cafe_name"],
                "amount": amount, "note": note[:60]}

    def stock_csv(self):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Item", "Unit", "In stock", "Low level", "Cost per unit", "Stock value", "Status"])
        for r in self.list_stock():
            w.writerow([r["name"], r["unit"], r["qty"], r["low_level"], r["cost"], r["value"],
                        "LOW - reorder" if r["low"] else "OK"])
        return buf.getvalue()

    # ---- top-ups from the PC (UPI QR or pay at counter, confirmed by staff) -------
    def _topup_kind(self, pc_id):
        """What a top-up on this PC would do: 'time' (prepaid), 'balance' (member),
        'extend' (walk-in whose time just ran out) or None."""
        s = self.active_session(pc_id)
        if s:
            return ({"prepaid": "time", "member": "balance"}.get(s["mode"]), s)
        last = self.db.execute(
            "SELECT * FROM sessions WHERE pc_id=? AND ended IS NOT NULL ORDER BY id DESC LIMIT 1",
            (pc_id,)).fetchone()
        if last and last["mode"] == "prepaid" and last["end_reason"] == "time_up" \
                and now() - last["ended"] < EXTEND_WINDOW:
            return "extend", last
        return None, None

    def topup_info(self, pc_id):
        kind, _ = self._topup_kind(pc_id)
        pay = self._section("payments")
        last = self.db.execute("SELECT id, status, minutes, amount, method FROM topups WHERE pc_id=? AND ts>?"
                               " ORDER BY id DESC LIMIT 1", (pc_id, now() - TOPUP_EXPIRES - 300)).fetchone()
        return {"kind": kind, "upi": bool(pay["upi_id"]), "options": pay["topup_options"],
                "last": dict(last) if last else None}

    def request_topup(self, pc_id, minutes, method="upi"):
        if method not in ("upi", "counter"):
            raise ApiError(400, "Payment method must be upi or counter")
        minutes = int(minutes or 0)
        if not 0 < minutes <= 600:
            raise ApiError(400, "Choose how many minutes to add")
        with self.lock:
            pc = self._pc(pc_id)
            kind, s = self._topup_kind(pc_id)
            if not kind:
                raise ApiError(409, "Top-up isn't available on this PC right now. Please see staff.")
            pay = self._section("payments")
            if method == "upi" and not pay["upi_id"]:
                raise ApiError(400, "UPI payment isn't set up. Please pay at the counter.")
            rate = s["rate"] if kind != "extend" else self.rate_for(pc["grp"])
            amount = self.cost(rate, minutes)
            self.db.execute("UPDATE topups SET status='cancelled', handled=? WHERE pc_id=? AND status='pending'",
                            (now(), pc_id))
            cur = self.db.execute(
                "INSERT INTO topups (ts, pc_id, pc_name, session_id, member_id, user_label, kind, minutes, amount,"
                " method) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now(), pc_id, pc["name"], s["id"], s["member_id"], s["user_label"], kind, minutes, amount, method))
            self.db.commit()
            tid = cur.lastrowid
            out = {"id": tid, "ref": f"T{tid}", "amount": amount, "minutes": minutes, "kind": kind,
                   "method": method, "currency": self.cfg["currency"]}
            if method == "upi":
                out.update(upi_id=pay["upi_id"], payee=pay["payee_name"] or self.cfg["cafe_name"],
                           note=f"{pc['name']} +{minutes} min T{tid}")
            return out

    def cancel_topup(self, pc_id):
        with self.lock:
            self.db.execute("UPDATE topups SET status='cancelled', handled=? WHERE pc_id=? AND status='pending'",
                            (now(), pc_id))
            self.db.commit()

    def _pending(self, topup_id):
        t = self.db.execute("SELECT * FROM topups WHERE id=?", (int(topup_id),)).fetchone()
        if not t:
            raise ApiError(404, "Top-up request not found")
        if t["status"] != "pending":
            raise ApiError(409, f"This request is already {t['status']}")
        return t

    def approve_topup(self, topup_id):
        with self.lock:
            t = self._pending(topup_id)
            method = "upi" if t["method"] == "upi" else "cash"
            if t["kind"] == "balance":
                self.topup(t["member_id"], t["amount"], method)
                msg = f"Payment received. {self.cfg['currency']}{t['amount']:.0f} added to your balance."
            else:
                s = self.active_session(t["pc_id"])
                if s and (s["id"] == t["session_id"] or s["mode"] == "prepaid"):
                    self.add_time(t["pc_id"], t["minutes"], method)
                elif s:
                    raise ApiError(409, "Another session is running on this PC now")
                else:   # time ran out while waiting, or extending from the lock screen
                    self.start_session(t["pc_id"], "prepaid", t["minutes"], label=t["user_label"],
                                       method=method, price=t["amount"])
                msg = f"Payment received. +{t['minutes']} minutes added. Enjoy!"
            self.db.execute("UPDATE topups SET status='approved', handled=? WHERE id=?", (now(), t["id"]))
            self.db.commit()
            self.queue_command(t["pc_id"], {"type": "message", "text": msg})
            return dict(t, status="approved")

    def reject_topup(self, topup_id):
        with self.lock:
            t = self._pending(topup_id)
            self.db.execute("UPDATE topups SET status='rejected', handled=? WHERE id=?", (now(), t["id"]))
            self.db.commit()
            self.queue_command(t["pc_id"], {"type": "message",
                                            "text": "Your payment could not be confirmed. Please see staff at the counter."})

    def list_topups(self, status=None, limit=50):
        q, args = "SELECT * FROM topups", []
        if status:
            q += " WHERE status=?"
            args.append(status)
        return [dict(r) for r in self.db.execute(q + " ORDER BY id DESC LIMIT ?", args + [int(limit)])]

    # ---- monitoring: live screens, processes, alerts ---------------------------
    def _live(self, pc_id):
        return self.live.setdefault(pc_id, {"last_seen": 0, "ip": "", "commands": [], "info": {}})

    def watch_mode(self, pc_id):
        """'full' while someone has this PC open in the viewer, 'thumb' while the Monitor grid shows it."""
        if not self.policy()["screen_view"]:
            return None
        w = self._live(pc_id).get("watch", {})
        t = now()
        if w.get("full", 0) > t:
            return "full"
        if w.get("thumb", 0) > t:
            return "thumb"
        return None

    def put_screen(self, pc_id, data, mime):
        if mime not in ("image/png", "image/jpeg"):
            raise ApiError(400, "Unsupported image type")
        with self.lock:
            self._live(pc_id)["screen"] = {"data": data, "mime": mime, "ts": now()}

    def get_screen(self, pc_id, mode="thumb"):
        """Mark the PC as watched and return its latest frame (or None if none yet)."""
        with self.lock:
            self._pc(pc_id)
            if not self.policy()["screen_view"]:
                raise ApiError(403, "Screen view is turned off in Settings")
            live = self._live(pc_id)
            live.setdefault("watch", {})["full" if mode == "full" else "thumb"] = now() + WATCH_SECONDS
            return live.get("screen")

    def put_processes(self, pc_id, procs):
        with self.lock:
            clean = [{"name": str(p.get("name", ""))[:100], "pid": int(p.get("pid", 0)),
                      "mem_mb": float(p.get("mem_mb", 0))} for p in (procs or [])[:500]]
            self._live(pc_id)["processes"] = {"list": clean, "ts": now()}

    def get_processes(self, pc_id):
        with self.lock:
            self._pc(pc_id)
            return self._live(pc_id).get("processes") or {"list": [], "ts": 0}

    def add_alert(self, pc_id, kind, detail, image=None, mime=None):
        """Record a rule violation reported by a PC. Locks the PC if the policy says so."""
        with self.lock:
            pc = self._pc(pc_id)
            s = self.active_session(pc_id)
            action = "alert"
            if self.cfg.get("monitoring", {}).get("on_violation") == "lock" and s:
                action = "locked"
            cur = self.db.execute(
                "INSERT INTO alerts (ts, pc_id, pc_name, user_label, session_id, kind, detail, action, image, mime)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now(), pc_id, pc["name"], s["user_label"] if s else "", s["id"] if s else None,
                 str(kind)[:30], str(detail)[:300], action, image, mime))
            # keep the database small: drop screenshots older than ALERT_IMAGE_DAYS
            self.db.execute("UPDATE alerts SET image=NULL WHERE ts<? AND image IS NOT NULL",
                            (now() - ALERT_IMAGE_DAYS * 86400,))
            self.db.commit()
            if action == "locked":
                self.end_session(pc_id, reason="policy_violation")
                self.queue_command(pc_id, {"type": "message",
                                           "text": "This PC was locked for breaking cafe rules. Please see staff."})
            return cur.lastrowid

    def list_alerts(self, only_open=False, limit=100):
        with self.lock:
            q = ("SELECT id, ts, pc_id, pc_name, user_label, kind, detail, action, acked,"
                 " image IS NOT NULL AS has_image FROM alerts")
            q += " WHERE acked=0" if only_open else ""
            return [dict(r) for r in self.db.execute(q + " ORDER BY id DESC LIMIT ?", (int(limit),))]

    def alert_image(self, alert_id):
        with self.lock:
            r = self.db.execute("SELECT image, mime FROM alerts WHERE id=?", (int(alert_id),)).fetchone()
        if not r or r["image"] is None:
            raise ApiError(404, "No screenshot for this alert")
        return r["image"], r["mime"]

    def ack_alerts(self, ids=None):
        with self.lock:
            if ids:
                self.db.executemany("UPDATE alerts SET acked=1 WHERE id=?", [(int(i),) for i in ids])
            else:
                self.db.execute("UPDATE alerts SET acked=1 WHERE acked=0")
            self.db.commit()

    # ---- games catalogue & rig register ---------------------------------------
    def _game(self, game_id):
        try:
            row = self.db.execute("SELECT * FROM games WHERE id=?", (int(game_id),)).fetchone()
        except (TypeError, ValueError):
            row = None
        if not row:
            raise ApiError(404, "Game not found")
        return row

    def pc_games(self, pc_id):
        row = self.db.execute("SELECT games FROM pcs WHERE id=?", (pc_id,)).fetchone()
        try:
            return json.loads(row["games"] or "[]") if row else []
        except json.JSONDecodeError:
            return []

    def games_for_pc(self, pc_id):
        """Games the agent should show in its launcher: installed on this rig and launchable."""
        installed = set(self.pc_games(pc_id))
        return [g for g in self.list_games() if g["id"] in installed and g["launch"]]

    def account_conflicts(self, pc_ids):
        """Warn when two rigs in the same booking use the same Steam/EA account."""
        seen, out = {}, []
        for p in pc_ids:
            row = self._pc(p)
            acc = (row["account"] or "").strip().lower()
            if acc:
                if acc in seen:
                    out.append(f"{seen[acc]} and {row['name']} use the same game account "
                               f"({row['account']}) - only one can play online at a time")
                else:
                    seen[acc] = row["name"]
        return out

    def list_games(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM games ORDER BY sort, name COLLATE NOCASE")]

    def save_game(self, data):
        name = str(data.get("name", "")).strip()[:60]
        if not name:
            raise ApiError(400, "Game name is required")
        vals = (name, str(data.get("launch", "")).strip()[:500], str(data.get("category", "")).strip()[:30],
                int(data.get("max_players") or 0), int(data.get("sort") or 0))
        with self.lock:
            if data.get("id"):
                self._game(data["id"])
                self.db.execute("UPDATE games SET name=?, launch=?, category=?, max_players=?, sort=? WHERE id=?",
                                vals + (int(data["id"]),))
                gid = int(data["id"])
            else:
                gid = self.db.execute("INSERT INTO games (name, launch, category, max_players, sort)"
                                      " VALUES (?,?,?,?,?)", vals).lastrowid
            self.db.commit()
            return gid

    def delete_game(self, game_id):
        with self.lock:
            self._game(game_id)
            gid = int(game_id)
            self.db.execute("DELETE FROM games WHERE id=?", (gid,))
            self.db.execute("UPDATE packages SET game_id=NULL WHERE game_id=?", (gid,))
            for r in self.db.execute("SELECT id, games FROM pcs").fetchall():
                ids = [g for g in json.loads(r["games"] or "[]") if g != gid]
                self.db.execute("UPDATE pcs SET games=? WHERE id=?", (json.dumps(ids), r["id"]))
            self.db.commit()

    def log_launch(self, pc_id, game_id):
        with self.lock:
            game = self._game(game_id)
            s = self.active_session(pc_id)
            if not s:
                raise ApiError(409, "No running session")
            self.db.execute("INSERT INTO game_launches (ts, pc_id, game_id, game_name, session_id)"
                            " VALUES (?,?,?,?,?)", (now(), pc_id, game["id"], game["name"], s["id"]))
            self.db.commit()

    # ---- packages ---------------------------------------------------------------
    def list_packages(self):
        return [dict(r) for r in self.db.execute(
            "SELECT p.*, g.name AS game_name FROM packages p LEFT JOIN games g ON g.id=p.game_id"
            " WHERE p.active=1 ORDER BY p.rigs, p.minutes")]

    def save_package(self, data):
        name = str(data.get("name", "")).strip()[:60]
        minutes, price, rigs = int(data.get("minutes") or 0), float(data.get("price") or 0), int(data.get("rigs") or 1)
        if not name or minutes <= 0 or price < 0 or rigs < 1:
            raise ApiError(400, "Package needs a name, minutes > 0, price >= 0 and rigs >= 1")
        game_id = int(data["game_id"]) if data.get("game_id") else None
        with self.lock:
            if game_id:
                self._game(game_id)
            vals = (name, minutes, round(price, 2), rigs, game_id)
            if data.get("id"):
                self.db.execute("UPDATE packages SET name=?, minutes=?, price=?, rigs=?, game_id=? WHERE id=?",
                                vals + (int(data["id"]),))
                pid = int(data["id"])
            else:
                pid = self.db.execute("INSERT INTO packages (name, minutes, price, rigs, game_id)"
                                      " VALUES (?,?,?,?,?)", vals).lastrowid
            self.db.commit()
            return pid

    def delete_package(self, package_id):
        with self.lock:  # soft delete keeps history readable
            self.db.execute("UPDATE packages SET active=0 WHERE id=?", (int(package_id),))
            self.db.commit()

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
            self._sync_member_end(member_id)   # extend an active member session to match the new balance
            self.db.commit()

    def _sync_member_end(self, member_id):
        """A member's session runs until their balance is used up; recompute after the balance changes."""
        s = self.db.execute("SELECT * FROM sessions WHERE member_id=? AND ended IS NULL", (member_id,)).fetchone()
        m = self.db.execute("SELECT balance FROM members WHERE id=?", (member_id,)).fetchone()
        if s and m and s["rate"] > 0:
            self.db.execute("UPDATE sessions SET ends=? WHERE id=?",
                            (s["started"] + max(m["balance"], 0) / s["rate"] * 3600, s["id"]))

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
            orders = [dict(r) for r in self.db.execute(
                "SELECT * FROM orders WHERE ts>=? AND ts<? AND status!='cancelled'", (start, end))]
            top_items = [dict(r) for r in self.db.execute(
                "SELECT oi.name, SUM(oi.qty) AS qty, ROUND(SUM(oi.qty*oi.price),2) AS sales,"
                " ROUND(SUM(oi.qty*(oi.price-oi.cost)),2) AS margin FROM order_items oi JOIN orders o"
                " ON o.id=oi.order_id WHERE o.ts>=? AND o.ts<? AND o.status!='cancelled'"
                " GROUP BY oi.name ORDER BY qty DESC", (start, end))]
            top_games = [dict(r) for r in self.db.execute(
                "SELECT game_name AS name, COUNT(*) AS launches, COUNT(DISTINCT pc_id) AS rigs"
                " FROM game_launches WHERE ts>=? AND ts<? GROUP BY game_name ORDER BY launches DESC",
                (start, end))]
        cash_in = sum(t["amount"] for t in txns
                      if t["kind"] in ("session_prepaid", "session_postpaid", "topup", "food", "food_refund"))
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
            "package_sales": round(sum(s["prepaid"] or 0 for s in sessions if s["package"]), 2),
            "group_bookings": len({s["group_id"] for s in sessions if s["group_id"]}),
            "food_sales": round(sum(o["total"] for o in orders), 2),
            "food_cost": round(sum(o["cost"] for o in orders), 2),
            "food_margin": round(sum(o["total"] - o["cost"] for o in orders), 2),
            "food_orders": len(orders),
            "food_unpaid": round(sum(o["total"] for o in orders if not o["paid"]), 2),
        }
        return {"totals": totals, "transactions": txns, "sessions": sessions, "top_games": top_games,
                "top_items": top_items}

    def report_csv(self, day):
        rep = self.report(day)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Session ID", "PC", "User", "Type", "Package", "Group", "Rate/hr", "Start", "End",
                    "Minutes", "Amount", "End reason"])
        fmt = lambda ts: dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
        for s in rep["sessions"]:
            w.writerow([s["id"], s["pc_name"], s["user_label"], s["mode"], s["package"] or "",
                        s["group_id"] or "", s["rate"], fmt(s["started"]),
                        fmt(s["ended"]), s["minutes"] if s["minutes"] is not None else "running",
                        s["amount"] if s["ended"] else s["prepaid"], s["end_reason"] or ""])
        w.writerow([])
        w.writerow(["Transactions"])
        w.writerow(["Time", "Type", "PC", "Amount", "Method", "Note"])
        for t in rep["transactions"]:
            w.writerow([fmt(t["ts"]), t["kind"], t["pc_id"] or "", t["amount"], t["method"] or "", t["note"] or ""])
        w.writerow([])
        w.writerow(["Food & drinks sold"])
        w.writerow(["Item", "Qty", "Sales", "Margin"])
        for it in rep["top_items"]:
            w.writerow([it["name"], it["qty"], it["sales"], it["margin"]])
        w.writerow([])
        w.writerow(["Games played"])
        w.writerow(["Game", "Launches", "Rigs"])
        for g in rep["top_games"]:
            w.writerow([g["name"], g["launches"], g["rigs"]])
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

        def _body(self, limit=1_000_000):
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
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
            ok = False
            try:
                self._route(method)
                ok = True
            except ApiError as e:
                self._json({"error": e.message}, e.status)
            except (ValueError, TypeError, KeyError) as e:
                self._json({"error": f"Bad request: {e}"}, 400)
            except Exception as e:  # pragma: no cover
                self.log_error("Unhandled: %r", e)
                self._json({"error": "Server error"}, 500)
            finally:
                # never leave a half-finished write open: it would lock the database file
                with cafe.lock:
                    if cafe.db.in_transaction:
                        cafe.db.commit() if ok else cafe.db.rollback()

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

            # agent screen upload: raw image body, credentials in headers
            if route == ["agent", "screen"] and method == "POST":
                pc_id = self._agent({"key": self.headers.get("X-Agent-Key", ""),
                                     "pc_id": self.headers.get("X-PC-Id", "")})
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= 8_000_000:
                    raise ApiError(413, "Image too large")
                cafe.put_screen(pc_id, self.rfile.read(n), self.headers.get("Content-Type", ""))
                return self._json({"ok": True, "watch": cafe.watch_mode(pc_id)})

            # agent endpoints (gaming PCs)
            if route[:1] == ["agent"] and method == "POST":
                body = self._body(limit=8_000_000 if route[1:] == ["alert"] else 1_000_000)
                pc_id = self._agent(body)
                action = route[1] if len(route) > 1 else ""
                if action == "heartbeat":
                    return self._json(cafe.heartbeat(pc_id, body.get("name"), self.client_address[0],
                                                     body.get("info")))
                if action == "login":
                    cafe.member_login(pc_id, str(body.get("username", "")), str(body.get("password", "")))
                    return self._json({"ok": True})
                if action == "launch":
                    cafe.log_launch(pc_id, body.get("game_id"))
                    return self._json({"ok": True})
                if action == "processes":
                    cafe.put_processes(pc_id, body.get("list"))
                    return self._json({"ok": True})
                if action == "menu":
                    return self._json(cafe.pc_menu(pc_id))
                if action == "order":
                    o = cafe.place_order(body.get("items"), body.get("pay_method", "cash"), pc_id=pc_id,
                                         source="pc", note=body.get("note", ""))
                    if o["pay_method"] == "upi":
                        o["upi"] = cafe.upi_details(o["total"], f"{o['pc_name']} order #{o['id']}")
                    return self._json(o)
                if action == "topup":
                    if body.get("cancel"):
                        cafe.cancel_topup(pc_id)
                        return self._json({"ok": True})
                    return self._json(cafe.request_topup(pc_id, body.get("minutes"), body.get("method", "upi")))
                if action == "alert":
                    img = base64.b64decode(body["image"]) if body.get("image") else None
                    mime = body.get("mime") if img else None
                    aid = cafe.add_alert(pc_id, body.get("kind", "rule"), body.get("detail", ""), img, mime)
                    return self._json({"ok": True, "id": aid})
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

            if route[:1] == ["pc"] and len(route) == 3 and method == "GET":
                pc_id, action = route[1], route[2]
                if action == "screen":
                    frame = cafe.get_screen(pc_id, qs.get("mode", ["thumb"])[0])
                    if not frame:
                        return self._send(204, b"", "text/plain")
                    return self._send(200, frame["data"], frame["mime"],
                                      {"X-Frame-Time": f'{frame["ts"]:.3f}'})
                if action == "processes":
                    if qs.get("refresh"):
                        cafe.command(pc_id, "processes")
                    return self._json(cafe.get_processes(pc_id))

            # ---- F&B ----
            if route == ["menu"]:
                if method == "GET":
                    return self._json(cafe.list_menu(include_inactive=bool(qs.get("all"))))
                if method == "POST":
                    return self._json({"ok": True, "id": cafe.save_menu(body)})
            if route[:1] == ["menu"] and len(route) == 2 and method == "DELETE":
                cafe.delete_menu(route[1])
                return self._json({"ok": True})
            if route == ["stock"]:
                if method == "GET":
                    return self._json(cafe.list_stock())
                if method == "POST":
                    return self._json({"ok": True, "id": cafe.save_stock(body)})
            if route == ["stock", "moves"] and method == "GET":
                return self._json(cafe.stock_moves(qs.get("stock_id", [None])[0]))
            if route == ["stock.csv"] and method == "GET":
                return self._send(200, "\ufeff" + cafe.stock_csv(), "text/csv; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="cafe-inventory.csv"'})
            if route[:1] == ["stock"] and len(route) == 2 and method == "DELETE":
                cafe.delete_stock(route[1])
                return self._json({"ok": True})
            if route[:1] == ["stock"] and len(route) == 3 and method == "POST":
                if route[2] == "in":
                    cafe.stock_in(route[1], body.get("qty"), body.get("cost_total"), body.get("note", ""))
                    return self._json({"ok": True})
                if route[2] == "adjust":
                    cafe.adjust_stock(route[1], body.get("counted"), body.get("change"), body.get("reason", "adjust"))
                    return self._json({"ok": True})
            if route == ["orders"]:
                if method == "GET":
                    with cafe.lock:
                        return self._json(cafe.list_orders(active=bool(qs.get("active")),
                                                           day=qs.get("date", [None])[0]))
                if method == "POST":   # counter POS
                    return self._json(cafe.place_order(
                        body.get("items"), body.get("pay_method", "cash"), pc_id=body.get("pc_id") or None,
                        source="counter", customer=str(body.get("customer") or ""), note=body.get("note", ""),
                        paid_now=bool(body.get("paid_now", True))))
            if route[:1] == ["orders"] and len(route) == 3 and method == "POST":
                if route[2] == "status":
                    return self._json(cafe.set_order_status(route[1], body.get("status")))
                if route[2] == "paid":
                    cafe.mark_order_paid(route[1], body.get("method", "cash"))
                    return self._json({"ok": True})

            if route == ["topups"] and method == "GET":
                with cafe.lock:
                    return self._json(cafe.list_topups(qs.get("status", [None])[0]))
            if route[:1] == ["topups"] and len(route) == 3 and method == "POST":
                if route[2] == "approve":
                    return self._json({"ok": True, "topup": cafe.approve_topup(route[1])})
                if route[2] == "reject":
                    cafe.reject_topup(route[1])
                    return self._json({"ok": True})

            if route == ["alerts"] and method == "GET":
                return self._json(cafe.list_alerts(only_open=bool(qs.get("open"))))
            if route == ["alerts", "ack"] and method == "POST":
                cafe.ack_alerts(body.get("ids"))
                return self._json({"ok": True})
            if route[:1] == ["alerts"] and len(route) == 3 and route[2] == "image" and method == "GET":
                data, mime = cafe.alert_image(route[1])
                return self._send(200, data, mime)

            if route[:1] == ["pc"] and len(route) >= 2:
                pc_id, action = route[1], (route[2] if len(route) > 2 else "")
                if method == "DELETE" and not action:
                    cafe.delete_pc(pc_id)
                    return self._json({"ok": True})
                if method == "POST":
                    if action == "start":
                        mode = body.get("mode", "prepaid")
                        if mode == "package":
                            r = cafe.start_group([pc_id], body.get("package_id"), label=body.get("label"),
                                                 method=body.get("method", "cash"), game_id=body.get("game_id"))
                            return self._json(dict(ok=True, **r))
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
                        warnings = []
                        if body.get("game_id"):
                            try:
                                cafe.command(pc_id, "launch", game_id=body["game_id"])
                            except ApiError as e:
                                warnings.append(e.message)
                        return self._json({"ok": True, "session_id": sid, "warnings": warnings})
                    if action == "addtime":
                        cafe.add_time(pc_id, body.get("minutes", 0), body.get("method", "cash"))
                        return self._json({"ok": True})
                    if action == "end":
                        return self._json({"ok": True, "bill": cafe.end_session(pc_id)})
                    if action == "command":
                        bill = cafe.command(pc_id, body.get("type"), body.get("text"), body.get("game_id"))
                        return self._json({"ok": True, "bill": bill})
                    if action == "update":
                        cafe.update_pc(pc_id, body)
                        return self._json({"ok": True})

            if route == ["bulk"] and method == "POST":
                ids = body.get("ids") or [p["id"] for p in cafe.state()["pcs"] if p["online"]]
                results = {}
                for pc_id in ids:
                    try:
                        cafe.command(pc_id, body.get("type"), body.get("text"), body.get("game_id"))
                        results[pc_id] = "ok"
                    except ApiError as e:
                        results[pc_id] = e.message
                return self._json({"ok": True, "results": results})

            if route == ["group", "start"] and method == "POST":
                return self._json(dict(ok=True, **cafe.start_group(
                    body.get("ids"), body.get("package_id"), body.get("minutes"), body.get("label"),
                    body.get("method", "cash"), body.get("game_id"))))
            if route[:1] == ["group"] and len(route) == 3 and route[2] == "end" and method == "POST":
                return self._json(dict(ok=True, **cafe.end_group(route[1])))

            for coll, save, delete in (("games", cafe.save_game, cafe.delete_game),
                                       ("packages", cafe.save_package, cafe.delete_package)):
                if route == [coll]:
                    if method == "GET":
                        return self._json(cafe.list_games() if coll == "games" else cafe.list_packages())
                    if method == "POST":
                        return self._json({"ok": True, "id": save(body)})
                if route[:1] == [coll] and len(route) == 2 and method == "DELETE":
                    delete(route[1])
                    return self._json({"ok": True})

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

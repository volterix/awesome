#!/usr/bin/env python3
"""
CafeMaster agent - runs on every gaming PC.

* Shows a full-screen lock screen until the master server starts a session
  (or a member logs in with their username/password).
* While a session runs, shows a small always-on-top timer.
* Locks again automatically when time is up, even if the network drops.
* Executes remote commands from the master: message, lock, log off, restart, shut down.

Standard library only (needs tkinter, which ships with the python.org Windows installer).
Config: agent_config.json next to this file (created on first run) or command-line flags.
"""
import argparse
import json
import os
import platform
import queue
import shlex
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import ttk

import base64

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor as mon  # noqa: E402  (sits next to this file)
import qr  # noqa: E402

HERE = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
CONFIG_PATH = os.path.join(HERE, "agent_config.json")
HEARTBEAT_EVERY = 2.0
OFFLINE_GRACE = 15          # seconds without server before we show "no connection"
DEFAULT_WARN_MINUTES = (10, 5, 1)   # overridden by Settings › Time-up on the server

BG, PANEL, TEXT, MUTED, ACCENT, RED = "#0d1117", "#161b22", "#e6edf3", "#8b98a8", "#4f8cff", "#e5534b"


def load_config(args):
    cfg = {"server": "http://127.0.0.1:8080", "agent_key": "", "pc_id": "",
           "name": "", "dry_run": False, "exit_code": "0000", "allow_desktop": False}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    for k in ("server", "agent_key", "pc_id", "name", "exit_code"):
        v = getattr(args, k, None)
        if v:
            cfg[k] = v
    if args.dry_run:
        cfg["dry_run"] = True
    host = socket.gethostname()
    cfg["pc_id"] = "".join(c for c in (cfg["pc_id"] or host) if c.isalnum() or c in "-_.")[:64] or "PC"
    cfg["name"] = cfg["name"] or host
    cfg["server"] = cfg["server"].rstrip("/")
    if not os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except OSError:
            pass
    return cfg


# --------------------------------------------------------------------------- #
# Networking (background thread)
# --------------------------------------------------------------------------- #
class Link:
    def __init__(self, cfg, inbox):
        self.cfg, self.inbox = cfg, inbox
        self.info = {"os": f"{platform.system()} {platform.release()}",
                     "user": os.environ.get("USERNAME") or os.environ.get("USER", ""),
                     "agent": "1.0"}

    def post(self, path, payload, timeout=5):
        body = dict(payload, key=self.cfg["agent_key"], pc_id=self.cfg["pc_id"])
        req = urllib.request.Request(self.cfg["server"] + "/api/agent/" + path,
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", str(e))
            except Exception:
                msg = str(e)
            raise RuntimeError(msg)

    def loop(self):
        while True:
            t0 = time.time()
            try:
                data = self.post("heartbeat", {"name": self.cfg["name"], "info": self.info})
                data["_rtt"] = time.time() - t0
                self.inbox.put(("state", data))
            except Exception as e:
                self.inbox.put(("offline", str(e)))
            time.sleep(max(0.2, HEARTBEAT_EVERY - (time.time() - t0)))

    def post_raw(self, path, data, content_type, timeout=10):
        req = urllib.request.Request(self.cfg["server"] + "/api/agent/" + path, data=data, headers={
            "Content-Type": content_type, "X-Agent-Key": self.cfg["agent_key"], "X-PC-Id": self.cfg["pc_id"]})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def call_async(self, path, payload, tag):
        def run():
            try:
                self.inbox.put((tag, self.post(path, payload)))
            except Exception as e:
                self.inbox.put((tag + "_error", str(e)))
        threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------- #
# Monitoring: screen streaming, rule enforcement, site blocking (background threads)
# --------------------------------------------------------------------------- #
class Watcher:
    RULE_EVERY = 3        # seconds between rule checks
    PROC_EVERY = 6        # seconds between process scans
    REALERT_AFTER = 120   # don't repeat the same alert within this many seconds

    def __init__(self, cfg, link, inbox):
        self.cfg, self.link, self.inbox = cfg, link, inbox
        self.watch, self.policy, self.active = None, {}, False
        self._sites_applied = None
        self._recent = {}
        self._procs, self._procs_ts = [], 0

    def start(self):
        threading.Thread(target=self._screen_loop, daemon=True).start()
        threading.Thread(target=self._rules_loop, daemon=True).start()

    def update(self, watch, policy, active):
        self.watch, self.policy, self.active = watch, policy or {}, active

    # -- live screen ----------------------------------------------------------
    def _screen_loop(self):
        while True:
            mode = self.watch if self.policy.get("screen_view", True) else None
            if not mode:
                time.sleep(0.5)
                continue
            try:
                data, mime = mon.capture_screen(1280 if mode == "full" else 480)
                r = self.link.post_raw("screen", data, mime)
                self.watch = r.get("watch")
                time.sleep(1.0 if mode == "full" else 4.0)
            except Exception as e:
                print("screen capture/upload failed:", e, flush=True)
                time.sleep(3)

    # -- rules ----------------------------------------------------------------
    def _rules_loop(self):
        while True:
            try:
                self._check_rules()
            except Exception as e:
                print("rule check failed:", e, flush=True)
            time.sleep(self.RULE_EVERY)

    def _check_rules(self):
        fg = mon.foreground()
        self.link.info["active"] = fg
        sites = tuple(self.policy.get("blocked_sites") or ())
        if self.policy and sites != self._sites_applied:
            err = mon.apply_site_blocks(sites, self.cfg.get("dry_run"))
            self._sites_applied = sites
            if err:
                print(err, flush=True)
        if not self.active or not self.policy:
            return
        if time.time() - self._procs_ts > self.PROC_EVERY:
            self._procs, self._procs_ts = mon.processes(), time.time()
        for v in mon.check_policy(self.policy, fg, self._procs):
            if v["kill"]:
                mon.kill_process(v["kill"], self.cfg.get("dry_run"))
                self._procs_ts = 0
                self.inbox.put(("notice", f"{v['kill']} is not allowed in this cafe and was closed."))
            key = v["detail"]
            if time.time() - self._recent.get(key, 0) < self.REALERT_AFTER:
                continue
            self._recent[key] = time.time()
            self._send_alert(v["kind"], v["detail"])

    def _send_alert(self, kind, detail):
        payload = {"kind": kind, "detail": detail}
        try:
            data, mime = mon.capture_screen(960)
            payload.update(image=base64.b64encode(data).decode(), mime=mime)
        except Exception:
            pass
        try:
            self.link.post("alert", payload, timeout=15)
        except Exception as e:
            print("alert upload failed:", e, flush=True)

    # -- staff commands ---------------------------------------------------------
    def send_processes(self):
        def run():
            try:
                self.link.post("processes", {"list": mon.processes()}, timeout=15)
            except Exception as e:
                print("process list upload failed:", e, flush=True)
        threading.Thread(target=run, daemon=True).start()

    def kill(self, name):
        def run():
            err = mon.kill_process(name, self.cfg.get("dry_run"))
            if err:
                print("close app failed:", err, flush=True)
            time.sleep(1)
            self.send_processes()
        threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------- #
# OS actions
# --------------------------------------------------------------------------- #
def os_action(kind, dry_run):
    win = platform.system() == "Windows"
    cmds = {
        "shutdown": ["shutdown", "/s", "/t", "5"] if win else ["systemctl", "poweroff"],
        "restart": ["shutdown", "/r", "/t", "5"] if win else ["systemctl", "reboot"],
        "logoff": ["shutdown", "/l"] if win else ["loginctl", "terminate-user", os.environ.get("USER", "")],
    }
    cmd = cmds.get(kind)
    if not cmd:
        return
    if dry_run:
        print("[dry-run] would run:", " ".join(cmd), flush=True)
        return
    try:
        subprocess.Popen(cmd)
    except OSError as e:
        print("Command failed:", e, flush=True)


def launch_target(target, dry_run):
    """Start a game: steam://rungameid/..., an .exe/.lnk/.url path, or a full command line."""
    target = (target or "").strip()
    if not target:
        return "No launch target set"
    if dry_run:
        print("[dry-run] would launch:", target, flush=True)
        return None
    try:
        if platform.system() == "Windows":
            if os.path.exists(target) or "://" in target:
                os.startfile(target)          # handles .exe, .lnk, .url and steam:// links
            else:
                subprocess.Popen(target, shell=False)
        elif "://" in target or os.path.exists(target) and not os.access(target, os.X_OK):
            opener = "open" if platform.system() == "Darwin" else "xdg-open"
            subprocess.Popen([opener, target])
        else:
            subprocess.Popen(shlex.split(target))
    except OSError as e:
        return str(e)
    return None


def fmt_dur(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
class AgentApp:
    def __init__(self, cfg):
        self.cfg = cfg
        self.inbox = queue.Queue()
        self.link = Link(cfg, self.inbox)
        self.watcher = Watcher(cfg, self.link, self.inbox)
        self.session = None
        self.state = "locked"
        self.offset = 0.0              # server_time - local_time
        self.last_ok = 0.0
        self.warned = set()
        self.cafe_name, self.currency = "CafeMaster", "₹"
        self.games, self._games_sig = [], None
        self.timeup = {"warn_minutes": list(DEFAULT_WARN_MINUTES), "close_game": True}
        self.topup = {"kind": None, "upi": False, "options": [30, 60, 120], "last": None}
        self.rate = 0.0
        self.topup_win = None
        self.food = {"enabled": False, "orders": []}
        self.food_win, self.cart, self.menu = None, {}, None

        self.root = tk.Tk()
        self.root.title("CafeMaster")
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        self._build_lock()
        self._build_overlay()
        self._build_launcher()
        self.root.bind_all("<Control-Shift-F12>", lambda e: self._staff_exit())

        threading.Thread(target=self.link.loop, daemon=True).start()
        self.watcher.start()
        self.show_lock()
        self.root.after(200, self._pump)
        self.root.after(1000, self._tick)

    # ---- lock screen --------------------------------------------------------
    def _build_lock(self):
        r = self.root
        self.lock_frame = tk.Frame(r, bg=BG)
        self.lock_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        box = tk.Frame(self.lock_frame, bg=PANEL, padx=40, pady=32, highlightthickness=1,
                       highlightbackground="#2b3440")
        box.place(relx=.5, rely=.5, anchor="center")
        self.l_cafe = tk.Label(box, text="CafeMaster", font=("Segoe UI", 26, "bold"), fg=TEXT, bg=PANEL)
        self.l_cafe.pack()
        self.l_pc = tk.Label(box, text=self.cfg["name"], font=("Segoe UI", 13), fg=MUTED, bg=PANEL)
        self.l_pc.pack(pady=(2, 18))
        self.l_clock = tk.Label(box, font=("Segoe UI", 40, "bold"), fg=TEXT, bg=PANEL)
        self.l_clock.pack()
        self.l_rate = tk.Label(box, font=("Segoe UI", 11), fg=MUTED, bg=PANEL)
        self.l_rate.pack(pady=(0, 20))

        tk.Label(box, text="Member login", font=("Segoe UI", 11, "bold"), fg=TEXT, bg=PANEL).pack(anchor="w")
        self.e_user = self._entry(box, "Username")
        self.e_pass = self._entry(box, "Password", show="•")
        self.e_pass.bind("<Return>", lambda e: self._login())
        self.b_login = tk.Button(box, text="Log in", command=self._login, bg=ACCENT, fg="white",
                                 activebackground=ACCENT, relief="flat", font=("Segoe UI", 11, "bold"),
                                 padx=10, pady=6, cursor="hand2")
        self.b_login.pack(fill="x", pady=(10, 4))
        self.l_status = tk.Label(box, text="Walk-in? Pay at the counter and staff will unlock this PC.",
                                 font=("Segoe UI", 10), fg=MUTED, bg=PANEL, wraplength=320)
        self.l_status.pack(pady=(8, 0))
        self.b_extend = tk.Button(box, text="Time up? Pay here to extend", command=self.open_topup, bg="#2ea043",
                                  fg="white", activebackground="#2ea043", relief="flat",
                                  font=("Segoe UI", 11, "bold"), padx=10, pady=6, cursor="hand2")
        tk.Label(box, text="Notice: staff may view this PC's screen and close programs\n"
                           "to keep the cafe safe and enforce cafe rules.",
                 font=("Segoe UI", 9), fg=MUTED, bg=PANEL, justify="center").pack(pady=(14, 0))
        self.l_conn = tk.Label(self.lock_frame, font=("Segoe UI", 9), fg=MUTED, bg=BG)
        self.l_conn.place(relx=.5, rely=.97, anchor="center")
        for seq in ("<Alt-F4>", "<Escape>", "<Alt-Tab>"):
            r.bind(seq, lambda e: "break")

    def _entry(self, parent, placeholder, show=None):
        tk.Label(parent, text=placeholder, font=("Segoe UI", 9), fg=MUTED, bg=PANEL).pack(anchor="w", pady=(8, 0))
        e = tk.Entry(parent, font=("Segoe UI", 12), bg=BG, fg=TEXT, insertbackground=TEXT,
                     relief="flat", highlightthickness=1, highlightbackground="#2b3440",
                     highlightcolor=ACCENT, width=28, show=show or "")
        e.pack(fill="x", ipady=5)
        return e

    def show_lock(self):
        self.overlay.withdraw()
        if hasattr(self, "launcher"):
            self.launcher.withdraw()
        self.root.deiconify()
        try:
            self.root.overrideredirect(True)
        except tk.TclError:
            pass
        w, h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+0+0")
        self.root.attributes("-topmost", True)
        if platform.system() == "Windows":
            self.root.attributes("-fullscreen", True)
        self.root.lift()
        self.root.focus_force()
        self.e_user.focus_set()

    def hide_lock(self):
        self.root.attributes("-topmost", False)
        if platform.system() == "Windows":
            self.root.attributes("-fullscreen", False)
        self.root.withdraw()
        self.e_user.delete(0, "end")
        self.e_pass.delete(0, "end")
        self._place_overlay()
        self.overlay.deiconify()
        self.overlay.attributes("-topmost", True)
        if self.games:
            self.show_launcher()

    # ---- in-session overlay -------------------------------------------------
    def _build_overlay(self):
        o = self.overlay = tk.Toplevel(self.root, bg=PANEL)
        o.overrideredirect(True)
        o.attributes("-topmost", True)
        try:
            o.attributes("-alpha", 0.92)
        except tk.TclError:
            pass
        o.protocol("WM_DELETE_WINDOW", lambda: None)
        f = tk.Frame(o, bg=PANEL, padx=12, pady=8, highlightthickness=1, highlightbackground="#2b3440")
        f.pack(fill="both", expand=True)
        top = tk.Frame(f, bg=PANEL)
        top.pack(fill="x")
        self.o_title = tk.Label(top, font=("Segoe UI", 9), fg=MUTED, bg=PANEL)
        self.o_title.pack(side="left")
        self.o_min = tk.Label(top, text="—", font=("Segoe UI", 10, "bold"), fg=MUTED, bg=PANEL, cursor="hand2")
        self.o_min.pack(side="right")
        self.o_min.bind("<Button-1>", lambda e: self._toggle_compact())
        self.o_time = tk.Label(f, font=("Segoe UI", 22, "bold"), fg=TEXT, bg=PANEL)
        self.o_time.pack(anchor="w")
        self.o_detail = tk.Frame(f, bg=PANEL)
        self.o_detail.pack(fill="x")
        self.o_cost = tk.Label(self.o_detail, font=("Segoe UI", 9), fg=MUTED, bg=PANEL, justify="left")
        self.o_cost.pack(anchor="w")
        self.o_end = tk.Button(self.o_detail, text="End session", command=self._logout, bg="#1f2630", fg=TEXT,
                               relief="flat", font=("Segoe UI", 9), cursor="hand2", activebackground="#2b3440")
        self.o_games = tk.Button(self.o_detail, text="Games", command=lambda: self.show_launcher(), bg="#1f2630",
                                 fg=TEXT, relief="flat", font=("Segoe UI", 9), cursor="hand2",
                                 activebackground="#2b3440")
        self.o_order = tk.Label(self.o_detail, font=("Segoe UI", 9, "bold"), fg="#e3b341", bg=PANEL, justify="left")
        self.o_food = tk.Button(self.o_detail, text="Food & drinks", command=self.open_food, bg="#d29922", fg="white",
                                relief="flat", font=("Segoe UI", 9, "bold"), cursor="hand2", activebackground="#d29922")
        self.o_topup = tk.Button(self.o_detail, text="Top up", command=self.open_topup, bg="#2ea043", fg="white",
                                 relief="flat", font=("Segoe UI", 9, "bold"), cursor="hand2",
                                 activebackground="#2ea043")
        self.compact = False
        for w in (f, self.o_time, self.o_title):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
        o.withdraw()

    def _place_overlay(self):
        self.overlay.update_idletasks()
        w = self.overlay.winfo_reqwidth()
        if not getattr(self, "_moved", False):
            self.overlay.geometry(f"+{self.root.winfo_screenwidth() - max(w, 220) - 20}+20")

    def _toggle_compact(self):
        self.compact = not self.compact
        if self.compact:
            self.o_detail.pack_forget()
        else:
            self.o_detail.pack(fill="x")
        self.o_min.config(text="+" if self.compact else "—")

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.overlay.winfo_x(), e.y_root - self.overlay.winfo_y()

    def _drag_move(self, e):
        self._moved = True
        self.overlay.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    # ---- game launcher -------------------------------------------------------
    def _build_launcher(self):
        L = self.launcher = tk.Toplevel(self.root, bg=BG)
        L.title("Games")
        L.protocol("WM_DELETE_WINDOW", lambda: None)
        try:
            L.overrideredirect(True)
        except tk.TclError:
            pass
        head = tk.Frame(L, bg=BG, padx=40, pady=24)
        head.pack(fill="x")
        self.g_title = tk.Label(head, text="Choose a game", font=("Segoe UI", 24, "bold"), fg=TEXT, bg=BG)
        self.g_title.pack(side="left")
        right = tk.Frame(head, bg=BG)
        right.pack(side="right", padx=(0, 260))   # keep clear of the timer overlay (top-right)
        self.g_info = tk.Label(right, font=("Segoe UI", 12), fg=MUTED, bg=BG)
        self.g_info.pack(side="left", padx=(0, 16))
        self.g_food = tk.Button(right, text="Food & drinks", command=self.open_food, bg="#d29922", fg="white",
                                relief="flat", font=("Segoe UI", 10, "bold"), padx=12, pady=4, cursor="hand2")
        self.g_end = tk.Button(right, text="End session", command=self._logout, bg=PANEL, fg=TEXT, relief="flat",
                               font=("Segoe UI", 10), padx=12, pady=4, cursor="hand2")
        if self.cfg.get("allow_desktop"):
            tk.Button(right, text="Desktop", command=L.withdraw, bg=PANEL, fg=TEXT, relief="flat",
                      font=("Segoe UI", 10), padx=12, pady=4, cursor="hand2").pack(side="right", padx=(8, 0))
        self.g_grid = tk.Frame(L, bg=BG, padx=40)
        self.g_grid.pack(fill="both", expand=True)
        self.g_note = tk.Label(L, font=("Segoe UI", 10), fg=MUTED, bg=BG, pady=12)
        self.g_note.pack(side="bottom")
        L.withdraw()

    def _render_games(self):
        sig = json.dumps(self.games, sort_keys=True)
        if sig == self._games_sig:
            return
        self._games_sig = sig
        for w in self.g_grid.winfo_children():
            w.destroy()
        cols = 4
        palette = ["#1f6feb", "#8957e5", "#d29922", "#2ea043", "#e5534b", "#3fb8af", "#db61a2"]
        for i, g in enumerate(self.games):
            color = palette[sum(map(ord, g["name"])) % len(palette)]
            tile = tk.Frame(self.g_grid, bg=PANEL, highlightthickness=2, highlightbackground=PANEL,
                            cursor="hand2", width=260, height=150)
            tile.grid(row=i // cols, column=i % cols, padx=10, pady=10, sticky="nsew")
            tile.pack_propagate(False)
            tk.Frame(tile, bg=color, height=6).pack(fill="x")
            name = tk.Label(tile, text=g["name"], font=("Segoe UI", 16, "bold"), fg=TEXT, bg=PANEL,
                            wraplength=230, justify="left")
            name.pack(anchor="w", padx=16, pady=(18, 2))
            cat = tk.Label(tile, text=g.get("category") or "", font=("Segoe UI", 10), fg=MUTED, bg=PANEL)
            cat.pack(anchor="w", padx=16)
            play = tk.Label(tile, text="▶  PLAY", font=("Segoe UI", 10, "bold"), fg=color, bg=PANEL)
            play.pack(anchor="w", padx=16, pady=(14, 0))
            for w in (tile, name, cat, play):
                w.bind("<Button-1>", lambda e, gg=g: self.launch_game(gg))
                w.bind("<Enter>", lambda e, t=tile, c=color: t.config(highlightbackground=c))
                w.bind("<Leave>", lambda e, t=tile: t.config(highlightbackground=PANEL))
        for c in range(cols):
            self.g_grid.grid_columnconfigure(c, weight=1, uniform="g")

    def show_launcher(self):
        if self.state != "active" or not self.games:
            return
        self._render_games()
        self.g_note.config(text="")
        L = self.launcher
        L.geometry(f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0")
        L.deiconify()
        L.lift()
        L.focus_force()
        self.overlay.lift()

    def launch_game(self, game):
        err = launch_target(game.get("launch"), self.cfg.get("dry_run"))
        if err:
            self.popup(f"Could not start {game['name']}: {err}. Please call staff.", "Launch failed", urgent=True)
            return
        self.g_note.config(text=f"Starting {game['name']}…")
        self.launcher.after(1500, self.launcher.withdraw)
        self.link.call_async("launch", {"game_id": game["id"]}, "launched")

    # ---- food & drinks -------------------------------------------------------
    PAY_TEXT = {"tab": "Add to my bill", "balance": "Pay from my balance", "upi": "UPI (scan QR)",
                "cash": "Cash on delivery"}

    def open_food(self):
        if self.state != "active":
            return
        if self.food_win and self.food_win.winfo_exists():
            self.food_win.deiconify()
            self.food_win.lift()
            return
        w = self.food_win = tk.Toplevel(self.root, bg=PANEL)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        self.cart, self.menu, self._cat = {}, None, None
        self._food_frame = tk.Frame(w, bg=PANEL, padx=24, pady=20, highlightthickness=2, highlightbackground="#d29922")
        self._food_frame.pack()
        self._food_clear("Loading menu…")
        self._food_place()
        self.link.call_async("menu", {}, "menu")
        self.root.after(200, lambda: mon.force_foreground(self._hwnd(w)))

    def _food_place(self):
        w = self.food_win
        w.update_idletasks()
        w.geometry(f"+{(w.winfo_screenwidth() - w.winfo_reqwidth()) // 2}"
                   f"+{max(20, (w.winfo_screenheight() - w.winfo_reqheight()) // 2)}")

    def _food_clear(self, title):
        for c in self._food_frame.winfo_children():
            c.destroy()
        head = tk.Frame(self._food_frame, bg=PANEL)
        head.pack(fill="x")
        tk.Label(head, text=title, font=("Segoe UI", 18, "bold"), fg=TEXT, bg=PANEL).pack(side="left")
        tk.Button(head, text="Close", command=self._food_close, bg="#1f2630", fg=TEXT, relief="flat",
                  font=("Segoe UI", 10), padx=12, cursor="hand2").pack(side="right", padx=(24, 0))

    def _food_close(self):
        if self.food_win and self.food_win.winfo_exists():
            self.food_win.destroy()
        self.food_win = None

    def _on_menu(self, m):
        if not (self.food_win and self.food_win.winfo_exists()):
            return
        self.menu = m
        cats = list(dict.fromkeys(i["category"] for i in m["items"]))
        self._cat = self._cat if self._cat in cats else (cats[0] if cats else None)
        msg, self._food_msg = getattr(self, "_food_msg", None), None
        self._food_draw(msg)

    def _on_menu_error(self, err):
        if self.food_win and self.food_win.winfo_exists():
            self._food_clear("Menu unavailable")
            tk.Label(self._food_frame, text=err, fg=RED, bg=PANEL, font=("Segoe UI", 11)).pack(anchor="w", pady=8)

    def _food_draw(self, msg=None):
        m, cur = self.menu, self.menu["currency"]
        self._food_clear("Food & drinks")
        body = tk.Frame(self._food_frame, bg=PANEL)
        body.pack(fill="both", pady=(12, 0))
        cats = tk.Frame(body, bg=PANEL)
        cats.grid(row=0, column=0, sticky="n", padx=(0, 14))
        for c in dict.fromkeys(i["category"] for i in m["items"]):
            on = c == self._cat
            tk.Button(cats, text=c, width=12, anchor="w",
                      command=lambda c=c: (setattr(self, "_cat", c), self._food_draw()),
                      bg=ACCENT if on else "#1f2630", fg="white" if on else TEXT, relief="flat",
                      font=("Segoe UI", 11, "bold" if on else "normal"), pady=6, cursor="hand2").pack(fill="x", pady=2)
        grid = tk.Frame(body, bg=PANEL)
        grid.grid(row=0, column=1, sticky="n")
        for i, it in enumerate([x for x in m["items"] if x["category"] == self._cat]):
            ok = it["available"]
            tk.Button(grid, text=f"{it['name']}\n{cur}{it['price']:g}" + ("" if ok else "\nSold out"),
                      command=lambda it=it: self._cart_add(it, 1), state="normal" if ok else "disabled",
                      bg="#1f2630", fg=TEXT, disabledforeground=MUTED, activebackground="#2b3440", relief="flat",
                      font=("Segoe UI", 11, "bold"), width=15, height=3, wraplength=150,
                      cursor="hand2").grid(row=i // 3, column=i % 3, padx=4, pady=4)
        cart = tk.Frame(body, bg="#1f2630", padx=14, pady=12)
        cart.grid(row=0, column=2, sticky="n", padx=(14, 0))
        tk.Label(cart, text="Your order", font=("Segoe UI", 12, "bold"), fg=TEXT, bg="#1f2630").pack(anchor="w")
        items = {i["id"]: i for i in m["items"]}
        total = 0
        for mid, q in self.cart.items():
            it = items.get(mid)
            if not it or q <= 0:
                continue
            total += it["price"] * q
            row = tk.Frame(cart, bg="#1f2630")
            row.pack(fill="x", pady=2)
            tk.Label(row, text=it["name"][:18], width=18, anchor="w", fg=TEXT, bg="#1f2630",
                     font=("Segoe UI", 10)).pack(side="left")
            tk.Button(row, text="−", command=lambda it=it: self._cart_add(it, -1), bg=PANEL, fg=TEXT,
                      relief="flat", width=2, cursor="hand2").pack(side="left", padx=1)
            tk.Label(row, text=str(q), width=2, fg=TEXT, bg="#1f2630").pack(side="left")
            tk.Button(row, text="+", command=lambda it=it: self._cart_add(it, 1), bg=PANEL, fg=TEXT,
                      relief="flat", width=2, cursor="hand2").pack(side="left", padx=1)
        if not total:
            tk.Label(cart, text="Tap an item to add it", fg=MUTED, bg="#1f2630",
                     font=("Segoe UI", 10)).pack(anchor="w", pady=6)
        tk.Label(cart, text=f"Total  {cur}{total:g}", font=("Segoe UI", 14, "bold"), fg=TEXT,
                 bg="#1f2630").pack(anchor="w", pady=(10, 6))
        if msg:
            tk.Label(cart, text=msg, fg=RED, bg="#1f2630", wraplength=220, font=("Segoe UI", 10)).pack(anchor="w")
        if total:
            tk.Label(cart, text="Pay by", fg=MUTED, bg="#1f2630", font=("Segoe UI", 9)).pack(anchor="w")
            for meth in m["methods"]:
                tk.Button(cart, text=self.PAY_TEXT.get(meth, meth), command=lambda meth=meth: self._food_order(meth),
                          bg="#2ea043" if meth in ("tab", "balance") else ACCENT, fg="white", relief="flat",
                          font=("Segoe UI", 10, "bold"), pady=4, cursor="hand2").pack(fill="x", pady=2)
        self._food_place()

    def _cart_add(self, it, d):
        self.cart[it["id"]] = max(0, self.cart.get(it["id"], 0) + d)
        if it.get("can_make") is not None and self.cart[it["id"]] > it["can_make"]:
            self.cart[it["id"]] = it["can_make"]
            return self._food_draw(f"Only {it['can_make']} × {it['name']} left")
        self._food_draw()

    def _food_order(self, method):
        items = [{"menu_id": k, "qty": q} for k, q in self.cart.items() if q > 0]
        self._food_clear("Placing order…")
        self.link.call_async("order", {"items": items, "pay_method": method}, "order")

    def _on_order(self, o):
        if not (self.food_win and self.food_win.winfo_exists()):
            return
        cur = self.menu["currency"] if self.menu else self.currency
        self.cart = {}
        if o["pay_method"] == "upi" and o.get("upi"):
            u = o["upi"]
            self._food_clear(f"Order #{o['id']}: scan to pay {cur}{o['total']:g}")
            self._draw_qr(self._food_frame, qr.make(qr.upi_link(u["upi_id"], u["payee"], u["amount"], u["note"])))
            tk.Label(self._food_frame, text=f"Pay with any UPI app to {u['upi_id']}.\nWe'll bring your order to "
                                            f"{self.cfg['name']} once staff confirm.", fg=MUTED, bg=PANEL,
                     font=("Segoe UI", 10), justify="center").pack(pady=(8, 0))
        else:
            how = {"tab": f"{cur}{o['total']:g} added to your bill. Pay when you finish.",
                   "balance": f"{cur}{o['total']:g} paid from your balance.",
                   "cash": f"Please pay {cur}{o['total']:g} in cash when it arrives."}.get(o["pay_method"], "")
            self._food_clear(f"Order #{o['id']} placed!")
            tk.Label(self._food_frame, text=f"We'll bring it to {self.cfg['name']}.\n{how}", fg=TEXT, bg=PANEL,
                     font=("Segoe UI", 12), justify="left").pack(anchor="w", pady=(10, 0))
            self.root.after(5000, self._food_close)
        self._food_place()

    def _on_order_error(self, err):
        if self.food_win and self.food_win.winfo_exists() and self.menu:
            self._food_msg = err
            self.link.call_async("menu", {}, "menu")          # refresh stock levels, then show the error
            self._food_draw(err)

    # ---- top-up with UPI QR ---------------------------------------------------
    def open_topup(self):
        if not self.topup.get("kind"):
            self.popup("Top-up isn't available right now. Please see staff.", "Top up")
            return
        if self.topup_win and self.topup_win.winfo_exists():
            self.topup_win.lift()
            return
        w = self.topup_win = tk.Toplevel(self.root, bg=PANEL)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        self._topup_req = None
        self._topup_frame = tk.Frame(w, bg=PANEL, padx=28, pady=22, highlightthickness=2,
                                     highlightbackground="#2ea043")
        self._topup_frame.pack()
        self._topup_choose()
        w.lift()
        w.focus_force()
        self.root.after(200, lambda: mon.force_foreground(self._hwnd(w)))

    def _topup_clear(self, title):
        for c in self._topup_frame.winfo_children():
            c.destroy()
        tk.Label(self._topup_frame, text=title, font=("Segoe UI", 18, "bold"), fg=TEXT, bg=PANEL).pack(anchor="w")

    def _topup_center(self):
        w = self.topup_win
        w.update_idletasks()
        w.geometry(f"+{(w.winfo_screenwidth() - w.winfo_reqwidth()) // 2}"
                   f"+{max(20, (w.winfo_screenheight() - w.winfo_reqheight()) // 2)}")

    def _btn(self, parent, text, cmd, color=ACCENT, **kw):
        return tk.Button(parent, text=text, command=cmd, bg=color, fg="white", activebackground=color,
                         relief="flat", font=("Segoe UI", 11, "bold"), padx=14, pady=6, cursor="hand2", **kw)

    def _topup_choose(self):
        kind = self.topup["kind"]
        self._topup_clear("Add more time" if kind != "balance" else "Top up your balance")
        f = self._topup_frame
        sub = {"time": "Pick how much time to add. It is added as soon as staff confirm your payment.",
               "extend": "Your time ran out. Pay now to unlock this PC again.",
               "balance": "Money is added to your member balance."}[kind]
        tk.Label(f, text=sub, font=("Segoe UI", 10), fg=MUTED, bg=PANEL, wraplength=420,
                 justify="left").pack(anchor="w", pady=(2, 14))
        grid = tk.Frame(f, bg=PANEL)
        grid.pack(fill="x")
        for i, m in enumerate(self.topup.get("options") or [30, 60, 120]):
            amt = self.rate * m / 60
            label = (f"+{m // 60} hr" if m % 60 == 0 else f"+{m} min") + f"\n{self.currency}{amt:g}"
            b = tk.Button(grid, text=label, command=lambda m=m: self._topup_method(m), bg="#1f2630", fg=TEXT,
                          activebackground="#2b3440", relief="flat", font=("Segoe UI", 13, "bold"),
                          width=8, pady=10, cursor="hand2")
            b.grid(row=0, column=i, padx=(0, 10))
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x", pady=(18, 0))
        self._btn(row, "Close", self._topup_close, "#1f2630").pack(side="right")
        self._topup_center()

    def _topup_method(self, minutes):
        amt = self.rate * minutes / 60
        self._topup_clear(f"Pay {self.currency}{amt:g} for +{minutes} min")
        f = self._topup_frame
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x", pady=(16, 0))
        if self.topup.get("upi"):
            self._btn(row, "Pay by UPI (scan QR)", lambda: self._topup_send(minutes, "upi"), "#2ea043").pack(
                side="left", padx=(0, 10))
        self._btn(row, "Pay at counter", lambda: self._topup_send(minutes, "counter")).pack(side="left", padx=(0, 10))
        self._btn(row, "Back", self._topup_choose, "#1f2630").pack(side="left")
        self._topup_center()

    def _topup_send(self, minutes, method):
        self._topup_clear("One moment…")
        self.link.call_async("topup", {"minutes": minutes, "method": method}, "topup_req")

    def _on_topup_req(self, r):
        if not (self.topup_win and self.topup_win.winfo_exists()):
            return
        self._topup_req = r
        f = self._topup_frame
        if r["method"] == "upi":
            self._topup_clear(f"Scan to pay {r['currency']}{r['amount']:g}")
            link = qr.upi_link(r["upi_id"], r["payee"], r["amount"], r["note"])
            self._draw_qr(f, qr.make(link))
            tk.Label(f, text=f"Use any UPI app: GPay, PhonePe, Paytm, BHIM\nPaying to {r['upi_id']}  ·  Ref {r['ref']}",
                     font=("Segoe UI", 10), fg=MUTED, bg=PANEL, justify="center").pack(pady=(8, 0))
        else:
            self._topup_clear(f"Pay {r['currency']}{r['amount']:g} at the counter")
            tk.Label(f, text=f"Tell staff your PC name ({self.cfg['name']}) and reference {r['ref']}.",
                     font=("Segoe UI", 11), fg=TEXT, bg=PANEL).pack(anchor="w", pady=(8, 0))
        self._topup_msg = tk.Label(f, text="Waiting for staff to confirm your payment…", font=("Segoe UI", 11),
                                   fg="#e3b341", bg=PANEL)
        self._topup_msg.pack(pady=(12, 0))
        row = tk.Frame(f, bg=PANEL)
        row.pack(fill="x", pady=(14, 0))
        self._btn(row, "Hide (keep playing)", self.topup_win.withdraw, "#1f2630").pack(side="left")
        self._btn(row, "Cancel payment", self._topup_cancel, "#1f2630").pack(side="right")
        self._topup_center()

    def _on_topup_req_error(self, err):
        if self.topup_win and self.topup_win.winfo_exists():
            self._topup_clear("Couldn't start payment")
            tk.Label(self._topup_frame, text=err, font=("Segoe UI", 11), fg=RED, bg=PANEL, wraplength=420).pack(
                anchor="w", pady=(8, 0))
            self._btn(self._topup_frame, "Close", self._topup_close, "#1f2630").pack(anchor="e", pady=(14, 0))
            self._topup_center()

    def _draw_qr(self, parent, matrix, px=6, border=4):
        n = len(matrix)
        size = (n + 2 * border) * px
        c = tk.Canvas(parent, width=size, height=size, bg="white", highlightthickness=0)
        c.pack(pady=(12, 0))
        for y, row in enumerate(matrix):
            for x, dark in enumerate(row):
                if dark:
                    x0, y0 = (x + border) * px, (y + border) * px
                    c.create_rectangle(x0, y0, x0 + px, y0 + px, fill="black", outline="")

    def _topup_status(self):
        """Follow the pending request via the heartbeat and react when staff approve or reject."""
        req, last = getattr(self, "_topup_req", None), self.topup.get("last")
        if not req or not last or last["id"] != req["id"] or last["status"] == "pending":
            return
        if not (self.topup_win and self.topup_win.winfo_exists()):
            self._topup_req = None
            return
        ok = last["status"] == "approved"
        self.topup_win.deiconify()
        self._topup_msg.config(text="Payment confirmed. Time added. Enjoy!" if ok
                               else f"Payment {last['status']}. Please see staff at the counter.",
                               fg="#2ea043" if ok else RED)
        self._topup_req = None
        if ok:
            self.root.after(2500, self._topup_close)

    def _topup_cancel(self):
        if self._topup_req:
            self.link.call_async("topup", {"cancel": True}, "topup_cancel")
        self._topup_close()

    def _topup_close(self):
        self._topup_req = None
        if self.topup_win and self.topup_win.winfo_exists():
            self.topup_win.destroy()
        self.topup_win = None

    # ---- popups -------------------------------------------------------------
    def warn_toast(self, text, title):
        """Time warning: shows on top of the game without taking keyboard focus, beeps, closes itself."""
        t = tk.Toplevel(self.root, bg=PANEL)
        self._toasts = [w for w in getattr(self, "_toasts", []) if w.winfo_exists()] + [t]
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        f = tk.Frame(t, bg=PANEL, padx=22, pady=14, highlightthickness=3, highlightbackground=RED)
        f.pack()
        tk.Label(f, text=title, font=("Segoe UI", 18, "bold"), fg=RED, bg=PANEL).pack(anchor="w")
        tk.Label(f, text=text, font=("Segoe UI", 12), fg=TEXT, bg=PANEL, wraplength=440,
                 justify="left").pack(anchor="w", pady=(4, 10))
        row = tk.Frame(f, bg=PANEL)
        row.pack(anchor="e")
        if self.topup.get("kind"):
            tk.Button(row, text="Top up now", command=lambda: (t.destroy(), self.open_topup()), bg="#2ea043",
                      fg="white", relief="flat", padx=14, font=("Segoe UI", 10, "bold"),
                      cursor="hand2").pack(side="left", padx=(0, 8))
        tk.Button(row, text="OK", command=t.destroy, bg=ACCENT, fg="white", relief="flat", padx=18,
                  font=("Segoe UI", 10, "bold"), cursor="hand2").pack(side="left")
        t.update_idletasks()
        t.geometry(f"+{(t.winfo_screenwidth() - t.winfo_reqwidth()) // 2}+40")
        t.lift()
        mon.beep()
        self.root.bell()
        t.after(15_000, lambda: t.winfo_exists() and t.destroy())

    def popup(self, text, title="Message from staff", urgent=False):
        t = tk.Toplevel(self.root, bg=PANEL)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        f = tk.Frame(t, bg=PANEL, padx=24, pady=18, highlightthickness=2,
                     highlightbackground=RED if urgent else ACCENT)
        f.pack()
        tk.Label(f, text=title, font=("Segoe UI", 10, "bold"), fg=MUTED, bg=PANEL).pack(anchor="w")
        tk.Label(f, text=text, font=("Segoe UI", 14), fg=TEXT, bg=PANEL, wraplength=420,
                 justify="left").pack(anchor="w", pady=(6, 12))
        tk.Button(f, text="OK", command=t.destroy, bg=ACCENT, fg="white", relief="flat", padx=18,
                  font=("Segoe UI", 10, "bold"), cursor="hand2").pack(anchor="e")
        t.update_idletasks()
        x = (t.winfo_screenwidth() - t.winfo_reqwidth()) // 2
        y = (t.winfo_screenheight() - t.winfo_reqheight()) // 3
        t.geometry(f"+{x}+{y}")
        t.lift()
        t.after(60_000, lambda: t.winfo_exists() and t.destroy())

    # ---- actions ------------------------------------------------------------
    def _login(self):
        u, p = self.e_user.get().strip(), self.e_pass.get()
        if not u or not p:
            self.l_status.config(text="Enter your username and password", fg=RED)
            return
        self.b_login.config(state="disabled", text="Checking…")
        self.link.call_async("login", {"username": u, "password": p}, "login")

    def _logout(self):
        self.o_end.config(state="disabled")
        self.link.call_async("logout", {}, "logout")

    def _staff_exit(self):
        d = tk.Toplevel(self.root, bg=PANEL)
        d.attributes("-topmost", True)
        d.title("Staff exit")
        tk.Label(d, text="Staff exit code", fg=TEXT, bg=PANEL, padx=20, pady=10).pack()
        e = tk.Entry(d, show="•")
        e.pack(padx=20)
        e.focus_force()

        def check(_=None):
            if e.get() == str(self.cfg.get("exit_code")):
                self.root.destroy()
            else:
                d.destroy()
        e.bind("<Return>", check)

    # ---- main loop ----------------------------------------------------------
    def _pump(self):
        try:
            while True:
                kind, data = self.inbox.get_nowait()
                getattr(self, "_on_" + kind, lambda d: None)(data)
        except queue.Empty:
            pass
        self.root.after(200, self._pump)

    def _on_state(self, d):
        self.last_ok = time.time()
        self.offset = d["server_time"] + d.get("_rtt", 0) / 2 - time.time()
        self.cafe_name, self.currency = d.get("cafe_name", self.cafe_name), d.get("currency", self.currency)
        self.cfg["name"] = d["pc"]["name"]
        self.l_cafe.config(text=self.cafe_name)
        self.l_pc.config(text=f'{d["pc"]["name"]}  ·  {d["pc"]["group"]}')
        self.l_rate.config(text=f'{self.currency}{d["rate"]:g} per hour')
        self.l_conn.config(text=f"Connected to {self.cfg['server']}", fg=MUTED)
        self.games = d.get("games") or []
        self.timeup = d.get("timeup") or self.timeup
        self.food = d.get("food") or self.food
        self.topup = d.get("topup") or self.topup
        self.rate = (d.get("session") or {}).get("rate") or d.get("rate") or 0
        self._topup_status()
        if self.launcher.winfo_viewable():
            self._render_games()
        new = d.get("session")
        if new and new.get("ends") and new["ends"] <= time.time() + self.offset:
            new = None  # already expired; server will close it on its next tick
        if new and (not self.session or new["id"] != self.session["id"]):
            self.warned = set()
        self.session = new
        self._apply("active" if new else "locked")
        self.watcher.update(d.get("watch"), d.get("policy"), bool(new))
        for c in d.get("commands", []):   # after state change, so "launch" sees the new session
            self._run_command(c)

    def _on_notice(self, text):
        self.popup(text, "Cafe rules", urgent=True)

    def _on_offline(self, err):
        if time.time() - self.last_ok > OFFLINE_GRACE:
            self.l_conn.config(text=f"No connection to server ({err})", fg=RED)

    def _on_login(self, _):
        self.b_login.config(state="normal", text="Log in")
        self.l_status.config(text="Welcome! Starting session…", fg=MUTED)

    def _on_login_error(self, err):
        self.b_login.config(state="normal", text="Log in")
        self.l_status.config(text=err, fg=RED)

    def _on_logout(self, d):
        self.o_end.config(state="normal")
        bill = (d or {}).get("bill") or {}
        self.session = None
        self._apply("locked")
        if bill.get("due"):
            self.popup(f"Please pay {self.currency}{bill['due']:.0f} at the counter.", "Session ended")
        elif "balance_left" in bill:
            self.popup(f"Charged {self.currency}{bill['amount']:.0f}. "
                       f"Balance left: {self.currency}{bill['balance_left']:.0f}", "Session ended")

    def _on_logout_error(self, err):
        self.o_end.config(state="normal")
        self.popup(err, "Could not end session", urgent=True)

    def _run_command(self, c):
        t = c.get("type")
        if t == "message":
            self.popup(c.get("text", ""))
        elif t == "lock":
            self.session = None
            self._apply("locked")
        elif t == "processes":
            self.watcher.send_processes()
        elif t == "kill":
            self.watcher.kill(c.get("text", ""))
        elif t == "launch":
            game = next((g for g in self.games if g["id"] == c.get("game_id")), None)
            if game and self.state == "active":
                self.launch_game(game)
        elif t in ("shutdown", "restart", "logoff"):
            self.session = None
            self._apply("locked")
            self.popup(f"This PC will {t.replace('logoff', 'log off')} now.", "Staff action", urgent=True)
            os_action(t, self.cfg.get("dry_run"))

    def _apply(self, state):
        if state == self.state:
            return
        was_active = self.state == "active"
        self.state = state
        if state == "locked":
            for w in getattr(self, "_toasts", []):     # "N minutes left" no longer applies
                if w.winfo_exists():
                    w.destroy()
            self._toasts = []
            if self.food_win and self.food_win.winfo_exists():
                self.food_win.destroy()
            if was_active:
                # a full-screen game can sit above our window: close (or minimise) it first
                app = mon.clear_foreground_app(close=self.timeup.get("close_game", True),
                                               dry_run=self.cfg.get("dry_run"))
                if app:
                    print("cleared for lock:", app, flush=True)
            self.show_lock()
            self.root.after(300, lambda: mon.force_foreground(self._hwnd(self.root)))
        else:
            self.hide_lock()

    @staticmethod
    def _hwnd(win):
        try:
            return int(win.wm_frame(), 16)
        except (tk.TclError, ValueError):
            return win.winfo_id()

    def _warn(self, rem, s):
        """Show one pop-up when a warning threshold is crossed; re-arm after a top-up."""
        marks = sorted(self.timeup.get("warn_minutes") or DEFAULT_WARN_MINUTES)
        for m in marks:
            if rem > m * 60 + 30:
                self.warned.discard(m)              # time was added: warn again next time
        crossed = [m for m in marks if rem <= m * 60 and m not in self.warned]
        if not crossed or rem <= 0:
            return
        self.warned.update(m for m in marks if rem <= m * 60)
        left = max(1, int(rem // 60) + (1 if rem % 60 > 30 else 0))
        how = ("Tap Top up now to keep playing." if self.topup.get("kind")
               else "Ask staff to add time." if s["mode"] == "prepaid" else "Top up at the counter to keep playing.")
        self.warn_toast(f"Your PC locks automatically when time runs out. {how}",
                        f"{left} minute{'s' if left != 1 else ''} left")

    def _tick(self):
        s = self.session
        srv_now = time.time() + self.offset
        if self.state == "locked":
            self.l_clock.config(text=time.strftime("%H:%M"))
            if self.topup.get("kind") == "extend":
                self.b_extend.pack(fill="x", pady=(10, 0))
            else:
                self.b_extend.pack_forget()
            # keep the lock screen on top, but keep messages and the payment window above it
            self.root.attributes("-topmost", True)
            self.root.lift()
            for w in self.root.winfo_children():
                if isinstance(w, tk.Toplevel) and w not in (self.overlay, self.launcher) \
                        and w.winfo_exists() and w.winfo_viewable():
                    w.attributes("-topmost", True)
                    w.lift()
        elif s:
            mode_txt = {"prepaid": "Prepaid", "postpaid": "Pay later", "member": "Member", "free": "Free"}
            title = f'{self.cfg["name"]} · {s["user"]} · {s.get("package") or mode_txt.get(s["mode"], "")}'
            self.o_title.config(text=title if len(title) <= 42 else title[:41] + "…")
            if s.get("ends"):
                rem = s["ends"] - srv_now
                self.o_time.config(text=fmt_dur(rem) + " left", fg=RED if rem < 300 else TEXT)
                self._warn(rem, s)
                if rem <= 0:  # enforce locally even if the server is unreachable
                    self.session = None
                    self._apply("locked")
                    self.popup("Your time is up. Thanks for playing!", "Session ended")
            else:
                self.o_time.config(text=fmt_dur(srv_now - s["started"]), fg=TEXT)
            cost = f'{self.currency}{s.get("cost", 0):.0f} so far' if s["mode"] in ("postpaid", "member") else ""
            bal = f'  ·  balance {self.currency}{s["balance"]:.0f}' if "balance" in s else ""
            self.o_cost.config(text=cost + bal)
            can_end = s["mode"] in ("member", "postpaid")
            if can_end:
                self.o_end.pack(anchor="w", pady=(6, 0))
                self.g_end.pack(side="right")
            else:
                self.o_end.pack_forget()
                self.g_end.pack_forget()
            if self.food.get("enabled"):
                self.o_food.pack(anchor="w", pady=(6, 0))
                self.g_food.pack(side="right", padx=(8, 0))
            else:
                self.o_food.pack_forget()
                self.g_food.pack_forget()
            labels = {"new": "received", "preparing": "being prepared", "delivered": "delivered"}
            live = [o for o in self.food.get("orders", []) if o["status"] in ("new", "preparing")]
            if live:
                self.o_order.config(text="\n".join(f"Order #{o['id']} {labels[o['status']]}" for o in live[:2]))
                self.o_order.pack(anchor="w", pady=(4, 0))
            else:
                self.o_order.pack_forget()
            if self.topup.get("kind"):
                self.o_topup.pack(anchor="w", pady=(6, 0))
            else:
                self.o_topup.pack_forget()
            if self.games:
                self.o_games.pack(anchor="w", pady=(6, 0))
            else:
                self.o_games.pack_forget()
            self.g_title.config(text=f"Hi {s['user']}, choose a game")
            self.g_info.config(text=self.o_time.cget("text") + ("  ·  " + s["package"] if s.get("package") else ""))
            self.overlay.attributes("-topmost", True)
            self._place_overlay()
        self.root.after(1000, self._tick)

    def run(self):
        self.root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="CafeMaster agent for gaming PCs")
    ap.add_argument("--server", help="e.g. http://192.168.1.10:8080")
    ap.add_argument("--agent-key", dest="agent_key", help="shown in dashboard > Settings")
    ap.add_argument("--pc-id", dest="pc_id", help="unique id (default: computer name)")
    ap.add_argument("--name", help="display name, e.g. PC-01")
    ap.add_argument("--exit-code", dest="exit_code", help="staff code for Ctrl+Shift+F12 exit")
    ap.add_argument("--dry-run", action="store_true", help="print shutdown/restart instead of doing it")
    cfg = load_config(ap.parse_args())
    if not cfg["agent_key"]:
        print("Set agent_key in agent_config.json (copy it from dashboard > Settings).")
    AgentApp(cfg).run()


if __name__ == "__main__":
    main()

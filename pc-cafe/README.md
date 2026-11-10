# CafeMaster: PC cafe management

One master PC controls every gaming PC in the cafe. Staff use a browser dashboard to start and stop sessions, bill customers, manage member accounts and send remote commands. Each gaming PC runs a small agent that keeps the PC locked until a session is paid for.

```
 ┌──────────────── Counter / master PC ────────────────┐
 │  server.py  ── web dashboard (http://<ip>:8080)     │
 │             ── SQLite: members, sessions, revenue   │
 └───────────────▲──────────────▲──────────────▲───────┘
                 │ LAN, check-in every 2 s     │
          ┌──────┴─────┐  ┌─────┴──────┐  ┌────┴───────┐
          │ agent.py   │  │ agent.py   │  │ agent.py   │   … one per gaming PC
          │ PC-01      │  │ PC-02      │  │ VIP-01     │
          └────────────┘  └────────────┘  └────────────┘
```

## Features

| Area | What it does |
|---|---|
| **Live PC grid** | Shows each PC as Free, In use or Offline, with a live countdown (or time used), the customer's name and the amount so far. A card turns amber when less than 5 minutes are left. |
| **Session types** | **Prepaid**: the customer pays first and the PC locks when the time runs out. **Pay later**: the clock runs and staff bill at the end. **Member**: the customer logs in at the PC and the cost comes off their balance. **Free / staff**: no charge. |
| **Remote control** | Message, lock, log off, restart or shut down one PC, a selection of PCs, or all of them. |
| **Billing** | Rates per hour for each PC group (for example Standard ₹60, VIP ₹100). Adds a minimum billable time and optional rounding to blocks of minutes. Shows a bill on screen when a session ends, and records cash, UPI or card. |
| **Members** | Accounts with a prepaid balance. Staff can top up, refund and reset passwords. A top-up during a session extends the time straight away. A member can't be logged in on two PCs at once. |
| **Reports** | Daily cash collected, walk-in sales, member top-ups and usage, sessions and hours played. Downloads as a CSV that opens directly in Excel or Google Sheets. |
| **Client lock screen** | Full-screen and always on top. Includes member login, a small timer overlay that can be dragged around during play, and warnings at 5 minutes and 1 minute. The PC locks itself when time is up, even if the network drops. |
| **Resilience** | Running sessions survive a server restart because they are stored in SQLite. Commands are sent to each PC once. |

The only requirement is **Python 3.9 or later**. There is nothing to `pip install`.

## Setup (about 10 minutes)

### 1. Master PC (counter)
1. Install Python from python.org and tick **"Add to PATH"**.
2. Copy the `server/` folder to the PC and double-click `start_server.bat` (or run `python server.py`).
3. Open **http://localhost:8080** and log in with the default password `admin123`.
4. Go to **Settings** and do four things: change the admin password, set your rates, set the cafe name, and copy the **Agent key**.
5. Find this PC's LAN IP address (`ipconfig`, for example `192.168.1.10`). Allow port 8080 through Windows Firewall for **Private** networks.

### 2. Each gaming PC
1. Install Python from python.org. Tkinter is included by default.
2. Copy the `client/` folder to the PC. Rename `agent_config.example.json` to `agent_config.json` and fill in:
   - `server`: `http://192.168.1.10:8080`
   - `agent_key`: the key from the dashboard
   - `name`: for example `PC-01`
   - `exit_code`: a staff PIN
3. Double-click `start_agent.bat`. The PC shows up on the dashboard within 2 seconds.
4. **Make it start automatically:** press `Win+R`, run `shell:startup` and put a shortcut to `start_agent.bat` there. Better still, use a Task Scheduler task set to "At log on" with the highest privileges.

**Staff exit on a gaming PC:** press `Ctrl+Shift+F12` and enter the `exit_code`.

**Testing without shutting PCs down:** run `python agent.py --dry-run`. Shutdown, restart and log off are then printed instead of carried out.

## Daily use

| Situation | Action |
|---|---|
| Walk-in pays ₹120 for 2 hours | Click **Start** on a free PC, choose Prepaid, then 2 hr and Cash |
| Customer wants more time | Click **+ Time** on the PC card |
| Regular customer | **Members**, then **+ New member**, then **Top up**. The customer logs in on any PC with their username and password |
| "Pay when I leave" | Start the session as **Pay later**. Clicking **End & bill** shows the amount to collect |
| Closing time | **Select all**, then **Message** ("Closing in 10 min"), then **Shut down** |
| End-of-day cash tally | **Reports**, then **Download CSV** |

## Limits of this version

- **The lock screen is not tamper-proof.** It is a full-screen window that stays on top. A determined user can still get past it with `Ctrl+Alt+Del` and Task Manager. For production, lock down each gaming PC's Windows account:
  - Use a **standard (non-admin) user**.
  - Use Group Policy to remove Task Manager (`gpedit.msc`, then User Config, then Admin Templates, then System, then Ctrl+Alt+Del Options).
  - Hide the desktop and block `cmd`/`regedit`.
  - Ideally, run the agent as a Windows **service** so that killing it isn't possible.

  Commercial tools such as ggLeap and SENET go further: they replace the Windows shell and add a driver-level keyboard hook.
- **The lock screen covers only the primary monitor.**
- **No game launcher, disk restore or remote screen view.** These are the natural next additions (see the roadmap).
- **Keep it on a trusted LAN.** Traffic is plain HTTP. Don't expose port 8080 to the internet.

## Roadmap ideas
1. A game and app launcher on the client that shows only an approved list of games.
2. Food and drink orders from the PC, with a POS screen on the dashboard.
3. Happy-hour pricing based on time of day, and discount packages (for example 5 hours for ₹250).
4. Live screenshots or thumbnails of each PC.
5. Bundling the agent as a Windows service and a single `.exe` using PyInstaller, so gaming PCs don't need Python.
6. Multiple staff logins with an audit trail.

## Developer notes

```
server/server.py       HTTP API + billing engine (ThreadingHTTPServer + SQLite)
server/dashboard.html  Staff UI (vanilla JS, polls /api/state every 2 s)
client/agent.py        Gaming-PC agent (tkinter UI + background heartbeat thread)
tests/test_server.py   End-to-end tests: python3 -m unittest discover -s tests
```

**Agent protocol:** the agent sends `POST /api/agent/heartbeat {key, pc_id, name, info}` and gets back `{state: locked|active, session, commands[], server_time}`. The agent works out the clock offset between itself and the server from `server_time`, so the countdown stays correct even if the PC's clock is wrong.

**Data:** stored in `server/data/` as `cafe.db` and `config.json`. Back up this folder.

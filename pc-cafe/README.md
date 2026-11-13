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
| **Client lock screen** | Full-screen and always on top. Includes member login and a small timer overlay that can be dragged around during play. |
| **Auto-lock when time is up** | The PC locks itself the moment paid time runs out, **even mid-game**. It closes the game in front (or just minimises it, if you prefer) and pulls the lock screen to the front. It works even if the network drops. |
| **Time warnings** | Pop-ups at **10, 5 and 1 minute** left (you can change these). They appear on top of the game without taking the keyboard away, beep, include a **Top up now** button and close themselves. They re-arm after a top-up. |
| **Top-up with UPI QR** | The customer taps **Top up** (on the timer, in a warning, or on the lock screen after time is up), picks +30 min, +1 hr or +2 hr, and gets a **UPI QR code** for that exact amount, which works with GPay, PhonePe, Paytm and BHIM. Staff see the payment at the top of the dashboard with a chime, check their UPI app or soundbox, and press **Approve**. The time is added instantly, or the PC unlocks if it had already locked. "Pay at counter" works the same way for cash. |
| **Game launcher** | When a session starts, the rig shows full-screen tiles for the games installed on it. The customer clicks a tile to play. Staff can also launch a game remotely on one rig or many from the dashboard. |
| **Rig register** | Records which Steam or EA account is signed in on each rig, which games are installed and hardware notes. Flags rigs that share one account, because those rigs can't play online at the same time. Never stores passwords. |
| **Packages and group start** | Fixed-price bundles such as "5 hours for ₹250" (charged per rig) or "Race night: 4 rigs × 1 hour for ₹800" (one price, split across the rigs). Tick several rigs and click **Start group** to start them all together, charge once and auto-launch the same game on every rig. **End group** closes them together. |
| **Remote screen view** | The **Monitor** tab shows live thumbnails of every PC in use. Click one to watch it full size (refreshes about once a second) and see the window or browser tab that's open. From the same screen you can **Lock now**, **Close current app**, **Send warning**, see **Running programs** (and close any of them), restart or shut down the PC. |
| **Cafe rules** | Set in **Settings › Monitoring & cafe rules**. **Blocked programs** (torrent clients by default) are closed automatically. **Blocked words** in a window or tab title raise an alert. **Blocked websites** become unreachable on every PC. Choose whether a broken rule only alerts staff or locks the PC straight away. |
| **Alerts log** | Every broken rule is logged with the time, PC, customer and a screenshot of the screen at that moment, for evidence if needed. Screenshots are kept for 7 days. Unreviewed alerts show as a red badge. |
| **Food & drinks: ordering** | Customers tap **Food & drinks** on their PC (on the timer or in the game launcher), browse categories, add to a cart and choose how to pay:<br>• **Add to my bill**: collected at End & bill.<br>• **Pay from my balance**: members only.<br>• **UPI**: shows a QR code.<br>• **Cash on delivery**.<br>Sold-out items are greyed out. The timer shows the order's progress ("Order #12 being prepared"). Staff can also take walk-in orders on the **Counter order** POS. |
| **Kitchen board** | **F&B › Orders** has three columns: New, Preparing, and Delivered-but-unpaid. Staff tap **Accept**, then **Delivered**, then **Paid cash** or **Paid UPI**. New orders chime. **Cancel** puts the stock back and refunds a balance payment. |
| **Menu with recipes** | Packaged items (Coke, chips) track their own stock one-for-one. Made items list the stock they use: Masala Maggi uses 1 Maggi packet, Cutting Chai uses 60 ml milk, 3 g tea and 8 g sugar. Each item shows its cost, margin % and how many can still be made. |
| **Inventory** | **Stock in** records a purchase and updates the average cost. **Count / waste** corrects stock after a physical count or records wastage. Every change goes into a log. Items flagged **Low** need reordering. Shows total stock value and exports to CSV for Excel or Sheets. |
| **F&B in billing and reports** | Food added to a PC bill is included in **End & bill**. Reports show food sales, cost of goods, margin, items sold and unpaid orders, and the CSV export includes them. |
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

### 3. Payments and warnings (one-time)
Go to **Settings › Time-up warnings & payments** and fill in:
- Your **UPI ID**, for example `yourcafe@okhdfcbank`.
- The name customers see in their UPI app.
- The top-up options.
- The warning minutes.

Then test it: start a 15-minute session on one PC, tap **Top up**, scan the QR with your own phone (don't pay) and check that the cafe name and amount are correct.

**Why staff approve each payment:** a plain UPI QR can't tell the software that money arrived. That needs a payment gateway (Razorpay, Cashfree, PhonePe PG), which brings merchant KYC and about 2% fees. Approving takes one click after you glance at your UPI app or soundbox. Unanswered requests expire after 15 minutes. A gateway can be added later.

### 4. Food & drinks (one-time, about 20 minutes)
1. Go to **F&B › Inventory › + Stock item** for everything you buy. For each one, set:
   - the unit (pcs, g, ml)
   - the cost per unit
   - the quantity you have now
   - the level at which you should reorder

   Packaged items (Coke, chips, water) can skip this step, because the next step creates their stock automatically.
2. Go to **F&B › Menu › + Menu item**:
   - **Packaged:** enter the selling price, the buying cost and the quantity in stock.
   - **Made from ingredients:** add each ingredient and the amount one serving uses.
3. From then on, record deliveries with **Stock in**, and do a weekly **Count** so the system matches the shelf.

Menu prices are the final price the customer pays, including GST. If you're GST-registered, check the right rate with your CA. Restaurant service is usually 5%, and packaged goods are sold at MRP. You'll also need a GST invoice format; this version doesn't print tax invoices.

### 5. Games and rigs (one-time)
1. **Games tab, then + Add game.** Enter the name and the **launch target**:
   - **Steam games:** in Steam, right-click the game, choose **Manage**, then **Add desktop shortcut**. Open the shortcut's **Properties** and copy the `steam://rungameid/…` link.
   - **EA app and other games:** use the `.exe` or shortcut path. It must be the same path on every rig.
2. **PCs tab, then ⋯ on each rig.** Enter the game account signed in on that rig (for example `Steam: rig01_cafe`), tick the installed games and add notes such as "Fanatec DD wheel".
3. **Packages tab.** Create bundles, for example "Race night 4×1hr" with Rigs set to 4, a price of ₹800 and F1 25 set to auto-launch.

**Racing games (F1 25):**
- F1 25 has no LAN mode. Multiplayer runs through EA's servers, so each rig needs internet access, **its own EA account and its own copy of the game**.
- Make all the rig accounts EA friends once. On race night, host a private lobby on one rig and invite the rest.
- If two rigs share one account, the dashboard shows a ⚠ warning.

## Daily use

| Situation | Action |
|---|---|
| Walk-in pays ₹120 for 2 hours | Click **Start** on a free PC, choose Prepaid, then 2 hr and Cash |
| Customer wants more time | Click **+ Time** on the PC card |
| Regular customer | **Members**, then **+ New member**, then **Top up**. The customer logs in on any PC with their username and password |
| "Pay when I leave" | Start the session as **Pay later**. Clicking **End & bill** shows the amount to collect |
| Closing time | **Select all**, then **Message** ("Closing in 10 min"), then **Shut down** |
| Race night for 4 friends | Tick the 4 rigs, click **Start group**, choose the "Race night" package and enter the team name. F1 25 opens on all 4 rigs. Then create a private lobby in-game |
| End-of-day cash tally | **Reports**, then **Download CSV** |

## Monitoring: what to know before you switch it on

**Tell customers.**
- Every lock screen shows a notice that staff may view the screen and close programs. Keep it there.
- Also put up a printed notice at the counter, and add a line to your cafe rules or terms.
- Under India's DPDP Act 2023, screen captures are personal data:
  - Use them only for safety and rule enforcement.
  - Restrict dashboard access to authorised staff (change the admin password).
  - Don't keep screenshots longer than you need. Alert screenshots are deleted automatically after 7 days.

**What each feature can and can't do:**

| Feature | Works well | Limits |
|---|---|---|
| Live screen | Desktop, browsers, windowed or borderless games | Some games in exclusive full-screen mode show black. Use borderless-window mode in the game settings. Covers the primary monitor only. |
| Website blocking | Stops the listed sites at the system level (hosts file) | Needs the agent to run **as administrator**. A browser with "secure DNS" / DNS-over-HTTPS or a VPN can get around it. Turn secure DNS off by policy, and add VPN apps to Blocked programs. For stronger filtering, use a router or DNS filter as well (for example a family DNS service). |
| Blocked words | Catches page titles such as "… torrent …" | Only sees the window/tab **title**, not the page content. |
| Blocked programs | Closes torrent clients and similar within about 6 seconds | Matches by program name, so a renamed .exe isn't caught. Also block program installs with a standard (non-admin) Windows account. |
| Downloads | Torrent apps are closed. Torrent sites can be blocked or flagged | Ordinary browser downloads aren't blocked file by file. Watch the screen, or limit downloads on your router. |

## Limits of this version

- **Some games can block the time-up lock.** A game in exclusive full-screen mode can occasionally hold the screen. That's why "Close the game in front when time is up" is on by default. If you turn it off, the game is only minimised, so set games to borderless-window mode.
- **The lock screen is not tamper-proof.** It is a full-screen window that stays on top. A determined user can still get past it with `Ctrl+Alt+Del` and Task Manager. For production, lock down each gaming PC's Windows account:
  - Use a **standard (non-admin) user**.
  - Use Group Policy to remove Task Manager (`gpedit.msc`, then User Config, then Admin Templates, then System, then Ctrl+Alt+Del Options).
  - Hide the desktop and block `cmd`/`regedit`.
  - Ideally, run the agent as a Windows **service** so that killing it isn't possible.

  Commercial tools such as ggLeap and SENET go further: they replace the Windows shell and add a driver-level keyboard hook.
- **The lock screen covers only the primary monitor.**
- **The launcher is not a locked-down kiosk.** It covers the desktop and shows only approved games. Once a game is running, a customer can still Alt+Tab to Windows. Combine it with the account restrictions above. Set `"allow_desktop": true` in `agent_config.json` to show a "Desktop" button for staff or home use.
- **Game logins aren't automated.** Each rig stays signed in to its own Steam/EA account. CafeMaster records which account is on which rig but never stores or enters passwords.
- **No disk restore or remote screen view.**
- **Keep it on a trusted LAN.** Traffic is plain HTTP. Don't expose port 8080 to the internet.

## Roadmap ideas
1. Food and drink orders from the PC, with a POS screen on the dashboard.
2. Happy-hour pricing based on time of day.
3. Leaderboards for race nights.
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

**Payments:** `client/qr.py` is a small built-in QR generator, so nothing needs to be installed on the PCs. It was checked module-for-module against the `qrcode` package and decoded with OpenCV, including straight from a screenshot of the PC.

**Monitoring files:** `client/monitor.py` holds the screen capture (Windows GDI via ctypes, nothing to install), window title, process list, closing programs, rule checks and hosts-file blocking. A PC streams its screen only while someone is watching it on the dashboard.

**Agent protocol:** the agent sends `POST /api/agent/heartbeat {key, pc_id, name, info}` and gets back `{state: locked|active, session, commands[], games[], server_time}`. Commands are `message`, `lock`, `launch`, `kill`, `processes`, `logoff`, `restart` and `shutdown`. The agent reports game starts with `POST /api/agent/launch` so the daily report can show which games were played. The agent works out the clock offset between itself and the server from `server_time`, so the countdown stays correct even if the PC's clock is wrong.

**Data:** stored in `server/data/` as `cafe.db` and `config.json`. Back up this folder.

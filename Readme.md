# Python Url Checker — Telegram Bot

Send the bot a `wordlist.txt` and a `linklist.txt` (links containing a `(Word)` placeholder). It tests
**every link with every word** — up to **10,000,000 combinations per run** — and sends you each working link
**with its picture and the date it was uploaded, in your own country's time**.

## Files

| File | What it is |
|---|---|
| `bot.py` | The Telegram bot — **run this one** |
| `checker.py` | The fast checking engine |
| `access.py` | Users, permissions and the daily statistics |
| `countries.py` | Countries, time zones, time formatting |
| `ui.py` | Every message the bot sends (one consistent look) |
| `requirements.txt`, `.env.example` | Packages / settings template |
| `Procfile`, `railway.json`, `.python-version` | Make Railway start the bot correctly (Python 3.12) |

Keep all `.py` files in the same folder.

## Setup

1. Create a bot with **@BotFather** → copy the token. Find your own ID with **@userinfobot**.
2. ```bash
   pip install -r requirements.txt
   cp .env.example .env      # set BOT_TOKEN and put YOUR user ID in ALLOWED_CHAT_IDS
   python bot.py
   ```
3. **Open your bot and press Start once** — that is how it can send you access requests.
   Keep it running 24/7 with `tmux`/`screen`, `pm2` or a `systemd` service.

## Deploy on Railway

1. Put this folder in a **GitHub repo** (the included `.gitignore` keeps your `.env` out — never upload your token)
   → Railway → **New Project → Deploy from GitHub repo**. The files must be at the **repo root**.
2. **Variables** tab — add:
   * `BOT_TOKEN` = the token from @BotFather
   * `ALLOWED_CHAT_IDS` = your own Telegram user ID (this makes you the admin)
   * optional on small plans: `PROCESSES=2` and `CONCURRENCY=500`
3. **Add a Volume** (service → *Volumes* → *New*) with mount path `/data`.
   Without it Railway wipes the disk on every deploy, so **approvals, countries and history would be lost**.
   The bot detects the volume by itself (or set `DATA_DIR=/data`).
4. Keep **replicas = 1**. Start command is already set by `railway.json` (`python -u bot.py`).
5. Open the bot in Telegram and press **Start** (so it can send you access requests).

**The first lines of the deploy log tell you if everything is fine:**
```
Python 3.12.x ... | CPUs: ...
python-telegram-bot 21.6 | aiohttp 3.x | uvloop 0.x | tzdata ...
Data folder: /data
BOT_TOKEN: set | admins: 1 | allowed groups: 0
Python Url Checker bot is starting...
```
**Test the setup without Telegram:** in Railway's shell run `python bot.py --selfcheck`. It checks the token, the admin ID,
the data folder and that the checker worker process starts, and prints `[ ok ]` / `[FAIL]` for each.

| What you see in the log | Meaning / fix |
|---|---|
| `BOT_TOKEN is not set` / `looks wrong` | Add / re-copy `BOT_TOKEN` in *Variables* (quotes are fine, spaces are not) |
| `Unauthorized` / `Telegram rejected BOT_TOKEN` | Token revoked or wrong — copy it again from @BotFather |
| `Conflict: terminated by other getUpdates request` | **Two copies** of the bot are running (your PC, an old deployment, 2 replicas). Stop the others |
| `WARNING: running on Railway WITHOUT a Volume` | Add the Volume at `/data` (step 3) |
| Build fails while installing packages | Set variable `NIXPACKS_PYTHON_VERSION=3.12` (or `RAILPACK_PYTHON_VERSION=3.12`) and redeploy |
| "No start command" / cannot find `bot.py` | Files must be at the repo root; keep `railway.json` and `Procfile` |
| Out of memory / restarts during big checks | `PROCESSES=2`, `CONCURRENCY=500`, or a larger plan |
| `No admin configured` | Put **your own** user ID (positive number) in `ALLOWED_CHAT_IDS` |

## Who can use the bot

* **You (admin)** — every personal ID in `ALLOWED_CHAT_IDS` / `ADMIN_IDS` — can do everything.
* **Everybody else needs your approval first.** When someone presses Start they choose their country, and you get a card:

  ```
  🔔 New access request
  👤 Rahim (@rahim)   🆔 123456789   🌍 🇮🇳 India
  [✅ Allow all] [🎛 Choose commands] [⛔ Deny]
  ```
* **Per-command permissions.** *Choose commands* opens a panel where you tap exactly what the user may do:
  `/check` (+ uploads, `/cancel`), `/addword`, `/test`, `/found`, `/autocheck` (+ `/stopautocheck`, `/notify`), `/status` (+ `/stats`).
  Only `/test`? Tap only `/test`. Everything? *Allow all*. The user is told when their access changes, and `/help`
  shows each user only the commands they have. Taking away access also stops what they had running.

| Admin command | What it does |
|---|---|
| `/users` | Everyone, their country and access (tap a name to edit) |
| `/user ID` | Open one user's permission panel (works for IDs that never wrote to the bot) |
| `/grant ID all` · `/grant ID test found` | Give access by text |
| `/revoke ID` · `/revoke ID test` | Block a user · remove some commands |
| `/broadcast TEXT` | Message every user (see below) |
| `/allow` · `/disallow` · `/allowed` | Allow / remove a **group** (`/allow ID` with a positive ID gives that user full access) |
| `/resetstats` | Reset today's statistics for this chat |

User-management commands only work in a private chat with the bot.

## Country and time

On first `/start` every user picks a country (🇧🇩 Bangladesh, 🇮🇳 India, 🇮🇷 Iran, 🇻🇳 Vietnam, 🇱🇰 Sri Lanka, 🇹🇭 Thailand,
🇧🇷 Brazil … 70 time zones, paged buttons). Not listed? `/country Your Country`, `/country Asia/Dhaka` or `/country UTC+5:30`.
From then on **every time the bot shows is in that time zone, 12-hour clock**: the file's *Last modified* date, check
times, status, the statistics reset time. Change it any time with `/country`. In a group the *group admin* sets the
group's time zone with `/country`.

## Daily statistics

`/stats` shows today's checks, links checked, links found, unverified, best speed, checking time, auto-check cycles,
tests and alerts, plus yesterday's totals. **Every day at 04:00 AM Bangladesh time the numbers reset** — each user
sees that moment in *their own* time (for example 03:30 AM in India, 01:30 AM in Iran, 05:00 AM in Vietnam) with a countdown.
Admins also see the total across all users.

## Broadcast

`/broadcast Your message` (or reply to any message with `/broadcast` to copy it) shows a **preview** and the number of
recipients; nothing is sent until you press **Send**. Everyone who is not blocked receives it; the report tells you
how many were delivered and how many failed (users who blocked the bot).

## Using the checker

1. Send `wordlist.txt` (one word per line) and `linklist.txt` (one link per line, `(Word)` marks the word) **as files**
   (names just need to contain `wordlist` / `linklist`; max 20 MB each).
2. `/check` — live progress, speed and time left. `/cancel` stops it.

Every working link arrives as its picture with this caption:

```
🆕 NEW Working Link Found!
🔗 Open link
https://.../1750x1070_M1917ShadowRing_en.jpg

📅 Last modified · India Time
Mon, 09 Feb 2026 - 12:00 PM
🕒 8 months ago

🖼 JPEG · 1750×1070 px · 📦 1.4 MB
```

| Command | What it does |
|---|---|
| `/check` / `/cancel` | Check everything / stop |
| `/addword W1 W2 …` | Add words and check **only those** against all links |
| `/test LINK` | Detailed inspection of one link (below) |
| `/found` | Every working link found so far, newest first (`/found clear`) |
| `/autocheck N` / `/stopautocheck` | Re-check every N minutes (1–10); resumes after a restart; status update every 30 min |
| `/notify all` / `/notify new` | Auto-check alerts: every time, or only for new links |
| `/status`, `/stats` | Live status, daily statistics |
| `/country`, `/id`, `/help` | Time zone, IDs, your personal command list |

### /test

A premium report for one link: verdict (✅ working / ❌ not working and why), HTTP status with its meaning, content type,
real image format **and resolution**, size, response time, server/CDN, redirect target, *Last modified* in your time with
"3 hours ago", and a **server honesty check** (what the server answers for a link that cannot exist). Buttons: 🔗 *Open link*, 🔁 *Test again*.

## What counts as "working"

The file must **really be there**: a real image for `.jpg/.png/.gif/.webp` links, and not identical to what the server
returns for a link that surely doesn't exist (placeholders, error pages, redirects to a default picture are rejected).
If an abnormal share (>25%) looks "working", the bot warns you and doesn't save them. Nothing is skipped: network
errors/429/5xx are retried, and anything still unverified is reported and listed in a file.

## Speed

One tiny request per link, several worker processes, automatic speed control when a server rate-limits, `uvloop`/`aiodns`
when installed, found images downloaded in the background. Real speed depends on your server and the target server.

## Groups

1. Add the bot to the group, send `/id` there → the group ID (negative).
2. Allow it: put the ID in `ALLOWED_CHAT_IDS` and restart, **or** send `/allow` in the group as an admin.
3. Members still need your approval (users, not groups, get permissions). To upload files in a group make the bot a group
   admin or disable privacy in @BotFather (`/setprivacy` → Disable). Telegram allows ~20 bot messages per minute per
   group, so many finds arrive slower there — nothing is lost (a results file has all of them).

## Troubleshooting

* **Nobody gets approval requests** → the admin must press Start in the bot once; the user's next message retries.
* **Fake or missing results?** `/test <one link>` shows what the server really answers.
* **Many "could not verify"** → the server is rate-limiting; lower `CONCURRENCY`.

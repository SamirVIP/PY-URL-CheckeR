# Python Url Checker — Telegram Bot

A Telegram bot that takes a `wordlist.txt` and a `linklist.txt` (links
containing a `(Word)` placeholder), builds every link×word combination,
checks them all **fast** (concurrently, with `asyncio` + `aiohttp`), and
messages you the moment a link is actually working — with the image
attached. It can also loop automatically every 1–10 minutes and only
pings you about *newly* found working links.

Only chat IDs you allow can use it.

## 1. Create your bot

1. Open Telegram, message **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot` and follow the prompts.
3. BotFather gives you a token like `123456789:AAExampleTokenGoesHere` — copy it.

## 2. Find your chat ID

1. Message **[@userinfobot](https://t.me/userinfobot)** on Telegram — it replies with your numeric ID.
2. (You can allow multiple chat IDs — e.g. yourself + a group.)

## 3. Install & configure

```bash
cd python-url-checker-bot
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```
BOT_TOKEN=123456789:AAExampleTokenGoesHere
ALLOWED_CHAT_IDS=111111111,222222222
```

## 4. Run it

```bash
python bot.py
```

Leave this running (use a VPS, a small server, `screen`/`tmux`, `pm2`,
or a systemd service so it keeps running in the background 24/7).

## 5. Use it in Telegram

1. Send `/start` to your bot.
2. Send `wordlist.txt` as a **file/document** (not pasted text) — one word per line:
   ```
   ShadowRing
   PowlerRing
   ```
3. Send `linklist.txt` as a file — one URL per line, `(Word)` marks where each word goes:
   ```
   https://dl.dir.freefiremobile.com/common/Local/BD/Splashanno/1750x1070_M1917(Word)_en.jpg
   https://dl.dir.freefiremobile.com/common/Local/BD/Splashanno/1750x1070_G36(Word)_en.jpg
   ```
   (Sample files matching your example are included: `sample_wordlist.txt`, `sample_linklist.txt`.)
4. Send `/check` — the bot builds every combination and checks them all
   at once, then reports any that are working (200 OK), with the image.
5. To make it keep checking automatically, send e.g. `/autocheck 5` —
   it will re-check everything every 5 minutes and message you whenever
   a **new** working link appears. Stop it any time with `/stopautocheck`.

### All commands

| Command | What it does |
|---|---|
| `/start` | Welcome message |
| `/help` | Full instructions |
| `/check` | Check every link×word combination right now, with **live progress** — guarantees every working link found gets sent to you |
| `/cancel` | Stop a `/check` that's currently running (also works as `/cancle`) |
| `/autocheck N` | Auto re-check every N minutes (1–10) |
| `/stopautocheck` | Stop the automatic loop and its 30-minute status updates |
| `/resetstats` | Clear the "working links seen" counter shown in `/status` |
| `/status` | Show how many words/links are loaded, combo count, autocheck state, cycle stats |

## How checking & notifications work

- **Every** working link (HTTP 200, and not an HTML "not found" page in
  disguise) is queued the instant it's confirmed and sent by a single
  dedicated sender — this guarantees every one gets delivered, even
  when dozens are found in the same second, instead of firing them all
  at once and risking some getting silently dropped by Telegram's rate
  limits. If Telegram briefly rate-limits a send, it's retried
  automatically rather than lost.
- Notifications are sent as **plain text** (no Markdown formatting).
  Earlier versions used Markdown, and Telegram treats a single
  underscore (`_`) as an italics marker — a real filename like
  `..._en.jpg` has an odd number of underscores, which made Telegram
  reject the whole message and silently drop it. Plain text avoids that
  entirely, for underscores or any other character that shows up in a link.
- `/check` shows a live progress message that updates every few seconds:
  `Progress: 340/1000 — Working found so far: 2`, and can be stopped
  any time with `/cancel` — anything already found up to that point
  still gets sent before it stops.
- `/autocheck N` re-checks the full combination list every N minutes,
  and messages you **every time** it finds a working link — every
  cycle, even if it already reported that same link before. (If a link
  stays live across multiple cycles, you'll get repeated notifications
  — that's intentional, so you never miss a "still working" confirmation.)
- Separately, while autocheck is on, a **status update is sent every 30
  minutes** — independent of your check interval — summarizing cycles
  run, last check size, and total unique working links seen so far.

## Notes on speed & reliability

- Checks run concurrently (`MAX_CONCURRENT_REQUESTS`, default 150 at a
  time) using real `GET` requests. Earlier versions used `HEAD` first,
  which some CDNs answer inconsistently versus a real `GET` — that was
  causing genuinely live links to be missed. `GET` is what actually
  matters, so that's what's used to decide "working."
- If a server starts blocking you for going too fast, lower
  `MAX_CONCURRENT_REQUESTS` in `.env`.
- Each chat's uploaded files are stored under `data/<chat_id>/`, so they
  survive a bot restart.

## Notes on security

- The bot refuses to respond to any chat ID not listed in
  `ALLOWED_CHAT_IDS` — it just tells them their chat ID and stops there.
- If `ALLOWED_CHAT_IDS` is left empty, the bot allows **nobody** (safe
  default) — you must configure it before use.

## Deploying so it runs 24/7

Any small VPS works. A couple of simple options:

**tmux / screen** (quick and simple):
```bash
tmux new -s urlchecker
python bot.py
# press Ctrl+B then D to detach; it keeps running
```

**systemd service** (auto-restarts on crash/reboot) — create
`/etc/systemd/system/urlchecker.service`:
```ini
[Unit]
Description=Python Url Checker Telegram Bot
After=network.target

[Service]
WorkingDirectory=/path/to/python-url-checker-bot
ExecStart=/usr/bin/python3 bot.py
Restart=always
EnvironmentFile=/path/to/python-url-checker-bot/.env

[Install]
WantedBy=multi-user.target
```
Then:
```bash
sudo systemctl enable --now urlchecker
```

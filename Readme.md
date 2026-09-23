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
| `/check` | Check every link×word combination right now |
| `/autocheck N` | Auto re-check every N minutes (1–10) |
| `/stopautocheck` | Stop the automatic loop |
| `/resetfound` | Forget which links were already reported, so they can be reported again |
| `/status` | Show how many words/links are loaded, combo count, autocheck state |

## Notes on speed

- Checks run concurrently (`MAX_CONCURRENT_REQUESTS`, default 200 at a
  time), using a fast `HEAD` request first and only falling back to
  `GET` if a server doesn't support `HEAD`.
- If a server starts blocking you for going too fast, lower
  `MAX_CONCURRENT_REQUESTS` in `.env`.
- The bot avoids repeat-spamming: during automatic loop checks it only
  messages you about links that are newly found (not ones it already
  reported). Use `/resetfound` if you want it to re-announce everything.
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

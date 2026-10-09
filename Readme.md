# Python Url Checker — Telegram Bot

Send the bot a `wordlist.txt` and a `linklist.txt` (links containing a
`(Word)` placeholder). It tests **every link with every word** — up to
**600,000 combinations per run** — at high speed, sends you every working
link with its image, shows live progress with speed and time left, and can
repeat the whole check automatically every 1–10 minutes.

Works in private chats **and Telegram groups** (only chats you allow).

## Files

| File | What it is |
|---|---|
| `bot.py` | The Telegram bot (run this one) |
| `checker.py` | The fast checking engine (used by `bot.py`, keep it in the same folder) |
| `requirements.txt` | Python packages |
| `.env.example` | Settings template → copy to `.env` |
| `sample_wordlist.txt`, `sample_linklist.txt` | Example input files |

## Setup

1. Create a bot with **@BotFather** → copy the token.
2. Find your own ID with **@userinfobot**.
3. Install and configure:
   ```bash
   pip install -r requirements.txt
   cp .env.example .env      # then edit .env (BOT_TOKEN, ALLOWED_CHAT_IDS)
   python bot.py
   ```
   Keep it running 24/7 with `tmux`/`screen`, `pm2` or a `systemd` service.

## Using it

1. Send `wordlist.txt` as a **file** — one word per line.
2. Send `linklist.txt` as a **file** — one link per line, `(Word)` marks where the word goes:
   ```
   https://dl.dir.freefiremobile.com/common/Local/BD/Splashanno/1750x1070_M1917(Word)_en.jpg
   ```
   (`(word)`/`(WORD)` also work. Files can have any name containing `wordlist` / `linklist`,
   e.g. `wordlist (1).txt`. Max 20 MB each — Telegram's limit for bots.)
3. Send `/check`.

Live progress example:
```
🔎 Checking links...
████████░░░░░░░░░░░░ 41.3%
Checked: 247,800 / 600,000
Working found: 3
Speed: 2,950 links/sec
Elapsed: 1m 24s
Time left: ~1m 59s
```

### Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Welcome / help |
| `/check` | Check every link with every word (live progress + time left) |
| `/cancel` (or `/cancle`) | Stop the running check — everything found so far is still delivered |
| `/autocheck N` | Re-check automatically every N minutes (1–10) |
| `/stopautocheck` | Stop automatic checking |
| `/status` | What's loaded / running, progress, time left |
| `/resetstats` | Clear statistics |
| `/id` | Show this chat's ID |
| `/allow [id]`, `/disallow [id]`, `/allowed` | (admin) manage allowed chats |

## Groups

1. Add the bot to your group.
2. Send `/id` in the group — it shows the group ID (a negative number like `-1001234567890`).
3. Allow it, either way:
   - add the ID to `ALLOWED_CHAT_IDS` in `.env` and restart, **or**
   - as an admin (any personal ID in `ALLOWED_CHAT_IDS`), just send `/allow` inside the group. No restart needed.
4. **To upload the word/link files inside a group**, the bot must be able to see them:
   either make the bot a group admin, or in @BotFather run `/setprivacy` → *Disable*
   (then remove and re-add the bot to the group).
   Each chat keeps its own lists, so upload the files in the chat where you run `/check`.

Everyone in an allowed group can use the bot. Telegram limits bots to ~20 messages per minute in a
group, so when many links are found at once they arrive a bit slower there — nothing is lost (a
`working_links_….txt` file with the **complete** list is also sent when the check ends).
Anonymous group admins can't use `/allow` (Telegram hides their identity); use `.env` instead.

## How it is fast

* **Several worker processes** (one per CPU core, max 4) — the checking uses all your cores and never slows the bot down.
* **uvloop** and **aiodns** are used automatically when installed.
* Connections are **reused** and only one byte of each file is requested (nothing is downloaded).
* Links are generated on the fly, so 600,000 combinations use very little memory.

Real speed depends mostly on your server and the target server. If it starts rejecting you
(many errors/429), lower `CONCURRENCY` in `.env`; if your CPU is idle you can raise it.

## How it makes sure nothing is missed

* Every link × every word is checked. The check summary shows `links × words = total` and `Checked: N`.
* Network errors, timeouts, HTTP 429 and 5xx are **never** treated as "not found": each link is retried
  with back-off, and anything still failing is re-checked in extra rounds at the end.
* If a link still can't be verified, it is reported (`Could not verify: N`) and listed in
  `unverified_links_….txt` — it is never silently counted as "not working".
* If the server blocks you completely (thousands of errors in a row), the run stops early and says so,
  instead of running for hours.
* Blank lines, duplicates, Windows line endings and a UTF-8 BOM at the start of a file are handled
  (a BOM used to corrupt the first word).
* Every working link is sent as its own message with the image (plain text, so underscores `_` in
  links are safe). If Telegram can't fetch an image, the link is sent as text instead.
  At the end you also get `working_links_….txt` with all of them.

## Auto-check

`/autocheck 5` re-checks everything every 5 minutes and messages you **every time** a working link
is found (every cycle). A status update is sent every 30 minutes (cycles done, last result, and the
progress/time-left of a cycle that is currently running). If a check takes longer than the interval,
the next one waits — checks never overlap. Note: auto-check is not remembered after the bot restarts.

## Troubleshooting

* **`/cancel` seems slow** → it stops the workers within a second; the summary message then appears.
* **Nothing found but you expect links** → try a quick manual test of one URL in a browser; check the
  `(Word)` placeholder spelling and that your files were loaded (`/status`).
* **Many "could not verify"** → the server is rate-limiting you; lower `CONCURRENCY`.

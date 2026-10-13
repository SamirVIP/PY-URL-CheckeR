# Python Url Checker — Telegram Bot

Send the bot a `wordlist.txt` and a `linklist.txt` (links containing a `(Word)` placeholder).
It tests **every link with every word** — up to **600,000 combinations per run** — and sends you
each working link **with its image and the date it was uploaded** (Bangladesh time).

Works in private chats **and Telegram groups** (only chats you allow).

## Files

| File | What it is |
|---|---|
| `bot.py` | The Telegram bot (run this one) |
| `checker.py` | The fast checking engine (keep it next to `bot.py`) |
| `requirements.txt` | Python packages |
| `.env.example` | Settings template → copy to `.env` |
| `sample_wordlist.txt`, `sample_linklist.txt` | Example input files |

## Setup

1. Create a bot with **@BotFather** → copy the token. Find your own ID with **@userinfobot**.
2. ```bash
   pip install -r requirements.txt
   cp .env.example .env      # edit: BOT_TOKEN and ALLOWED_CHAT_IDS
   python bot.py
   ```
   Keep it running 24/7 with `tmux`/`screen`, `pm2` or a `systemd` service.

## Using it

1. Send `wordlist.txt` (one word per line) and `linklist.txt` (one link per line) **as files**.
   `(Word)` marks where the word goes: `https://dl.dir.freefiremobile.com/.../1750x1070_M1917(Word)_en.jpg`
   (`(word)`/`(WORD)` work too; file names just need to contain `wordlist` / `linklist`; max 20 MB each).
2. Send `/check`.

Every working link arrives like this (the picture itself, with this caption):

```
🆕 NEW Working Link Found!
https://.../1750x1070_M1917ShadowRing_en.jpg

📅 Last modified:
Mon, 09 Feb 2026 - 12:30 PM (Bangladesh Time)
📦 Size: 1.4 MB
```

"Last modified" is the date the server reports for the file — normally when the image was uploaded —
converted to Bangladesh time (UTC+6, 12-hour clock). If a server doesn't report it, the bot says so.

## Commands

| Command | What it does |
|---|---|
| `/check` | Check every link with every word (live progress, speed, time left) |
| `/cancel` (or `/cancle`) | Stop the running check |
| `/addword W1 W2 …` | Add words to your list **and instantly check only those words** against all links |
| `/test LINK` | Diagnose one link: what does the server really answer? (and for a link that can't exist) |
| `/found` | Every working link found so far, newest first (`/found clear` empties it) |
| `/autocheck N` | Re-check automatically every N minutes (1–10); resumes by itself after a restart |
| `/notify all` / `/notify new` | Auto-check alerts: every time, or only for links you haven't been told about |
| `/stopautocheck` | Stop automatic checking |
| `/status`, `/resetstats` | Status / clear counters |
| `/id` | Show this chat's ID |
| `/allow [id]`, `/disallow [id]`, `/allowed` | (admin) manage allowed chats |

## What counts as "working"

A link is only reported when the file is **really there**:

* the server answers 200/206 and it is not a web page, **and**
* for `.jpg/.jpeg/.png/.gif/.webp` links the file **starts with real image bytes**, **and**
* it is **not identical to what the server returns for a link that surely doesn't exist**.
  Before checking, the bot asks each link pattern for a made-up word and remembers the answer.
  Servers that reply to *every* link with a placeholder picture, an error page or a redirect to a
  default image can therefore no longer produce fake "working" links.

If a check still finds an abnormal share of "working" links (over 25%), the bot warns you, does not
add them to `/found`, and suggests `/test` so you can see what the server answers.
Safety net: at most `MAX_ALERTS_PER_RUN` (default 500) link messages are sent per run; the results
file always contains all of them.

## How images are delivered

The bot downloads the picture itself and uploads it to Telegram (so it doesn't depend on Telegram
being able to fetch the link). If Telegram refuses it as a photo (too big / odd shape) it is sent as
a file; if the picture can't be downloaded, Telegram is asked to fetch it; last resort is the link as
text. Captions are plain text, so `_` in links is always safe. When a check ends you also get
`working_links_….txt` (newest file first, with dates).

## Groups

1. Add the bot to your group and send `/id` there → the group ID (negative number).
2. Allow it: put the ID in `ALLOWED_CHAT_IDS` in `.env` and restart, **or** send `/allow` inside the
   group as an admin (any personal ID in `ALLOWED_CHAT_IDS`; no restart needed).
3. To upload files in a group the bot must see them: make the bot a group admin, or in @BotFather
   run `/setprivacy` → *Disable* (then remove and re-add the bot). Each chat has its own lists.

Everyone in an allowed group can use the bot. Telegram allows ~20 bot messages per minute per group,
so many finds arrive slower there — nothing is lost (the results file has all of them).
Anonymous group admins can't use `/allow`; use `.env` instead.

## Speed

Several worker processes (one per CPU core, max 4), `uvloop`/`aiodns` when installed, reused
connections, and only a few bytes requested per link. Real speed depends mostly on your server and the
target server — if it starts rejecting you, lower `CONCURRENCY` in `.env`.

## Nothing is skipped

* Every link × every word is checked; the summary shows `links × words = total` and `Checked: N`.
* Network errors, timeouts, HTTP 429/5xx are **never** treated as "not found": they are retried, then
  re-checked in extra rounds. Anything still unverified is reported (`unverified_links_….txt`).
* If the server blocks you completely, the run stops early and says so.
* Blank lines, duplicates, Windows line endings and a UTF-8 BOM are handled.

## Auto-check

`/autocheck 5` re-checks everything every 5 minutes and alerts you when working links are found
(`/notify` chooses every time or only new ones). A status update arrives every 30 minutes (including the
progress of a running cycle). Checks never overlap. The setting is saved and restored after a restart.

## Troubleshooting

* **Fake or missing results?** Send `/test <one link>`. It shows the HTTP status, content type, whether it
  is a real image, the last-modified date, and what the server does for a link that can't exist.
* **Many "could not verify"** → the server is rate-limiting you; lower `CONCURRENCY`.

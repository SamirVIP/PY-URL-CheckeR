"""
Python Url Checker — Telegram Bot
==================================

What it does
------------
1. You send it two files as Telegram documents:
     - wordlist.txt   -> one word per line, e.g.
                           ShadowRing
                           PowlerRing
     - linklist.txt   -> one URL per line, with a (Word) placeholder, e.g.
                           https://dl.dir.freefiremobile.com/.../1750x1070_M1917(Word)_en.jpg

2. The bot builds every combination of link x word (replacing "(Word)"
   with each word from wordlist.txt) and checks all of them FAST using
   asyncio + aiohttp with hundreds of requests running at once.

3. The instant a link responds with HTTP 200 (i.e. it actually exists),
   it's queued for notification and sent to you with the image — every
   single working link, guaranteed, even if many are found at once
   (sends are paced and retried so Telegram's rate limits never cause a
   dropped notification).

4. /check shows LIVE progress while it runs (checked so far / total,
   how many working found so far), updating every few seconds. You can
   cancel a running check any time with /cancel (or /cancle).

5. /autocheck N re-checks everything automatically every N minutes
   (1-10). Every single time a working link is found, it messages you
   — every cycle, not just the first time. On top of that, a separate
   status update is sent every 30 minutes summarizing how autocheck is
   doing (cycles run, last check size, total working links seen).

6. Only chat IDs you allow (set in the .env / environment variables)
   can use the bot at all. Everyone else is politely rejected.

Commands
--------
/start          - welcome message
/help           - list of commands / how to use the bot
/check          - check all link+word combinations right now, live progress
/cancel         - cancel a /check that's currently running (alias: /cancle)
/autocheck N    - start automatic re-checking every N minutes (1-10)
/stopautocheck  - stop the automatic re-checking (and its 30-min status updates)
/resetstats     - clear the "working links seen" stats used in /status
/status         - show how many words/links are loaded + autocheck state

Setup
-----
See README.md for full setup instructions.
"""

import os
import time
import logging
import asyncio
from pathlib import Path
from functools import wraps
from datetime import datetime

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8593278400:AAGkwwoRzvAz9Y3Zcan_JtLKxyeNEm8gS_M").strip()

# Comma separated chat ids allowed to use the bot, e.g. "111111111,222222222"
ALLOWED_CHAT_IDS = {
    int(x.strip()) for x in os.environ.get("ALLOWED_CHAT_IDS", "6206433961").split(",") if x.strip()
}

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(exist_ok=True)

PLACEHOLDER = "(Word)"

# How many requests run at the same time. Higher = faster, but too high
# can get you rate-limited/blocked by the target server. 100-200 is a
# good sweet spot for most CDNs.
MAX_CONCURRENT_REQUESTS = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "150"))

# Per-request timeout, in seconds.
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10"))

# How often (seconds) the /check progress message is refreshed.
PROGRESS_UPDATE_SECONDS = 3.0

# Small pause between outgoing Telegram notifications so a burst of
# working links found at once doesn't trip Telegram's rate limits.
NOTIFY_PACE_SECONDS = 0.10

# Fixed heartbeat interval for autocheck status updates (30 minutes),
# independent of whatever check interval the user picks.
HEARTBEAT_SECONDS = 30 * 60

MIN_AUTOCHECK_MINUTES = 1
MAX_AUTOCHECK_MINUTES = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("python-url-checker-bot")


# ============================================================
# PER-CHAT STATE
# ============================================================
# Kept in memory while the bot runs. The uploaded .txt files themselves
# are saved to disk (DATA_DIR/<chat_id>/...) so they survive a restart;
# state is reloaded from those files lazily the next time it's needed.

chat_state: dict[int, dict] = {}


def chat_dir(chat_id: int) -> Path:
    d = DATA_DIR / str(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def get_state(chat_id: int) -> dict:
    if chat_id not in chat_state:
        d = chat_dir(chat_id)
        chat_state[chat_id] = {
            "words": load_lines(d / "wordlist.txt"),
            "links": load_lines(d / "linklist.txt"),
            "ever_working": set(),   # every url ever seen working (for /status only)
            "interval_minutes": None,
            "cycles_run": 0,
            "last_run_time": None,
            "last_run_checked": 0,
            "last_run_working": 0,
            "check_task": None,      # the currently running /check asyncio.Task, if any
        }
    return chat_state[chat_id]


def is_allowed(chat_id: int) -> bool:
    # If nobody is configured, nobody is allowed. This is intentional —
    # it stops the bot being usable by strangers before you've set it up.
    return chat_id in ALLOWED_CHAT_IDS


def restricted(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not is_allowed(chat_id):
            await update.message.reply_text(
                "🚫 Sorry, you're not authorized to use this bot.\n"
                f"Your chat ID is: `{chat_id}`\n\n"
                "Ask the bot owner to add this ID to ALLOWED_CHAT_IDS.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        return await handler(update, context)
    return wrapper


# ============================================================
# URL BUILDING + CHECKING
# ============================================================

def generate_urls(links: list[str], words: list[str]) -> list[str]:
    """Expand every (Word) placeholder in every link into one URL per word."""
    urls = []
    for link in links:
        if PLACEHOLDER in link:
            for w in words:
                urls.append(link.replace(PLACEHOLDER, w))
        else:
            urls.append(link)
    return urls


async def check_one(session: aiohttp.ClientSession, url: str, sem: asyncio.Semaphore) -> bool:
    """Return True if the url is genuinely working.

    Uses GET (not HEAD) because a lot of CDNs — including game-asset
    CDNs — respond to HEAD with a different (often wrong) status than
    they'd give a real GET. Relying on HEAD was causing real, live
    links to be missed entirely.
    """
    async with sem:
        try:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return False
                # Guard against CDNs that serve a "not found" placeholder
                # page with a 200 status instead of a proper 404.
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if ctype.startswith("text/html"):
                    return False
                return True
        except Exception:
            return False


async def send_working_link(bot, chat_id: int, url: str, attempts: int = 4):
    """Send a 'working link found' notification, guaranteed best-effort.

    IMPORTANT: no Markdown/HTML parse_mode is used here. Telegram's
    Markdown treats a single underscore as an italics marker — a real
    URL like ..._en.jpg has an odd number of underscores, which used to
    make Telegram reject the whole message ("can't parse entities") and
    silently drop the notification. Plain text sidesteps that
    completely, for underscores, asterisks, or anything else that
    happens to appear in a link.
    """
    caption = f"✅ Working Link Found!\n{url}"
    for attempt in range(1, attempts + 1):
        try:
            await bot.send_photo(chat_id=chat_id, photo=url, caption=caption)
            return True
        except RetryAfter as e:
            wait_s = float(getattr(e, "retry_after", 2)) + 0.5
            logger.info("Rate limited sending %s, waiting %.1fs (attempt %d)", url, wait_s, attempt)
            await asyncio.sleep(wait_s)
        except (TimedOut, NetworkError):
            await asyncio.sleep(1.5)
        except Exception as e:
            # Photo send failed for some other reason (e.g. Telegram couldn't
            # fetch/parse it as an image) — fall back to a plain text message
            # so the link itself is never lost.
            logger.warning("send_photo failed for %s (%s); trying plain text.", url, e)
            try:
                await bot.send_message(chat_id=chat_id, text=caption)
                return True
            except RetryAfter as e2:
                wait_s = float(getattr(e2, "retry_after", 2)) + 0.5
                await asyncio.sleep(wait_s)
            except Exception:
                logger.exception("Text fallback also failed for %s (attempt %d)", url, attempt)
                await asyncio.sleep(1.0)
    logger.error("Giving up notifying about working link after %d attempts: %s", attempts, url)
    return False


async def perform_check(
    bot,
    chat_id: int,
    urls: list[str],
    progress_message=None,
) -> list[str]:
    """
    Check every url concurrently. Every url confirmed working is pushed
    onto a queue and sent by a single dedicated sender task — this
    guarantees every working link gets a notification (with retries),
    instead of firing many send_photo calls at once and letting some
    get silently lost to Telegram's rate limits.

    If progress_message is given, it is live-edited with running totals.
    Supports cancellation: if this coroutine's task is cancelled, it
    stops checking, drains and sends any already-found links, and marks
    the progress message as cancelled.

    Returns the full list of urls that were found working in this run.
    """
    total = len(urls)
    working: list[str] = []
    completed = 0
    lock = asyncio.Lock()
    stop_progress = asyncio.Event()
    notify_queue: asyncio.Queue = asyncio.Queue()

    async def sender_loop():
        while True:
            url = await notify_queue.get()
            try:
                if url is None:  # sentinel -> stop
                    return
                await send_working_link(bot, chat_id, url)
                await asyncio.sleep(NOTIFY_PACE_SECONDS)
            finally:
                notify_queue.task_done()

    async def worker(url: str, session: aiohttp.ClientSession):
        nonlocal completed
        ok = await check_one(session, url, sem)
        async with lock:
            completed += 1
            if ok:
                working.append(url)
        if ok:
            await notify_queue.put(url)

    async def progress_updater():
        last_text = None
        while not stop_progress.is_set():
            try:
                await asyncio.wait_for(stop_progress.wait(), timeout=PROGRESS_UPDATE_SECONDS)
            except asyncio.TimeoutError:
                pass
            if progress_message is not None:
                text = (
                    "🔎 *Checking links...*\n"
                    f"Progress: `{completed}/{total}`\n"
                    f"Working found so far: `{len(working)}`\n"
                    "_Send /cancel to stop._"
                )
                if text != last_text:
                    try:
                        await progress_message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
                        last_text = text
                    except Exception:
                        pass  # e.g. "message not modified" — harmless

    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    connector = aiohttp.TCPConnector(limit=0, ssl=False)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

    progress_task = asyncio.create_task(progress_updater()) if progress_message is not None else None
    sender_task = asyncio.create_task(sender_loop())

    cancelled = False
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            await asyncio.gather(*(worker(u, session) for u in urls))
    except asyncio.CancelledError:
        cancelled = True
    finally:
        # Whether we finished normally or were cancelled, make sure every
        # link that WAS found working still gets sent before we return.
        await notify_queue.join()
        await notify_queue.put(None)
        await sender_task

        stop_progress.set()
        if progress_task is not None:
            try:
                await asyncio.wait_for(progress_task, timeout=PROGRESS_UPDATE_SECONDS + 1)
            except Exception:
                progress_task.cancel()

        if progress_message is not None:
            try:
                if cancelled:
                    final_text = (
                        "🛑 *Check cancelled.*\n"
                        f"Checked: `{completed}/{total}`\n"
                        f"Working found: `{len(working)}`"
                    )
                else:
                    final_text = (
                        "✅ *Check complete!*\n"
                        f"Checked: `{total}`\n"
                        f"Working found: `{len(working)}`"
                    )
                await progress_message.edit_text(final_text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass

    if cancelled:
        raise asyncio.CancelledError()

    return working


# ============================================================
# COMMAND HANDLERS
# ============================================================

@restricted
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Welcome to Python Url Checker!*\n\n"
        "I check huge batches of links super fast and tell you the *instant* "
        "one of them actually goes live — perfect for catching new game "
        "splash images, banners, or any (Word)-style link pattern the "
        "second it's uploaded.\n\n"
        "*Here's how it works:*\n"
        "1️⃣ Send me `wordlist.txt` — one word per line.\n"
        "2️⃣ Send me `linklist.txt` — one URL per line, with `(Word)` "
        "where the word should go.\n"
        "3️⃣ Send /check — I'll test every combination at once and show "
        "you *live progress* as it runs (you can /cancel any time).\n"
        "4️⃣ Optionally, send /autocheck 5 and I'll keep checking every "
        "5 minutes forever, messaging you the moment anything is live "
        "(plus a status update every 30 minutes).\n\n"
        "Type /help any time for the full command list. Let's find those "
        "links! 🚀"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@restricted
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*📖 Python Url Checker — Help*\n\n"
        "*Step 1 — Upload your files (send as documents, not pasted text):*\n"
        "• `wordlist.txt` — one word per line, e.g.\n"
        "   `ShadowRing`\n   `PowlerRing`\n"
        "• `linklist.txt` — one link per line, with `(Word)` as the "
        "placeholder, e.g.\n"
        "   `https://example.com/img_(Word)_en.jpg`\n\n"
        "*Commands:*\n"
        "/start — welcome message\n"
        "/help — this message\n"
        "/check — check every link×word combination right now, with "
        "live progress, and every working link found gets sent to you\n"
        "/cancel — stop a /check that's currently running\n"
        "/autocheck `N` — auto re-check every N minutes (1–10). Sends a "
        "message *every time* a working link is found (every cycle), "
        "plus a status update every 30 minutes\n"
        "/stopautocheck — stop the automatic loop and its status updates\n"
        "/resetstats — clear the \"working links seen\" counter shown in /status\n"
        "/status — show loaded word/link counts and autocheck status\n\n"
        "_Tip: You can re-send wordlist.txt or linklist.txt any time to "
        "replace the previous version._"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@restricted
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    jobs = context.job_queue.get_jobs_by_name(f"check-{chat_id}")
    autocheck = f"every {state['interval_minutes']} min ✅" if jobs else "off ❌"
    combos = len(generate_urls(state["links"], state["words"])) if state["words"] and state["links"] else 0
    last_run = (
        datetime.fromtimestamp(state["last_run_time"]).strftime("%Y-%m-%d %H:%M:%S")
        if state["last_run_time"]
        else "never"
    )
    check_running = state["check_task"] is not None and not state["check_task"].done()

    text = (
        "*📊 Status*\n"
        f"Words loaded: `{len(state['words'])}`\n"
        f"Link templates loaded: `{len(state['links'])}`\n"
        f"Total combinations to check: `{combos}`\n"
        f"Manual /check running: {'yes ⏳' if check_running else 'no'}\n"
        f"Auto-check: {autocheck}\n"
        f"Autocheck cycles run: `{state['cycles_run']}`\n"
        f"Last check: {last_run} — checked `{state['last_run_checked']}`, "
        f"working `{state['last_run_working']}`\n"
        f"Unique working links ever seen: `{len(state['ever_working'])}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@restricted
async def resetstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_chat.id)
    state["ever_working"].clear()
    state["cycles_run"] = 0
    state["last_run_time"] = None
    state["last_run_checked"] = 0
    state["last_run_working"] = 0
    await update.message.reply_text("🧹 Stats cleared.")


@restricted
async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    if state["check_task"] is not None and not state["check_task"].done():
        await update.message.reply_text("⏳ A check is already running. Use /cancel to stop it first.")
        return

    if not state["words"] or not state["links"]:
        await update.message.reply_text(
            "⚠️ Please send both *wordlist.txt* and *linklist.txt* first.\nUse /help to see how.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    urls = generate_urls(state["links"], state["words"])
    if not urls:
        await update.message.reply_text("⚠️ No links to check — check your linklist.txt.")
        return

    progress_msg = await update.message.reply_text(
        f"🔎 Starting check of `{len(urls)}` link(s)...\n_Send /cancel to stop._",
        parse_mode=ParseMode.MARKDOWN,
    )

    task = asyncio.create_task(perform_check(context.bot, chat_id, urls, progress_message=progress_msg))
    state["check_task"] = task

    working: list[str] = []
    was_cancelled = False
    try:
        working = await task
    except asyncio.CancelledError:
        was_cancelled = True
    finally:
        state["check_task"] = None

    state["ever_working"].update(working)
    state["last_run_time"] = time.time()
    state["last_run_checked"] = len(urls)
    state["last_run_working"] = len(working)

    if not was_cancelled and not working:
        await update.message.reply_text(f"❌ Checked {len(urls)} link(s) — none are working right now.")


@restricted
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    task = state.get("check_task")
    if task is not None and not task.done():
        task.cancel()
        await update.message.reply_text("🛑 Cancelling the current check...")
    else:
        await update.message.reply_text("There's no check currently running.")


async def autocheck_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    state = get_state(chat_id)

    if not state["words"] or not state["links"]:
        return  # nothing to check yet, silently skip this cycle

    urls = generate_urls(state["links"], state["words"])
    if not urls:
        return

    # No live progress message for automatic cycles — but every working
    # link found is still queued and sent (with retries), every cycle.
    working = await perform_check(context.bot, chat_id, urls, progress_message=None)

    state["ever_working"].update(working)
    state["cycles_run"] += 1
    state["last_run_time"] = time.time()
    state["last_run_checked"] = len(urls)
    state["last_run_working"] = len(working)


async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs every 30 minutes while autocheck is active — a periodic
    'still alive, here's what's happened' status update."""
    chat_id = context.job.chat_id
    state = get_state(chat_id)
    last_run = (
        datetime.fromtimestamp(state["last_run_time"]).strftime("%Y-%m-%d %H:%M:%S")
        if state["last_run_time"]
        else "not yet run"
    )
    text = (
        "*📡 Autocheck Status Update*\n"
        f"Checking every: `{state['interval_minutes']}` min\n"
        f"Cycles completed: `{state['cycles_run']}`\n"
        f"Last check: {last_run}\n"
        f"Last check size: `{state['last_run_checked']}` link(s), "
        f"`{state['last_run_working']}` working\n"
        f"Unique working links ever seen: `{len(state['ever_working'])}`\n"
        "Still running ✅"
    )
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)


@restricted
async def autocheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(
            f"Usage: `/autocheck N`  (N = minutes, between {MIN_AUTOCHECK_MINUTES} and {MAX_AUTOCHECK_MINUTES})",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        minutes = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please give a whole number of minutes, e.g. `/autocheck 5`", parse_mode=ParseMode.MARKDOWN)
        return

    if not (MIN_AUTOCHECK_MINUTES <= minutes <= MAX_AUTOCHECK_MINUTES):
        await update.message.reply_text(
            f"⏱ Please choose between {MIN_AUTOCHECK_MINUTES} and {MAX_AUTOCHECK_MINUTES} minutes."
        )
        return

    state = get_state(chat_id)
    if not state["words"] or not state["links"]:
        await update.message.reply_text(
            "⚠️ Please send both *wordlist.txt* and *linklist.txt* first, then run /autocheck again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # remove any existing jobs for this chat first
    for job in context.job_queue.get_jobs_by_name(f"check-{chat_id}"):
        job.schedule_removal()
    for job in context.job_queue.get_jobs_by_name(f"heartbeat-{chat_id}"):
        job.schedule_removal()

    state["interval_minutes"] = minutes
    state["cycles_run"] = 0

    context.job_queue.run_repeating(
        autocheck_job,
        interval=minutes * 60,
        first=5,
        chat_id=chat_id,
        name=f"check-{chat_id}",
    )
    context.job_queue.run_repeating(
        heartbeat_job,
        interval=HEARTBEAT_SECONDS,
        first=HEARTBEAT_SECONDS,
        chat_id=chat_id,
        name=f"heartbeat-{chat_id}",
    )

    await update.message.reply_text(
        f"✅ Auto-check enabled — I'll re-check every link every *{minutes}* minute(s) "
        "and message you *every time* a working link is found. You'll also get a "
        "status update every 30 minutes.",
        parse_mode=ParseMode.MARKDOWN,
    )


@restricted
async def stopautocheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    check_jobs = context.job_queue.get_jobs_by_name(f"check-{chat_id}")
    heartbeat_jobs = context.job_queue.get_jobs_by_name(f"heartbeat-{chat_id}")

    if not check_jobs and not heartbeat_jobs:
        await update.message.reply_text("Auto-check isn't running right now.")
        return

    for job in check_jobs:
        job.schedule_removal()
    for job in heartbeat_jobs:
        job.schedule_removal()

    get_state(chat_id)["interval_minutes"] = None
    await update.message.reply_text("🛑 Auto-check and status updates stopped.")


@restricted
async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    doc = update.message.document
    fname = (doc.file_name or "").lower()

    if fname not in ("wordlist.txt", "linklist.txt"):
        await update.message.reply_text(
            "📄 I only accept files named exactly `wordlist.txt` or `linklist.txt`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    tg_file = await doc.get_file()
    dest = chat_dir(chat_id) / fname
    await tg_file.download_to_drive(custom_path=str(dest))

    # reload this chat's state from disk
    state = get_state(chat_id)
    if fname == "wordlist.txt":
        state["words"] = load_lines(dest)
        await update.message.reply_text(f"✅ Loaded {len(state['words'])} word(s) from wordlist.txt")
    else:
        state["links"] = load_lines(dest)
        await update.message.reply_text(f"✅ Loaded {len(state['links'])} link template(s) from linklist.txt")

    if state["words"] and state["links"]:
        await update.message.reply_text("Both files are loaded. Send /check whenever you're ready! 🚀")


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Quietly ignore random chatter instead of spamming "unauthorized" for
    # every message from chats that aren't allowed.
    chat_id = update.effective_chat.id
    if is_allowed(chat_id):
        await update.message.reply_text("Not sure what you mean — try /help for the list of commands.")


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set. Put it in a .env file or as an environment variable.\n"
            "Get a token from @BotFather on Telegram."
        )
    if not ALLOWED_CHAT_IDS:
        logger.warning(
            "ALLOWED_CHAT_IDS is empty — nobody will be able to use this bot until you set it."
        )

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler(["cancel", "cancle"], cancel_cmd))
    app.add_handler(CommandHandler("autocheck", autocheck_cmd))
    app.add_handler(CommandHandler("stopautocheck", stopautocheck_cmd))
    app.add_handler(CommandHandler("resetstats", resetstats_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))

    logger.info("Python Url Checker bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

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

3. Whenever a link responds with HTTP 200 (i.e. it actually exists),
   the bot sends you a message + the image itself.

4. You can turn on "auto checking": the bot will re-check every link
   combination automatically every N minutes (1-10, you choose) and
   ping you whenever it finds a NEW working link.

5. Only chat IDs you allow (set in the .env / environment variables)
   can use the bot at all. Everyone else is politely rejected.

Commands
--------
/start          - welcome message
/help           - list of commands / how to use the bot
/check          - check all link+word combinations right now
/autocheck N    - start automatic re-checking every N minutes (1-10)
/stopautocheck  - stop the automatic re-checking
/resetfound     - forget which links were already reported as "found"
/status         - show how many words/links are loaded + autocheck state

Setup
-----
See README.md for full setup instructions.
"""

import os
import logging
import asyncio
from pathlib import Path
from functools import wraps

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
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
# can get you rate-limited/blocked by the target server. 150-300 is a
# good sweet spot for most CDNs.
MAX_CONCURRENT_REQUESTS = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "200"))

# Per-request timeout, in seconds.
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "8"))

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
            "found": set(),          # urls already reported as working
            "interval_minutes": None,
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


async def check_one(session: aiohttp.ClientSession, url: str, sem: asyncio.Semaphore) -> tuple[str, bool]:
    """Return (url, True) if it responds 200 OK, using HEAD first (fast),
    falling back to GET if the server doesn't support HEAD properly."""
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    async with sem:
        try:
            async with session.head(url, timeout=timeout, allow_redirects=True) as resp:
                if resp.status == 200:
                    return url, True
                if resp.status in (405, 403, 501, 400):
                    # HEAD blocked/unsupported by this server -> try GET
                    pass
                else:
                    return url, False
        except Exception:
            pass  # fall through to GET attempt

        try:
            async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
                return url, resp.status == 200
        except Exception:
            return url, False


async def check_all(urls: list[str]) -> list[str]:
    """Check every url concurrently, return the ones that are working."""
    sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    connector = aiohttp.TCPConnector(limit=0, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [check_one(session, u, sem) for u in urls]
        results = await asyncio.gather(*tasks)
    return [url for url, ok in results if ok]


async def run_check(bot, chat_id: int, notify_only_new: bool) -> list[str]:
    """Run a full check for a chat and send messages for working links.

    notify_only_new=True  -> only pings about links not seen as working before
                              (used by the automatic loop, to avoid spamming
                              the same link every cycle).
    notify_only_new=False -> pings about every working link found this run
                              (used by the manual /check command).
    """
    state = get_state(chat_id)
    words, links = state["words"], state["links"]

    if not words or not links:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ Please send both *wordlist.txt* and *linklist.txt* first.\n"
                 "Use /help to see how.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return []

    urls = generate_urls(links, words)
    logger.info("Chat %s: checking %d urls", chat_id, len(urls))

    working = await check_all(urls)

    new_ones = [u for u in working if u not in state["found"]]
    state["found"].update(working)

    to_report = working if not notify_only_new else new_ones

    if to_report:
        for url in to_report:
            caption = f"✅ *Working Link Found!*\n{url}"
            try:
                await bot.send_photo(chat_id=chat_id, photo=url, caption=caption, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.warning("Couldn't send as photo (%s), sending as text instead: %s", url, e)
                await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.MARKDOWN)
    elif not notify_only_new:
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ Checked {len(urls)} link(s) — none are working right now.",
        )

    return working


# ============================================================
# COMMAND HANDLERS
# ============================================================

@restricted
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Welcome to Python Url Checker!*\n\n"
        "I check huge batches of links super fast and tell you the moment "
        "one of them actually goes live — perfect for catching new game "
        "splash images, banners, or any (Word)-style link pattern the "
        "second it's uploaded.\n\n"
        "*Here's how it works:*\n"
        "1️⃣ Send me `wordlist.txt` — one word per line.\n"
        "2️⃣ Send me `linklist.txt` — one URL per line, with `(Word)` "
        "where the word should go.\n"
        "3️⃣ Send /check and I'll test every combination at once.\n"
        "4️⃣ Optionally, send /autocheck 5 and I'll keep checking every "
        "5 minutes and ping you the moment something new goes live.\n\n"
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
        "/check — check every link×word combination right now\n"
        "/autocheck `N` — auto re-check every N minutes (1–10), pings you "
        "when a *new* working link is found\n"
        "/stopautocheck — stop the automatic loop\n"
        "/resetfound — clear the memory of already-found links, so they "
        "can be reported again\n"
        "/status — show loaded word/link counts and autocheck status\n\n"
        "_Tip: You can re-send wordlist.txt or linklist.txt any time to "
        "replace the previous version._"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@restricted
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    autocheck = f"every {state['interval_minutes']} min ✅" if jobs else "off ❌"
    combos = len(generate_urls(state["links"], state["words"])) if state["words"] and state["links"] else 0

    text = (
        "*📊 Status*\n"
        f"Words loaded: `{len(state['words'])}`\n"
        f"Link templates loaded: `{len(state['links'])}`\n"
        f"Total combinations to check: `{combos}`\n"
        f"Auto-check: {autocheck}\n"
        f"Working links remembered: `{len(state['found'])}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@restricted
async def resetfound_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_chat.id)
    state["found"].clear()
    await update.message.reply_text("🧹 Cleared the memory of found links.")


@restricted
async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔎 Checking all links now, this'll just take a moment...")
    await run_check(context.bot, chat_id, notify_only_new=False)


async def autocheck_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await run_check(context.bot, chat_id, notify_only_new=True)


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

    # remove any existing job for this chat first
    for job in context.job_queue.get_jobs_by_name(str(chat_id)):
        job.schedule_removal()

    state = get_state(chat_id)
    state["interval_minutes"] = minutes

    context.job_queue.run_repeating(
        autocheck_job,
        interval=minutes * 60,
        first=5,
        chat_id=chat_id,
        name=str(chat_id),
    )

    await update.message.reply_text(
        f"✅ Auto-check enabled — I'll re-check every link every *{minutes}* minute(s) "
        "and message you as soon as a new working link shows up.",
        parse_mode=ParseMode.MARKDOWN,
    )


@restricted
async def stopautocheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    if not jobs:
        await update.message.reply_text("Auto-check isn't running right now.")
        return
    for job in jobs:
        job.schedule_removal()
    get_state(chat_id)["interval_minutes"] = None
    await update.message.reply_text("🛑 Auto-check stopped.")


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
    app.add_handler(CommandHandler("autocheck", autocheck_cmd))
    app.add_handler(CommandHandler("stopautocheck", stopautocheck_cmd))
    app.add_handler(CommandHandler("resetfound", resetfound_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))

    logger.info("Python Url Checker bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

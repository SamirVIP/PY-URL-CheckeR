"""
Python Url Checker - Telegram Bot
==================================

Send the bot two files:
  wordlist.txt  - one word per line
  linklist.txt  - one link per line, with (Word) where the word goes

Then /check tests EVERY link with EVERY word (up to 600,000 combinations
per run) using the multi-process engine in checker.py, shows live
progress with speed + estimated time left, and sends every working link
with its image the moment it is found.

Commands
--------
/start           welcome message
/help            help
/check           check everything now (live progress + time estimate)
/cancel          stop a running check (alias: /cancle)
/autocheck N     re-check automatically every N minutes (1-10)
/stopautocheck   stop automatic checking
/status          show what is loaded / running
/resetstats      clear the statistics shown in /status
/id              show this chat's ID (use it to allow a group)
/allow [id]      (admin) allow this chat, or the given chat ID
/disallow [id]   (admin) remove a chat that was added with /allow
/allowed         (admin) list allowed chats

Only allowed chats can use the bot - private chats AND groups
(see README: ALLOWED_CHAT_IDS, /allow).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime
from functools import wraps
from html import escape
from pathlib import Path

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import checker

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ============================================================
# CONFIG
# ============================================================

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _parse_ids(text: str) -> set[int]:
    ids = set()
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError:
                pass
    return ids


BOT_TOKEN = os.environ.get("BOT_TOKEN", "8593278400:AAGkwwoRzvAz9Y3Zcan_JtLKxyeNEm8gS_M").strip()

# Chats (private chats AND groups) that may use the bot. Group IDs are
# negative numbers like -1001234567890.
ENV_ALLOWED = _parse_ids(os.environ.get("ALLOWED_CHAT_IDS", "6206433961"))
# People who may use /allow and /disallow. Defaults to every positive
# (= personal) ID in ALLOWED_CHAT_IDS.
ADMIN_IDS = _parse_ids(os.environ.get("ADMIN_IDS", "")) | {i for i in ENV_ALLOWED if i > 0}

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(exist_ok=True)

# Highest number of link x word combinations allowed in one run.
MAX_COMBINATIONS = _env_int("MAX_COMBINATIONS", 600_000)

# Total simultaneous requests (shared between all worker processes).
CONCURRENCY = _env_int("CONCURRENCY", 600)
# Worker processes. 0 = automatic (one per CPU core, max 4).
PROCESSES = _env_int("PROCESSES", 0) or None
CONNECT_TIMEOUT = _env_float("CONNECT_TIMEOUT", 8.0)
TOTAL_TIMEOUT = _env_float("TOTAL_TIMEOUT", 15.0)
ATTEMPTS = _env_int("ATTEMPTS", 3)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024        # Telegram bots can download files up to 20 MB

PROGRESS_SECONDS_PRIVATE = 3.0             # how often the progress message refreshes
PROGRESS_SECONDS_GROUP = 6.0               # groups have stricter Telegram rate limits
NOTIFY_PACE_PRIVATE = 0.1                  # pause between "working link" messages
NOTIFY_PACE_GROUP = 1.5
HEARTBEAT_SECONDS = 30 * 60                # autocheck status update interval
AUTO_FILE_THRESHOLD = 10                   # autocheck sends a results file when >= this many found

MIN_AUTOCHECK_MINUTES = 1
MAX_AUTOCHECK_MINUTES = 10

HTML = ParseMode.HTML

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("python-url-checker-bot")


# ============================================================
# ALLOWED CHATS (env list + chats added with /allow)
# ============================================================

ALLOWED_FILE = DATA_DIR / "allowed_chats.json"


def _load_dynamic() -> set[int]:
    try:
        return {int(x) for x in json.loads(ALLOWED_FILE.read_text())}
    except Exception:
        return set()


dynamic_allowed: set[int] = _load_dynamic()


def _save_dynamic():
    ALLOWED_FILE.write_text(json.dumps(sorted(dynamic_allowed)))


def is_allowed(chat_id: int) -> bool:
    # If nobody is configured, nobody is allowed (safe default).
    return chat_id in ENV_ALLOWED or chat_id in dynamic_allowed


def restricted(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat, msg = update.effective_chat, update.effective_message
        if chat is None or msg is None:
            return
        if not is_allowed(chat.id):
            await msg.reply_text(
                "🚫 Sorry, this chat is not authorized to use this bot.\n"
                f"Chat ID: <code>{chat.id}</code>\n\n"
                "The bot owner can allow it by adding this ID to <code>ALLOWED_CHAT_IDS</code> "
                "or by sending /allow here.",
                parse_mode=HTML,
            )
            return
        return await handler(update, context)
    return wrapper


# ============================================================
# PER-CHAT STATE
# ============================================================

chat_state: dict[int, dict] = {}


def chat_dir(chat_id: int) -> Path:
    d = DATA_DIR / str(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_state(chat_id: int) -> dict:
    if chat_id not in chat_state:
        d = chat_dir(chat_id)
        chat_state[chat_id] = {
            "words": checker.read_lines(d / "wordlist.txt")[0],
            "links": checker.read_lines(d / "linklist.txt")[0],
            "run": None,                 # the running checker.CheckRun, if any
            "run_kind": None,            # "manual" or "auto"
            "busy": False,               # True from the moment a check is started
            "pending_cancel": False,
            "stop_notify": False,
            "ever_working": set(),
            "interval_minutes": None,
            "cycles_run": 0,
            "skipped_cycles": 0,
            "last_run_time": None,
            "last_run_checked": 0,
            "last_run_working": 0,
            "last_run_unverified": 0,
        }
    return chat_state[chat_id]


# ============================================================
# SMALL HELPERS
# ============================================================

def fmt_dur(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def retry_seconds(exc: RetryAfter) -> float:
    ra = exc.retry_after
    return (ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)) + 0.5


def render_progress(run: checker.CheckRun) -> str:
    s = run.snapshot()
    total = s["total"] or 1
    pct = min(100.0, s["processed"] * 100.0 / total)
    filled = int(pct // 5)
    bar = "█" * filled + "░" * (20 - filled)
    lines = [
        "🔎 Checking links...",
        f"{bar} {pct:.1f}%",
        f"Checked: {s['processed']:,} / {s['total']:,}",
        f"Working found: {s['found']:,}",
    ]
    if s["speed"] > 0:
        lines.append(f"Speed: {s['speed']:,.0f} links/sec")
    lines.append(f"Elapsed: {fmt_dur(s['elapsed'])}")
    if s["in_retry"]:
        lines.append(f"Re-checking {s['retrying']:,} links that had network errors...")
        lines.append("Time left: almost done")
    elif s["eta"] is not None:
        lines.append(f"Time left: ~{fmt_dur(s['eta'])}")
    else:
        lines.append("Time left: calculating...")
    if s["retrying"] and not s["in_retry"]:
        lines.append(f"Retrying (network errors): {s['retrying']:,}")
    lines.append(f"Worker processes: {s['processes']}")
    lines.append("Send /cancel to stop.")
    return "\n".join(lines)


def build_summary(run: checker.CheckRun) -> str:
    if run.cancelled:
        head = "🛑 Check cancelled."
    elif run.aborted or run.crashed or run.errors:
        head = "⚠️ Check stopped early."
    elif run.unverified_count:
        head = "⚠️ Check finished, but some links could not be verified."
    else:
        head = "✅ Check complete!"
    checked = f"{run.processed:,}" + (f" of {run.total:,}" if run.processed != run.total else "")
    lines = [
        head,
        f"Link templates: {len(run.links):,}  |  Words: {len(run.words):,}",
        f"Total combinations: {run.total:,}",
        f"Checked: {checked}",
        f"Working found: {len(run.found):,}",
    ]
    if run.unverified_count:
        lines.append(f"Could not verify: {run.unverified_count:,} (network/server errors - list attached)")
    el = run.elapsed()
    avg = f" (average {run.processed / el:,.0f} links/sec)" if el > 0 and run.processed else ""
    lines.append(f"Time taken: {fmt_dur(el)}{avg}")
    if run.aborted:
        lines.append("The server stopped answering (too many errors in a row). "
                     "Try again later or lower CONCURRENCY in .env.")
    if run.crashed:
        lines.append(f"{len(run.crashed)} worker process(es) crashed - results may be incomplete.")
    if run.errors:
        lines.append(f"Error: {run.errors[0][:200]}")
    return "\n".join(lines)


# ============================================================
# SENDING "WORKING LINK" MESSAGES (never lose one)
# ============================================================

async def send_working_link(bot, chat_id: int, url: str, attempts: int = 5) -> bool:
    """Send one working link with its image. Plain text only (no Markdown),
    because Telegram treats "_" as an italics marker - a link like ..._en.jpg
    would otherwise be rejected as 'can't parse entities'."""
    caption = f"✅ Working Link Found!\n{url}"
    use_photo = True
    for _ in range(attempts):
        try:
            if use_photo:
                await bot.send_photo(chat_id=chat_id, photo=url, caption=caption)
            else:
                await bot.send_message(chat_id=chat_id, text=caption)
            return True
        except RetryAfter as e:                      # Telegram says: slow down
            await asyncio.sleep(retry_seconds(e))
        except BadRequest as e:                      # e.g. Telegram can't download/show the image
            if use_photo:
                logger.info("Photo failed for %s (%s) - sending the link as text", url, e)
                use_photo = False
            else:
                logger.error("Could not send %s: %s", url, e)
                return False
        except Forbidden:                            # bot was blocked / removed from the chat
            return False
        except (TimedOut, NetworkError):
            await asyncio.sleep(1.5)
        except Exception:
            logger.exception("Unexpected error sending %s", url)
            use_photo = False
            await asyncio.sleep(1.0)
    return False


class Notifier:
    """Sends found links one by one (paced + retried). Nothing found is dropped."""

    def __init__(self, bot, chat_id: int):
        self.bot, self.chat_id = bot, chat_id
        self.pace = NOTIFY_PACE_GROUP if chat_id < 0 else NOTIFY_PACE_PRIVATE
        self.queue: asyncio.Queue = asyncio.Queue()
        self.pending = 0
        self.failed: list[str] = []
        self.task = asyncio.create_task(self._loop())

    def add(self, url: str):
        self.pending += 1
        self.queue.put_nowait(url)

    async def _loop(self):
        while True:
            url = await self.queue.get()
            try:
                if not await send_working_link(self.bot, self.chat_id, url):
                    self.failed.append(url)
                await asyncio.sleep(self.pace)
            finally:
                self.pending -= 1

    async def drain(self, should_abort, timeout: float | None = None):
        start = time.monotonic()
        while self.pending > 0 and not should_abort():
            if timeout is not None and time.monotonic() - start > timeout:
                return
            await asyncio.sleep(0.3)

    def stop(self):
        self.task.cancel()


async def progress_loop(run: checker.CheckRun, msg, chat_id: int):
    interval = PROGRESS_SECONDS_GROUP if chat_id < 0 else PROGRESS_SECONDS_PRIVATE
    last = None
    while not run.finished:
        await asyncio.sleep(interval)
        if run.finished:
            break
        text = render_progress(run)
        if text == last:
            continue
        try:
            await msg.edit_text(text)
            last = text
        except RetryAfter as e:
            await asyncio.sleep(retry_seconds(e))
        except Exception:
            pass  # "message is not modified" etc. - harmless


async def send_text_file(bot, chat_id: int, path: Path, caption: str):
    try:
        with open(path, "rb") as f:
            await bot.send_document(chat_id=chat_id, document=f, filename=path.name, caption=caption)
    except RetryAfter as e:
        await asyncio.sleep(retry_seconds(e))
        with open(path, "rb") as f:
            await bot.send_document(chat_id=chat_id, document=f, filename=path.name, caption=caption)
    except Exception:
        logger.exception("Could not send file %s", path)


# ============================================================
# RUNNING A CHECK
# ============================================================

async def run_check_session(bot, chat_id: int, kind: str, progress_msg=None):
    """Run one complete check for a chat (kind = "manual" or "auto")."""
    state = get_state(chat_id)
    workdir = None
    notifier = None
    ptask = None
    try:
        workdir = Path(tempfile.mkdtemp(prefix="run_", dir=str(chat_dir(chat_id))))
        run = checker.CheckRun(
            list(state["links"]), list(state["words"]),
            workdir=workdir, processes=PROCESSES, concurrency=CONCURRENCY,
            connect_timeout=CONNECT_TIMEOUT, total_timeout=TOTAL_TIMEOUT, attempts=ATTEMPTS,
        )
        state["run"], state["run_kind"] = run, kind
        state["stop_notify"] = False
        if state["pending_cancel"]:
            state["pending_cancel"] = False
            run.cancel()

        notifier = Notifier(bot, chat_id)
        if progress_msg is not None:
            ptask = asyncio.create_task(progress_loop(run, progress_msg, chat_id))

        try:
            await run.run(on_found=notifier.add)
        except asyncio.CancelledError:
            run.shutdown()
            raise
        except Exception as e:
            logger.exception("Check failed")
            run.errors.append(repr(e))

        if ptask is not None:
            ptask.cancel()
            await asyncio.gather(ptask, return_exceptions=True)
            ptask = None

        def stop_sending() -> bool:
            return run.cancelled or state["stop_notify"]

        # Give the individual "working link" messages a moment to go out.
        await notifier.drain(stop_sending, timeout=30)

        # ---- statistics ----
        state["ever_working"].update(run.found)
        state["last_run_time"] = time.time()
        state["last_run_checked"] = run.processed
        state["last_run_working"] = len(run.found)
        state["last_run_unverified"] = run.unverified_count
        if kind == "auto" and not run.cancelled:
            state["cycles_run"] += 1

        # ---- final report ----
        summary = build_summary(run)
        if notifier.pending > 0 and not stop_sending():
            summary += (f"\nStill sending {notifier.pending:,} working-link message(s) - "
                        "the file below already contains all of them.")
        problem = bool(run.aborted or run.crashed or run.errors)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if kind == "manual":
            try:
                await progress_msg.edit_text(summary)
            except Exception:
                await bot.send_message(chat_id=chat_id, text=summary)
        elif problem:
            await bot.send_message(chat_id=chat_id, text="⚠️ Auto-check problem:\n" + summary)

        send_found_file = bool(run.found) and (kind == "manual" or len(run.found) >= AUTO_FILE_THRESHOLD)
        if send_found_file:
            p = workdir / f"working_links_{stamp}.txt"
            p.write_text("\n".join(run.found) + "\n", encoding="utf-8")
            await send_text_file(bot, chat_id, p, f"All {len(run.found):,} working link(s) found in this check.")

        if run.unverified_count and kind == "manual":
            p = workdir / f"unverified_links_{stamp}.txt"
            written = run.write_unverified(p)
            note = f"{run.unverified_count:,} link(s) could not be verified (network/server errors)."
            if written < run.unverified_count:
                note += f" First {written:,} listed."
            await send_text_file(bot, chat_id, p, note)

        # Finish delivering the individual messages (the file already has everything).
        await notifier.drain(stop_sending)
        if notifier.failed:
            logger.warning("%d notification(s) could not be delivered individually (they are in the results file)",
                           len(notifier.failed))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Check session crashed")
        try:
            await bot.send_message(chat_id=chat_id, text="❌ Something went wrong while checking. See the bot log.")
        except Exception:
            pass
    finally:
        if ptask is not None:
            ptask.cancel()
        if notifier is not None:
            notifier.stop()
        state["run"] = None
        state["run_kind"] = None
        state["busy"] = False
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


def _log_task_result(task: asyncio.Task):
    if not task.cancelled() and task.exception():
        logger.error("Background check task failed", exc_info=task.exception())


# ============================================================
# COMMANDS
# ============================================================

@restricted
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>Welcome to Python Url Checker!</b>\n\n"
        f"I check huge batches of links at high speed (up to {MAX_COMBINATIONS:,} link × word "
        "combinations in one run) and tell you the instant one of them goes live.\n\n"
        "<b>How it works</b>\n"
        "1️⃣ Send me <code>wordlist.txt</code> — one word per line.\n"
        "2️⃣ Send me <code>linklist.txt</code> — one link per line, with <code>(Word)</code> "
        "where the word should go.\n"
        "3️⃣ Send /check — every link is tested with every word. You get live progress, speed "
        "and an estimated time left. /cancel stops it any time.\n"
        "4️⃣ Optional: <code>/autocheck 5</code> re-checks automatically every 5 minutes (1–10) "
        "and sends a status update every 30 minutes.\n\n"
        "Every working link is sent to you with its image. Type /help for all commands. 🚀"
    )
    await update.effective_message.reply_text(text, parse_mode=HTML)


@restricted
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>Python Url Checker — Help</b>\n\n"
        "<b>Step 1 — upload two files</b> (send them as documents):\n"
        "• <code>wordlist.txt</code> — one word per line\n"
        "• <code>linklist.txt</code> — one link per line, e.g.\n"
        "<code>https://example.com/img_(Word)_en.jpg</code>\n"
        "Send a file again any time to replace it. Max file size: 20 MB.\n\n"
        "<b>Commands</b>\n"
        "/check — check every link with every word, live progress + time estimate\n"
        "/cancel — stop a running check (also works as /cancle)\n"
        "/autocheck <code>N</code> — re-check every N minutes (1–10). Every working link found "
        "is sent, plus a status update every 30 minutes\n"
        "/stopautocheck — stop automatic checking\n"
        "/status — what is loaded / running\n"
        "/resetstats — clear the statistics\n"
        "/id — show this chat's ID\n"
        "/allow, /disallow, /allowed — (admin) manage which chats and groups may use the bot\n\n"
        f"Limit: {MAX_COMBINATIONS:,} combinations per run (links × words). "
        "Nothing is skipped: network errors are retried, and anything that still can't be "
        "verified is reported to you explicitly."
    )
    await update.effective_message.reply_text(text, parse_mode=HTML)


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open to everyone - needed to find a group's ID before allowing it."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat is None or msg is None:
        return
    text = (
        f"Chat ID: <code>{chat.id}</code>\n"
        f"Chat type: {escape(chat.type)}\n"
        f"Your user ID: <code>{user.id if user else 'unknown'}</code>\n"
        f"Allowed: {'yes ✅' if is_allowed(chat.id) else 'no ❌'}"
    )
    await msg.reply_text(text, parse_mode=HTML)


async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat is None or msg is None:
        return
    if user is None or user.id not in ADMIN_IDS:
        await msg.reply_text("⛔ Only the bot admin can use /allow.")
        return
    target = chat.id
    if context.args:
        try:
            target = int(context.args[0])
        except ValueError:
            await msg.reply_text("Usage: /allow  (allow this chat)  or  /allow -1001234567890")
            return
    if is_allowed(target):
        await msg.reply_text(f"Chat {target} is already allowed ✅")
        return
    dynamic_allowed.add(target)
    _save_dynamic()
    await msg.reply_text(f"✅ Chat {target} is now allowed to use this bot.")


async def disallow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat is None or msg is None:
        return
    if user is None or user.id not in ADMIN_IDS:
        await msg.reply_text("⛔ Only the bot admin can use /disallow.")
        return
    target = chat.id
    if context.args:
        try:
            target = int(context.args[0])
        except ValueError:
            await msg.reply_text("Usage: /disallow  (this chat)  or  /disallow -1001234567890")
            return
    if target in ENV_ALLOWED:
        await msg.reply_text("That chat is set in ALLOWED_CHAT_IDS (.env). Remove it there and restart the bot.")
        return
    if target not in dynamic_allowed:
        await msg.reply_text("That chat wasn't added with /allow.")
        return
    dynamic_allowed.discard(target)
    _save_dynamic()
    await msg.reply_text(f"🗑 Chat {target} removed.")


async def allowed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = update.effective_user, update.effective_message
    if msg is None:
        return
    if user is None or user.id not in ADMIN_IDS:
        await msg.reply_text("⛔ Only the bot admin can use /allowed.")
        return
    env_list = ", ".join(str(i) for i in sorted(ENV_ALLOWED)) or "none"
    dyn_list = ", ".join(str(i) for i in sorted(dynamic_allowed)) or "none"
    await msg.reply_text(f"From .env: {env_list}\nAdded with /allow: {dyn_list}")


@restricted
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    total = checker.count_combinations(state["links"], state["words"])
    jobs = context.job_queue.get_jobs_by_name(f"check-{chat_id}")
    auto = f"every {state['interval_minutes']} min ✅" if jobs else "off ❌"
    last = (datetime.fromtimestamp(state["last_run_time"]).strftime("%Y-%m-%d %H:%M:%S")
            if state["last_run_time"] else "never")

    lines = [
        "📊 <b>Status</b>",
        f"Words loaded: <code>{len(state['words']):,}</code>",
        f"Link templates loaded: <code>{len(state['links']):,}</code>",
        f"Total combinations: <code>{total:,}</code> (limit {MAX_COMBINATIONS:,})",
        f"Auto-check: {auto}",
        f"Auto-check cycles completed: <code>{state['cycles_run']}</code>"
        + (f" (skipped {state['skipped_cycles']} because a check was still running)" if state["skipped_cycles"] else ""),
        f"Last check: {last} — checked <code>{state['last_run_checked']:,}</code>, "
        f"working <code>{state['last_run_working']:,}</code>, "
        f"unverified <code>{state['last_run_unverified']:,}</code>",
        f"Unique working links ever seen: <code>{len(state['ever_working']):,}</code>",
    ]
    run = state["run"]
    if run is not None and not run.finished:
        s = run.snapshot()
        eta = fmt_dur(s["eta"]) if s["eta"] is not None else "calculating"
        lines.append(
            f"⏳ Running now ({state['run_kind']}): {s['processed']:,}/{s['total']:,} "
            f"({s['processed'] * 100 / max(1, s['total']):.1f}%), time left ~{eta}"
        )
    elif state["busy"]:
        lines.append("⏳ A check is starting...")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=HTML)


@restricted
async def resetstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_chat.id)
    state["ever_working"].clear()
    state.update(cycles_run=0, skipped_cycles=0, last_run_time=None,
                 last_run_checked=0, last_run_working=0, last_run_unverified=0)
    await update.effective_message.reply_text("🧹 Stats cleared.")


def _check_ready(state: dict) -> str | None:
    """Return an error message if a check can't start, else None."""
    if not state["words"] or not state["links"]:
        return "⚠️ Please send both wordlist.txt and linklist.txt first. Use /help to see how."
    total = checker.count_combinations(state["links"], state["words"])
    if total == 0:
        return "⚠️ Nothing to check - your lists produce 0 links."
    if total > MAX_COMBINATIONS:
        return (f"⚠️ {total:,} combinations ({len(state['links']):,} links × {len(state['words']):,} words) "
                f"is more than the limit of {MAX_COMBINATIONS:,} per run.\n"
                "Split your lists into smaller files, or raise MAX_COMBINATIONS in .env.")
    return None


@restricted
async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = update.effective_message
    state = get_state(chat_id)

    if state["busy"]:
        await msg.reply_text("⏳ A check is already running. Use /cancel to stop it first.")
        return
    problem = _check_ready(state)
    if problem:
        await msg.reply_text(problem)
        return

    total = checker.count_combinations(state["links"], state["words"])
    state["busy"] = True
    state["pending_cancel"] = False
    try:
        progress_msg = await msg.reply_text(
            f"🚀 Starting: {len(state['links']):,} links × {len(state['words']):,} words = {total:,} combinations\n"
            "Send /cancel to stop."
        )
    except Exception:
        state["busy"] = False
        raise
    # Run in the background so the bot stays responsive (this is what makes /cancel work).
    task = asyncio.create_task(run_check_session(context.bot, chat_id, "manual", progress_msg))
    task.add_done_callback(_log_task_result)
    state["task"] = task


@restricted
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_chat.id)
    msg = update.effective_message
    run = state["run"]
    if run is not None and not run.finished:
        run.cancel()
        extra = ""
        if state["run_kind"] == "auto":
            extra = " (auto-check stays on - use /stopautocheck to turn it off)"
        await msg.reply_text("🛑 Cancelling..." + extra)
    elif run is not None:
        # checking is over; only the delivery of the individual messages is still going
        state["stop_notify"] = True
        await msg.reply_text("🛑 Stopped sending the remaining link messages. The results file has every working link.")
    elif state["busy"]:
        state["pending_cancel"] = True
        await msg.reply_text("🛑 Cancelling...")
    else:
        await msg.reply_text("There's no check running right now.")


async def autocheck_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    state = get_state(chat_id)
    if state["busy"]:
        state["skipped_cycles"] += 1          # previous check still running - never overlap
        return
    if _check_ready(state):
        return
    state["busy"] = True
    state["pending_cancel"] = False
    await run_check_session(context.bot, chat_id, "auto", None)


async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    """Status update every 30 minutes while autocheck is on."""
    chat_id = context.job.chat_id
    state = get_state(chat_id)
    last = (datetime.fromtimestamp(state["last_run_time"]).strftime("%Y-%m-%d %H:%M:%S")
            if state["last_run_time"] else "not yet")
    lines = [
        "📡 <b>Autocheck status update</b>",
        f"Checking every: <code>{state['interval_minutes']}</code> min",
        f"Cycles completed: <code>{state['cycles_run']}</code>",
        f"Last check: {last}",
        f"Last check: <code>{state['last_run_checked']:,}</code> links, "
        f"<code>{state['last_run_working']:,}</code> working, "
        f"<code>{state['last_run_unverified']:,}</code> unverified",
        f"Unique working links ever seen: <code>{len(state['ever_working']):,}</code>",
    ]
    run = state["run"]
    if run is not None and not run.finished:
        s = run.snapshot()
        eta = fmt_dur(s["eta"]) if s["eta"] is not None else "calculating"
        lines.append(
            f"⏳ Current cycle: {s['processed']:,}/{s['total']:,} "
            f"({s['processed'] * 100 / max(1, s['total']):.1f}%), time left ~{eta}, "
            f"working found so far {s['found']:,}"
        )
    else:
        lines.append("Current cycle: waiting for the next run")
    lines.append("Still running ✅")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=HTML)


@restricted
async def autocheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = update.effective_message
    state = get_state(chat_id)

    if not context.args:
        await msg.reply_text(
            f"Usage: /autocheck N   (N = minutes, {MIN_AUTOCHECK_MINUTES} to {MAX_AUTOCHECK_MINUTES})\n"
            "Example: /autocheck 5"
        )
        return
    try:
        minutes = int(context.args[0])
    except ValueError:
        await msg.reply_text("Please give a whole number of minutes, e.g. /autocheck 5")
        return
    if not (MIN_AUTOCHECK_MINUTES <= minutes <= MAX_AUTOCHECK_MINUTES):
        await msg.reply_text(f"⏱ Please choose between {MIN_AUTOCHECK_MINUTES} and {MAX_AUTOCHECK_MINUTES} minutes.")
        return
    problem = _check_ready(state)
    if problem:
        await msg.reply_text(problem)
        return

    for name in (f"check-{chat_id}", f"heartbeat-{chat_id}"):
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()

    state["interval_minutes"] = minutes
    state["cycles_run"] = 0
    state["skipped_cycles"] = 0
    context.job_queue.run_repeating(autocheck_job, interval=minutes * 60, first=5,
                                    chat_id=chat_id, name=f"check-{chat_id}")
    context.job_queue.run_repeating(heartbeat_job, interval=HEARTBEAT_SECONDS, first=HEARTBEAT_SECONDS,
                                    chat_id=chat_id, name=f"heartbeat-{chat_id}")
    await msg.reply_text(
        f"✅ Auto-check enabled: every {minutes} minute(s) I re-check every link with every word and "
        "send you every working link I find. You'll also get a status update every 30 minutes.\n"
        "If a check takes longer than the interval, the next one waits (checks never overlap)."
    )


@restricted
async def stopautocheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    msg = update.effective_message
    jobs = context.job_queue.get_jobs_by_name(f"check-{chat_id}") + \
        context.job_queue.get_jobs_by_name(f"heartbeat-{chat_id}")
    if not jobs:
        await msg.reply_text("Auto-check isn't running right now.")
        return
    for job in jobs:
        job.schedule_removal()
    state["interval_minutes"] = None
    run = state["run"]
    if run is not None and not run.finished and state["run_kind"] == "auto":
        run.cancel()
    await msg.reply_text("🛑 Auto-check and status updates stopped.")


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, msg = update.effective_chat, update.effective_message
    if chat is None or msg is None or msg.document is None or not is_allowed(chat.id):
        return  # stay silent in chats that are not allowed
    doc = msg.document
    name = (doc.file_name or "").lower()
    kind = "words" if "wordlist" in name else "links" if "linklist" in name else None
    if kind is None:
        if chat.type == "private":
            await msg.reply_text("📄 Please send files named wordlist.txt and linklist.txt.")
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await msg.reply_text("⚠️ That file is bigger than 20 MB, which is Telegram's limit for bots. "
                             "Please split it into smaller files.")
        return

    state = get_state(chat.id)
    filename = "wordlist.txt" if kind == "words" else "linklist.txt"
    dest = chat_dir(chat.id) / filename
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=dest)
        items, skipped = await asyncio.to_thread(checker.read_lines, dest)
    except Exception:
        logger.exception("Could not read uploaded file")
        await msg.reply_text("❌ I couldn't download or read that file. Please try sending it again.")
        return

    state[kind] = items
    label = "word(s)" if kind == "words" else "link(s)"
    text = f"✅ Loaded {len(items):,} {label} from {filename}"
    if skipped:
        text += f" ({skipped:,} blank/duplicate line(s) skipped)"
    if kind == "links" and items and not any(checker.PLACEHOLDER_RE.search(x) for x in items):
        text += "\n⚠️ None of your links contain (Word) - each link will just be checked once."
    await msg.reply_text(text)

    if state["words"] and state["links"]:
        total = checker.count_combinations(state["links"], state["words"])
        note = f"Both files loaded: {len(state['links']):,} links × {len(state['words']):,} words = {total:,} combinations."
        if total > MAX_COMBINATIONS:
            note += f"\n⚠️ That is more than the limit of {MAX_COMBINATIONS:,} per run."
        else:
            note += "\nSend /check when you're ready! 🚀"
        await msg.reply_text(note)


async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, msg = update.effective_chat, update.effective_message
    if chat and msg and chat.type == "private" and is_allowed(chat.id):
        await msg.reply_text("Not sure what you mean - try /help.")


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

async def post_init(app: Application):
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Welcome message"),
            BotCommand("help", "How to use the bot"),
            BotCommand("check", "Check all links with all words"),
            BotCommand("cancel", "Cancel the running check"),
            BotCommand("autocheck", "Re-check automatically (1-10 min)"),
            BotCommand("stopautocheck", "Stop automatic checking"),
            BotCommand("status", "Show status"),
            BotCommand("id", "Show this chat's ID"),
        ])
    except Exception:
        logger.warning("Could not set the command menu", exc_info=True)


async def post_stop(app: Application):
    # Make sure no checker worker process is left running.
    for state in chat_state.values():
        run = state.get("run")
        if run is not None:
            run.shutdown()


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Put it in a .env file or as an environment variable.\n"
                         "Get a token from @BotFather on Telegram.")
    if not ENV_ALLOWED and not dynamic_allowed:
        logger.warning("ALLOWED_CHAT_IDS is empty - nobody can use the bot until you set it.")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)          # lets /cancel, /status ... work while a check runs
        .read_timeout(30).write_timeout(60).connect_timeout(15).pool_timeout(30)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler(["cancel", "cancle"], cancel_cmd))
    app.add_handler(CommandHandler("autocheck", autocheck_cmd))
    app.add_handler(CommandHandler("stopautocheck", stopautocheck_cmd))
    app.add_handler(CommandHandler("resetstats", resetstats_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("allow", allow_cmd))
    app.add_handler(CommandHandler("disallow", disallow_cmd))
    app.add_handler(CommandHandler("allowed", allowed_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, private_text))

    logger.info("Python Url Checker bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

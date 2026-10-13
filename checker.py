"""
Python Url Checker - Telegram Bot
==================================

Send the bot two files:
  wordlist.txt  - one word per line
  linklist.txt  - one link per line, with (Word) where the word goes

/check tests EVERY link with EVERY word (up to 600,000 combinations per
run) using the multi-process engine in checker.py. A link only counts as
working when the file is REALLY there (real image bytes, not a
placeholder the server returns for everything). Every working link is
sent with its image and its "Last modified" date in Bangladesh time.

Commands
--------
/start            welcome
/help             help
/check            check everything now (live progress + time left)
/cancel           stop a running check (alias: /cancle)
/addword W1 W2    add words to your list AND instantly check only those
/test LINK        diagnose one link (what does the server really answer?)
/found            every working link found so far (newest first)
/autocheck N      re-check automatically every N minutes (1-10)
/stopautocheck    stop automatic checking
/notify all|new   auto-check alerts: every time, or only NEW links
/status           what is loaded / running
/resetstats       clear the counters
/id               show this chat's ID (use it to allow a group)
/allow [id]       (admin) allow this chat or the given chat ID
/disallow [id]    (admin) remove a chat added with /allow
/allowed          (admin) list allowed chats
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from html import escape
from pathlib import Path

import aiohttp
from telegram import BotCommand, LinkPreviewOptions, Update
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
ADMIN_IDS = _parse_ids(os.environ.get("ADMIN_IDS", "6206433961")) | {i for i in ENV_ALLOWED if i > 0}

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(exist_ok=True)

MAX_COMBINATIONS = _env_int("MAX_COMBINATIONS", 600_000)   # per run
CONCURRENCY = _env_int("CONCURRENCY", 600)                 # simultaneous requests, all workers together
PROCESSES = _env_int("PROCESSES", 0) or None               # 0 = automatic
CONNECT_TIMEOUT = _env_float("CONNECT_TIMEOUT", 8.0)
TOTAL_TIMEOUT = _env_float("TOTAL_TIMEOUT", 15.0)
ATTEMPTS = _env_int("ATTEMPTS", 3)

# Safety net: most link messages one run may send (the results file always has ALL).
# 0 = unlimited.
MAX_ALERTS_PER_RUN = _env_int("MAX_ALERTS_PER_RUN", 500)
# Largest image the bot will download to send you (Telegram bots can upload up to 50 MB).
MAX_IMAGE_BYTES = _env_int("MAX_IMAGE_MB", 20) * 1024 * 1024

MAX_UPLOAD_BYTES = 20 * 1024 * 1024        # Telegram bots can download files up to 20 MB
HISTORY_LIMIT = 50_000

PROGRESS_SECONDS_PRIVATE = 3.0
PROGRESS_SECONDS_GROUP = 6.0               # groups have stricter Telegram rate limits
NOTIFY_PACE_PRIVATE = 0.3                  # pause between "working link" messages
NOTIFY_PACE_GROUP = 1.5
HEARTBEAT_SECONDS = 30 * 60                # autocheck status update interval
AUTO_FILE_THRESHOLD = 10                   # autocheck sends a results file when >= this many found

MIN_AUTOCHECK_MINUTES = 1
MAX_AUTOCHECK_MINUTES = 10

HTML = ParseMode.HTML
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

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
# DATES, SIZES  (Last modified is shown in Bangladesh time, 12-hour clock)
# ============================================================

BD_TZ = timezone(timedelta(hours=6))      # Bangladesh: UTC+6, no daylight saving
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def parse_http_date(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(str(raw))
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_bd(raw) -> str | None:
    """'Mon, 09 Feb 2026 - 12:30 PM (Bangladesh Time)' from a Last-Modified header."""
    dt = parse_http_date(raw)
    if dt is None:
        return None
    dt = dt.astimezone(BD_TZ)
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return (f"{_DAYS[dt.weekday()]}, {dt.day:02d} {_MONTHS[dt.month - 1]} {dt.year} - "
            f"{hour12:02d}:{dt.minute:02d} {ampm} (Bangladesh Time)")


def lm_epoch(raw) -> float:
    dt = parse_http_date(raw)
    return dt.timestamp() if dt else 0.0


def fmt_size(n) -> str | None:
    if not n:
        return None
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


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


# ============================================================
# PER-CHAT STATE (lists, settings and history are saved on disk)
# ============================================================

chat_state: dict[int, dict] = {}


def chat_dir(chat_id: int) -> Path:
    d = DATA_DIR / str(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_settings(chat_id: int):
    st = chat_state[chat_id]
    (chat_dir(chat_id) / "settings.json").write_text(
        json.dumps({"interval": st["interval_minutes"], "notify": st["notify_mode"]}), encoding="utf-8")


def save_history(chat_id: int):
    hist = chat_state[chat_id]["history"]
    if len(hist) > HISTORY_LIMIT:                       # keep the newest entries
        keep = sorted(hist.items(), key=lambda kv: kv[1].get("first", 0), reverse=True)[:HISTORY_LIMIT]
        hist.clear()
        hist.update(keep)
    (chat_dir(chat_id) / "found_history.json").write_text(json.dumps(hist), encoding="utf-8")


def get_state(chat_id: int) -> dict:
    if chat_id not in chat_state:
        d = chat_dir(chat_id)
        settings = _read_json(d / "settings.json", {})
        notify = settings.get("notify", "all")
        chat_state[chat_id] = {
            "words": checker.read_lines(d / "wordlist.txt")[0],
            "links": checker.read_lines(d / "linklist.txt")[0],
            "history": _read_json(d / "found_history.json", {}),   # url -> {"first": epoch, "lm": raw}
            "notify_mode": notify if notify in ("all", "new") else "all",
            "interval_minutes": settings.get("interval"),
            "run": None,                 # the running checker.CheckRun, if any
            "run_kind": None,            # "manual" or "auto"
            "busy": False,               # True from the moment a check is started
            "pending_cancel": False,
            "stop_notify": False,
            "cycles_run": 0,
            "skipped_cycles": 0,
            "last_run_time": None,
            "last_run_checked": 0,
            "last_run_working": 0,
            "last_run_unverified": 0,
        }
    return chat_state[chat_id]


# ============================================================
# TEXT BUILDERS
# ============================================================

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
    if run.suspicious:
        lines.append("⚠️ Unusually many links look 'working' - the server may be answering everything.")
    lines.append(f"Worker processes: {s['processes']}")
    lines.append("Send /cancel to stop.")
    return "\n".join(lines)


def build_summary(run: checker.CheckRun, extra: list[str] | None = None) -> str:
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
    if run.soft404:
        lines.append("ℹ️ This server answers non-existent links with a placeholder - those were filtered out automatically.")
    if run.suspicious:
        pct = len(run.found) * 100 / max(1, run.processed)
        lines.append(f"⚠️ {pct:.0f}% of the links look 'working'. That is not normal, so these results may be wrong. "
                     "Send /test <one link> to see what the server really answers.")
    if run.aborted:
        lines.append("The server stopped answering (too many errors in a row). "
                     "Try again later or lower CONCURRENCY in .env.")
    if run.crashed:
        lines.append(f"{len(run.crashed)} worker process(es) crashed - results may be incomplete.")
    if run.errors:
        lines.append(f"Error: {run.errors[0][:200]}")
    lines.extend(extra or [])
    return "\n".join(lines)


def build_caption(url: str, lm_raw, size, known: bool) -> str:
    head = "✅ Working Link Found! (seen before)" if known else "🆕 NEW Working Link Found!"
    lines = [head, url, "", "📅 Last modified:", fmt_bd(lm_raw) or "not provided by the server"]
    sz = fmt_size(size)
    if sz:
        lines.append(f"📦 Size: {sz}")
    return "\n".join(lines)


def result_lines(items: list[tuple[str, str | None]]) -> list[str]:
    """[(url, last_modified_raw)] -> text lines, newest file first."""
    rows = sorted(((lm_epoch(raw), url, fmt_bd(raw)) for url, raw in items), key=lambda r: r[0], reverse=True)
    return [f"{url}  |  Last modified: {lm or 'not provided by the server'}" for _, url, lm in rows]


# ============================================================
# SENDING "WORKING LINK" MESSAGES (never lose one)
# ============================================================

class Notifier:
    """Sends found links one by one (paced + retried), WITH the image.

    How an image is delivered (first one that works):
      1. the bot downloads the picture itself and uploads it as a photo
      2. if Telegram refuses it as a photo (too big / odd shape) -> as a file
      3. if the picture can't be downloaded -> Telegram is asked to fetch the link
      4. last resort -> the link as text
    Plain text only (no Markdown): "_" in a link must never break a message.
    """

    def __init__(self, bot, chat_id: int, mode: str, history: dict):
        self.bot, self.chat_id, self.mode, self.history = bot, chat_id, mode, history
        self.pace = NOTIFY_PACE_GROUP if chat_id < 0 else NOTIFY_PACE_PRIVATE
        self.queue: asyncio.Queue = asyncio.Queue()
        self.pending = 0
        self.accepted = 0
        self.sent = 0
        self.suppressed_cap = 0
        self.suppressed_known = 0
        self.failed: list[str] = []
        self.http = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
            timeout=aiohttp.ClientTimeout(total=60),
            headers=checker.BASE_HEADERS,
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        self.task = asyncio.create_task(self._loop())

    def add(self, url: str, meta: dict):
        known = url in self.history
        if self.mode == "new" and known:
            self.suppressed_known += 1
            return
        if MAX_ALERTS_PER_RUN and self.accepted >= MAX_ALERTS_PER_RUN:
            self.suppressed_cap += 1
            return
        self.accepted += 1
        self.pending += 1
        self.queue.put_nowait((url, meta or {}, known))

    async def _loop(self):
        while True:
            url, meta, known = await self.queue.get()
            try:
                if await self.deliver(url, meta, known):
                    self.sent += 1
                else:
                    self.failed.append(url)
                await asyncio.sleep(self.pace)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Could not deliver %s", url)
                self.failed.append(url)
            finally:
                self.pending -= 1

    async def _download(self, url: str):
        """Returns (bytes | None, last_modified_raw | None, size | None)."""
        try:
            async with self.http.get(url, allow_redirects=True, max_redirects=5) as resp:
                if resp.status != 200:
                    return None, None, None
                lm = resp.headers.get("Last-Modified")
                clen = resp.headers.get("Content-Length")
                size = int(clen) if clen and clen.isdigit() else None
                if size and size > MAX_IMAGE_BYTES:
                    return None, lm, size
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buf += chunk
                    if len(buf) > MAX_IMAGE_BYTES:
                        return None, lm, len(buf)
                return bytes(buf), lm, len(buf)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info("Could not download %s: %s", url, e)
            return None, None, None

    async def _try(self, send, attempts: int = 5) -> bool:
        """Run one Telegram send. True = delivered, False = Telegram refused it (try the next way)."""
        for _ in range(attempts):
            try:
                await send()
                return True
            except RetryAfter as e:                      # Telegram says: slow down
                await asyncio.sleep(retry_seconds(e))
            except (BadRequest, Forbidden) as e:         # must come before NetworkError (BadRequest is a subclass)
                logger.info("Telegram refused a message: %s", e)
                return False
            except (TimedOut, NetworkError):
                await asyncio.sleep(1.5)
            except Exception:
                logger.exception("Unexpected error while sending")
                return False
        return False

    async def deliver(self, url: str, meta: dict, known: bool) -> bool:
        bot, chat_id = self.bot, self.chat_id
        data, lm, size = await self._download(url)
        caption = build_caption(url, lm or meta.get("lm"), size or meta.get("sz"), known)
        filename = (url.split("?", 1)[0].rsplit("/", 1)[-1] or "image.jpg")

        if data:
            if await self._try(lambda: bot.send_photo(chat_id=chat_id, photo=data, caption=caption)):
                return True
            if await self._try(lambda: bot.send_document(chat_id=chat_id, document=data,
                                                         filename=filename, caption=caption)):
                return True
        else:
            if await self._try(lambda: bot.send_photo(chat_id=chat_id, photo=url, caption=caption)):
                return True
        return await self._try(lambda: bot.send_message(chat_id=chat_id, text=caption))

    async def drain(self, should_abort, timeout: float | None = None):
        start = time.monotonic()
        while self.pending > 0 and not should_abort():
            if timeout is not None and time.monotonic() - start > timeout:
                return
            await asyncio.sleep(0.3)

    async def aclose(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        try:
            await self.http.close()
        except Exception:
            pass


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
    for _ in range(2):
        try:
            with open(path, "rb") as f:
                await bot.send_document(chat_id=chat_id, document=f, filename=path.name, caption=caption)
            return
        except RetryAfter as e:
            await asyncio.sleep(retry_seconds(e))
        except Exception:
            logger.exception("Could not send file %s", path)
            return


# ============================================================
# RUNNING A CHECK
# ============================================================

async def run_check_session(bot, chat_id: int, kind: str, progress_msg=None, words_override=None):
    """Run one complete check for a chat (kind = "manual" or "auto").
    words_override: check only these words (used by /addword)."""
    state = get_state(chat_id)
    workdir = None
    notifier = None
    ptask = None
    try:
        workdir = Path(tempfile.mkdtemp(prefix="run_", dir=str(chat_dir(chat_id))))
        words = list(words_override) if words_override is not None else list(state["words"])
        run = checker.CheckRun(
            list(state["links"]), words,
            workdir=workdir, processes=PROCESSES, concurrency=CONCURRENCY,
            connect_timeout=CONNECT_TIMEOUT, total_timeout=TOTAL_TIMEOUT, attempts=ATTEMPTS,
        )
        state["run"], state["run_kind"] = run, kind
        state["stop_notify"] = False
        if state["pending_cancel"]:
            state["pending_cancel"] = False
            run.cancel()

        # Auto-check can be set to alert only for NEW links; manual checks always show everything.
        mode = state["notify_mode"] if kind == "auto" else "all"
        notifier = Notifier(bot, chat_id, mode, state["history"])
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

        # ---- statistics + history ----
        state["last_run_time"] = time.time()
        state["last_run_checked"] = run.processed
        state["last_run_working"] = len(run.found)
        state["last_run_unverified"] = run.unverified_count
        if kind == "auto" and not run.cancelled:
            state["cycles_run"] += 1
        if run.found and not run.suspicious:      # never pollute the history with doubtful results
            now = time.time()
            for url in run.found:
                lm = (run.found_meta.get(url) or {}).get("lm")
                entry = state["history"].setdefault(url, {"first": now, "lm": lm})
                if lm:
                    entry["lm"] = lm
            try:
                save_history(chat_id)
            except Exception:
                logger.exception("Could not save history")

        # ---- final report ----
        extra = []
        if notifier.suppressed_cap:
            extra.append(f"Only the first {MAX_ALERTS_PER_RUN} link messages were sent "
                         f"({notifier.suppressed_cap:,} more are in the file).")
        if notifier.suppressed_known:
            extra.append(f"{notifier.suppressed_known:,} already-known link(s) were not repeated (/notify new).")
        if notifier.pending > 0 and not stop_sending():
            extra.append(f"Still sending {notifier.pending:,} working-link message(s) - "
                         "the file below already contains all of them.")
        summary = build_summary(run, extra)
        problem = bool(run.aborted or run.crashed or run.errors)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if kind == "manual":
            try:
                await progress_msg.edit_text(summary)
            except Exception:
                await bot.send_message(chat_id=chat_id, text=summary)
        elif problem:
            await bot.send_message(chat_id=chat_id, text="⚠️ Auto-check problem:\n" + summary)

        if run.found and (kind == "manual" or len(run.found) >= AUTO_FILE_THRESHOLD):
            p = workdir / f"working_links_{stamp}.txt"
            lines = result_lines([(u, (run.found_meta.get(u) or {}).get("lm")) for u in run.found])
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            await send_text_file(bot, chat_id, p,
                                 f"All {len(run.found):,} working link(s) from this check (newest file first).")

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
            logger.warning("%d link message(s) could not be delivered individually (they are in the results file)",
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
            await notifier.aclose()
        state["run"] = None
        state["run_kind"] = None
        state["busy"] = False
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


def _log_task_result(task: asyncio.Task):
    if not task.cancelled() and task.exception():
        logger.error("Background check task failed", exc_info=task.exception())


def _start_background_check(context, chat_id: int, progress_msg, words_override=None):
    task = asyncio.create_task(run_check_session(context.bot, chat_id, "manual", progress_msg, words_override))
    task.add_done_callback(_log_task_result)
    get_state(chat_id)["task"] = task      # keep a reference so it is never garbage-collected


def _check_ready(state: dict, words=None) -> str | None:
    """Return an error message if a check can't start, else None."""
    words = state["words"] if words is None else words
    if not words or not state["links"]:
        return "⚠️ Please send both wordlist.txt and linklist.txt first. Use /help to see how."
    total = checker.count_combinations(state["links"], words)
    if total == 0:
        return "⚠️ Nothing to check - your lists produce 0 links."
    if total > MAX_COMBINATIONS:
        return (f"⚠️ {total:,} combinations ({len(state['links']):,} links × {len(words):,} words) "
                f"is more than the limit of {MAX_COMBINATIONS:,} per run.\n"
                "Split your lists into smaller files, or raise MAX_COMBINATIONS in .env.")
    return None


# ============================================================
# COMMANDS
# ============================================================

@restricted
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>Welcome to Python Url Checker!</b>\n\n"
        f"I check huge batches of links at high speed (up to {MAX_COMBINATIONS:,} link × word "
        "combinations in one run) and tell you the instant a file really goes live — "
        "with its image and the date it was uploaded.\n\n"
        "<b>How it works</b>\n"
        "1️⃣ Send me <code>wordlist.txt</code> — one word per line.\n"
        "2️⃣ Send me <code>linklist.txt</code> — one link per line, with <code>(Word)</code> "
        "where the word should go.\n"
        "3️⃣ Send /check — every link is tested with every word, with live progress, speed and "
        "time left. /cancel stops it any time.\n"
        "4️⃣ Optional: <code>/autocheck 5</code> re-checks automatically every 5 minutes.\n\n"
        "Quick tools: /addword (add + check new words instantly), /test (diagnose one link), "
        "/found (everything found so far).\n\nType /help for all commands. 🚀"
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
        "<b>Checking</b>\n"
        "/check — check every link with every word (live progress + time left)\n"
        "/cancel — stop a running check (also works as /cancle)\n"
        "/addword <code>Word1 Word2</code> — add words to your list and check ONLY those right away\n"
        "/test <code>link</code> — see exactly what the server answers for one link\n\n"
        "<b>Results</b>\n"
        "/found — every working link found so far, newest first (<code>/found clear</code> empties it)\n\n"
        "<b>Automatic checking</b>\n"
        "/autocheck <code>N</code> — re-check every N minutes (1–10), status update every 30 min. "
        "It resumes by itself after a restart.\n"
        "/notify <code>all</code> | <code>new</code> — auto-check alerts: every time, or only for NEW links\n"
        "/stopautocheck — stop it\n\n"
        "<b>Other</b>\n"
        "/status — what is loaded / running · /resetstats — clear counters · /id — this chat's ID\n"
        "/allow, /disallow, /allowed — (admin) manage which chats and groups may use the bot\n\n"
        f"Limit: {MAX_COMBINATIONS:,} combinations per run. A link only counts as working when a real "
        "file is there. Network errors are retried, and anything that still can't be verified is "
        "reported — nothing is silently skipped."
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
        f"Auto-check: {auto} · alerts: <b>{state['notify_mode']}</b>",
        f"Auto-check cycles completed: <code>{state['cycles_run']}</code>"
        + (f" (skipped {state['skipped_cycles']} because a check was still running)" if state["skipped_cycles"] else ""),
        f"Last check: {last} — checked <code>{state['last_run_checked']:,}</code>, "
        f"working <code>{state['last_run_working']:,}</code>, "
        f"unverified <code>{state['last_run_unverified']:,}</code>",
        f"Working links in history: <code>{len(state['history']):,}</code> (see /found)",
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
    state.update(cycles_run=0, skipped_cycles=0, last_run_time=None,
                 last_run_checked=0, last_run_working=0, last_run_unverified=0)
    await update.effective_message.reply_text("🧹 Counters cleared. (Your found-links history is kept - use /found clear to empty it.)")


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
    # Runs in the background so the bot stays responsive (this is what makes /cancel work).
    _start_background_check(context, chat_id, progress_msg)


@restricted
async def addword_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add words to the list and immediately check ONLY those words against every link."""
    chat_id = update.effective_chat.id
    msg = update.effective_message
    state = get_state(chat_id)

    given = [w.strip() for w in context.args if w.strip()]
    if not given:
        await msg.reply_text("Usage: /addword Word1 Word2 Word3\n"
                             "Adds the words to your wordlist and checks only those words against all your links.")
        return
    existing = set(state["words"])
    new = [w for w in dict.fromkeys(given) if w not in existing]
    if not new:
        await msg.reply_text("Those word(s) are already in your wordlist. Use /check to check everything.")
        return

    state["words"] = state["words"] + new
    (chat_dir(chat_id) / "wordlist.txt").write_text("\n".join(state["words"]) + "\n", encoding="utf-8")
    shown = ", ".join(new[:10]) + (" ..." if len(new) > 10 else "")
    text = f"✅ Added {len(new)} word(s): {shown}\nWords in list: {len(state['words']):,}"

    if not state["links"]:
        await msg.reply_text(text + "\nNow send linklist.txt, then /check.")
        return
    if state["busy"]:
        await msg.reply_text(text + "\n⏳ A check is running - the new word(s) will be included in the next /check.")
        return
    problem = _check_ready(state, new)
    if problem:
        await msg.reply_text(text + "\n" + problem)
        return

    total = checker.count_combinations(state["links"], new)
    state["busy"] = True
    state["pending_cancel"] = False
    try:
        progress_msg = await msg.reply_text(
            f"{text}\n🚀 Checking only the new word(s) with all {len(state['links']):,} links "
            f"({total:,} combinations)...\nSend /cancel to stop."
        )
    except Exception:
        state["busy"] = False
        raise
    _start_background_check(context, chat_id, progress_msg, words_override=new)


@restricted
async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Diagnose one link: what does the server really answer?"""
    msg = update.effective_message
    state = get_state(update.effective_chat.id)
    if not context.args:
        await msg.reply_text("Usage: /test https://example.com/path/image.jpg\n"
                             "Shows exactly what the server answers for that link, and for a similar link "
                             "that cannot exist - so you can see why a link is (not) counted as working.")
        return
    url = context.args[0].strip()
    if checker.PLACEHOLDER_RE.search(url):
        if not state["words"]:
            await msg.reply_text("That link contains (Word). Send a real link, or upload your wordlist first.")
            return
        url = checker.PLACEHOLDER_RE.sub(lambda m: checker.quote_word(state["words"][0]), url)
    if not url.lower().startswith(("http://", "https://")):
        await msg.reply_text("Please send a full link starting with http:// or https://")
        return

    wait = await msg.reply_text("🔬 Testing the link...")
    try:
        res = await checker.inspect_url(url)
    except Exception as e:
        await wait.edit_text(f"❌ The test failed: {type(e).__name__}: {e}"[:500])
        return

    def block(label: str, info: dict) -> list[str]:
        if info.get("error"):
            return [f"{label}", f"  Error: {info['error']}"]
        out = [label, f"  HTTP status: {info['status']}"]
        if info["status"] in (200, 206):
            out.append(f"  Content-Type: {info['ctype'] or 'not given'}")
            if info.get("total"):
                out.append(f"  Size: {fmt_size(info['total'])}")
            out.append("  Real image: " + (f"yes ({info['kind']}) ✅" if info.get("kind") else "no ❌"))
            out.append("  Last modified: " + (fmt_bd(info.get("lm")) or "not provided by the server"))
        return out

    main, ctrl = res["main"], res["control"]
    lines = ["🔬 Test result", url, ""]
    lines += block("This link:", main)
    lines.append("")
    lines.append(("✅ VERDICT: WORKING — " if res["ok"] else "❌ VERDICT: NOT working — ") + res["reason"])
    lines.append("")
    lines.append("Control (a link that certainly does not exist):")
    lines.append("  " + (ctrl.get("url") or ""))
    if ctrl.get("error"):
        lines.append(f"  Error: {ctrl['error']}")
    else:
        lines.append(f"  HTTP status: {ctrl['status']}")
        if ctrl["status"] in (200, 206):
            lines.append("  ⚠️ The server ALSO returns a file for links that don't exist, so it can't be trusted "
                         "on status code alone - the bot compares the file itself (done automatically).")
        else:
            lines.append("  ✅ The server correctly refuses missing files.")
    await wait.edit_text("\n".join(lines)[:4000], link_preview_options=NO_PREVIEW)


@restricted
async def found_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = update.effective_message
    state = get_state(chat_id)
    hist = state["history"]

    if context.args and context.args[0].lower() == "clear":
        hist.clear()
        save_history(chat_id)
        await msg.reply_text("🧹 Found-links history cleared.")
        return
    if not hist:
        await msg.reply_text("Nothing found yet. Working links appear here after a check finds them.")
        return

    ordered = sorted(hist.items(), key=lambda kv: (lm_epoch(kv[1].get("lm")), kv[1].get("first", 0)), reverse=True)
    top = ordered[:10]
    lines = [f"📚 {len(ordered):,} working link(s) found so far. Newest first:", ""]
    for url, entry in top:
        lines.append(url)
        lines.append("   📅 " + (fmt_bd(entry.get("lm")) or "Last modified: not provided"))
    if len(ordered) > len(top):
        lines.append(f"\n...and {len(ordered) - len(top):,} more in the file below.")
    await msg.reply_text("\n".join(lines), link_preview_options=NO_PREVIEW)

    if len(ordered) > len(top):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / f"all_found_links_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            p.write_text("\n".join(result_lines([(u, e.get("lm")) for u, e in ordered])) + "\n", encoding="utf-8")
            await send_text_file(context.bot, chat_id, p, f"All {len(ordered):,} working links found so far.")


@restricted
async def notify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = update.effective_message
    state = get_state(chat_id)
    choice = context.args[0].lower() if context.args else ""
    if choice not in ("all", "new"):
        await msg.reply_text(
            f"Auto-check alerts are set to: {state['notify_mode']}\n\n"
            "/notify all — message me every time a working link is found, every cycle\n"
            "/notify new — message me only for links I haven't been told about before")
        return
    state["notify_mode"] = choice
    save_settings(chat_id)
    await msg.reply_text("✅ Auto-check will alert you every time a working link is found." if choice == "all"
                         else "✅ Auto-check will alert you only for NEW links (everything is still in /found).")


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


# ---------------- automatic checking ----------------

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
        f"Checking every: <code>{state['interval_minutes']}</code> min · alerts: <b>{state['notify_mode']}</b>",
        f"Cycles completed: <code>{state['cycles_run']}</code>",
        f"Last check: {last}",
        f"Last check: <code>{state['last_run_checked']:,}</code> links, "
        f"<code>{state['last_run_working']:,}</code> working, "
        f"<code>{state['last_run_unverified']:,}</code> unverified",
        f"Working links in history: <code>{len(state['history']):,}</code>",
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


def schedule_autocheck(job_queue, chat_id: int, minutes: int, first: int = 5):
    for name in (f"check-{chat_id}", f"heartbeat-{chat_id}"):
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    job_queue.run_repeating(autocheck_job, interval=minutes * 60, first=first,
                            chat_id=chat_id, name=f"check-{chat_id}")
    job_queue.run_repeating(heartbeat_job, interval=HEARTBEAT_SECONDS, first=HEARTBEAT_SECONDS,
                            chat_id=chat_id, name=f"heartbeat-{chat_id}")


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

    state["interval_minutes"] = minutes
    state["cycles_run"] = 0
    state["skipped_cycles"] = 0
    save_settings(chat_id)
    schedule_autocheck(context.job_queue, chat_id, minutes)
    mode_note = ("every working link every cycle" if state["notify_mode"] == "all"
                 else "only NEW links (change with /notify all)")
    await msg.reply_text(
        f"✅ Auto-check enabled: every {minutes} minute(s) I re-check every link with every word.\n"
        f"Alerts: {mode_note}. Status update every 30 minutes. It resumes by itself if the bot restarts.\n"
        "If a check takes longer than the interval, the next one waits (checks never overlap)."
    )


@restricted
async def stopautocheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    msg = update.effective_message
    jobs = context.job_queue.get_jobs_by_name(f"check-{chat_id}") + \
        context.job_queue.get_jobs_by_name(f"heartbeat-{chat_id}")
    if not jobs and not state["interval_minutes"]:
        await msg.reply_text("Auto-check isn't running right now.")
        return
    for job in jobs:
        job.schedule_removal()
    state["interval_minutes"] = None
    save_settings(chat_id)
    run = state["run"]
    if run is not None and not run.finished and state["run_kind"] == "auto":
        run.cancel()
    await msg.reply_text("🛑 Auto-check and status updates stopped.")


# ---------------- uploads ----------------

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

async def restore_autocheck(app: Application):
    """After a restart, switch auto-check back on for chats that had it on."""
    for d in DATA_DIR.iterdir():
        name = d.name
        if not d.is_dir() or not name.lstrip("-").isdigit():
            continue
        chat_id = int(name)
        if not is_allowed(chat_id):
            continue
        state = get_state(chat_id)
        minutes = state["interval_minutes"]
        if not isinstance(minutes, int) or not (MIN_AUTOCHECK_MINUTES <= minutes <= MAX_AUTOCHECK_MINUTES):
            continue
        if _check_ready(state):
            continue
        schedule_autocheck(app.job_queue, chat_id, minutes, first=15)
        try:
            await app.bot.send_message(chat_id=chat_id, text=f"♻️ Auto-check resumed after the restart: every {minutes} minute(s).")
        except Exception:
            logger.info("Could not tell chat %s that auto-check resumed", chat_id)


async def post_init(app: Application):
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "Welcome message"),
            BotCommand("help", "How to use the bot"),
            BotCommand("check", "Check all links with all words"),
            BotCommand("cancel", "Cancel the running check"),
            BotCommand("addword", "Add words and check only those"),
            BotCommand("test", "Diagnose one link"),
            BotCommand("found", "All working links found so far"),
            BotCommand("autocheck", "Re-check automatically (1-10 min)"),
            BotCommand("notify", "Auto-check alerts: all or new"),
            BotCommand("stopautocheck", "Stop automatic checking"),
            BotCommand("status", "Show status"),
            BotCommand("id", "Show this chat's ID"),
        ])
    except Exception:
        logger.warning("Could not set the command menu", exc_info=True)
    try:
        await restore_autocheck(app)
    except Exception:
        logger.warning("Could not restore auto-check", exc_info=True)


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
    app.add_handler(CommandHandler("addword", addword_cmd))
    app.add_handler(CommandHandler("test", test_cmd))
    app.add_handler(CommandHandler("found", found_cmd))
    app.add_handler(CommandHandler("autocheck", autocheck_cmd))
    app.add_handler(CommandHandler("stopautocheck", stopautocheck_cmd))
    app.add_handler(CommandHandler("notify", notify_cmd))
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

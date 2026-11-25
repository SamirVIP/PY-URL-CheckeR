"""
Python Url Checker - Telegram Bot
==================================

Send the bot two files (wordlist.txt, linklist.txt) and /check tests EVERY link with
EVERY word - up to 10,000,000 combinations per run - and sends each working link with
its picture and "Last modified" date in the user's own country time.

Who can use it
--------------
* Admins (ADMIN_IDS / personal IDs in ALLOWED_CHAT_IDS) can do everything.
* Everyone else needs the admin's approval first. The admin can give a user ALL
  commands, or only some of them (for example only /test).
* Every user chooses a country on first /start; all times are shown in that time zone.
* Statistics reset every day at 04:00 AM Bangladesh time (shown in each user's time).

Run:  python bot.py   (see README.md)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shutil
import tempfile
import time
from collections import OrderedDict
from urllib.parse import quote
from datetime import datetime, time as dtime, timezone
from functools import wraps
from pathlib import Path

import aiohttp
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import checker
import countries as C
import ui
from access import PERMISSIONS, UPLOAD_PERMS, AccessStore, StatsStore, parse_perm_words

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
    for part in text.replace(";", ",").replace("\n", ",").split(","):
        part = part.strip().strip("\"'").strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError:
                pass
    return ids


BOT_TOKEN = os.environ.get("BOT_TOKEN", "8593278400:AAGTYrj2d60CfEAW1-VHVWltzrzKM_rnlNs").strip().strip("\"'").strip()

# ALLOWED_CHAT_IDS: personal IDs (positive) are ADMINS; group IDs (negative, -100...) are allowed groups.
ENV_ALLOWED = _parse_ids(os.environ.get("ALLOWED_CHAT_IDS", "6206433961"))
ADMIN_IDS = _parse_ids(os.environ.get("ADMIN_IDS", "6206433961")) | {i for i in ENV_ALLOWED if i > 0}
ENV_GROUPS = {i for i in ENV_ALLOWED if i < 0}

ON_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
VOLUME_PATH = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()


def _pick_data_dir() -> tuple[Path, str]:
    """DATA_DIR if set, else the Railway volume if one is attached, else ./data.
    If the chosen folder can't be written, fall back to ./data instead of crashing."""
    wanted = os.environ.get("DATA_DIR", "").strip() or VOLUME_PATH or "data"
    for candidate in (wanted, "data"):
        try:
            path = Path(candidate)
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_test"
            probe.write_text("ok")
            probe.unlink()
            return path, (f"{candidate} (fallback: {wanted!r} is not writable)" if candidate != wanted else candidate)
        except OSError:
            continue
    raise SystemExit("Cannot write to any data folder. Set DATA_DIR to a writable folder.")


DATA_DIR, DATA_DIR_NOTE = _pick_data_dir()

MAX_COMBINATIONS = _env_int("MAX_COMBINATIONS", 10_000_000)   # per run
CONCURRENCY = _env_int("CONCURRENCY", 1000)                  # maximum simultaneous requests (auto-adjusts)
PROCESSES = _env_int("PROCESSES", 0) or None                 # 0 = automatic
CONNECT_TIMEOUT = _env_float("CONNECT_TIMEOUT", 6.0)
TOTAL_TIMEOUT = _env_float("TOTAL_TIMEOUT", 12.0)
ATTEMPTS = _env_int("ATTEMPTS", 3)

MAX_ALERTS_PER_RUN = _env_int("MAX_ALERTS_PER_RUN", 500)     # 0 = unlimited (the results file always has ALL)
MAX_IMAGE_BYTES = _env_int("MAX_IMAGE_MB", 20) * 1024 * 1024
MAX_UPLOAD_BYTES = 20 * 1024 * 1024                          # Telegram's limit for files bots can download
HISTORY_LIMIT = 50_000

PROGRESS_SECONDS_PRIVATE = 3.0
PROGRESS_SECONDS_GROUP = 6.0
NOTIFY_PACE_PRIVATE = 0.3
NOTIFY_PACE_GROUP = 1.5
HEARTBEAT_SECONDS = 30 * 60
AUTO_FILE_THRESHOLD = 10
MIN_AUTOCHECK_MINUTES = 1
MAX_AUTOCHECK_MINUTES = 10

HTML = ParseMode.HTML
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("python-url-checker-bot")

store = AccessStore(DATA_DIR, ADMIN_IDS)
stats = StatsStore(DATA_DIR)


# ============================================================
# ALLOWED GROUPS
# ============================================================

GROUPS_FILE = DATA_DIR / "allowed_chats.json"


def _load_groups() -> set[int]:
    try:
        return {int(x) for x in json.loads(GROUPS_FILE.read_text()) if int(x) < 0}
    except Exception:
        return set()


dynamic_groups: set[int] = _load_groups()


def _save_groups():
    GROUPS_FILE.write_text(json.dumps(sorted(dynamic_groups)))


def group_allowed(chat_id: int) -> bool:
    return chat_id in ENV_GROUPS or chat_id in dynamic_groups


# ============================================================
# TELEGRAM HELPERS
# ============================================================

EMOJI_FILE = DATA_DIR / "premium_emoji.json"
PREMIUM_ENABLED = os.environ.get("PREMIUM_EMOJI", "1").strip().lower() not in ("0", "false", "no", "off")


def load_premium():
    data = {}
    try:
        data = json.loads(EMOJI_FILE.read_text(encoding="utf-8")) if EMOJI_FILE.exists() else {}
    except Exception:
        logger.warning("Could not read %s", EMOJI_FILE, exc_info=True)
    if not isinstance(data, dict):
        data = {}
    ui.set_premium(data.get("map") or {}, bool(data.get("on", True)) and PREMIUM_ENABLED)


def save_premium():
    try:
        EMOJI_FILE.write_text(json.dumps({"on": ui.PREMIUM["on"], "map": ui.PREMIUM["map"]}, ensure_ascii=False),
                              encoding="utf-8")
    except Exception:
        logger.exception("Could not save the premium emoji")


def install_premium_emoji():
    """Every HTML message / caption the bot sends or edits passes through ui.premiumize() first.
    If Telegram ever refuses it, the very same message is sent again with normal emoji - nothing is lost."""
    from telegram.ext import ExtBot

    def wrap(name: str, field: str):
        orig = getattr(ExtBot, name)

        @wraps(orig)
        async def patched(self, *args, **kwargs):
            text = kwargs.get(field)
            new = ui.premiumize(text) if isinstance(text, str) and kwargs.get("parse_mode") == HTML else text
            if new is text or new == text:
                return await orig(self, *args, **kwargs)
            try:
                return await orig(self, *args, **dict(kwargs, **{field: new}))
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    raise
                logger.info("Premium emoji refused by Telegram (%s) - sending normal emoji", e)
                return await orig(self, *args, **kwargs)
        setattr(ExtBot, name, patched)

    for name, field in (("send_message", "text"), ("edit_message_text", "text"), ("send_photo", "caption"),
                        ("send_document", "caption"), ("edit_message_caption", "caption")):
        wrap(name, field)


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


def retry_seconds(exc: RetryAfter) -> float:
    ra = exc.retry_after
    return (ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)) + 0.5


async def say(msg, text: str, markup=None):
    """Reply with an HTML card. Long texts are split on line breaks."""
    chunks = [text]
    if len(text) > 3900:
        chunks, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > 3800:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        chunks.append(cur)
    sent = None
    for k, chunk in enumerate(chunks):
        sent = await msg.reply_text(chunk, parse_mode=HTML, link_preview_options=NO_PREVIEW,
                                    reply_markup=markup if k == len(chunks) - 1 else None)
    return sent


async def edit_card(message, text: str, markup=None) -> bool:
    """Edit a message to a new card; harmless if nothing changed."""
    for use_markup in ((True, False) if markup is not None else (False,)):
        try:
            await message.edit_text(text[:4000], parse_mode=HTML, link_preview_options=NO_PREVIEW,
                                    reply_markup=markup if use_markup else None)
            return True
        except RetryAfter as e:
            await asyncio.sleep(retry_seconds(e))
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return True
            logger.info("edit failed: %s", e)
        except Exception:
            logger.exception("edit failed")
            return False
    return False


async def notify_user(bot, uid: int, text: str):
    try:
        await bot.send_message(chat_id=uid, text=text, parse_mode=HTML, link_preview_options=NO_PREVIEW)
    except Exception:
        logger.info("Could not message user %s", uid)


_throttle: dict[tuple, float] = {}


def throttled(key: tuple, seconds: float = 30.0) -> bool:
    """True if this key was used less than `seconds` ago (so don't repeat the reply)."""
    now = time.monotonic()
    last = _throttle.get(key)
    if last is not None and now - last < seconds:
        return True
    _throttle[key] = now
    if len(_throttle) > 5000:               # keep the table small
        for k in [k for k, v in _throttle.items() if now - v > 300]:
            _throttle.pop(k, None)
    return False


def is_private(chat) -> bool:
    return chat.type == "private"


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
        json.dumps({"interval": st["interval_minutes"], "notify": st["notify_mode"],
                    "source": st.get("source", "own"), "speed": st.get("last_speed")}), encoding="utf-8")


def save_history(chat_id: int):
    hist = chat_state[chat_id]["history"]
    if len(hist) > HISTORY_LIMIT:
        keep = sorted(hist.items(), key=lambda kv: kv[1].get("first", 0), reverse=True)[:HISTORY_LIMIT]
        hist.clear()
        hist.update(keep)
    (chat_dir(chat_id) / "found_history.json").write_text(json.dumps(hist), encoding="utf-8")


def save_blocklist(chat_id: int):
    (chat_dir(chat_id) / "blocklist.json").write_text(json.dumps(chat_state[chat_id]["blocked"]), encoding="utf-8")


def get_state(chat_id: int) -> dict:
    if chat_id not in chat_state:
        d = chat_dir(chat_id)
        settings = _read_json(d / "settings.json", {})
        blocked = _read_json(d / "blocklist.json", {})
        notify = settings.get("notify", "all")
        source = settings.get("source", "own")
        chat_state[chat_id] = {
            "words": checker.read_lines(d / "wordlist.txt")[0],
            "links": checker.read_lines(d / "linklist.txt")[0],
            "history": _read_json(d / "found_history.json", {}),
            "blocked": blocked if isinstance(blocked, dict) else {},     # url -> time blocked
            "notify_mode": notify if notify in ("all", "new") else "all",
            "source": source if source in ("own", "mena") else "own",    # lists the LAST check used (/autocheck repeats it)
            "mena_warned": False,
            "last_speed": settings.get("speed") if isinstance(settings.get("speed"), (int, float)) else None,
            "interval_minutes": settings.get("interval"),
            "run": None,
            "run_kind": None,
            "busy": False,
            "pending_cancel": False,
            "stop_notify": False,
            "last_run_time": None,
            "last_run_checked": 0,
            "last_run_working": 0,
            "last_run_unverified": 0,
        }
    return chat_state[chat_id]


# ============================================================
# ACCESS CONTROL  (admin approval, per-command permissions, country onboarding)
# ============================================================

async def submit_request(context, rec: dict):
    """Tell the admins that someone wants access (only once per user)."""
    if rec.get("requested"):
        return
    delivered = False
    for admin in store.admin_ids:
        try:
            await context.bot.send_message(chat_id=admin, text=ui.request_admin_card(rec), parse_mode=HTML,
                                           reply_markup=kb(ui.request_buttons(rec["id"])))
            delivered = True
        except Exception:
            logger.info("Could not notify admin %s (the admin must send /start to the bot once)", admin)
    if delivered:                      # otherwise try again next time the user writes
        rec["requested"] = True
        store.save()


COUNTRY_PAGE = 10


def country_page(page: int, scope: str) -> tuple[list[list[tuple[str, str]]], int]:
    pages = (len(C.COUNTRIES) + COUNTRY_PAGE - 1) // COUNTRY_PAGE
    page = max(0, min(page, pages - 1))
    chunk = list(range(page * COUNTRY_PAGE, min(len(C.COUNTRIES), (page + 1) * COUNTRY_PAGE)))
    rows, row = [], []
    for idx in chunk:
        row.append((C.country_label(idx), f"cty:s:{scope}:{idx}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("◀️", f"cty:p:{scope}:{max(0, page - 1)}"), (f"{page + 1}/{pages}", f"cty:p:{scope}:{page}"),
                 ("▶️", f"cty:p:{scope}:{min(pages - 1, page + 1)}")])
    return rows, page


async def send_country_picker(msg, first_time: bool, scope: str = "u"):
    rows, _ = country_page(0, scope)
    await say(msg, ui.country_picker_text(first_time), kb(rows))


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE, need, cmd: str) -> bool:
    """Decide whether this user may run this command. Sends the right explanation if not."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat is None or user is None or msg is None:
        return False
    rec = store.touch(user)
    private = is_private(chat)

    if not private and not group_allowed(chat.id):
        if not throttled(("grp", chat.id), 60):
            await say(msg, ui.group_denied_card(chat.id))
        return False

    status = store.status(user.id)
    if status == "denied":
        if not throttled(("den", user.id), 60):
            await say(msg, ui.denied_card())
        return False
    if status == "pending":
        if not private:
            if not throttled(("pend", user.id), 60):
                await say(msg, ui.notice("🔒", "Access required",
                                         "You need the admin's approval first.",
                                         "<i>Open a private chat with me and send /start.</i>"))
            return False
        if not store.has_tz(user.id):
            await send_country_picker(msg, first_time=True)
            return False
        await submit_request(context, rec)
        await say(msg, ui.pending_card(rec["name"], None))
        return False

    if need is not None and not store.can(user.id, need):
        if not throttled(("perm", user.id, cmd), 20):
            await say(msg, ui.no_perm_card(cmd, store.allowed_keys(user.id)))
        return False
    if private and not store.has_tz(user.id):
        await send_country_picker(msg, first_time=True)
        return False
    return True


def gated(need, cmd: str | None = None):
    def deco(handler):
        name = cmd or handler.__name__.removesuffix("_cmd")

        @wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not await gate(update, context, need, name):
                return
            return await handler(update, context)
        return wrapper
    return deco


def admin_only(private_only: bool = False):
    def deco(handler):
        @wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
            if chat is None or user is None or msg is None:
                return
            if not store.is_admin(user.id):
                if not throttled(("adm", user.id), 30):
                    await say(msg, ui.notice("⛔", "Admin only", "This command is only for the bot admin."))
                return
            if private_only and not is_private(chat):
                await say(msg, ui.notice("🔐", "Private chat only",
                                         "<i>For safety, please use this command in a private chat with me.</i>"))
                return
            return await handler(update, context)
        return wrapper
    return deco


def tz_of(chat_id: int) -> C.TzCtx:
    return store.tz_ctx(chat_id)


async def stop_autocheck_for(context, chat_id: int):
    """Switch auto-check off for a chat (used when a user loses access)."""
    jq = context.job_queue
    if jq is not None:
        for name in (f"check-{chat_id}", f"heartbeat-{chat_id}"):
            for job in jq.get_jobs_by_name(name):
                job.schedule_removal()
    st = chat_state.get(chat_id)
    if st:
        st["interval_minutes"] = None
        save_settings(chat_id)


async def enforce_permissions(context, uid: int):
    """After a permission change: stop what the user may no longer do (private chat = user id)."""
    if not store.can(uid, "autocheck"):
        await stop_autocheck_for(context, uid)
    st = chat_state.get(uid)
    if st and st["run"] is not None and not st["run"].finished and not store.can(uid, ("check", "addword", "autocheck")):
        st["run"].cancel()


# ============================================================
# DATES / RESULT FILES
# ============================================================

def checker_parse(raw):
    from email.utils import parsedate_to_datetime
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(str(raw))
    except Exception:
        return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def lm_epoch(raw) -> float:
    dt = checker_parse(raw)
    return dt.timestamp() if dt else 0.0


def result_lines(items: list[tuple[str, str | None]], tz: C.TzCtx) -> list[str]:
    """[(url, last_modified_raw)] -> text lines, newest file first, in the user's time."""
    rows = sorted(((lm_epoch(raw), url, checker_parse(raw)) for url, raw in items), key=lambda r: r[0], reverse=True)
    return [f"{url}  |  Last modified: {tz.fmt(dt) + ' (' + tz.label + ')' if dt else 'not provided by the server'}"
            for _, url, dt in rows]


# ============================================================
# SENDING "WORKING LINK" MESSAGES (never lose one)
# ============================================================

class Notifier:
    """Sends found links one by one (paced + retried), WITH the image.

    Delivery order (first that works): download the picture and upload it as a photo ->
    send it as a file -> ask Telegram to fetch the link -> the link as text.
    Captions are HTML (escaped); if Telegram ever refuses the formatting, a plain caption is used.
    """

    def __init__(self, bot, chat_id: int, mode: str, history: dict, tz: C.TzCtx, blocked: dict):
        self.bot, self.chat_id, self.mode, self.history, self.tz = bot, chat_id, mode, history, tz
        self.blocked = blocked              # live reference: a link blocked mid-run is dropped from the queue too
        self.suppressed_blocked = 0
        self.pace = NOTIFY_PACE_GROUP if chat_id < 0 else NOTIFY_PACE_PRIVATE
        self.queue: asyncio.Queue = asyncio.Queue()
        self.pending = 0
        self.accepted = 0
        self.sent = 0
        self.suppressed_cap = 0
        self.suppressed_known = 0
        self.failed: list[str] = []
        self.delivered: set[str] = set()
        self.parse_error = False
        self.prefetched = 0
        self.prefetch_tasks: set[asyncio.Task] = set()
        self.http = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
            timeout=aiohttp.ClientTimeout(total=60),
            headers=checker.BASE_HEADERS,
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        self.task = asyncio.create_task(self._loop())

    def add(self, url: str, meta: dict):
        if url in self.blocked:
            self.suppressed_blocked += 1
            return
        known = url in self.history
        if self.mode == "new" and known:
            self.suppressed_known += 1
            return
        if MAX_ALERTS_PER_RUN and self.accepted >= MAX_ALERTS_PER_RUN:
            self.suppressed_cap += 1
            return
        self.accepted += 1
        self.pending += 1
        task = None
        if self.prefetched < 6:             # download the picture while earlier ones are being sent
            self.prefetched += 1
            task = asyncio.create_task(self._download(url))
            self.prefetch_tasks.add(task)
            task.add_done_callback(self.prefetch_tasks.discard)
        self.queue.put_nowait((url, meta or {}, known, task))

    async def _loop(self):
        while True:
            url, meta, known, task = await self.queue.get()
            if task is not None:
                self.prefetched -= 1
            if url in self.blocked:         # blocklisted while waiting in the queue
                if task is not None:
                    task.cancel()
                self.suppressed_blocked += 1
                self.pending -= 1
                continue
            try:
                if await self.deliver(url, meta, known, task):
                    self.sent += 1
                    self.delivered.add(url)
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
        """(bytes | None, last_modified_raw | None, size | None)"""
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
        """One Telegram send. True = delivered, False = Telegram refused it (try the next way)."""
        for _ in range(attempts):
            try:
                await send()
                return True
            except RetryAfter as e:
                await asyncio.sleep(retry_seconds(e))
            except (BadRequest, Forbidden) as e:        # before NetworkError: BadRequest is a subclass
                low = str(e).lower()
                if "parse" in low or "entities" in low:
                    self.parse_error = True
                logger.info("Telegram refused a message: %s", e)
                return False
            except (TimedOut, NetworkError):
                await asyncio.sleep(1.5)
            except Exception:
                logger.exception("Unexpected error while sending")
                return False
        return False

    async def _chain(self, data, url: str, caption: str, filename: str, html: bool, markup=None) -> bool:
        bot, chat_id = self.bot, self.chat_id
        pm = HTML if html else None
        self.parse_error = False
        if data:
            if await self._try(lambda: bot.send_photo(chat_id=chat_id, photo=data, caption=caption, parse_mode=pm,
                                                         reply_markup=markup)):
                return True
            if self.parse_error:
                return False
            if await self._try(lambda: bot.send_document(chat_id=chat_id, document=data, filename=filename,
                                                         caption=caption, parse_mode=pm, reply_markup=markup)):
                return True
        else:
            if await self._try(lambda: bot.send_photo(chat_id=chat_id, photo=url, caption=caption, parse_mode=pm,
                                                         reply_markup=markup)):
                return True
            if self.parse_error:
                return False
        return await self._try(lambda: bot.send_message(chat_id=chat_id, text=caption, parse_mode=pm,
                                                        link_preview_options=NO_PREVIEW, reply_markup=markup))

    async def deliver(self, url: str, meta: dict, known: bool, task=None) -> bool:
        data, lm, size = await (task if task is not None else self._download(url))
        kind = dims = None
        if data:
            kind = checker.sniff_image(data[:16])
            dims = checker.image_size(data[:262144])
        filename = url.split("?", 1)[0].rsplit("/", 1)[-1] or "image.jpg"
        markup = _alert_markup(_remember_url(url), False)
        for html in (True, False):
            caption = ui.alert_caption(url, lm or meta.get("lm"), size or meta.get("sz"), known, self.tz,
                                       kind, dims, html)
            if await self._chain(data, url, caption, filename, html, markup):
                return True
            if not self.parse_error:
                break
        return False

    async def drain(self, should_abort, timeout: float | None = None):
        start = time.monotonic()
        while self.pending > 0 and not should_abort():
            if timeout is not None and time.monotonic() - start > timeout:
                return
            await asyncio.sleep(0.3)

    async def aclose(self):
        self.task.cancel()
        for t in list(self.prefetch_tasks):
            t.cancel()
        await asyncio.gather(self.task, *self.prefetch_tasks, return_exceptions=True)
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
        text = ui.progress_card(run.snapshot(), run.suspicious)
        if text == last:
            continue
        if await edit_card(msg, text):
            last = text


async def send_failed_links(bot, chat_id: int, urls: list[str]) -> bool:
    """Plain-text fallback for working links that could not be sent with their picture."""
    if not urls:
        return True
    batches, cur, size = [], [], 0
    for u in urls:
        line = f"<code>{ui.e(u)}</code>"
        if cur and size + len(line) > 3300:
            batches.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        batches.append(cur)
    ok = True
    for i, batch in enumerate(batches):
        text = (f"⚠️ <b>{len(urls):,} working link(s) could not be sent with a picture</b>\n\n" if i == 0 else "") + "\n".join(batch)
        sent = False
        for _ in range(3):
            try:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode=HTML, link_preview_options=NO_PREVIEW)
                sent = True
                break
            except RetryAfter as e:
                await asyncio.sleep(retry_seconds(e))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Could not send the fallback link list")
                break
        ok = ok and sent
    return ok


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

async def run_check_session(bot, chat_id: int, kind: str, progress_msg=None, words_override=None, links_override=None):
    """One complete check for a chat (kind = "manual" or "auto"). words_override / links_override: use these lists."""
    state = get_state(chat_id)
    tz = tz_of(chat_id)
    workdir = None
    notifier = None
    ptask = None
    try:
        workdir = Path(tempfile.mkdtemp(prefix="run_", dir=str(chat_dir(chat_id))))
        words = list(words_override) if words_override is not None else list(state["words"])
        links = list(links_override) if links_override is not None else list(state["links"])
        run = checker.CheckRun(
            links, words,
            workdir=workdir, processes=PROCESSES, concurrency=CONCURRENCY,
            connect_timeout=CONNECT_TIMEOUT, total_timeout=TOTAL_TIMEOUT, attempts=ATTEMPTS,
        )
        state["run"], state["run_kind"] = run, kind
        state["stop_notify"] = False
        if state["pending_cancel"]:
            state["pending_cancel"] = False
            run.cancel()

        mode = state["notify_mode"] if kind == "auto" else "all"
        notifier = Notifier(bot, chat_id, mode, state["history"], tz, state["blocked"])
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

        await notifier.drain(stop_sending, timeout=30)

        found_urls = [u for u in run.found if u not in state["blocked"]]      # blocklisted links never reach history/file

        # ---- statistics + history ----
        state["last_run_time"] = time.time()
        state["last_run_checked"] = run.processed
        state["last_run_working"] = len(run.found)
        state["last_run_unverified"] = run.unverified_count
        el = run.elapsed()
        stats.add(chat_id, checks=1, combos=run.processed, found=len(run.found), unverified=run.unverified_count,
                  seconds=el, best_speed=(run.processed / el if el > 0 else 0),
                  auto_cycles=1 if (kind == "auto" and not run.cancelled) else 0)
        if run.processed >= 2000 and el > 3 and not run.cancelled:
            state["last_speed"] = run.processed / el            # used for the "estimated time" on the next start card
            try:
                save_settings(chat_id)
            except Exception:
                logger.exception("Could not save settings")
        # ---- final report ----
        extra = []
        if notifier.suppressed_cap:
            extra.append(f"Only the first {MAX_ALERTS_PER_RUN} link messages were sent "
                         f"({notifier.suppressed_cap:,} more are in the file).")
        if notifier.suppressed_blocked:
            extra.append(f"{notifier.suppressed_blocked:,} blocklisted link(s) were skipped (/blocklist).")
        if notifier.suppressed_known:
            extra.append(f"{notifier.suppressed_known:,} already-known link(s) were not repeated (/notify new).")
        if notifier.pending > 0 and not stop_sending():
            extra.append(f"Still sending {notifier.pending:,} working-link message(s) - "
                         "the file below already contains all of them.")
        summary = ui.summary_card(run, extra, tz)
        problem = bool(run.aborted or run.crashed or run.errors or (kind == "auto" and run.unverified_count))
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if kind == "manual":
            again = kb([[("🔁 Check again", "act:mena" if state.get("source") == "mena" else "act:check"),
                         ("📚 Found links", "act:found")]])
            if not await edit_card(progress_msg, summary, again):
                await bot.send_message(chat_id=chat_id, text=summary[:4000], parse_mode=HTML, reply_markup=again)
        elif problem:
            await bot.send_message(chat_id=chat_id, text=("⚠️ <b>Auto-check problem</b>\n" + summary)[:4000], parse_mode=HTML)

        if found_urls and (kind == "manual" or len(found_urls) >= AUTO_FILE_THRESHOLD):
            p = workdir / f"working_links_{stamp}.txt"
            lines = result_lines([(u, (run.found_meta.get(u) or {}).get("lm")) for u in found_urls], tz)
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            await send_text_file(bot, chat_id, p,
                                 f"📎 All {len(found_urls):,} working link(s) from this check · newest first · {tz.label}")

        if run.unverified_count:
            p = workdir / f"unverified_links_{stamp}.txt"
            written = run.write_unverified(p)
            note = f"⚠️ {run.unverified_count:,} link(s) could not be verified (network/server errors)."
            if written < run.unverified_count:
                note += f" First {written:,} listed."
            await send_text_file(bot, chat_id, p, note)

        await notifier.drain(stop_sending)
        if notifier.failed:
            logger.warning("%d link message(s) could not be delivered individually", len(notifier.failed))
            if not stop_sending():
                # never lose a working link: whatever could not go out with its picture goes out as text
                todo = [u for u in notifier.failed if u not in state["blocked"]]
                if await send_failed_links(bot, chat_id, todo):
                    notifier.delivered.update(todo)

        # history = links that really reached the chat (a link cut by the alert cap, a failed send or a
        # cancelled run stays "new", so /notify new can never swallow it)
        if found_urls and not run.suspicious:
            now = time.time()
            for url in found_urls:
                if url not in notifier.delivered and url not in state["history"]:
                    continue
                lm = (run.found_meta.get(url) or {}).get("lm")
                entry = state["history"].setdefault(url, {"first": now, "lm": lm})
                if lm:
                    entry["lm"] = lm
            try:
                save_history(chat_id)
            except Exception:
                logger.exception("Could not save history")
        stats.add(chat_id, alerts=notifier.sent)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Check session crashed")
        try:
            await bot.send_message(chat_id=chat_id, parse_mode=HTML,
                                   text=ui.warn("Something went wrong", "<i>The check could not finish. Please try again.</i>"))
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


def _start_background_check(context, chat_id: int, progress_msg, words_override=None, links_override=None):
    task = asyncio.create_task(run_check_session(context.bot, chat_id, "manual", progress_msg, words_override,
                                                 links_override))
    task.add_done_callback(_log_task_result)
    get_state(chat_id)["task"] = task      # keep a reference so it is never garbage-collected


def _check_ready(state: dict, words=None, links=None) -> str | None:
    """HTML error text if a check can't start, else None."""
    words = state["words"] if words is None else words
    links = state["links"] if links is None else links
    if not words or not links:
        return ui.warn("Files needed", "Please send both <code>wordlist.txt</code> and <code>linklist.txt</code> first.",
                       "<i>Use /help to see how.</i>")
    total = checker.count_combinations(links, words)
    if total == 0:
        return ui.warn("Nothing to check", "Your lists produce 0 links.")
    if total > MAX_COMBINATIONS:
        return ui.warn("Too many combinations",
                       f"<b>{total:,}</b> ({len(links):,} links × {len(words):,} words) is more than the "
                       f"limit of <b>{MAX_COMBINATIONS:,}</b> per run.",
                       "<i>Split your lists into smaller files, or raise MAX_COMBINATIONS in .env.</i>")
    return None


# ============================================================
# ONBOARDING: /start, /country, /help, /id
# ============================================================

async def finish_onboarding(context, chat_id: int, user, rec: dict):
    """After the country is chosen: welcome approved users, send the request for new ones."""
    status = store.status(user.id)
    ctx = tz_of(user.id)
    if status in ("admin", "approved"):
        keys = store.allowed_keys(user.id)
        await context.bot.send_message(
            chat_id=chat_id, parse_mode=HTML, link_preview_options=NO_PREVIEW,
            text=ui.welcome_card(rec["name"], ctx, store.access_text(user.id), keys, MAX_COMBINATIONS, store.is_admin(user.id)))
    elif status == "pending":
        await submit_request(context, rec)
        await context.bot.send_message(chat_id=chat_id, text=ui.pending_card(rec["name"], ctx), parse_mode=HTML)
    else:
        await context.bot.send_message(chat_id=chat_id, text=ui.denied_card(), parse_mode=HTML)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat is None or user is None or msg is None:
        return
    rec = store.touch(user)
    if not is_private(chat):
        if not group_allowed(chat.id):
            if not throttled(("grp", chat.id), 60):
                await say(msg, ui.group_denied_card(chat.id))
            return
        if not store.can(user.id, None):
            if not throttled(("pend", user.id), 60):
                await say(msg, ui.notice("🔒", "Access required", "You need the admin's approval first.",
                                         "<i>Open a private chat with me and send /start.</i>"))
            return
        await say(msg, ui.notice("✨", "Ready!", f"🕒 Times here use <b>{ui.e(tz_of(chat.id).label)}</b>.",
                                 "<i>Send /help to see what you can do. Group admins can change the time zone with /country.</i>"))
        return
    if not store.has_tz(user.id):
        await send_country_picker(msg, first_time=True)
        return
    await finish_onboarding(context, chat.id, user, rec)


@gated(None)
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keys = store.allowed_keys(user.id)
    uploads = store.can(user.id, UPLOAD_PERMS)
    await say(update.effective_message, ui.help_card(keys, store.is_admin(user.id), MAX_COMBINATIONS, uploads))


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open to everyone - needed to find a group's ID before allowing it."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat is None or msg is None:
        return
    status = store.status(user.id) if user else "pending"
    where = f"Allowed ✅" if (group_allowed(chat.id) if not is_private(chat) else store.can(user.id, None)) else "Not allowed ❌"
    await say(msg, ui.notice("🆔", "IDs",
                             f"💬 Chat ID  <code>{chat.id}</code>",
                             f"📂 Chat type  <code>{ui.e(chat.type)}</code>",
                             f"👤 Your user ID  <code>{user.id if user else 'unknown'}</code>",
                             f"🎛 Your status  <b>{ui.e(status)}</b>",
                             f"🔐 This chat  <b>{where}</b>"))


async def country_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Choose your country (private chat) or the group's time zone (group admin)."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat is None or user is None or msg is None:
        return
    rec = store.touch(user)
    private = is_private(chat)
    if not private:
        if not group_allowed(chat.id):
            if not throttled(("grp", chat.id), 60):
                await say(msg, ui.group_denied_card(chat.id))
            return
        if not store.is_admin(user.id):
            await say(msg, ui.notice("⛔", "Admin only", "Only the bot admin can change a group's time zone."))
            return
    if store.status(user.id) == "denied":
        await say(msg, ui.denied_card())
        return
    if not context.args:
        await send_country_picker(msg, first_time=private and not store.has_tz(user.id), scope="u" if private else "g")
        return
    ctx = C.ctx_from_text(" ".join(context.args))
    if ctx is None:
        await say(msg, ui.warn("Country not found",
                               "Try the country name, e.g. <code>/country India</code>,",
                               "a zone like <code>/country Asia/Dhaka</code>, or an offset like <code>/country UTC+5:30</code>.",
                               "<i>Or send just /country to pick from a list.</i>"))
        return
    first = private and not store.has_tz(user.id)
    if private:
        store.set_user_tz(user.id, ctx)
    else:
        store.set_chat_tz(chat.id, ctx)
    await say(msg, ui.country_set_card(ctx))
    if first:
        await finish_onboarding(context, chat.id, user, rec)


async def country_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")            # cty:p|s:scope:value
    user, chat = q.from_user, q.message.chat
    scope = parts[2]
    if parts[1] == "p":
        rows, _ = country_page(int(parts[3]), scope)
        await q.answer()
        try:
            await q.edit_message_reply_markup(reply_markup=kb(rows))
        except BadRequest:
            pass
        return
    idx = int(parts[3])
    ctx = C.ctx_for_country(idx)
    rec = store.touch(user)
    if scope == "g":
        if not store.is_admin(user.id) or not group_allowed(chat.id):
            await q.answer("Only the bot admin can change the group's time zone.", show_alert=True)
            return
        store.set_chat_tz(chat.id, ctx)
        await q.answer("Saved")
        await edit_card(q.message, ui.country_set_card(ctx))
        return
    if store.status(user.id) == "denied":
        await q.answer("No access.", show_alert=True)
        return
    first = not store.has_tz(user.id)
    store.set_user_tz(user.id, ctx)
    await q.answer("Saved ✅")
    await edit_card(q.message, ui.country_set_card(ctx))
    if first:
        await finish_onboarding(context, q.message.chat.id, user, rec)


# ============================================================
# CHECKING COMMANDS
# ============================================================

@gated("check")
async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = update.effective_message
    state = get_state(chat_id)

    if state["busy"]:
        await say(msg, ui.warn("Already running", "A check is already running.", "<i>Use /cancel to stop it first.</i>"))
        return
    problem = _check_ready(state)
    if problem:
        await say(msg, problem)
        return

    total = checker.count_combinations(state["links"], state["words"])
    eta = ui.eta_text(total, state.get("last_speed"))
    if state["source"] != "own":
        state["source"] = "own"                  # /autocheck now repeats THIS check
        save_settings(chat_id)
    state["busy"] = True
    state["pending_cancel"] = False
    try:
        progress_msg = await say(msg, ui.notice(
            "🚀", "STARTING CHECK",
            f"🔗 <code>{len(state['links']):,}</code> links × 📝 <code>{len(state['words']):,}</code> words "
            f"= <b>{total:,}</b> combinations",
            *([eta] if eta else []),
            "<i>Preparing workers… send /cancel to stop.</i>"))
    except Exception:
        state["busy"] = False
        raise
    _start_background_check(context, chat_id, progress_msg)


@gated("addword")
async def addword_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add words to the list and immediately check ONLY those words against every link."""
    chat_id = update.effective_chat.id
    msg = update.effective_message
    state = get_state(chat_id)

    given = [w.strip() for w in context.args if w.strip()]
    if not given:
        await say(msg, ui.notice("➕", "ADD WORDS", "Usage: <code>/addword Word1 Word2 Word3</code>",
                                 "<i>Adds the words to your wordlist and checks only those words against all your links.</i>"))
        return
    existing = set(state["words"])
    new = [w for w in dict.fromkeys(given) if w not in existing]
    if not new:
        await say(msg, ui.notice("ℹ️", "Nothing new", "Those word(s) are already in your wordlist.",
                                 "<i>Use /check to check everything.</i>"))
        return

    state["words"] = state["words"] + new
    (chat_dir(chat_id) / "wordlist.txt").write_text("\n".join(state["words"]) + "\n", encoding="utf-8")
    shown = ", ".join(new[:10]) + (" …" if len(new) > 10 else "")
    added = (f"➕ Added <b>{len(new)}</b> word(s): <code>{ui.e(shown)}</code>\n"
             f"📝 Words in list: <code>{len(state['words']):,}</code>")

    if not state["links"]:
        await say(msg, ui.notice("✅", "WORDS SAVED", added, "<i>Now send linklist.txt, then /check.</i>"))
        return
    if state["busy"]:
        await say(msg, ui.notice("✅", "WORDS SAVED", added,
                                 "⏳ <i>A check is running — the new word(s) will be included in the next /check.</i>"))
        return
    problem = _check_ready(state, new)
    if problem:
        await say(msg, ui.notice("✅", "WORDS SAVED", added) + "\n\n" + problem)
        return

    total = checker.count_combinations(state["links"], new)
    state["busy"] = True
    state["pending_cancel"] = False
    try:
        progress_msg = await say(msg, ui.notice(
            "🚀", "CHECKING NEW WORDS", added,
            f"🔗 <code>{len(state['links']):,}</code> links × <code>{len(new)}</code> new word(s) = <b>{total:,}</b> combinations",
            "<i>Send /cancel to stop.</i>"))
    except Exception:
        state["busy"] = False
        raise
    _start_background_check(context, chat_id, progress_msg, words_override=new)


@gated(("check", "addword", "autocheck"), cmd="cancel")
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(update.effective_chat.id)
    msg = update.effective_message
    run = state["run"]
    if run is not None and not run.finished:
        run.cancel()
        extra = "\n<i>Auto-check stays on — use /stopautocheck to turn it off.</i>" if state["run_kind"] == "auto" else ""
        await say(msg, ui.notice("🛑", "CANCELLING", "Stopping the check… what was found so far is kept." + extra))
    elif run is not None:
        state["stop_notify"] = True
        await say(msg, ui.notice("🛑", "STOPPED SENDING", "The remaining link messages will not be sent.",
                                 "<i>The results file has every working link.</i>"))
    elif state["busy"]:
        state["pending_cancel"] = True
        await say(msg, ui.notice("🛑", "CANCELLING", "The check will stop as soon as it starts."))
    else:
        await say(msg, ui.notice("ℹ️", "Nothing to cancel", "No check is running right now."))


# ---------- /test ----------

_test_urls: "OrderedDict[str, str]" = OrderedDict()


def _remember_test_url(url: str) -> str:
    token = secrets.token_hex(4)
    _test_urls[token] = url
    while len(_test_urls) > 300:
        _test_urls.popitem(last=False)
    return token


def _test_markup(url: str, token: str, with_url_button: bool = True) -> InlineKeyboardMarkup:
    row = []
    if with_url_button:
        row.append(InlineKeyboardButton("🔗 Open link", url=url))
    row.append(InlineKeyboardButton("🔁 Test again", callback_data=f"tst:{token}"))
    return InlineKeyboardMarkup([row])


async def _run_test(message, chat_id: int, url: str):
    tz = tz_of(chat_id)
    try:
        res = await checker.inspect_url(url)
    except Exception as e:
        await edit_card(message, ui.warn("The test failed", f"<code>{ui.e(type(e).__name__)}: {ui.e(str(e)[:300])}</code>"))
        return
    stats.add(chat_id, tests=1)
    token = _remember_test_url(url)
    text = ui.test_card(url, res, tz)
    for with_btn in (True, False):          # a link Telegram refuses as a URL button must not break the test
        try:
            await message.edit_text(text[:4000], parse_mode=HTML, link_preview_options=NO_PREVIEW,
                                    reply_markup=_test_markup(url, token, with_btn))
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
        except Exception:
            logger.exception("test card failed")
            return


@gated("test")
async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inspect one link in detail."""
    msg = update.effective_message
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not context.args:
        await say(msg, ui.notice("🔬", "LINK TEST", "Usage: <code>/test https://example.com/path/image.jpg</code>",
                                 "<i>Shows exactly what the server answers for that link — and for a similar link that "
                                 "cannot exist — so you can see why a link is (not) counted as working.</i>"))
        return
    url = context.args[0].strip()
    if checker.PLACEHOLDER_RE.search(url):
        if not state["words"]:
            await say(msg, ui.warn("Link has (Word)", "Send a real link, or upload your wordlist first."))
            return
        url = checker.PLACEHOLDER_RE.sub(lambda m: checker.quote_word(state["words"][0]), url)
    if not url.lower().startswith(("http://", "https://")):
        await say(msg, ui.warn("Invalid link", "Please send a full link starting with <code>http://</code> or <code>https://</code>."))
        return
    wait = await say(msg, ui.notice("🔬", "TESTING…", f"🔗 <code>{ui.e(url)}</code>", "<i>Asking the server…</i>"))
    await _run_test(wait, chat_id, url)


async def test_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    if not store.can(user.id, "test"):
        await q.answer("You don't have permission for /test.", show_alert=True)
        return
    url = _test_urls.get(q.data.split(":", 1)[1])
    if not url:
        await q.answer("This test is too old. Send /test again.", show_alert=True)
        return
    await q.answer("Testing again…")
    await edit_card(q.message, ui.notice("🔬", "TESTING…", f"🔗 <code>{ui.e(url)}</code>", "<i>Asking the server…</i>"))
    await _run_test(q.message, q.message.chat.id, url)


# ---------- /checkmena (the lists shipped with the bot) ----------

# Where /checkmena reads its two files from. Set MENA_REPO (e.g. "yourname/your-repo") and the bot downloads
# wordlist.txt + linklist.txt from GitHub on EVERY /checkmena - push new files and the next run uses them.
# MENA_REPO empty -> falls back to the two files next to bot.py.
_repo = os.getenv("MENA_REPO", "").strip()
for _p in ("https://github.com/", "http://github.com/", "github.com/"):
    if _repo.lower().startswith(_p):
        _repo = _repo[len(_p):]
MENA_REPO = _repo.removesuffix(".git").strip("/")
MENA_BRANCH = os.getenv("MENA_BRANCH", "main").strip() or "main"
MENA_DIR = os.getenv("MENA_DIR", "").strip().strip("/")        # optional sub-folder inside the repo
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()           # only needed if the repo is private

BUNDLE_DIR = Path(__file__).resolve().parent
MENA_WORDS = BUNDLE_DIR / "wordlist.txt"
MENA_LINKS = BUNDLE_DIR / "linklist.txt"


async def _fetch_github(name: str) -> bytes:
    path = f"{MENA_DIR}/{name}" if MENA_DIR else name
    tries = [
        (f"https://api.github.com/repos/{MENA_REPO}/contents/{quote(path)}?ref={quote(MENA_BRANCH)}",
         {"Accept": "application/vnd.github.raw+json"}),
        (f"https://raw.githubusercontent.com/{MENA_REPO}/{quote(MENA_BRANCH)}/{quote(path)}?t={int(time.time())}", {}),
    ]
    base = {"User-Agent": "python-url-checker-bot"}
    if GITHUB_TOKEN:
        base["Authorization"] = f"token {GITHUB_TOKEN}"
    last = "unknown error"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as sess:
        for url, extra in tries:
            try:
                async with sess.get(url, headers={**base, **extra}) as r:
                    if r.status == 200:
                        data = await r.read()
                        if len(data) > MAX_UPLOAD_BYTES:
                            raise RuntimeError(f"{name} is bigger than 20 MB")
                        return data
                    last = f"HTTP {r.status}"
            except asyncio.CancelledError:
                raise
            except RuntimeError:
                raise
            except Exception as e:
                last = type(e).__name__
    raise RuntimeError(f"{name}: {last}")


def _parse_list(raw: bytes) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "list.txt"
        f.write_bytes(raw)
        return checker.read_lines(f)[0]


async def _load_mena_lists() -> tuple[list[str], list[str], str]:
    """(words, links, where they came from)."""
    if MENA_REPO:
        w_raw, l_raw = await asyncio.gather(_fetch_github("wordlist.txt"), _fetch_github("linklist.txt"))
        return _parse_list(w_raw), _parse_list(l_raw), f"GitHub · {MENA_REPO}@{MENA_BRANCH}"
    words = (await asyncio.to_thread(checker.read_lines, MENA_WORDS))[0]
    links = (await asyncio.to_thread(checker.read_lines, MENA_LINKS))[0]
    return words, links, "bot folder"


@gated("check")
async def checkmena_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check the wordlist.txt + linklist.txt from the GitHub repo (MENA_REPO) - not the user's own uploads."""
    chat_id = update.effective_chat.id
    msg = update.effective_message
    state = get_state(chat_id)

    if state["busy"]:
        await say(msg, ui.warn("Already running", "A check is already running.", "<i>Use /cancel to stop it first.</i>"))
        return
    try:
        words, links, source = await _load_mena_lists()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        hint = ("<i>Check MENA_REPO / MENA_BRANCH. A private repo also needs GITHUB_TOKEN.</i>" if MENA_REPO
                else "<i>Set MENA_REPO, or keep both files next to bot.py.</i>")
        await say(msg, ui.warn("Could not load the lists", f"<code>{ui.e(str(e)[:200])}</code>", hint))
        return
    if not words or not links:
        await say(msg, ui.warn("Lists are empty", f"Source: <code>{ui.e(source)}</code>",
                               "<i>Both <code>wordlist.txt</code> and <code>linklist.txt</code> need at least one line.</i>"))
        return
    problem = _check_ready(state, words, links)
    if problem:
        await say(msg, problem)
        return

    total = checker.count_combinations(links, words)
    eta = ui.eta_text(total, state.get("last_speed"))
    if state["source"] != "mena":
        state["source"] = "mena"                 # /autocheck now repeats THIS check (lists re-loaded every cycle)
        save_settings(chat_id)
    state["busy"] = True
    state["pending_cancel"] = False
    try:
        progress_msg = await say(msg, ui.notice(
            "🚀", "STARTING CHECKMENA",
            f"🔗 <code>{len(links):,}</code> links × 📝 <code>{len(words):,}</code> words = <b>{total:,}</b> combinations",
            f"📡 <code>{ui.e(source)}</code>",
            *([eta] if eta else []),
            "<i>Your own lists are untouched · /autocheck N repeats this check · /cancel to stop.</i>"))
    except Exception:
        state["busy"] = False
        raise
    _start_background_check(context, chat_id, progress_msg, words_override=words, links_override=links)


# ---------- blocklist ----------

BLK_PAGE = 8
_url_by_hash: "OrderedDict[str, str]" = OrderedDict()


def _uh(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _remember_url(url: str) -> str:
    h = _uh(url)
    _url_by_hash[h] = url
    _url_by_hash.move_to_end(h)
    while len(_url_by_hash) > 5000:
        _url_by_hash.popitem(last=False)
    return h


def _resolve_url(chat_id: int, h: str) -> str | None:
    """Button data carries only a hash (64-byte limit). Works after a restart too, via blocklist/history."""
    url = _url_by_hash.get(h)
    if url:
        return url
    st = get_state(chat_id)
    for u in list(st["blocked"]) + list(st["history"]):
        if _uh(u) == h:
            return u
    return None


def _alert_markup(h: str, blocked: bool) -> InlineKeyboardMarkup:
    if blocked:
        return kb([[("↩️ Remove from blocklist", f"blk:u:{h}")]])
    return kb([[("🚫 Put in blocklist", f"blk:a:{h}")]])


def blocklist_view(chat_id: int, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    items = list(get_state(chat_id)["blocked"])
    if not items:
        return ui.notice("🚫", "BLOCKLIST", "Your blocklist is empty.",
                         "<i>Press 🚫 Put in blocklist under a found link and it will never be sent again.</i>"), None
    pages = (len(items) + BLK_PAGE - 1) // BLK_PAGE
    page = min(max(page, 0), pages - 1)
    start = page * BLK_PAGE
    chunk = items[start:start + BLK_PAGE]
    lines = [f"{start + i + 1}. <code>{ui.e(u)}</code>" for i, u in enumerate(chunk)]
    rows = []
    btns = [(f"🗑 {start + i + 1}", f"blk:r:{_uh(u)}:{page}") for i, u in enumerate(chunk)]
    for k in range(0, len(btns), 4):
        rows.append(btns[k:k + 4])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(("⬅️", f"blk:p:{page - 1}"))
        nav.append((f"{page + 1}/{pages}", "blk:n"))
        if page < pages - 1:
            nav.append(("➡️", f"blk:p:{page + 1}"))
        rows.append(nav)
    rows.append([("🧹 Clear all", "blk:c")])
    text = ui.notice("🚫", f"BLOCKLIST · {len(items):,}", *lines, "", "<i>Tap 🗑 + number to remove a link from the blocklist.</i>")
    return text, kb(rows)


@gated(UPLOAD_PERMS, cmd="blocklist")
async def blocklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = update.effective_message
    if context.args and context.args[0].lower() == "all":
        if not store.is_admin(update.effective_user.id):
            await say(msg, ui.warn("Admins only", "Only an admin can block every sent link at once."))
            return
        state = get_state(chat_id)
        urls = list(state["history"])                      # every link that was sent so far
        run = state["run"]
        if run is not None:
            urls += list(run.found)                        # + what the running check has found already
        now = time.time()
        added = 0
        for u in dict.fromkeys(urls):
            if u not in state["blocked"]:
                state["blocked"][u] = now
                added += 1
        state["history"].clear()
        save_blocklist(chat_id)
        save_history(chat_id)
        await say(msg, ui.notice(
            "🚫", "ALL SENT LINKS BLOCKED", f"<b>{added:,}</b> link(s) added to the blocklist "
            f"(<b>{len(state['blocked']):,}</b> in total).", "",
            "From now on the bot only sends links that are <b>new</b>.",
            "<i>/blocklist shows them and lets you remove any.</i>"))
        return
    text, markup = blocklist_view(chat_id, 0)
    await say(msg, text, markup)


async def blk_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message is None:
        await q.answer()
        return
    if not store.can(q.from_user.id, UPLOAD_PERMS):
        await q.answer("You don't have permission for this.", show_alert=True)
        return
    chat_id = q.message.chat.id
    state = get_state(chat_id)
    parts = q.data.split(":")
    act = parts[1] if len(parts) > 1 else ""

    async def set_markup(markup):
        try:
            await q.edit_message_reply_markup(reply_markup=markup)
        except BadRequest:
            pass

    async def show(page: int):
        text, markup = blocklist_view(chat_id, page)
        await edit_card(q.message, text, markup)

    if act in ("a", "u", "r"):
        url = _resolve_url(chat_id, parts[2]) if len(parts) > 2 else None
        if not url:
            await q.answer("This button is too old. Use /blocklist.", show_alert=True)
            return
        if act == "a":
            state["blocked"][url] = time.time()
            state["history"].pop(url, None)
            save_blocklist(chat_id)
            save_history(chat_id)
            await q.answer("🚫 Blocklisted - it will never be sent again")
            await set_markup(_alert_markup(parts[2], True))
        elif act == "u":
            state["blocked"].pop(url, None)
            save_blocklist(chat_id)
            await q.answer("↩️ Removed from blocklist")
            await set_markup(_alert_markup(parts[2], False))
        else:
            state["blocked"].pop(url, None)
            save_blocklist(chat_id)
            await q.answer("Removed from blocklist ✅")
            await show(int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0)
    elif act == "p":
        await q.answer()
        await show(int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0)
    elif act == "c":
        await q.answer()
        await edit_card(q.message, ui.notice("🧹", "CLEAR BLOCKLIST", f"Remove all <b>{len(state['blocked']):,}</b> blocked link(s)?"),
                        kb([[("✅ Yes, clear all", "blk:cy"), ("↩️ Cancel", "blk:p:0")]]))
    elif act == "cy":
        state["blocked"].clear()
        save_blocklist(chat_id)
        await q.answer("Blocklist cleared ✅")
        await show(0)
    else:
        await q.answer()


# ---------- /found, /status, /stats ----------

@gated("found")
async def found_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = update.effective_message
    state = get_state(chat_id)
    hist = state["history"]
    tz = tz_of(chat_id)

    if context.args and context.args[0].lower() == "clear":
        hist.clear()
        save_history(chat_id)
        await say(msg, ui.notice("🧹", "HISTORY CLEARED", "Your found-links history is now empty."))
        return
    if not hist:
        await say(msg, ui.notice("📚", "FOUND LINKS", "Nothing found yet.",
                                 "<i>Working links appear here after a check finds them.</i>"))
        return
    ordered = sorted(hist.items(), key=lambda kv: (lm_epoch(kv[1].get("lm")), kv[1].get("first", 0)), reverse=True)
    top = ordered[:10]
    more = len(ordered) > len(top)
    await say(msg, ui.found_card([(u, e.get("lm")) for u, e in top], len(ordered), tz, more))
    if more:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / f"all_found_links_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            p.write_text("\n".join(result_lines([(u, e.get("lm")) for u, e in ordered], tz)) + "\n", encoding="utf-8")
            await send_text_file(context.bot, chat_id, p, f"📎 All {len(ordered):,} working links found so far · {tz.label}")


@gated("status")
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    total = checker.count_combinations(state["links"], state["words"])
    jobs = context.job_queue.get_jobs_by_name(f"check-{chat_id}") if context.job_queue else ()
    run = state["run"]
    snap = run.snapshot() if run is not None and not run.finished else None
    await say(update.effective_message,
              ui.status_card(state, total, MAX_COMBINATIONS, bool(jobs), tz_of(chat_id), snap, state["run_kind"]))


@gated("status")
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    data = stats.get(chat.id)
    glob = stats.get("global")["today"] if store.is_admin(update.effective_user.id) else None
    await say(update.effective_message, ui.stats_card(data["today"], data["yesterday"], tz_of(chat.id), glob))


@admin_only()
async def resetstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats.reset(update.effective_chat.id)
    await say(update.effective_message, ui.notice("🧹", "STATS RESET", "Today's statistics for this chat were cleared.",
                                                  "<i>They also reset by themselves every day at 04:00 AM Bangladesh Time.</i>"))


# ---------- notify / autocheck ----------

@gated("autocheck")
async def notify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    choice = context.args[0].lower() if context.args else ""
    if choice not in ("all", "new"):
        await say(update.effective_message, ui.notice(
            "🔔", "AUTO-CHECK ALERTS", f"Currently: <b>{ui.e(state['notify_mode'])}</b>", "",
            "<code>/notify all</code> — message me every time a working link is found, every cycle",
            "<code>/notify new</code> — message me only for links I haven't been told about before"))
        return
    state["notify_mode"] = choice
    save_settings(chat_id)
    await say(update.effective_message, ui.notice(
        "✅", "ALERTS UPDATED",
        "Auto-check will alert you <b>every time</b> a working link is found." if choice == "all"
        else "Auto-check will alert you <b>only for NEW links</b> (everything stays in /found)."))


async def autocheck_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    state = get_state(chat_id)
    if state["busy"]:
        stats.add(chat_id, skipped=1)          # previous check still running - never overlap
        return
    words = links = None
    if state["source"] == "mena":              # the last check was /checkmena -> repeat it (fresh lists every cycle)
        try:
            words, links, _src = await _load_mena_lists()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Auto-check could not load the MENA lists: %s", e)
            if not state["mena_warned"]:
                state["mena_warned"] = True
                try:
                    await context.bot.send_message(chat_id=chat_id, parse_mode=HTML, text=ui.warn(
                        "Auto-check could not load the lists", f"<code>{ui.e(str(e)[:200])}</code>",
                        "<i>It will try again next cycle.</i>"))
                except Exception:
                    pass
            return
        state["mena_warned"] = False
    if _check_ready(state, words, links):
        return
    state["busy"] = True
    state["pending_cancel"] = False
    await run_check_session(context.bot, chat_id, "auto", None, words, links)


async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    state = get_state(chat_id)
    run = state["run"]
    snap = run.snapshot() if run is not None and not run.finished else None
    today = stats.get(chat_id)["today"]
    await context.bot.send_message(chat_id=chat_id, text=ui.heartbeat_card(state, today, tz_of(chat_id), snap),
                                   parse_mode=HTML)


def schedule_autocheck(job_queue, chat_id: int, minutes: int, first: int = 5):
    for name in (f"check-{chat_id}", f"heartbeat-{chat_id}"):
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    job_queue.run_repeating(autocheck_job, interval=minutes * 60, first=first, chat_id=chat_id, name=f"check-{chat_id}")
    job_queue.run_repeating(heartbeat_job, interval=HEARTBEAT_SECONDS, first=HEARTBEAT_SECONDS,
                            chat_id=chat_id, name=f"heartbeat-{chat_id}")


@gated("autocheck")
async def autocheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = update.effective_message
    state = get_state(chat_id)

    if not context.args:
        await say(msg, ui.notice("🔁", "AUTO-CHECK", f"Usage: <code>/autocheck N</code>  (N = minutes, "
                                 f"{MIN_AUTOCHECK_MINUTES}–{MAX_AUTOCHECK_MINUTES})", "Example: <code>/autocheck 5</code>"))
        return
    try:
        minutes = int(context.args[0])
    except ValueError:
        await say(msg, ui.warn("Invalid number", "Please give a whole number of minutes, e.g. <code>/autocheck 5</code>"))
        return
    if not (MIN_AUTOCHECK_MINUTES <= minutes <= MAX_AUTOCHECK_MINUTES):
        await say(msg, ui.warn("Out of range", f"Please choose between <b>{MIN_AUTOCHECK_MINUTES}</b> and <b>{MAX_AUTOCHECK_MINUTES}</b> minutes."))
        return
    words = links = None
    if state["source"] == "mena":
        try:
            words, links, _src = await _load_mena_lists()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await say(msg, ui.warn("Could not load the MENA lists", f"<code>{ui.e(str(e)[:200])}</code>"))
            return
    problem = _check_ready(state, words, links)
    if problem:
        await say(msg, problem)
        return

    state["interval_minutes"] = minutes
    save_settings(chat_id)
    schedule_autocheck(context.job_queue, chat_id, minutes)
    card = ui.autocheck_on_card(minutes, state["notify_mode"])
    if state["source"] == "mena":
        card += "\n\n📡 Repeating the <b>/checkmena</b> check (the lists are re-loaded every cycle)"
    await say(msg, card)


@gated("autocheck")
async def stopautocheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    msg = update.effective_message
    jobs = context.job_queue.get_jobs_by_name(f"check-{chat_id}") + context.job_queue.get_jobs_by_name(f"heartbeat-{chat_id}")
    if not jobs and not state["interval_minutes"]:
        await say(msg, ui.notice("ℹ️", "Auto-check is off", "It isn't running right now."))
        return
    await stop_autocheck_for(context, chat_id)
    run = state["run"]
    if run is not None and not run.finished and state["run_kind"] == "auto":
        run.cancel()
    await say(msg, ui.notice("🛑", "AUTO-CHECK OFF", "Automatic checking and status updates are stopped."))


# ---------- uploads ----------

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat is None or user is None or msg is None or msg.document is None:
        return
    private = is_private(chat)
    doc = msg.document
    name = (doc.file_name or "").lower()
    kind = "words" if "wordlist" in name else "links" if "linklist" in name else None
    if private:
        if kind is None:
            if store.can(user.id, UPLOAD_PERMS):
                await say(msg, ui.notice("📄", "Unknown file", "Please send files named <code>wordlist.txt</code> and "
                                         "<code>linklist.txt</code>."))
            return
        if not await gate(update, context, UPLOAD_PERMS, "upload"):    # approval, permission, country - same as commands
            return
    else:
        if kind is None or not group_allowed(chat.id) or not store.can(user.id, UPLOAD_PERMS):
            return                                  # stay silent in groups
        store.touch(user)
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await say(msg, ui.warn("File too big", "That file is bigger than <b>20 MB</b>, Telegram's limit for bots.",
                               "<i>Please split it into smaller files.</i>"))
        return

    state = get_state(chat.id)
    filename = "wordlist.txt" if kind == "words" else "linklist.txt"
    dest = chat_dir(chat.id) / filename
    working = None
    if doc.file_size and doc.file_size > 1_000_000:          # big file: show that something is happening
        try:
            working = await say(msg, ui.notice(
                "⏳", "READING YOUR FILE", f"📥 <code>{ui.e(filename)}</code> · <b>{ui.e(ui.fmt_size(doc.file_size))}</b>",
                "<i>Big files take a few seconds — please wait…</i>"))
        except Exception:
            working = None

    async def reply(card: str, markup=None):
        if working is not None and await edit_card(working, card, markup):
            return
        await say(msg, card, markup)

    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=dest)
        items, skipped = await asyncio.to_thread(checker.read_lines, dest)
    except Exception:
        logger.exception("Could not read uploaded file")
        await reply(ui.warn("Upload failed", "I couldn't download or read that file.", "<i>Please try sending it again.</i>"))
        return

    state[kind] = items
    label = "word(s)" if kind == "words" else "link(s)"
    lines = [f"📥 Loaded <b>{len(items):,}</b> {label} from <code>{filename}</code>"]
    if doc.file_size:
        lines[0] += f" · {ui.e(ui.fmt_size(doc.file_size))}"
    if skipped:
        lines.append(f"<i>{skipped:,} blank/duplicate line(s) skipped</i>")
    if kind == "links" and items and not any(checker.PLACEHOLDER_RE.search(x) for x in items):
        lines.append("⚠️ None of your links contain <code>(Word)</code> — each link will just be checked once.")
    markup = None
    if state["words"] and state["links"]:
        total = checker.count_combinations(state["links"], state["words"])
        lines += ["", f"🔗 <code>{len(state['links']):,}</code> links × 📝 <code>{len(state['words']):,}</code> words = <b>{total:,}</b> combinations"]
        if total > MAX_COMBINATIONS:
            lines.append(f"⚠️ <i>That is more than the limit of {MAX_COMBINATIONS:,} per run.</i>")
        else:
            eta = ui.eta_text(total, state.get("last_speed"))
            if eta:
                lines.append(eta)
            lines.append("<i>Ready — press the button or send /check 🚀</i>")
            markup = kb([[("🚀 Start check", "act:check")]])
    await reply(ui.notice("✅", "FILE LOADED", *lines), markup)


async def act_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick-action buttons (Start check / Check again / Found links) run the normal, permission-checked command."""
    q = update.callback_query
    action = q.data.split(":", 1)[1] if q.data and ":" in q.data else ""
    try:
        await q.answer()
    except Exception:
        pass
    fn = {"check": check_cmd, "mena": checkmena_cmd, "found": found_cmd}.get(action)
    if fn is not None:
        await fn(update, context)


@admin_only(private_only=True)
async def emoji_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/emoji - status · on | off | reset | remove ✅ 🔗"""
    msg = update.effective_message
    args = context.args or []
    arg = args[0].lower() if args else ""
    mapping = dict(ui.PREMIUM["map"])
    on = ui.PREMIUM["on"]
    if arg in ("on", "off"):
        on = arg == "on" and PREMIUM_ENABLED
    elif arg == "reset":
        mapping = {}
    elif arg == "remove" and len(args) > 1:
        for tok in args[1:]:
            for key in list(mapping):
                if key in ui._base_emoji(tok):
                    mapping.pop(key, None)
    ui.set_premium(mapping, on)
    save_premium()
    await say(msg, ui.premium_card(ui.PREMIUM["on"], ui.PREMIUM["map"]))


async def emoji_learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The admin sends a message that contains Telegram-Premium emoji -> the bot learns them."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat is None or user is None or msg is None:
        return
    found: dict[str, str] = {}
    if store.is_admin(user.id):
        for ent in msg.entities or ():
            if ent.type == "custom_emoji" and ent.custom_emoji_id:
                base = ui._base_emoji(msg.parse_entity(ent))
                if base:
                    found[base] = ent.custom_emoji_id
    if not found:
        await private_text(update, context)           # not the admin / nothing usable: normal behaviour
        return
    mapping = dict(ui.PREMIUM["map"])
    mapping.update(found)
    ui.set_premium(mapping, PREMIUM_ENABLED)
    save_premium()
    await say(msg, ui.premium_card(ui.PREMIUM["on"], ui.PREMIUM["map"], list(found)))


async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat and user and msg and is_private(chat) and store.can(user.id, None) and store.has_tz(user.id):
        await say(msg, ui.notice("🤔", "Not sure what you mean", "<i>Try /help to see what I can do.</i>"))


# ============================================================
# ADMIN: users, permissions, groups, broadcast
# ============================================================

USERS_PER_PAGE = 8


def users_page(page: int) -> tuple[str, list[list[tuple[str, str]]]]:
    users = [dict(r, status="admin") if store.is_admin(r["id"]) else r for r in store.sorted_users()]
    users.sort(key=lambda r: 0 if r["status"] == "admin" else 1)       # admins first (sort is stable)
    pages = max(1, (len(users) + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = users[page * USERS_PER_PAGE:(page + 1) * USERS_PER_PAGE]
    counts = {}
    for r in users:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    rows, row = [], []
    for rec in chunk:
        row.append((f"⚙️ {rec['name'][:18]}", f"perm:menu:{rec['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if pages > 1:
        rows.append([("◀️", f"usr:p:{max(0, page - 1)}"), (f"{page + 1}/{pages}", f"usr:p:{page}"),
                     ("▶️", f"usr:p:{min(pages - 1, page + 1)}")])
    return ui.users_card(chunk, page, pages, len(users), counts), rows


@admin_only(private_only=True)
async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not store.users:
        await say(update.effective_message, ui.notice("👥", "USERS", "No users yet.",
                                                      "<i>People appear here when they send /start.</i>"))
        return
    text, rows = users_page(0)
    await say(update.effective_message, text, kb(rows) if rows else None)


async def users_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not store.is_admin(q.from_user.id):
        await q.answer("Admins only.", show_alert=True)
        return
    await q.answer()
    text, rows = users_page(int(q.data.split(":")[2]))
    await edit_card(q.message, text, kb(rows) if rows else None)


def panel(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    rec = store.ensure(uid)
    return (ui.perm_panel_text(rec, store.status(uid), store.access_text(uid)), kb(ui.perm_buttons(uid, rec)))


def _find_user_arg(text: str) -> dict | None:
    rec = store.find(text)
    if rec is None and text.strip().lstrip("-").isdigit():
        rec = store.ensure(int(text))
    return rec


@admin_only(private_only=True)
async def user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args:
        await say(msg, ui.notice("🎛", "USER PANEL", "Usage: <code>/user ID</code> or <code>/user @username</code>",
                                 "<i>Opens the permission panel. Use /users to see everyone.</i>"))
        return
    rec = _find_user_arg(context.args[0])
    if rec is None:
        await say(msg, ui.warn("User not found", "That user has never written to the bot. Use their numeric ID."))
        return
    text, markup = panel(rec["id"])
    await say(msg, text, markup)


@admin_only(private_only=True)
async def grant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if len(context.args) < 2:
        await say(msg, ui.notice("🎁", "GRANT ACCESS",
                                 "<code>/grant ID all</code> — every command",
                                 "<code>/grant ID test found</code> — only these commands",
                                 "",
                                 "<i>Commands: " + ui.e(" ".join("/" + k for k in PERMISSIONS)) + "</i>"))
        return
    rec = _find_user_arg(context.args[0])
    if rec is None:
        await say(msg, ui.warn("User not found", "Use the user's numeric ID."))
        return
    uid = rec["id"]
    if context.args[1].lower() == "all":
        store.approve_all(uid)
    else:
        keys, bad = parse_perm_words(context.args[1:])
        if bad or not keys:
            await say(msg, ui.warn("Unknown command(s)", f"<code>{ui.e(' '.join(bad) or '?')}</code>",
                                   "<i>Valid: " + ui.e(" ".join("/" + k for k in PERMISSIONS)) + "</i>"))
            return
        store.set_commands(uid, keys)
    await say(msg, ui.notice("✅", "ACCESS SAVED", f"👤 <b>{ui.who(rec)}</b> · <code>{uid}</code>",
                             f"🎛 <b>{ui.e(store.access_text(uid))}</b>"))
    await notify_user(context.bot, uid, ui.access_changed_card(rec, "The admin updated your access.", store.access_text(uid)))
    await enforce_permissions(context, uid)


@admin_only(private_only=True)
async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not context.args:
        await say(msg, ui.notice("🚫", "REVOKE ACCESS", "<code>/revoke ID</code> — block the user completely",
                                 "<code>/revoke ID test found</code> — remove only these commands"))
        return
    rec = _find_user_arg(context.args[0])
    if rec is None:
        await say(msg, ui.warn("User not found", "Use the user's numeric ID."))
        return
    uid = rec["id"]
    if store.is_admin(uid):
        await say(msg, ui.warn("Can't do that", "This user is an admin (set in .env)."))
        return
    if len(context.args) == 1:
        store.deny(uid)
        await notify_user(context.bot, uid, ui.denied_card())
    else:
        keys, bad = parse_perm_words(context.args[1:])
        if bad or not keys:
            await say(msg, ui.warn("Unknown command(s)", f"<code>{ui.e(' '.join(bad) or '?')}</code>"))
            return
        store.revoke(uid, keys)
        await notify_user(context.bot, uid, ui.access_changed_card(rec, "The admin updated your access.", store.access_text(uid)))
    await say(msg, ui.notice("✅", "ACCESS UPDATED", f"👤 <b>{ui.who(rec)}</b> · <code>{uid}</code>",
                             f"🎛 <b>{ui.e(store.access_text(uid))}</b>"))
    await enforce_permissions(context, uid)


async def perm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not store.is_admin(q.from_user.id):
        await q.answer("Only the admin can do this.", show_alert=True)
        return
    parts = q.data.split(":")                   # perm:<action>:<uid>[:<cmd>]
    action, uid = parts[1], int(parts[2])
    rec = store.ensure(uid)
    if store.is_admin(uid) and action in ("t", "all", "no"):
        await q.answer("That user is an admin.", show_alert=True)
        return

    if action == "menu":
        await q.answer()
    elif action == "t" and parts[3] in PERMISSIONS:
        on = store.toggle(uid, parts[3])
        await q.answer(f"/{parts[3]} {'ON' if on else 'OFF'}")
        await enforce_permissions(context, uid)
    elif action == "all":
        store.approve_all(uid)
        await q.answer("Full access given ✅")
        await notify_user(context.bot, uid, ui.access_changed_card(rec, "Welcome! The admin approved you.", store.access_text(uid)))
    elif action == "no":
        store.deny(uid)
        await q.answer("User blocked ⛔")
        await notify_user(context.bot, uid, ui.denied_card())
        await enforce_permissions(context, uid)
    elif action == "ok":
        await q.answer("Saved ✅")
        if store.status(uid) == "approved":
            await notify_user(context.bot, uid, ui.access_changed_card(rec, "The admin updated your access.", store.access_text(uid)))
        await edit_card(q.message, ui.notice("✅", "SAVED", f"👤 <b>{ui.who(rec)}</b> · <code>{uid}</code>",
                                             f"🎛 <b>{ui.e(store.access_text(uid))}</b>"))
        return
    text, markup = panel(uid)
    await edit_card(q.message, text, markup)


# ---------- groups ----------

@admin_only()
async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, msg = update.effective_chat, update.effective_message
    target = chat.id
    if context.args:
        try:
            target = int(context.args[0])
        except ValueError:
            await say(msg, ui.notice("🔓", "ALLOW", "Usage: <code>/allow</code> (this group) or <code>/allow -1001234567890</code>",
                                     "<i>A positive ID gives that user full access.</i>"))
            return
    if target > 0:
        store.approve_all(target)
        await say(msg, ui.notice("✅", "USER ALLOWED", f"User <code>{target}</code> now has <b>full access</b>.",
                                 "<i>Use /user to limit them to some commands.</i>"))
        await notify_user(context.bot, target, ui.access_changed_card(store.ensure(target), "The admin approved you.", "Full access"))
        return
    if group_allowed(target):
        await say(msg, ui.notice("ℹ️", "Already allowed", f"Group <code>{target}</code> is already allowed ✅"))
        return
    dynamic_groups.add(target)
    _save_groups()
    await say(msg, ui.notice("✅", "GROUP ALLOWED", f"Group <code>{target}</code> can now use the bot.",
                             "<i>Members still need the admin's approval to use commands.</i>"))


@admin_only()
async def disallow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, msg = update.effective_chat, update.effective_message
    target = chat.id
    if context.args:
        try:
            target = int(context.args[0])
        except ValueError:
            await say(msg, ui.notice("🔒", "DISALLOW", "Usage: <code>/disallow</code> (this group) or <code>/disallow -1001234567890</code>"))
            return
    if target > 0:
        if store.is_admin(target):
            await say(msg, ui.warn("Can't do that", "This user is an admin (set in .env)."))
            return
        store.deny(target)
        await say(msg, ui.notice("⛔", "USER BLOCKED", f"User <code>{target}</code> is blocked."))
        await enforce_permissions(context, target)
        return
    if target in ENV_GROUPS:
        await say(msg, ui.warn("Set in .env", "That group is listed in <code>ALLOWED_CHAT_IDS</code>.", "<i>Remove it there and restart the bot.</i>"))
        return
    if target not in dynamic_groups:
        await say(msg, ui.notice("ℹ️", "Not in the list", "That group wasn't added with /allow."))
        return
    dynamic_groups.discard(target)
    _save_groups()
    await say(msg, ui.notice("🗑", "GROUP REMOVED", f"Group <code>{target}</code> was removed."))


@admin_only()
async def allowed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    env_list = ", ".join(str(i) for i in sorted(ENV_GROUPS)) or "none"
    dyn_list = ", ".join(str(i) for i in sorted(dynamic_groups)) or "none"
    admins = ", ".join(str(i) for i in sorted(ADMIN_IDS)) or "none"
    await say(update.effective_message, ui.notice("🔐", "ALLOWED",
                                                  f"👑 Admins: <code>{ui.e(admins)}</code>",
                                                  f"👥 Groups from .env: <code>{ui.e(env_list)}</code>",
                                                  f"➕ Groups added with /allow: <code>{ui.e(dyn_list)}</code>",
                                                  "<i>Use /users to manage people.</i>"))


# ---------- broadcast ----------

_broadcasts: dict[str, dict] = {}


@admin_only(private_only=True)
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    raw = msg.text or ""
    parts = raw.split(None, 1)
    text = parts[1].strip() if len(parts) > 1 else ""
    reply = msg.reply_to_message
    if not text and reply is None:
        await say(msg, ui.notice("📢", "BROADCAST", "<code>/broadcast Your message here</code>",
                                 "<i>Or reply to any message with</i> <code>/broadcast</code> <i>to copy it to everyone.</i>",
                                 "<i>You'll see a preview and must confirm before anything is sent.</i>"))
        return
    targets = [r["id"] for r in store.users.values() if r["status"] != "denied"]
    for a in store.admin_ids:
        if a not in targets:
            targets.append(a)
    if not targets:
        await say(msg, ui.warn("No users", "Nobody has started the bot yet."))
        return
    token = secrets.token_hex(4)
    _broadcasts[token] = {"admin": user.id, "text": text,
                          "reply": (chat.id, reply.message_id) if reply is not None else None, "targets": targets}
    while len(_broadcasts) > 20:
        _broadcasts.pop(next(iter(_broadcasts)))
    await say(msg, ui.broadcast_preview(text, len(targets), reply is not None),
              kb([[(f"✅ Send to {len(targets):,} user(s)", f"bc:ok:{token}"), ("✖ Cancel", f"bc:no:{token}")]]))


async def _do_broadcast(bot, msg, job: dict):
    t0 = time.monotonic()
    sent = failed = 0
    targets = job["targets"]
    for k, uid in enumerate(targets, 1):
        for attempt in range(2):
            try:
                if job["reply"]:
                    await bot.copy_message(chat_id=uid, from_chat_id=job["reply"][0], message_id=job["reply"][1])
                else:
                    await bot.send_message(chat_id=uid, text=ui.announcement(job["text"]), parse_mode=HTML,
                                           link_preview_options=NO_PREVIEW)
                sent += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(retry_seconds(e))
            except Exception:
                failed += 1
                break
        else:
            failed += 1
        if k % 25 == 0:
            await edit_card(msg, ui.notice("📢", "SENDING…", f"<code>{k:,}</code> / <code>{len(targets):,}</code>",
                                           f"✅ {sent:,} · ❌ {failed:,}"))
        await asyncio.sleep(0.06)           # stay well under Telegram's 30 messages/second limit
    await edit_card(msg, ui.broadcast_report(sent, failed, len(targets), time.monotonic() - t0))


async def broadcast_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not store.is_admin(q.from_user.id):
        await q.answer("Only the admin can do this.", show_alert=True)
        return
    _, action, token = q.data.split(":")
    job = _broadcasts.pop(token, None)
    if job is None:
        await q.answer("This broadcast expired.", show_alert=True)
        return
    if action == "no":
        await q.answer("Cancelled")
        await edit_card(q.message, ui.notice("✖", "BROADCAST CANCELLED", "Nothing was sent."))
        return
    await q.answer("Sending…")
    await edit_card(q.message, ui.notice("📢", "SENDING…", f"<code>0</code> / <code>{len(job['targets']):,}</code>"))
    task = asyncio.create_task(_do_broadcast(context.bot, q.message, job))
    task.add_done_callback(_log_task_result)
    _broadcasts.setdefault("_tasks", {})[token] = task        # keep a reference


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
        allowed = group_allowed(chat_id) if chat_id < 0 else store.can(chat_id, "autocheck")
        if not allowed:
            continue
        state = get_state(chat_id)
        minutes = state["interval_minutes"]
        if not isinstance(minutes, int) or not (MIN_AUTOCHECK_MINUTES <= minutes <= MAX_AUTOCHECK_MINUTES):
            continue
        if _check_ready(state):
            continue
        schedule_autocheck(app.job_queue, chat_id, minutes, first=15)
        try:
            await app.bot.send_message(chat_id=chat_id, parse_mode=HTML, text=ui.notice(
                "♻️", "AUTO-CHECK RESUMED", f"The bot restarted — auto-check is back on: every <b>{minutes}</b> minute(s)."))
        except Exception:
            logger.info("Could not tell chat %s that auto-check resumed", chat_id)


async def daily_reset_job(context: ContextTypes.DEFAULT_TYPE):
    """04:00 Bangladesh time: start a new statistics day for everybody."""
    stats.rollover_all()
    logger.info("Daily statistics reset done")


BASE_COMMANDS = [
    ("start", "Welcome & setup"), ("help", "All your commands"), ("check", "Check all links with all words"),
    ("checkmena", "Check the MENA lists from GitHub"), ("blocklist", "Manage blocked links"),
    ("cancel", "Cancel the running check"), ("addword", "Add words & check only those"), ("test", "Inspect one link"),
    ("found", "Links found so far"), ("autocheck", "Re-check automatically"), ("status", "Live status"),
    ("stats", "Today's statistics"), ("country", "Change country / time zone"), ("id", "Show IDs"),
]
ADMIN_COMMANDS = [("users", "Users & permissions"), ("user", "Open a user's panel"), ("grant", "Give access"),
                  ("revoke", "Remove access"), ("broadcast", "Message every user"), ("allow", "Allow a group"),
                  ("allowed", "List allowed groups"), ("emoji", "Premium emoji for the bot")]


async def post_init(app: Application):
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in BASE_COMMANDS])
        for admin in store.admin_ids:
            try:
                await app.bot.set_my_commands([BotCommand(c, d) for c, d in BASE_COMMANDS + ADMIN_COMMANDS],
                                              scope=BotCommandScopeChat(chat_id=admin))
            except Exception:
                pass
    except Exception:
        logger.warning("Could not set the command menu", exc_info=True)
    try:
        app.job_queue.run_daily(daily_reset_job, time=dtime(hour=C.RESET_HOUR, minute=0, tzinfo=C.BD_TZ), name="daily-reset")
    except Exception:
        logger.warning("Could not schedule the daily reset", exc_info=True)
    try:
        await restore_autocheck(app)
    except Exception:
        logger.warning("Could not restore auto-check", exc_info=True)


async def post_stop(app: Application):
    for state in chat_state.values():
        run = state.get("run")
        if run is not None:
            run.shutdown()


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message is not None and not isinstance(context.error, (NetworkError, TimedOut)):
            if not throttled(("err", update.effective_chat.id if update.effective_chat else 0), 20):
                await update.effective_message.reply_text(
                    ui.warn("Something went wrong", "<i>Please try again in a moment.</i>"), parse_mode=HTML)
    except Exception:
        pass


def startup_report() -> list[str]:
    """What the logs show first - makes deployment problems easy to spot."""
    import platform
    from importlib import metadata

    def ver(name):
        try:
            return metadata.version(name)
        except Exception:
            return "not installed"
    lines = [
        f"Python {platform.python_version()} on {platform.system()} | CPUs: {os.cpu_count()}",
        f"python-telegram-bot {ver('python-telegram-bot')} | aiohttp {ver('aiohttp')} | uvloop {ver('uvloop')} | tzdata {ver('tzdata')}",
        f"Data folder: {DATA_DIR_NOTE}",
        f"BOT_TOKEN: {'set' if BOT_TOKEN else 'MISSING'} | admins: {len(ADMIN_IDS)} | allowed groups: {len(ENV_GROUPS)}",
        f"Limits: {MAX_COMBINATIONS:,} combinations, up to {CONCURRENCY} connections, workers: {PROCESSES or 'auto'}",
    ]
    if ON_RAILWAY and not (VOLUME_PATH or os.environ.get("DATA_DIR")):
        lines.append("WARNING: running on Railway WITHOUT a Volume - approvals, countries and history are lost on every "
                     "redeploy. Add a Volume mounted at /data (see README).")
    return lines


async def _selfcheck() -> int:
    """python bot.py --selfcheck : tests the setup without talking to Telegram."""
    import sys as _sys
    ok = True
    print("== Python Url Checker self-check ==")
    for line in startup_report():
        print(" ", line)
    if not BOT_TOKEN:
        print("  [FAIL] BOT_TOKEN is not set (Railway > Variables)"); ok = False
    else:
        print("  [ ok ] BOT_TOKEN is set")
    if not ADMIN_IDS:
        print("  [FAIL] no admin: put your Telegram user ID in ALLOWED_CHAT_IDS"); ok = False
    else:
        print(f"  [ ok ] admin IDs: {sorted(ADMIN_IDS)}")
    # can we start a checker worker process?
    try:
        proc = await asyncio.create_subprocess_exec(_sys.executable, str(checker.CHECKER_PATH), "--worker",
                                                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        cfg = dict(idx=0, nprocs=1, concurrency=5, links=["http://127.0.0.1:9/(Word)"], words=["x"], outdir=str(DATA_DIR),
                   connect_timeout=2.0, total_timeout=3.0, attempts=1, final_rounds=0, abort_after=100)
        proc.stdin.write((json.dumps(cfg) + "\n").encode()); await proc.stdin.drain()
        out = await asyncio.wait_for(proc.stdout.read(), 20)
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 5)
        if b'"t":"d"' in out:
            print("  [ ok ] checker worker process starts and finishes correctly")
        else:
            print("  [FAIL] checker worker gave no result:", out[:200]); ok = False
    except Exception as e:
        print(f"  [FAIL] cannot run the checker worker: {type(e).__name__}: {e}"); ok = False
    print("RESULT:", "everything looks good" if ok else "problems found (see [FAIL] lines)")
    return 0 if ok else 1


def main():
    import sys as _sys
    if "--selfcheck" in _sys.argv:
        raise SystemExit(asyncio.run(_selfcheck()))
    for line in startup_report():
        logger.info(line)
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. In Railway: your service > Variables > add BOT_TOKEN "
                         "(the token from @BotFather). Locally: put it in the .env file.")
    if ":" not in BOT_TOKEN:
        raise SystemExit("BOT_TOKEN looks wrong - it must look like 123456789:AAExampleToken... (copy it again from @BotFather).")
    if not ADMIN_IDS:
        logger.warning("No admin configured! Put your own Telegram user ID in ALLOWED_CHAT_IDS (or ADMIN_IDS) "
                       "- without an admin nobody can be approved.")

    load_premium()
    install_premium_emoji()
    logger.info("Premium emoji: %s, %d learned", "on" if ui.PREMIUM["on"] else "off", len(ui.PREMIUM["map"]))

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)          # lets /cancel, /status ... work while a check runs
        .read_timeout(30).write_timeout(60).connect_timeout(15).pool_timeout(30)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )

    for name, fn in [
        ("start", start_cmd), ("help", help_cmd), ("id", id_cmd), ("country", country_cmd),
        ("check", check_cmd), ("checkmena", checkmena_cmd), ("blocklist", blocklist_cmd), (["cancel", "cancle"], cancel_cmd), ("addword", addword_cmd), ("test", test_cmd),
        ("found", found_cmd), ("status", status_cmd), ("stats", stats_cmd),
        ("autocheck", autocheck_cmd), ("stopautocheck", stopautocheck_cmd), ("notify", notify_cmd),
        ("users", users_cmd), ("user", user_cmd), ("grant", grant_cmd), ("revoke", revoke_cmd),
        ("broadcast", broadcast_cmd), ("allow", allow_cmd), ("disallow", disallow_cmd), ("allowed", allowed_cmd),
        ("resetstats", resetstats_cmd), ("emoji", emoji_cmd),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(country_cb, pattern=r"^cty:"))
    app.add_handler(CallbackQueryHandler(perm_cb, pattern=r"^perm:"))
    app.add_handler(CallbackQueryHandler(users_cb, pattern=r"^usr:"))
    app.add_handler(CallbackQueryHandler(broadcast_cb, pattern=r"^bc:"))
    app.add_handler(CallbackQueryHandler(test_cb, pattern=r"^tst:"))
    app.add_handler(CallbackQueryHandler(blk_cb, pattern=r"^blk:"))
    app.add_handler(CallbackQueryHandler(act_cb, pattern=r"^act:"))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.Entity("custom_emoji") & filters.ChatType.PRIVATE, emoji_learn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, private_text))
    app.add_error_handler(on_error)

    logger.info("Python Url Checker bot is starting...")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        raise SystemExit("Telegram rejected BOT_TOKEN (Unauthorized). Copy the token again from @BotFather "
                         "(use /token there if needed) and update the BOT_TOKEN variable.")


if __name__ == "__main__":
    main()

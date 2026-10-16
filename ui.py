"""
ui.py - every message the bot sends is built here, so they all share one premium look.

All functions return Telegram HTML (<b>, <i>, <code>, <a>, <blockquote>) and escape
everything that comes from outside, so a strange character in a link or a name can never
break a message. Telegram-specific objects (buttons) are NOT used here: button layouts are
returned as plain lists of (label, callback_data).
"""

from __future__ import annotations

import http
from datetime import datetime, timezone
from html import escape

import countries as C
from access import PERMISSIONS

DIV = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"


# ---------------- small helpers ----------------

def e(x) -> str:
    return escape(str(x), quote=False)


def eq(x) -> str:
    return escape(str(x), quote=True)


def b(x) -> str:
    return f"<b>{e(x)}</b>"


def i(x) -> str:
    return f"<i>{e(x)}</i>"


def code(x) -> str:
    return f"<code>{e(x)}</code>"


def head(icon: str, title: str, sub: str | None = None) -> str:
    out = f"{icon} <b>{e(title)}</b>\n{DIV}"
    if sub:
        out += f"\n<i>{e(sub)}</i>"
    return out


def tree(rows: list[tuple[str, str]]) -> str:
    """[(label, value_html)] -> a neat ├ └ list."""
    out = []
    for n, (label, value) in enumerate(rows):
        glyph = "└" if n == len(rows) - 1 else "├"
        out.append(f"{glyph} {e(label)}  {value}")
    return "\n".join(out)


def n(x) -> str:
    return f"{int(x):,}"


def fmt_dur(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def fmt_size(num) -> str | None:
    if not num:
        return None
    v = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return None


def bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(width, int(pct // (100 / width))))
    return "█" * filled + "░" * (width - filled)


def notice(icon: str, title: str, *lines: str) -> str:
    """A small card. Lines are already-safe HTML."""
    return head(icon, title) + ("\n" + "\n".join(lines) if lines else "")


def warn(title: str, *lines: str) -> str:
    return notice("⚠️", title, *lines)


def who(rec: dict) -> str:
    name = rec.get("name") or str(rec.get("id"))
    uname = f" (@{rec['username']})" if rec.get("username") else ""
    return f"{e(name)}{e(uname)}"


def country_text(rec: dict) -> str:
    if rec.get("country"):
        return f"{C.flag(rec.get('iso') or '')} {e(rec['country'])}"
    return "not set"


# ---------------- onboarding / access ----------------

def country_picker_text(first_time: bool) -> str:
    if first_time:
        intro = ("Welcome! 👋 Before we start, choose your <b>country</b>.\n"
                 "Every time the bot shows you a date or time, it will use <b>your local time</b>.")
    else:
        intro = "Choose your country. Every time shown by the bot will use your local time."
    return (head("🌍", "Choose your country") + "\n" + intro +
            "\n\n<i>Can't find it? Send</i> <code>/country Your Country</code> <i>or</i> "
            "<code>/country UTC+5:30</code>")


def country_set_card(ctx: C.TzCtx, extra: str | None = None) -> str:
    now = ctx.now()
    lines = [
        head("✅", "Country saved"),
        f"{ctx.flag} <b>{e(ctx.name)}</b>",
        f"🕒 Your local time now: <code>{e(ctx.fmt(now))}</code>",
        f"<i>All times will be shown in {e(ctx.label)}.</i>",
    ]
    if extra:
        lines.append("\n" + extra)
    return "\n".join(lines)


def pending_card(name: str, ctx: C.TzCtx | None) -> str:
    return "\n".join([
        head("🔒", "Access required"),
        f"Hi <b>{e(name)}</b>! This bot is private. Your request has been sent to the admin.",
        "",
        "⏳ <b>Waiting for approval</b>",
        "<i>You'll get a message here as soon as the admin decides.</i>",
    ])


def denied_card() -> str:
    return notice("⛔", "Access denied", "The admin has not given you access to this bot.",
                  "<i>If you think this is a mistake, please contact the admin.</i>")


def no_perm_card(cmd: str, keys: list[str]) -> str:
    allowed = " ".join(f"/{k}" for k in keys) or "none yet"
    return notice("🚫", "Not allowed", f"You don't have permission to use <code>/{e(cmd)}</code>.",
                  f"🎛 Your commands: <code>{e(allowed)}</code>",
                  "<i>Ask the admin if you need more access.</i>")


def group_denied_card(chat_id: int) -> str:
    return notice("🚫", "This group is not allowed", f"Group ID: <code>{chat_id}</code>",
                  "<i>The admin can allow it by sending</i> <code>/allow</code> <i>in this group.</i>")


def request_admin_card(rec: dict) -> str:
    return "\n".join([
        head("🔔", "New access request"),
        f"👤 <b>{who(rec)}</b>",
        f"🆔 <code>{rec['id']}</code>",
        f"🌍 {country_text(rec)}",
        "",
        "Choose what this user may do:",
    ])


def access_changed_card(rec: dict, ctx_text: str, access: str) -> str:
    return "\n".join([
        head("🎉", "Your access was updated"),
        f"📌 <b>{e(access)}</b>",
        "",
        f"<i>{e(ctx_text)}</i>",
        "Send /help to see everything you can do.",
    ])


# ---------------- welcome / help ----------------

CMD_DOCS: dict[str, list[tuple[str, str]]] = {
    "check": [("/check", "Check every link with every word"), ("/cancel", "Stop a running check")],
    "addword": [("/addword Word1 Word2", "Add words & check only those")],
    "test": [("/test LINK", "Inspect one link in detail")],
    "found": [("/found", "Every working link found so far")],
    "autocheck": [("/autocheck N", "Re-check every N minutes (1–10)"),
                  ("/stopautocheck", "Turn auto-check off"),
                  ("/notify all|new", "Alerts: every time or only new")],
    "status": [("/status", "Live status"), ("/stats", "Today's statistics")],
}
ADMIN_DOCS = [
    ("/users", "All users & their access"),
    ("/user ID", "Open one user's permission panel"),
    ("/grant ID all|cmds", "Give access, e.g. /grant 123 test found"),
    ("/revoke ID [cmds]", "Remove commands, or block the user"),
    ("/broadcast TEXT", "Message every user (preview first)"),
    ("/allow · /disallow · /allowed", "Manage allowed groups"),
    ("/resetstats", "Reset today's statistics for this chat"),
]


def welcome_card(name: str, ctx: C.TzCtx, access: str, keys: list[str], limit: int, admin: bool) -> str:
    lines = [
        head("✨", f"Welcome, {name}!"),
        f"{ctx.flag} <b>{e(ctx.name)}</b> · 🕒 <code>{e(ctx.fmt(ctx.now()))}</code>",
        f"🎛 Access: <b>{e(access)}</b>",
        "",
    ]
    if admin or {"check", "addword", "autocheck"} & set(keys):
        lines += [
            "<b>⚡ Quick start</b>",
            "1️⃣ Send <code>wordlist.txt</code> — one word per line",
            "2️⃣ Send <code>linklist.txt</code> — links with <code>(Word)</code> where the word goes",
            "3️⃣ Run /check — live progress, speed and time left",
            "",
            f"<i>Up to {n(limit)} link × word combinations per run.</i>",
        ]
    elif "test" in keys:
        lines += ["<b>⚡ Quick start</b>", "Send <code>/test LINK</code> to inspect any link."]
    else:
        lines += ["<i>Send /help to see what you can do.</i>"]
    lines.append("\nSend /help for all your commands.")
    return "\n".join(lines)


def help_card(keys: list[str], admin: bool, limit: int, uploads: bool) -> str:
    lines = [head("📖", "Help")]
    if uploads:
        lines += [
            "",
            "<b>📂 Your files</b>",
            "Send these as documents:",
            "• <code>wordlist.txt</code> — one word per line",
            "• <code>linklist.txt</code> — one link per line, e.g.",
            "<code>https://example.com/img_(Word)_en.jpg</code>",
            "<i>Send a file again to replace it · max 20 MB each.</i>",
        ]
    for key in PERMISSIONS:
        if key in keys:
            lines.append("")
            lines.append(f"<b>{_key_icon(key)} {e(PERMISSIONS[key][1])}</b>")
            for cmd, desc in CMD_DOCS[key]:
                lines.append(f"• <code>{e(cmd)}</code> — {e(desc)}")
    lines += ["", "<b>🌍 General</b>",
              "• <code>/country</code> — change your country / time zone",
              "• <code>/id</code> — show this chat's ID"]
    if admin:
        lines += ["", "<b>👑 Admin</b>"]
        lines += [f"• <code>{e(c)}</code> — {e(d)}" for c, d in ADMIN_DOCS]
    lines += ["", f"<i>Limit: {n(limit)} combinations per run. Nothing is skipped — network errors are "
                  "retried and anything unverified is reported.</i>"]
    return "\n".join(lines)


def _key_icon(key: str) -> str:
    return {"check": "🔎", "addword": "➕", "test": "🔬", "found": "📚", "autocheck": "🔁", "status": "📊"}.get(key, "•")


# ---------------- checking ----------------

def progress_card(s: dict, suspicious: bool, title: str = "Checking links…") -> str:
    total = s["total"] or 1
    pct = min(100.0, s["processed"] * 100.0 / total)
    rows = [
        ("📦 Checked", f"{code(n(s['processed']))} / {code(n(s['total']))}"),
        ("✅ Working", f"<b>{n(s['found'])}</b>"),
    ]
    if s["speed"] > 0:
        rows.append(("⚡ Speed", f"{code(n(s['speed']))} links/s"))
    rows.append(("⏱ Elapsed", code(fmt_dur(s["elapsed"]))))
    if s["in_retry"]:
        rows.append(("⏳ Time left", "<i>almost done</i>"))
    elif s["eta"] is not None:
        rows.append(("⏳ Time left", code("~" + fmt_dur(s["eta"]))))
    else:
        rows.append(("⏳ Time left", "<i>calculating…</i>"))
    lines = [head("🔎", title), f"<code>{bar(pct)}</code> <b>{pct:.1f}%</b>", "", tree(rows)]
    notes = []
    if s["conns_max"] and s["conns"] < 0.85 * s["conns_max"] and s["processed"] < s["total"]:
        notes.append(f"⚙️ Server is limiting speed — using {n(s['conns'])} of {n(s['conns_max'])} connections "
                     "(adjusts automatically)")
    if s["in_retry"]:
        notes.append(f"🔁 Re-checking {n(s['retrying'])} links that had network errors")
    elif s["retrying"]:
        notes.append(f"🔁 Retrying {n(s['retrying'])} links (network errors)")
    if suspicious:
        notes.append("⚠️ Unusually many links look 'working' — the server may be answering everything")
    if notes:
        lines += ["", *[e(x) for x in notes]]
    lines += ["", f"<i>🧵 {s['processes']} worker process(es) · send /cancel to stop</i>"]
    return "\n".join(lines)


def summary_card(run, extra: list[str], tz: C.TzCtx) -> str:
    if run.cancelled:
        icon, title = "🛑", "CHECK CANCELLED"
    elif run.aborted or run.crashed or run.errors:
        icon, title = "⚠️", "CHECK STOPPED EARLY"
    elif run.unverified_count:
        icon, title = "⚠️", "CHECK FINISHED WITH WARNINGS"
    else:
        icon, title = "✅", "CHECK COMPLETE"
    el = run.elapsed()
    avg = f" · avg {code(n(run.processed / el))} links/s" if el > 0 and run.processed else ""
    checked = code(n(run.processed)) + (f" of {code(n(run.total))}" if run.processed != run.total else "")
    rows = [
        ("📦 Checked", checked),
        ("✅ Working", f"<b>{n(len(run.found))}</b>"),
    ]
    if run.unverified_count:
        rows.append(("⚠️ Unverified", f"<b>{n(run.unverified_count)}</b>"))
    rows.append(("⏱ Time", f"{code(fmt_dur(el))}{avg}"))
    rows.append(("🕒 Finished", f"{code(tz.fmt(tz.now()))} · <i>{e(tz.label)}</i>"))
    lines = [
        head(icon, title),
        f"🔗 <code>{n(len(run.links))}</code> links × 📝 <code>{n(len(run.words))}</code> words "
        f"= <b>{n(run.total)}</b> combinations",
        "",
        tree(rows),
    ]
    notes = []
    if run.unverified_count:
        notes.append("Some links could not be verified (network/server errors) — the list is attached.")
    if run.soft404:
        notes.append("ℹ️ This server answers non-existent links with a placeholder — those were filtered out automatically.")
    if run.suspicious:
        pct = len(run.found) * 100 / max(1, run.processed)
        notes.append(f"⚠️ {pct:.0f}% of the links look 'working'. That is not normal, so these results may be "
                     "wrong. Send /test with one link to see what the server really answers.")
    if run.aborted:
        notes.append("The server stopped answering (too many errors in a row). Try again later or lower CONCURRENCY.")
    if run.crashed:
        notes.append(f"{len(run.crashed)} worker process(es) crashed — results may be incomplete.")
    if run.errors:
        notes.append(f"Error: {run.errors[0][:200]}")
    notes.extend(extra or [])
    if notes:
        lines += ["", *[f"• {e(x)}" for x in notes]]
    return "\n".join(lines)


def alert_caption(url: str, lm_raw, size, known: bool, tz: C.TzCtx, kind=None, dims=None, html: bool = True) -> str:
    """Caption sent with every working link's picture."""
    from email.utils import parsedate_to_datetime
    dt = None
    if lm_raw:
        try:
            dt = parsedate_to_datetime(str(lm_raw))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = None
    title = "✅ Working Link Found! (seen before)" if known else "🆕 NEW Working Link Found!"
    when = tz.fmt(dt) if dt else "not provided by the server"
    ago_txt = C.ago(dt) if dt else None
    details = []
    if kind and dims:
        details.append(f"🖼 {kind} · {dims[0]}×{dims[1]} px")
    elif kind:
        details.append(f"🖼 {kind}")
    sz = fmt_size(size)
    if sz:
        details.append(f"📦 {sz}")
    if html:
        lines = [f"<b>{e(title)}</b>", DIV, f"🔗 <a href=\"{eq(url)}\">Open link</a>", f"<code>{e(url)}</code>", "",
                 f"📅 <b>Last modified</b> · {e(tz.label)}", f"<code>{e(when)}</code>"]
        if ago_txt:
            lines.append(f"🕒 <i>{e(ago_txt)}</i>")
        if details:
            lines += ["", " · ".join(e(d) for d in details)]
        return "\n".join(lines)
    lines = [title, url, "", f"📅 Last modified ({tz.label}):", when]
    if ago_txt:
        lines.append(f"🕒 {ago_txt}")
    if details:
        lines += ["", " · ".join(details)]
    return "\n".join(lines)


# ---------------- /test ----------------

_HINTS = {
    404: "The file does not exist (yet) at this address.",
    403: "Access denied — the file may not exist, or the server blocks this request.",
    401: "The server wants a login for this address.",
    410: "The file was removed on purpose.",
    429: "The server asked us to slow down. Try again in a moment.",
}


def _status_text(code_: int) -> str:
    try:
        return f"{code_} {http.HTTPStatus(code_).phrase}"
    except Exception:
        return str(code_)


def test_card(url: str, res: dict, tz: C.TzCtx) -> str:
    main, ctrl = res["main"], res["control"]
    lines = [head("🔬", "LINK TEST"), f"🔗 <code>{e(url)}</code>", ""]
    if res["ok"]:
        lines.append(f"✅ <b>WORKING</b> — {e(res['reason'])}")
    else:
        lines.append("❌ <b>NOT WORKING</b>")
        lines.append(f"<blockquote>{e(res['reason'][:1].upper() + res['reason'][1:])}</blockquote>")

    if main.get("error"):
        lines += ["", "📡 <b>Server response</b>", f"└ Error  <code>{e(main['error'])}</code>"]
    else:
        status = main["status"]
        rows = [("Status", code(_status_text(status)))]
        if main.get("redirected"):
            rows.append(("Redirect", f"<code>{e(main['final'])}</code>"))
        if status in (200, 206):
            rows.append(("Type", code(main["ctype"] or "not given")))
            if main.get("kind"):
                dims = f" · <code>{main['dims'][0]} × {main['dims'][1]}</code> px" if main.get("dims") else ""
                rows.append(("Image", f"<b>{e(main['kind'])}</b>{dims} ✅"))
            elif checker_is_image(url):
                rows.append(("Image", "not a real image ❌"))
            sz = fmt_size(main.get("total"))
            if sz:
                rows.append(("Size", code(sz)))
        if main.get("ms") is not None:
            rows.append(("Response", code(f"{main['ms']} ms")))
        srv = main.get("server")
        cdn = main.get("cdn")
        if srv or cdn:
            rows.append(("Server", " · ".join(code(x[:60]) for x in (srv, cdn) if x)))
        lines += ["", "📡 <b>Server response</b>", tree(rows)]
        hint = _HINTS.get(status)
        if hint and not res["ok"]:
            lines.append(f"<i>{e(hint)}</i>")

        if status in (200, 206):
            from email.utils import parsedate_to_datetime
            lm = main.get("lm")
            dt = None
            if lm:
                try:
                    dt = parsedate_to_datetime(lm)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except Exception:
                    dt = None
            lines += ["", f"📅 <b>Last modified</b> · {e(tz.label)}"]
            if dt:
                lines += [f"<code>{e(tz.fmt(dt))}</code>", f"🕒 <i>{e(C.ago(dt))}</i>"]
            else:
                lines.append("<i>not provided by the server</i>")

    lines += ["", "🛡 <b>Server honesty check</b>", f"<i>Control link (cannot exist):</i> <code>{e(ctrl.get('url', ''))}</code>"]
    if ctrl.get("error"):
        lines.append(f"└ Error  <code>{e(ctrl['error'])}</code>")
    elif ctrl["status"] in (200, 206):
        lines.append(f"└ ⚠️ HTTP <code>{ctrl['status']}</code> — the server ALSO returns a file for links that don't "
                     "exist, so status codes can't be trusted. The bot compares the files themselves "
                     "(done automatically).")
    else:
        lines.append(f"└ ✅ HTTP <code>{ctrl['status']}</code> — the server correctly rejects missing files.")
    lines += ["", f"<i>Tested {e(tz.fmt(tz.now()))} · {e(tz.label)}</i>"]
    return "\n".join(lines)


def checker_is_image(url: str) -> bool:
    import checker
    return checker.is_image_url(url)


# ---------------- found / status / stats ----------------

def found_card(entries: list[tuple[str, str | None]], total: int, tz: C.TzCtx, more_in_file: bool) -> str:
    from email.utils import parsedate_to_datetime
    lines = [head("📚", "FOUND LINKS"), f"<b>{n(total)}</b> working link(s) so far · newest first", ""]
    for url, lm in entries:
        dt = None
        if lm:
            try:
                dt = parsedate_to_datetime(lm)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                dt = None
        lines.append(f"🔗 <code>{e(url)}</code>")
        lines.append(f"   📅 {e(tz.fmt(dt)) if dt else 'last modified not provided'}" + (f" · <i>{e(C.ago(dt))}</i>" if dt else ""))
    lines.append("")
    lines.append(f"<i>Times shown in {e(tz.label)}.</i>" + (" The full list is attached below." if more_in_file else ""))
    return "\n".join(lines)


def status_card(state: dict, total: int, limit: int, auto_on: bool, tz: C.TzCtx, snap: dict | None, kind: str | None) -> str:
    last = tz.fmt(datetime.fromtimestamp(state["last_run_time"], timezone.utc)) if state["last_run_time"] else "never"
    rows = [
        ("📝 Words", code(n(len(state["words"])))),
        ("🔗 Link patterns", code(n(len(state["links"])))),
        ("🧮 Combinations", f"{code(n(total))} <i>(limit {n(limit)})</i>"),
        ("🔁 Auto-check", f"<b>every {state['interval_minutes']} min</b> ✅" if auto_on else "off"),
        ("🔔 Alerts", b(state["notify_mode"])),
        ("📚 In history", code(n(len(state["history"])))),
    ]
    lines = [head("📡", "STATUS"), tree(rows), "", "<b>🕘 Last check</b>"]
    if state["last_run_time"]:
        lines.append(tree([
            ("When", code(last)),
            ("Checked", code(n(state["last_run_checked"]))),
            ("Working", f"<b>{n(state['last_run_working'])}</b>"),
            ("Unverified", code(n(state["last_run_unverified"]))),
        ]))
    else:
        lines.append("<i>nothing checked yet</i>")
    if snap is not None:
        pct = snap["processed"] * 100 / max(1, snap["total"])
        eta = fmt_dur(snap["eta"]) if snap["eta"] is not None else "calculating…"
        lines += ["", f"⏳ <b>Running now</b> ({e(kind or '')})",
                  f"<code>{bar(pct)}</code> <b>{pct:.1f}%</b>",
                  f"{code(n(snap['processed']))} / {code(n(snap['total']))} · ~{code(eta)} left · "
                  f"✅ {n(snap['found'])}"]
    elif state["busy"]:
        lines += ["", "⏳ <i>A check is starting…</i>"]
    lines += ["", f"<i>Times shown in {e(tz.label)} · see /stats for today's numbers</i>"]
    return "\n".join(lines)


def stats_card(today: dict, yesterday: dict | None, tz: C.TzCtx, admin_global: dict | None) -> str:
    reset = C.next_reset()
    left = (reset - datetime.now(timezone.utc)).total_seconds()
    lines = [
        head("📊", "DAILY STATISTICS"),
        "🗓 <b>Today</b> · resets every day at <b>04:00 AM Bangladesh Time</b>",
        f"🔄 Next reset: <code>{e(tz.fmt(reset))}</code>",
        f"<i>{e(tz.label)} · in {e(fmt_dur(left))}</i>",
        "",
    ]

    def block(d: dict) -> str:
        rows = [
            ("🔎 Checks run", f"<b>{n(d['checks'])}</b>"),
            ("🔗 Links checked", f"<b>{n(d['combos'])}</b>"),
            ("✅ Working found", f"<b>{n(d['found'])}</b>"),
            ("⚠️ Unverified", f"<b>{n(d['unverified'])}</b>"),
            ("⚡ Best speed", f"{code(n(d['best_speed']))} links/s"),
            ("⏱ Checking time", code(fmt_dur(d["seconds"]))),
            ("🔁 Auto cycles", f"<b>{n(d['auto_cycles'])}</b>" + (f" <i>(skipped {n(d['skipped'])})</i>" if d["skipped"] else "")),
            ("🧪 Link tests", f"<b>{n(d['tests'])}</b>"),
            ("📨 Alerts sent", f"<b>{n(d['alerts'])}</b>"),
        ]
        return tree(rows)

    lines.append(block(today))
    if yesterday:
        lines += ["", f"🌙 <b>Yesterday</b>: {n(yesterday['checks'])} checks · {n(yesterday['combos'])} links · "
                      f"{n(yesterday['found'])} found"]
    if admin_global:
        lines += ["", "🌐 <b>All users today</b>",
                  f"{n(admin_global['checks'])} checks · {n(admin_global['combos'])} links · "
                  f"{n(admin_global['found'])} found · {n(admin_global['tests'])} tests"]
    return "\n".join(lines)


# ---------------- auto-check ----------------

def autocheck_on_card(minutes: int, mode: str) -> str:
    return "\n".join([
        head("🔁", "AUTO-CHECK ON"),
        f"⏱ Every <b>{minutes} minute(s)</b> every link is re-checked with every word",
        f"🔔 Alerts: <b>{'every working link, every cycle' if mode == 'all' else 'only NEW links'}</b>",
        "📡 A status update arrives every 30 minutes",
        "♻️ It resumes by itself if the bot restarts",
        "",
        "<i>If a check takes longer than the interval, the next one waits — checks never overlap.</i>",
    ])


def heartbeat_card(state: dict, today: dict, tz: C.TzCtx, snap: dict | None) -> str:
    last = tz.fmt(datetime.fromtimestamp(state["last_run_time"], timezone.utc)) if state["last_run_time"] else "not yet"
    rows = [
        ("⏱ Interval", f"{code(str(state['interval_minutes']) + ' min')} · alerts {b(state['notify_mode'])}"),
        ("🔁 Cycles today", f"<b>{n(today['auto_cycles'])}</b>"),
        ("🕘 Last check", code(last)),
        ("📦 Last size", f"{code(n(state['last_run_checked']))} links · ✅ {n(state['last_run_working'])} · ⚠️ {n(state['last_run_unverified'])}"),
        ("📚 In history", code(n(len(state["history"])))),
    ]
    lines = [head("📡", "AUTO-CHECK STATUS"), tree(rows)]
    if snap is not None:
        pct = snap["processed"] * 100 / max(1, snap["total"])
        eta = fmt_dur(snap["eta"]) if snap["eta"] is not None else "calculating…"
        lines += ["", f"⏳ <b>Current cycle</b>  <code>{bar(pct)}</code> <b>{pct:.1f}%</b>",
                  f"{code(n(snap['processed']))} / {code(n(snap['total']))} · ~{code(eta)} left · ✅ {n(snap['found'])}"]
    else:
        lines += ["", "💤 <i>Waiting for the next cycle</i>"]
    lines += ["", f"<i>Still running ✅ · times shown in {e(tz.label)}</i>"]
    return "\n".join(lines)


# ---------------- admin ----------------

def users_card(users: list[dict], page: int, pages: int, total: int, counts: dict) -> str:
    lines = [head("👥", "USERS"),
             f"<b>{n(total)}</b> total · 👑 {counts.get('admin', 0)} admin · ⏳ {counts.get('pending', 0)} pending · "
             f"✅ {counts.get('approved', 0)} approved · ⛔ {counts.get('denied', 0)} blocked", ""]
    for rec in users:
        icon = {"pending": "⏳", "approved": "✅", "denied": "⛔", "admin": "👑"}.get(rec["status"], "•")
        if rec["status"] == "admin":
            access = "Admin · full control"
        elif rec["status"] == "approved":
            access = ("Full access" if rec["cmds"] == "all" else
                      (" ".join("/" + k for k in PERMISSIONS if k in rec["cmds"]) or "no commands"))
        else:
            access = "waiting for approval" if rec["status"] == "pending" else "blocked"
        lines.append(f"{icon} <b>{who(rec)}</b> · <code>{rec['id']}</code>")
        lines.append(f"   {country_text(rec)} · <i>{e(access)}</i>")
    lines += ["", f"<i>Page {page + 1}/{pages} · tap a name to manage permissions</i>"]
    return "\n".join(lines)


def perm_panel_text(rec: dict, status: str, access: str) -> str:
    icon = {"approved": "✅", "pending": "⏳", "denied": "⛔", "admin": "👑"}.get(status, "•")
    return "\n".join([
        head("🎛", "PERMISSIONS"),
        f"👤 <b>{who(rec)}</b>",
        f"🆔 <code>{rec['id']}</code> · 🌍 {country_text(rec)}",
        f"{icon} <b>{e(access)}</b>",
        "",
        "Tap a command to switch it on / off:",
        "",
        *[f"<code>/{k}</code> — {e(PERMISSIONS[k][1])}" for k in PERMISSIONS],
    ])


def perm_buttons(uid: int, rec: dict) -> list[list[tuple[str, str]]]:
    cmds = list(PERMISSIONS) if rec.get("cmds") == "all" else (rec.get("cmds") or [])
    on = rec.get("status") == "approved"
    rows, row = [], []
    for k in PERMISSIONS:
        mark = "✅" if (on and k in cmds) else "⬜"
        row.append((f"{mark} /{k}", f"perm:t:{uid}:{k}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("✅ Allow all", f"perm:all:{uid}"), ("⛔ Block", f"perm:no:{uid}")])
    rows.append([("💾 Done", f"perm:ok:{uid}")])
    return rows


def request_buttons(uid: int) -> list[list[tuple[str, str]]]:
    return [[("✅ Allow all", f"perm:all:{uid}"), ("🎛 Choose commands", f"perm:menu:{uid}")],
            [("⛔ Deny", f"perm:no:{uid}")]]


def broadcast_preview(text: str, count: int, reply_mode: bool) -> str:
    body = "<i>(the message you replied to will be copied as it is)</i>" if reply_mode else f"<blockquote>{e(text)}</blockquote>"
    return "\n".join([head("📢", "BROADCAST PREVIEW"), f"👥 Will be sent to <b>{n(count)}</b> user(s):", "", body, "",
                      "<i>Check it carefully — a sent broadcast cannot be taken back.</i>"])


def announcement(text: str) -> str:
    return "\n".join([head("📢", "ANNOUNCEMENT"), e(text), "", "<i>— Admin</i>"])


def broadcast_report(sent: int, failed: int, total: int, seconds: float) -> str:
    return "\n".join([head("📢", "BROADCAST DONE"),
                      tree([("✅ Delivered", f"<b>{n(sent)}</b>"), ("❌ Failed", f"<b>{n(failed)}</b>"),
                            ("👥 Total", code(n(total))), ("⏱ Time", code(fmt_dur(seconds)))]),
                      "<i>Failed = the user blocked the bot or never opened a private chat with it.</i>" if failed else ""])

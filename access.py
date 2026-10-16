"""
access.py - who may use the bot (and which commands), plus the daily statistics.

* Admins (ADMIN_IDS / personal IDs in ALLOWED_CHAT_IDS) can do everything.
* Everybody else must be approved by an admin first. An admin can give a user
  ALL commands or only some of them (for example only /test).
* Everything is saved in the data folder, so it survives restarts.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import countries as C

# Permission groups an admin can give to a user: key -> (commands included, description)
PERMISSIONS: dict[str, tuple[str, str]] = {
    "check": ("/check /cancel", "Full checks, uploading files"),
    "addword": ("/addword", "Add words & check only those"),
    "test": ("/test", "Diagnose a single link"),
    "found": ("/found", "See all links found so far"),
    "autocheck": ("/autocheck /stopautocheck /notify", "Automatic checking"),
    "status": ("/status /stats", "Live status & daily statistics"),
}
# Words an admin may type that mean one of the groups above.
ALIASES = {
    "cancel": "check", "upload": "check", "uploads": "check",
    "stopautocheck": "autocheck", "notify": "autocheck", "auto": "autocheck",
    "stats": "status",
}
UPLOAD_PERMS = ("check", "addword", "autocheck")


def parse_perm_words(words: list[str]) -> tuple[list[str], list[str]]:
    """['/test', 'found', 'foo'] -> (['test', 'found'], ['foo'])"""
    good, bad = [], []
    for w in words:
        k = w.strip().lstrip("/").lower()
        if not k:
            continue
        k = ALIASES.get(k, k)
        if k in PERMISSIONS:
            if k not in good:
                good.append(k)
        else:
            bad.append(w)
    return good, bad


def _atomic_write(path: Path, text: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class AccessStore:
    """Users, their permissions, their country/time zone, and group time zones."""

    def __init__(self, data_dir: Path, admin_ids: set[int]):
        self.dir = Path(data_dir)
        self.admin_ids = set(admin_ids)
        self.users_path = self.dir / "users.json"
        self.chats_path = self.dir / "chat_settings.json"
        self.users: dict[int, dict] = {int(k): v for k, v in _read_json(self.users_path, {}).items()}
        self.chats: dict[int, dict] = {int(k): v for k, v in _read_json(self.chats_path, {}).items()}

    # ---------- saving ----------
    def save(self):
        _atomic_write(self.users_path, json.dumps({str(k): v for k, v in self.users.items()}, ensure_ascii=False))

    def save_chats(self):
        _atomic_write(self.chats_path, json.dumps({str(k): v for k, v in self.chats.items()}, ensure_ascii=False))

    # ---------- users ----------
    def is_admin(self, uid: int) -> bool:
        return uid in self.admin_ids

    def get(self, uid: int) -> dict | None:
        return self.users.get(uid)

    def touch(self, user) -> dict:
        """Remember a user (new ones start as 'pending') and keep the name up to date."""
        uid = user.id
        name = " ".join(p for p in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if p) or str(uid)
        username = getattr(user, "username", None) or ""
        rec = self.users.get(uid)
        changed = False
        if rec is None:
            rec = {"id": uid, "name": name, "username": username, "status": "pending", "cmds": [],
                   "country": None, "iso": None, "tz": None, "joined": time.time(), "requested": False}
            self.users[uid] = rec
            changed = True
        elif rec.get("name") != name or rec.get("username") != username:
            rec["name"], rec["username"] = name, username
            changed = True
        rec["last_seen"] = time.time()
        if changed:
            self.save()
        return rec

    def status(self, uid: int) -> str:
        if self.is_admin(uid):
            return "admin"
        rec = self.users.get(uid)
        return rec["status"] if rec else "pending"

    def can(self, uid: int, need) -> bool:
        """need = None (any approved user) | 'test' | ('check', 'addword') meaning any of them."""
        if self.is_admin(uid):
            return True
        rec = self.users.get(uid)
        if not rec or rec["status"] != "approved":
            return False
        if need is None:
            return True
        cmds = rec["cmds"]
        if cmds == "all":
            return True
        needs = (need,) if isinstance(need, str) else tuple(need)
        return any(n in cmds for n in needs)

    def allowed_keys(self, uid: int) -> list[str]:
        if self.is_admin(uid):
            return list(PERMISSIONS)
        rec = self.users.get(uid)
        if not rec or rec["status"] != "approved":
            return []
        return list(PERMISSIONS) if rec["cmds"] == "all" else [k for k in PERMISSIONS if k in rec["cmds"]]

    def access_text(self, uid: int) -> str:
        if self.is_admin(uid):
            return "Admin (full control)"
        rec = self.users.get(uid)
        if not rec:
            return "No access"
        if rec["status"] == "denied":
            return "Blocked"
        if rec["status"] != "approved":
            return "Waiting for approval"
        if rec["cmds"] == "all":
            return "Full access"
        return ", ".join("/" + k for k in PERMISSIONS if k in rec["cmds"]) or "No commands"

    def approve_all(self, uid: int):
        rec = self.users.setdefault(uid, self._blank(uid))
        rec.update(status="approved", cmds="all")
        self.save()

    def set_commands(self, uid: int, keys: list[str]):
        rec = self.users.setdefault(uid, self._blank(uid))
        rec.update(status="approved", cmds=[k for k in PERMISSIONS if k in keys])
        self.save()

    def toggle(self, uid: int, key: str) -> bool:
        """Turn one permission on/off. An all-access user becomes a normal user first.
        Returns True if the permission is now ON."""
        rec = self.users.setdefault(uid, self._blank(uid))
        cmds = list(PERMISSIONS) if rec.get("cmds") == "all" else list(rec.get("cmds") or [])
        if key in cmds:
            cmds.remove(key)
            on = False
        else:
            cmds.append(key)
            on = True
        rec.update(status="approved", cmds=[k for k in PERMISSIONS if k in cmds])
        self.save()
        return on

    def revoke(self, uid: int, keys: list[str]):
        rec = self.users.get(uid)
        if not rec:
            return
        cmds = list(PERMISSIONS) if rec.get("cmds") == "all" else list(rec.get("cmds") or [])
        rec["cmds"] = [k for k in PERMISSIONS if k in cmds and k not in keys]
        self.save()

    def deny(self, uid: int):
        rec = self.users.setdefault(uid, self._blank(uid))
        rec.update(status="denied", cmds=[])
        self.save()

    def _blank(self, uid: int) -> dict:
        return {"id": uid, "name": str(uid), "username": "", "status": "pending", "cmds": [],
                "country": None, "iso": None, "tz": None, "joined": time.time(), "requested": False}

    def ensure(self, uid: int) -> dict:
        """The record for a user ID, creating an empty one if the user never wrote to the bot."""
        if uid not in self.users:
            self.users[uid] = self._blank(uid)
            self.save()
        return self.users[uid]

    def find(self, text: str) -> dict | None:
        """Find a user by numeric ID or @username."""
        t = text.strip().lstrip("@")
        if t.lstrip("-").isdigit():
            return self.users.get(int(t))
        for rec in self.users.values():
            if rec.get("username", "").lower() == t.lower():
                return rec
        return None

    def sorted_users(self) -> list[dict]:
        order = {"pending": 0, "approved": 1, "denied": 2}
        return sorted(self.users.values(), key=lambda r: (order.get(r["status"], 3), -(r.get("last_seen") or 0)))

    # ---------- country / time zone ----------
    def set_user_tz(self, uid: int, ctx: C.TzCtx):
        rec = self.users.setdefault(uid, self._blank(uid))
        rec.update(country=ctx.name, iso=ctx.iso, tz=ctx.key)
        self.save()

    def set_chat_tz(self, chat_id: int, ctx: C.TzCtx):
        self.chats[chat_id] = {"country": ctx.name, "iso": ctx.iso, "tz": ctx.key}
        self.save_chats()

    def has_tz(self, uid: int) -> bool:
        rec = self.users.get(uid)
        return bool(rec and rec.get("tz"))

    def tz_ctx(self, chat_id: int) -> C.TzCtx:
        """Time zone used for a chat: a user's own country in private chats, the group's setting in groups."""
        rec = self.users.get(chat_id) if chat_id > 0 else self.chats.get(chat_id)
        if rec and rec.get("tz"):
            return C.TzCtx(rec["tz"], rec.get("country") or rec["tz"], rec.get("iso") or "")
        return C.default_ctx()


# ============================================================
# DAILY STATISTICS (reset every day at 04:00 Bangladesh time)
# ============================================================

COUNTERS = ("checks", "combos", "found", "unverified", "seconds", "auto_cycles", "skipped", "tests", "alerts")


def _blank_day(day: str) -> dict:
    d = {k: 0 for k in COUNTERS}
    d["best_speed"] = 0.0
    d["day"] = day
    return d


class StatsStore:
    """Per-chat and global counters. A new 'statistics day' starts at 04:00 Bangladesh time;
    the finished day is kept as 'yesterday'."""

    def __init__(self, data_dir: Path, now_fn=None):
        self.dir = Path(data_dir)
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.cache: dict[str, dict] = {}

    def _path(self, key: str) -> Path:
        if key == "global":
            return self.dir / "global_stats.json"
        d = self.dir / key
        d.mkdir(parents=True, exist_ok=True)
        return d / "stats.json"

    def _load(self, key: str) -> dict:
        if key not in self.cache:
            data = _read_json(self._path(key), None)
            if not isinstance(data, dict) or "today" not in data:
                data = {"today": _blank_day(C.stats_day_key(self.now_fn())), "yesterday": None}
            self.cache[key] = data
        data = self.cache[key]
        day = C.stats_day_key(self.now_fn())
        if data["today"].get("day") != day:           # a new statistics day has begun -> reset
            old = data["today"]
            data["yesterday"] = old if old.get("day") else None
            data["today"] = _blank_day(day)
            self._save(key)
        return data

    def _save(self, key: str):
        _atomic_write(self._path(key), json.dumps(self.cache[key]))

    def get(self, key) -> dict:
        return self._load(str(key))

    def add(self, key, **inc):
        """Add to today's counters (best_speed keeps the highest value)."""
        for k in (str(key), "global"):
            data = self._load(k)
            today = data["today"]
            for name, val in inc.items():
                if name == "best_speed":
                    today["best_speed"] = max(today["best_speed"], float(val))
                elif name in COUNTERS:
                    today[name] += val
            self._save(k)

    def reset(self, key):
        data = self._load(str(key))
        data["today"] = _blank_day(C.stats_day_key(self.now_fn()))
        self._save(str(key))

    def rollover_all(self):
        """Called by the 04:00 job so every chat's statistics reset even if nobody uses the bot."""
        for k in list(self.cache):
            self._load(k)
        for p in self.dir.iterdir():
            name = p.name
            if p.is_dir() and name.lstrip("-").isdigit() and (p / "stats.json").exists():
                self._load(name)
        self._load("global")

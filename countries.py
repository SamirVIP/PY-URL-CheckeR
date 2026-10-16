"""
countries.py - countries, time zones and time formatting.

Every user picks a country once; all times the bot shows (file "Last modified"
dates, last-check times, the daily reset time...) are then shown in that
country's local time, 12-hour clock.

Time zone data comes from Python's zoneinfo (pip install tzdata on Windows).
If a zone name is unknown on your system, a fixed UTC offset is used instead,
so a missing time-zone database can never break the bot.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

try:
    from zoneinfo import ZoneInfo
except ImportError:  # very old Python
    ZoneInfo = None

# (ISO code, name shown to users, [time zone names to try], fallback UTC offset in minutes)
COUNTRIES: list[tuple[str, str, list[str], int]] = [
    ("BD", "Bangladesh", ["Asia/Dhaka"], 360),
    ("IN", "India", ["Asia/Kolkata", "Asia/Calcutta"], 330),
    ("PK", "Pakistan", ["Asia/Karachi"], 300),
    ("LK", "Sri Lanka", ["Asia/Colombo"], 330),
    ("NP", "Nepal", ["Asia/Kathmandu", "Asia/Katmandu"], 345),
    ("BT", "Bhutan", ["Asia/Thimphu"], 360),
    ("MV", "Maldives", ["Indian/Maldives"], 300),
    ("AF", "Afghanistan", ["Asia/Kabul"], 270),
    ("IR", "Iran", ["Asia/Tehran"], 210),
    ("IQ", "Iraq", ["Asia/Baghdad"], 180),
    ("SA", "Saudi Arabia", ["Asia/Riyadh"], 180),
    ("AE", "United Arab Emirates", ["Asia/Dubai"], 240),
    ("QA", "Qatar", ["Asia/Qatar"], 180),
    ("KW", "Kuwait", ["Asia/Kuwait"], 180),
    ("OM", "Oman", ["Asia/Muscat"], 240),
    ("TR", "Turkey", ["Europe/Istanbul", "Asia/Istanbul"], 180),
    ("EG", "Egypt", ["Africa/Cairo"], 120),
    ("NG", "Nigeria", ["Africa/Lagos"], 60),
    ("KE", "Kenya", ["Africa/Nairobi"], 180),
    ("ZA", "South Africa", ["Africa/Johannesburg"], 120),
    ("MA", "Morocco", ["Africa/Casablanca"], 60),
    ("DZ", "Algeria", ["Africa/Algiers"], 60),
    ("ET", "Ethiopia", ["Africa/Addis_Ababa"], 180),
    ("GH", "Ghana", ["Africa/Accra"], 0),
    ("TH", "Thailand", ["Asia/Bangkok"], 420),
    ("VN", "Vietnam", ["Asia/Ho_Chi_Minh", "Asia/Saigon"], 420),
    ("ID", "Indonesia", ["Asia/Jakarta"], 420),
    ("MY", "Malaysia", ["Asia/Kuala_Lumpur"], 480),
    ("SG", "Singapore", ["Asia/Singapore"], 480),
    ("PH", "Philippines", ["Asia/Manila"], 480),
    ("MM", "Myanmar", ["Asia/Yangon", "Asia/Rangoon"], 390),
    ("KH", "Cambodia", ["Asia/Phnom_Penh"], 420),
    ("LA", "Laos", ["Asia/Vientiane"], 420),
    ("CN", "China", ["Asia/Shanghai"], 480),
    ("HK", "Hong Kong", ["Asia/Hong_Kong"], 480),
    ("TW", "Taiwan", ["Asia/Taipei"], 480),
    ("JP", "Japan", ["Asia/Tokyo"], 540),
    ("KR", "South Korea", ["Asia/Seoul"], 540),
    ("MN", "Mongolia", ["Asia/Ulaanbaatar"], 480),
    ("KZ", "Kazakhstan", ["Asia/Almaty"], 300),
    ("UZ", "Uzbekistan", ["Asia/Tashkent"], 300),
    ("RU", "Russia (Moscow)", ["Europe/Moscow"], 180),
    ("UA", "Ukraine", ["Europe/Kyiv", "Europe/Kiev"], 120),
    ("PL", "Poland", ["Europe/Warsaw"], 60),
    ("DE", "Germany", ["Europe/Berlin"], 60),
    ("FR", "France", ["Europe/Paris"], 60),
    ("IT", "Italy", ["Europe/Rome"], 60),
    ("ES", "Spain", ["Europe/Madrid"], 60),
    ("PT", "Portugal", ["Europe/Lisbon"], 0),
    ("NL", "Netherlands", ["Europe/Amsterdam"], 60),
    ("SE", "Sweden", ["Europe/Stockholm"], 60),
    ("GB", "United Kingdom", ["Europe/London"], 0),
    ("IE", "Ireland", ["Europe/Dublin"], 0),
    ("GR", "Greece", ["Europe/Athens"], 120),
    ("RO", "Romania", ["Europe/Bucharest"], 120),
    ("US", "USA (New York)", ["America/New_York"], -300),
    ("US", "USA (Chicago)", ["America/Chicago"], -360),
    ("US", "USA (Denver)", ["America/Denver"], -420),
    ("US", "USA (Los Angeles)", ["America/Los_Angeles"], -480),
    ("CA", "Canada (Toronto)", ["America/Toronto"], -300),
    ("MX", "Mexico", ["America/Mexico_City"], -360),
    ("BR", "Brazil", ["America/Sao_Paulo"], -180),
    ("AR", "Argentina", ["America/Argentina/Buenos_Aires", "America/Buenos_Aires"], -180),
    ("CL", "Chile", ["America/Santiago"], -240),
    ("CO", "Colombia", ["America/Bogota"], -300),
    ("PE", "Peru", ["America/Lima"], -300),
    ("VE", "Venezuela", ["America/Caracas"], -240),
    ("AU", "Australia (Sydney)", ["Australia/Sydney"], 600),
    ("AU", "Australia (Perth)", ["Australia/Perth"], 480),
    ("NZ", "New Zealand", ["Pacific/Auckland"], 720),
]

DEFAULT_COUNTRY = "Bangladesh"

# The daily statistics reset happens at 04:00 Bangladesh time (UTC+6, no daylight saving).
BD_TZ = timezone(timedelta(hours=6))
RESET_HOUR = 4

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_UTC_RE = re.compile(r"^(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def flag(iso: str) -> str:
    """Flag emoji from a 2-letter country code."""
    if len(iso) != 2 or not iso.isalpha():
        return "🌍"
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in iso.upper())


def country_label(index: int) -> str:
    iso, name, _, _ = COUNTRIES[index]
    return f"{flag(iso)} {name}"


def find_countries(query: str) -> list[int]:
    """Indexes of countries matching a typed name or ISO code (best matches first)."""
    q = query.strip().lower()
    if not q:
        return []
    exact, starts, contains = [], [], []
    for i, (iso, name, _, _) in enumerate(COUNTRIES):
        n = name.lower()
        base = n.split(" (")[0]
        if q == n or q == base or q == iso.lower():
            exact.append(i)
        elif n.startswith(q):
            starts.append(i)
        elif q in n:
            contains.append(i)
    return exact + starts + contains


def parse_utc_offset(text: str) -> int | None:
    """'UTC+5:30' / '+06:00' / 'GMT-3' -> minutes, else None."""
    m = _UTC_RE.match(text.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    hours, minutes = int(m.group(2)), int(m.group(3) or 0)
    if hours > 14 or minutes > 59:
        return None
    return sign * (hours * 60 + minutes)


def offset_key(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h:02d}:{m:02d}"


def get_tzinfo(key: str, fallback_minutes: int | None = None) -> tzinfo:
    off = parse_utc_offset(key) if key.upper().startswith(("UTC", "GMT", "+", "-")) else None
    if off is not None:
        return timezone(timedelta(minutes=off))
    if ZoneInfo is not None:
        try:
            return ZoneInfo(key)
        except Exception:
            pass
    if fallback_minutes is None:
        for _, _, keys, fb in COUNTRIES:
            if key in keys:
                fallback_minutes = fb
                break
    return timezone(timedelta(minutes=fallback_minutes or 0))


def resolve_zone(iso_index: int) -> tuple[str, tzinfo]:
    """First time-zone name of a country that this system knows."""
    _, _, keys, fb = COUNTRIES[iso_index]
    if ZoneInfo is not None:
        for k in keys:
            try:
                return k, ZoneInfo(k)
            except Exception:
                continue
    return keys[0], timezone(timedelta(minutes=fb))


class TzCtx:
    """A time zone plus the name shown next to times, e.g. 'India Time'."""

    def __init__(self, key: str, name: str, iso: str = ""):
        self.key, self.name, self.iso = key, name, iso
        fb = None
        for _, n, keys, f in COUNTRIES:
            if n == name or key in keys:
                fb = f
                break
        self.tzinfo = get_tzinfo(key, fb)

    @property
    def label(self) -> str:
        return self.name if self.name.upper().startswith(("UTC", "GMT")) else f"{self.name} Time"

    @property
    def flag(self) -> str:
        return flag(self.iso) if self.iso else "🌍"

    def local(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(self.tzinfo)

    def fmt(self, dt: datetime) -> str:
        """'Mon, 09 Feb 2026 - 12:30 PM' (12-hour clock, local time)."""
        d = self.local(dt)
        hour12 = d.hour % 12 or 12
        ampm = "AM" if d.hour < 12 else "PM"
        return f"{_DAYS[d.weekday()]}, {d.day:02d} {_MONTHS[d.month - 1]} {d.year} - {hour12:02d}:{d.minute:02d} {ampm}"

    def clock(self, dt: datetime) -> str:
        """'12:30 PM'"""
        d = self.local(dt)
        return f"{d.hour % 12 or 12:02d}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"

    def now(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(self.tzinfo)


def default_ctx() -> TzCtx:
    idx = next(i for i, c in enumerate(COUNTRIES) if c[1] == DEFAULT_COUNTRY)
    key, _ = resolve_zone(idx)
    return TzCtx(key, DEFAULT_COUNTRY, COUNTRIES[idx][0])


def ctx_for_country(index: int) -> TzCtx:
    key, _ = resolve_zone(index)
    return TzCtx(key, COUNTRIES[index][1], COUNTRIES[index][0])


def ctx_from_text(text: str) -> TzCtx | None:
    """Understand what a user typed: a country name, a zone like Asia/Dhaka, or UTC+5:30."""
    text = text.strip()
    if not text:
        return None
    found = find_countries(text)
    if found:
        return ctx_for_country(found[0])
    off = parse_utc_offset(text)
    if off is not None:
        key = offset_key(off)
        return TzCtx(key, key)
    if "/" in text and ZoneInfo is not None:
        try:
            ZoneInfo(text)
        except Exception:
            return None
        return TzCtx(text, text.split("/")[-1].replace("_", " "))
    return None


# ---------------- the daily statistics reset (04:00 Bangladesh time) ----------------

def stats_day_key(now: datetime | None = None) -> str:
    """The 'statistics day' a moment belongs to. The day starts at 04:00 Bangladesh time."""
    now = now or datetime.now(timezone.utc)
    return (now.astimezone(BD_TZ) - timedelta(hours=RESET_HOUR)).strftime("%Y-%m-%d")


def next_reset(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(timezone.utc)).astimezone(BD_TZ)
    reset = now.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if now >= reset:
        reset += timedelta(days=1)
    return reset


def ago(dt: datetime, now: datetime | None = None) -> str:
    """'just now', '5 minutes ago', '3 hours ago', '2 days ago' ..."""
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (now - dt).total_seconds()
    if secs < 0:
        return "in the future"
    if secs < 60:
        return "just now"
    for unit, size in (("year", 31536000), ("month", 2592000), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = int(secs // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"

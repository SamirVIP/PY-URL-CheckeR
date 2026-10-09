#!/usr/bin/env python3
"""
checker.py - high-speed URL checking engine for Python Url Checker bot
=====================================================================

The bot (bot.py) uses this module in two ways:

1. As a library: read_lines(), count_combinations(), iter_urls() and the
   CheckRun class (which runs inside the bot's event loop).

2. As a program: CheckRun launches several copies of this very file as
   worker subprocesses ("python checker.py --worker"). Each worker
   checks its own share of the URLs, so checking uses ALL your CPU
   cores instead of just one, and the heavy network work never slows
   down the Telegram bot itself.

Why it is fast
--------------
* Several worker processes (one per CPU core, up to 4 by default).
* uvloop (a much faster event loop) is used automatically if installed.
* A fixed pool of workers pulls URLs from a generator, so even 600,000
  combinations never create 600,000 tasks or a giant list in memory.
* Keep-alive connections are reused (the file is requested with a
  "Range: bytes=0-0" header, so no image data is downloaded at all).
* DNS results are cached.

Why nothing is skipped
----------------------
* Network errors, timeouts, HTTP 429 and 5xx answers are never treated
  as "not found". They are retried several times with back-off, and
  anything still failing is re-tried again in extra rounds at the end.
* Whatever still cannot be verified after all that is reported
  explicitly as "unverified" (and listed in a file) - it is never
  silently counted as "not working".
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import random
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import aiohttp

CHECKER_PATH = Path(__file__).resolve()

# "(Word)" placeholder - matched case-insensitively, so (word) / (WORD) work too.
PLACEHOLDER_RE = re.compile(r"\(word\)", re.IGNORECASE)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BASE_HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}
# We only need the status code, so ask for a single byte. Servers that
# support ranges answer 206 and the connection can be reused (fast).
RANGE_HEADERS = {"Range": "bytes=0-0"}

RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})
RANGE_UNSUPPORTED = frozenset({400, 405, 416, 501})

# check outcomes
NO, OK, RETRY, RANGE_BAD = 0, 1, 2, 3


# ============================================================
# Lists + URL generation (shared by the bot and the workers)
# ============================================================

def read_lines(path) -> tuple[list[str], int]:
    """Read a wordlist/linklist file.

    Returns (clean_unique_lines, number_of_lines_skipped).
    Handles UTF-8 BOM, Windows/Mac/Linux line endings, blank lines and
    duplicates (order is preserved). A BOM at the start of a file used
    to silently corrupt the FIRST word - that is handled here.
    """
    p = Path(path)
    if not p.exists():
        return [], 0
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    cleaned = []
    for line in text.splitlines():
        line = line.replace("\ufeff", "").replace("\u200b", "").strip()
        if line:
            cleaned.append(line)
    unique = list(dict.fromkeys(cleaned))
    skipped = len(text.splitlines()) - len(unique)
    return unique, skipped


def quote_word(word: str) -> str:
    """Make a word safe to place inside a URL (letters, digits and _ - . ~
    are left exactly as they are)."""
    return quote(word, safe="-_.~")


def count_combinations(links: list[str], words: list[str]) -> int:
    n = 0
    for link in links:
        n += len(words) if PLACEHOLDER_RE.search(link) else 1
    return n


def iter_urls(links: list[str], words: list[str]):
    """Yield every link x word combination, lazily (no giant list)."""
    qwords = [quote_word(w) for w in words]
    for link in links:
        if PLACEHOLDER_RE.search(link):
            parts = PLACEHOLDER_RE.split(link)
            for qw in qwords:
                yield qw.join(parts)
        else:
            yield link


# ============================================================
# WORKER SIDE (runs inside "python checker.py --worker")
# ============================================================

async def _get(session, url: str, headers):
    """One request. Returns (outcome, status)."""
    async with session.get(url, headers=headers, allow_redirects=True, max_redirects=5) as resp:
        status = resp.status
        ctype = (resp.headers.get("Content-Type") or "").lower()
        clen = resp.headers.get("Content-Length")
        clen_n = int(clen) if clen is not None and clen.isdigit() else None

        if status in (200, 206):
            # Some CDNs answer a missing file with a 200 "not found" web page.
            outcome = NO if ctype.startswith("text/html") else OK
        elif status in RETRY_STATUSES:
            outcome = RETRY
        elif headers is not None and status in RANGE_UNSUPPORTED:
            outcome = RANGE_BAD
        else:
            outcome = NO

        # Read small bodies completely so the keep-alive connection can be
        # reused for the next URL (this is a big part of the speed).
        if outcome == OK:
            drain = clen_n is not None and clen_n <= 65536
        else:
            drain = clen_n is None or clen_n <= 1_000_000
        if drain:
            try:
                await resp.read()
            except Exception:
                pass
        return outcome, status


async def _probe(session, url: str, st) -> int:
    try:
        outcome, status = await _get(session, url, RANGE_HEADERS)
        if outcome == RANGE_BAD:  # server doesn't like Range -> plain GET
            outcome, status = await _get(session, url, None)
            if outcome == RANGE_BAD:
                outcome = NO
        if status == 429:  # server asks us to slow down -> brief global pause
            st.cooldown_until = max(st.cooldown_until, time.monotonic() + 1.0 + random.random())
        return outcome
    except asyncio.CancelledError:
        raise
    except (aiohttp.InvalidURL, aiohttp.TooManyRedirects):
        return NO
    except Exception:
        # timeout, connection reset, DNS hiccup, ... -> NOT "not found"
        return RETRY


async def _check(session, url: str, attempts: int, base_delay: float, st, honor_abort: bool = True) -> int:
    """Check one URL, retrying network-type failures. Returns NO / OK / RETRY.
    RETRY means "still could not verify after all attempts"."""
    for attempt in range(attempts):
        wait = st.cooldown_until - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        outcome = await _probe(session, url, st)
        if outcome != RETRY:
            st.consec = 0
            return outcome
        st.consec += 1
        st.errors += 1
        if honor_abort and st.consec >= st.abort_after:
            st.abort = True
            return RETRY
        if attempt + 1 < attempts:
            await asyncio.sleep(base_delay * (2 ** attempt) * (0.5 + random.random()))
    return RETRY


class _Out:
    """Buffered JSON-lines writer to stdout (parent process reads it)."""

    def __init__(self):
        self.buf: list[str] = []

    def emit(self, obj: dict):
        self.buf.append(json.dumps(obj, separators=(",", ":")))

    def flush(self):
        if not self.buf:
            return
        data = "\n".join(self.buf) + "\n"
        self.buf.clear()
        try:
            sys.stdout.write(data)
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            os._exit(1)  # parent is gone -> stop


def _watch_stdin(loop, ev: asyncio.Event):
    """Parent sends a line (or closes the pipe) to ask us to stop."""
    try:
        sys.stdin.readline()
    except Exception:
        pass
    try:
        loop.call_soon_threadsafe(ev.set)
    except RuntimeError:
        pass


async def _shard(cfg: dict):
    idx, nprocs = cfg["idx"], cfg["nprocs"]
    conc, attempts = cfg["concurrency"], cfg["attempts"]
    out = _Out()
    st = SimpleNamespace(
        processed=0, errors=0, consec=0, abort=False, cooldown_until=0.0,
        abort_after=cfg["abort_after"], phase="main", pending_retry=0,
    )
    # This worker's share: every nprocs-th URL, starting at idx.
    urls = itertools.islice(iter_urls(cfg["links"], cfg["words"]), idx, None, nprocs)
    deferred: list[str] = []

    loop = asyncio.get_running_loop()
    cancel_ev = asyncio.Event()
    threading.Thread(target=_watch_stdin, args=(loop, cancel_ev), daemon=True).start()

    connector = aiohttp.TCPConnector(
        limit=conc, limit_per_host=0, ttl_dns_cache=600, ssl=False, force_close=False,
    )
    timeout = aiohttp.ClientTimeout(total=cfg["total_timeout"], connect=cfg["connect_timeout"])

    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, headers=BASE_HEADERS,
        cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
    ) as session:

        async def first_pass_worker():
            while not st.abort:
                try:
                    url = next(urls)
                except StopIteration:
                    return
                r = await _check(session, url, attempts, 0.25, st)
                st.processed += 1
                if r == OK:
                    out.emit({"t": "f", "u": url})
                elif r == RETRY:
                    deferred.append(url)

        async def retry_round(items: list[str], conc2: int, attempts2: int) -> list[str]:
            it = iter(items)
            still: list[str] = []

            async def w():
                while True:
                    try:
                        url = next(it)
                    except StopIteration:
                        return
                    r = await _check(session, url, attempts2, 0.5, st, honor_abort=False)
                    if r == OK:
                        out.emit({"t": "f", "u": url})
                    if r == RETRY:
                        still.append(url)
                    st.pending_retry -= 1

            await asyncio.gather(*(w() for _ in range(conc2)))
            return still

        async def run_all():
            await asyncio.gather(*(first_pass_worker() for _ in range(conc)))
            if st.abort:
                return
            for rnd in range(cfg["final_rounds"]):
                if not deferred:
                    break
                st.phase = "retry"
                await asyncio.sleep(2.0 * (rnd + 1))  # let the network/server cool down
                items = deferred[:]
                deferred.clear()
                st.pending_retry = len(items)
                still = await retry_round(items, max(10, conc // 5), attempts + 2)
                deferred.extend(still)
                st.pending_retry = len(deferred)

        async def reporter():
            while True:
                pending = len(deferred) if st.phase == "main" else st.pending_retry
                out.emit({"t": "p", "i": idx, "n": st.processed, "r": pending, "e": st.errors, "ph": st.phase})
                out.flush()
                await asyncio.sleep(0.25)

        rep = asyncio.create_task(reporter())
        main_task = asyncio.create_task(run_all())
        cancel_task = asyncio.create_task(cancel_ev.wait())

        cancelled = False
        done, _ = await asyncio.wait({main_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        if main_task not in done:
            cancelled = True
            main_task.cancel()
            await asyncio.gather(main_task, return_exceptions=True)
        else:
            cancel_task.cancel()
            main_task.result()  # re-raise unexpected errors
        rep.cancel()
        await asyncio.gather(rep, cancel_task, return_exceptions=True)

    unverified = list(deferred)
    if st.abort:
        unverified.extend(urls)  # everything not even attempted

    unv_file = None
    if unverified and cfg.get("outdir") and not cancelled:
        unv_file = str(Path(cfg["outdir"]) / f"unverified_{idx}.txt")
        Path(unv_file).write_text("\n".join(unverified) + "\n", encoding="utf-8")

    out.emit({
        "t": "d", "i": idx, "n": st.processed, "e": st.errors,
        "unv": 0 if cancelled else len(unverified), "unv_file": unv_file,
        "abort": st.abort, "cancel": cancelled,
    })
    out.flush()


def _run_loop(coro_factory):
    try:
        import uvloop  # optional, 2x faster event loop
        if hasattr(uvloop, "run"):
            return uvloop.run(coro_factory())
        uvloop.install()
    except ImportError:
        pass
    return asyncio.run(coro_factory())


def worker_main():
    try:
        import resource  # Unix only: allow many open sockets
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = hard if hard != resource.RLIM_INFINITY else 65535
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, min(want, 65535)), hard))
    except Exception:
        pass

    cfg = json.loads(sys.stdin.readline())
    try:
        _run_loop(lambda: _shard(cfg))
    except BaseException as e:  # report instead of dying silently
        try:
            sys.stdout.write(json.dumps({"t": "x", "msg": repr(e)}) + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        os._exit(1)
    # os._exit: a daemon thread may still be blocked reading stdin.
    sys.stdout.flush()
    os._exit(0)


# ============================================================
# PARENT SIDE (used by bot.py)
# ============================================================

class CheckRun:
    """One full check of every link x every word, spread over worker processes."""

    def __init__(
        self,
        links: list[str],
        words: list[str],
        *,
        workdir: str | Path,
        processes: int | None = None,
        concurrency: int = 600,
        connect_timeout: float = 8.0,
        total_timeout: float = 15.0,
        attempts: int = 3,
        final_rounds: int = 3,
        abort_after: int = 1500,
        small_run: int = 5000,
    ):
        self.links, self.words = links, words
        self.total = count_combinations(links, words)
        self.workdir = Path(workdir)
        self.processes = processes or max(1, min(os.cpu_count() or 1, 4))
        self.concurrency = concurrency
        self.opts = dict(
            connect_timeout=connect_timeout, total_timeout=total_timeout,
            attempts=attempts, final_rounds=final_rounds, abort_after=abort_after,
        )
        self.small_run = small_run

        self.found: list[str] = []
        self.shards: dict[int, dict] = {}
        self.unverified_count = 0
        self.unverified_files: list[str] = []
        self.errors: list[str] = []
        self.crashed: list[int] = []
        self.aborted = False
        self.cancelled = False
        self.finished = False
        self.nprocs = 0
        self.started: float | None = None
        self.finished_at: float | None = None

        self._procs: list = []
        self._samples: deque = deque()
        self._on_found = None
        self._kill_timer = None

    # ---------- progress ----------
    @property
    def processed(self) -> int:
        return sum(s["n"] for s in self.shards.values())

    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        return (self.finished_at or time.monotonic()) - self.started

    def snapshot(self) -> dict:
        now = time.monotonic()
        processed = self.processed
        retrying = sum(s["r"] for s in self.shards.values())
        errors = sum(s.get("e", 0) for s in self.shards.values())

        self._samples.append((now, processed))
        while len(self._samples) > 2 and now - self._samples[0][0] > 30:
            self._samples.popleft()
        t0, p0 = self._samples[0]
        if now - t0 >= 2 and processed > p0:
            speed = (processed - p0) / (now - t0)          # recent speed
        else:
            el = self.elapsed()
            speed = processed / el if el > 0 else 0.0     # average so far

        remaining = max(0, self.total - processed)
        eta = remaining / speed if speed > 0 and remaining > 0 else None
        in_retry = processed >= self.total and retrying > 0
        return {
            "total": self.total, "processed": processed, "found": len(self.found),
            "retrying": retrying, "errors": errors, "speed": speed, "eta": eta,
            "elapsed": self.elapsed(), "in_retry": in_retry, "processes": self.nprocs,
        }

    # ---------- control ----------
    def cancel(self):
        """Ask all workers to stop (they flush what they found, then exit)."""
        if self.cancelled or self.finished:
            return
        self.cancelled = True
        self._send_cancel()
        try:
            self._kill_timer = asyncio.get_running_loop().call_later(8, self.shutdown)
        except RuntimeError:
            pass

    def _send_cancel(self):
        for p in self._procs:
            try:
                p.stdin.write(b"cancel\n")
            except Exception:
                pass

    def shutdown(self):
        """Hard-stop every worker right now."""
        for p in self._procs:
            if p.returncode is None:
                try:
                    p.terminate()
                except Exception:
                    pass

    # ---------- running ----------
    async def run(self, on_found=None):
        self._on_found = on_found
        self.started = time.monotonic()
        try:
            if self.total == 0:
                return
            self.nprocs = 1 if self.total < self.small_run else max(1, min(self.processes, self.total))
            per_conc = max(10, self.concurrency // self.nprocs)
            self.workdir.mkdir(parents=True, exist_ok=True)

            for i in range(self.nprocs):
                cfg = dict(self.opts, idx=i, nprocs=self.nprocs, concurrency=per_conc,
                           links=self.links, words=self.words, outdir=str(self.workdir))
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(CHECKER_PATH), "--worker",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    limit=4 * 1024 * 1024,
                )
                self._procs.append(proc)
                self.shards[i] = {"n": 0, "r": 0, "e": 0, "done": False}
                proc.stdin.write((json.dumps(cfg) + "\n").encode("ascii"))
                await proc.stdin.drain()
                if self.cancelled:
                    break

            if self.cancelled:
                self._send_cancel()

            await asyncio.gather(*(self._reader(i, p) for i, p in enumerate(self._procs)))
        finally:
            if self._kill_timer is not None:
                self._kill_timer.cancel()
            for p in self._procs:
                try:
                    if p.stdin and not p.stdin.is_closing():
                        p.stdin.close()
                except Exception:
                    pass
            self.shutdown()
            for p in self._procs:
                try:
                    await asyncio.wait_for(p.wait(), 5)
                except Exception:
                    pass
            self.finished_at = time.monotonic()
            self.finished = True

    async def _reader(self, i: int, proc):
        got_done = False
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if self._handle(i, msg):
                    got_done = True
        except Exception as e:
            self.errors.append(f"reader {i}: {e!r}")
        finally:
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            if not got_done and not self.cancelled:
                self.crashed.append(i)

    def _handle(self, i: int, msg: dict) -> bool:
        t = msg.get("t")
        sh = self.shards[i]
        if t == "f":
            url = msg["u"]
            self.found.append(url)
            if self._on_found:
                try:
                    self._on_found(url)
                except Exception:
                    pass
        elif t == "p":
            sh["n"], sh["r"], sh["e"] = msg["n"], msg["r"], msg.get("e", 0)
        elif t == "d":
            sh.update(n=msg["n"], r=0, e=msg.get("e", 0), done=True)
            self.unverified_count += msg.get("unv", 0)
            if msg.get("unv_file"):
                self.unverified_files.append(msg["unv_file"])
            if msg.get("abort"):
                self.aborted = True
            return True
        elif t == "x":
            self.errors.append(str(msg.get("msg")))
        return False

    def write_unverified(self, dest: str | Path, limit: int = 200_000) -> int:
        """Merge the workers' unverified lists into one file. Returns lines written."""
        written = 0
        with open(dest, "w", encoding="utf-8") as out:
            for f in self.unverified_files:
                try:
                    with open(f, encoding="utf-8") as src:
                        for line in src:
                            if written >= limit:
                                return written
                            out.write(line)
                            written += 1
                except OSError:
                    pass
        return written


if __name__ == "__main__":
    if "--worker" in sys.argv:
        worker_main()
    else:
        print("This file is the checking engine used by bot.py. Run bot.py instead.")

"""Spend guard: reserve the worst case before every API call, settle to the real usage after it.

Because each call reserves ``input + max_tokens`` at the dearest price it could be billed at before it is sent, jobs
running at the same time can never push the day past HEYP_AI_DAILY_USD, and one job can never pass HEYP_AI_REQUEST_USD.

The ledger is a small JSON file under HEYP_STATE_DIR guarded by an exclusive ``flock`` on a sibling lock file, so
every worker process (and every restart) shares one day total. Open reservations are stored too, with an expiry:
if a process dies mid-call, its reservation is counted as spent when it expires (the call may have been billed).
The file holds totals only - no client address, no file name, no content.

Failure policy (fail closed): an unreadable/corrupt ledger is moved aside and the rest of that UTC day is treated
as spent; if a reservation cannot be written, the call is refused.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("heypdf.ai")
EPS = 1e-9
RESERVATION_TTL_S = 2 * 3600       # longer than any AI tool's timeout


class BudgetExceeded(Exception):
    """scope: "request" (per-job cap), "daily" (server-wide day cap) or "ledger" (cannot record spend)."""

    def __init__(self, scope: str, message: str):
        super().__init__(message)
        self.scope = scope
        self.message = message


@dataclass
class Reservation:
    id: str
    amount: float
    settled: bool = False


def _day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


def seconds_to_reset(now: float | None = None) -> int:
    """Seconds until the next 00:00 UTC (when the daily cap resets)."""
    now = time.time() if now is None else now
    day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    nxt = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), dt.timezone.utc)
    return max(1, int(nxt.timestamp() - now))


class Ledger:
    """Server-wide daily spend, persisted with file locking (safe across threads and processes)."""

    def __init__(self, path: str | os.PathLike, daily_usd: float, *, reservation_ttl_s: int = RESERVATION_TTL_S,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.daily_usd = float(daily_usd)
        self.ttl = reservation_ttl_s
        self.clock = clock
        self._thread_lock = threading.Lock()

    # -- storage ----------------------------------------------------------------------------------------------------
    def _fresh(self, now: float) -> dict:
        return {"version": 1, "day": _day(now), "spent_usd": 0.0, "calls": 0, "open": {}}

    def _read(self, now: float) -> dict:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return self._fresh(now)
        try:
            state = json.loads(raw)
            if not (isinstance(state, dict) and isinstance(state.get("day"), str)
                    and isinstance(state.get("spent_usd"), (int, float)) and isinstance(state.get("open"), dict)):
                raise ValueError("unexpected shape")
            for info in state["open"].values():
                float(info["usd"]), float(info["expires"])
            state.setdefault("calls", 0)
            return state
        except (ValueError, TypeError, KeyError) as e:
            # Unknown spend: fail closed for the rest of today and keep the bad file for the operator.
            aside = self.path.with_name(f"{self.path.name}.corrupt-{int(now)}")
            with contextlib.suppress(OSError):
                os.replace(self.path, aside)
            log.error("AI ledger %s was unreadable (%s); moved to %s and today's AI budget is treated as spent",
                      self.path, type(e).__name__, aside.name)
            state = self._fresh(now)
            state["spent_usd"] = self.daily_usd
            return state

    def _write(self, state: dict) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".ai-ledger.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict]:
        """Exclusive access to the current state (rolled to today, expired reservations booked as spent).
        The state is written back when the block exits normally."""
        with self._thread_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                now = self.clock()
                state = self._read(now)
                today = _day(now)
                if state["day"] != today:
                    state.update(day=today, spent_usd=0.0, calls=0)   # in-flight reservations carry over
                for rid, info in list(state["open"].items()):
                    if float(info["expires"]) < now:
                        state["spent_usd"] += float(info["usd"])
                        del state["open"][rid]
                        log.warning("AI ledger: an expired reservation of $%.4f was booked as spent", info["usd"])
                yield state
                self._write(state)
            finally:
                os.close(fd)        # releases the flock

    # -- API --------------------------------------------------------------------------------------------------------
    def reserve(self, amount: float) -> Reservation:
        amount = max(0.0, float(amount))
        try:
            with self._locked() as state:
                reserved = sum(float(i["usd"]) for i in state["open"].values())
                if state["spent_usd"] + reserved + amount > self.daily_usd + EPS:
                    raise BudgetExceeded("daily", "The AI tools have used today's budget on this server; "
                                                  "they reset at 00:00 UTC")
                rid = secrets.token_hex(8)
                state["open"][rid] = {"usd": round(amount, 6), "expires": self.clock() + self.ttl}
        except OSError as e:
            log.error("AI ledger %s cannot be written (%s); refusing AI calls", self.path, type(e).__name__)
            raise BudgetExceeded("ledger", "AI usage cannot be recorded on this server right now") from None
        return Reservation(rid, amount)

    def settle(self, r: Reservation, actual: float) -> None:
        """Replace a reservation by the real cost. Idempotent; a write failure leaves the reservation to expire into
        spent (conservative)."""
        if r.settled:
            return
        r.settled = True
        try:
            with self._locked() as state:
                state["open"].pop(r.id, None)
                state["spent_usd"] = round(state["spent_usd"] + max(0.0, float(actual)), 6)
                state["calls"] = int(state.get("calls", 0)) + 1
        except OSError as e:
            log.error("AI ledger %s cannot be written (%s); the reservation will expire into spent",
                      self.path, type(e).__name__)

    def snapshot(self) -> dict:
        try:
            with self._locked() as state:
                reserved = sum(float(i["usd"]) for i in state["open"].values())
                spent = float(state["spent_usd"])
                day = state["day"]
        except OSError:
            return {"day": _day(self.clock()), "available": False}
        return {"day": day, "spent_usd": round(spent, 4), "reserved_usd": round(reserved, 4),
                "remaining_usd": round(max(0.0, self.daily_usd - spent - reserved), 4),
                "daily_usd": self.daily_usd, "available": True}

    def remaining(self) -> float:
        """USD still available today; raises BudgetExceeded("ledger") when the ledger cannot be read."""
        snap = self.snapshot()
        if not snap.get("available"):
            raise BudgetExceeded("ledger", "AI usage cannot be recorded on this server right now")
        return float(snap["remaining_usd"])

    def job(self, limit_usd: float) -> JobBudget:
        return JobBudget(self, limit_usd)


@dataclass
class JobBudget:
    """Per-job accumulator (thread-safe); every API call of a job reserves and settles through it."""

    ledger: Ledger
    limit_usd: float
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    calls: int = 0
    open: list[Reservation] = field(default_factory=list)
    closed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reserve(self, amount: float) -> Reservation:
        with self._lock:
            if self.closed:
                raise BudgetExceeded("request", "This AI job has already finished")
            if self.spent_usd + self.reserved_usd + amount > self.limit_usd + EPS:
                raise BudgetExceeded("request", f"This job would cost more than the ${self.limit_usd:.2f} "
                                                "per-request AI limit")
            r = self.ledger.reserve(amount)
            self.reserved_usd += r.amount
            self.open.append(r)
            return r

    def settle(self, r: Reservation, actual: float) -> None:
        with self._lock:
            if r.settled or r not in self.open:
                return
            self.ledger.settle(r, actual)
            self.open.remove(r)
            self.reserved_usd = max(0.0, self.reserved_usd - r.amount)
            self.spent_usd += max(0.0, actual)
            self.calls += 1

    def close(self) -> None:
        """Refuse further reservations and settle anything still open at its reserved (worst-case) amount, e.g. a
        call still in flight when the job failed or was cancelled."""
        with self._lock:
            self.closed = True
        for r in list(self.open):
            self.settle(r, r.amount)

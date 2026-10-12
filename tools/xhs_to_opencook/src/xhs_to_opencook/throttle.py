"""Durable per-account access throttle: window allowances, active hours and freeze backoff.

See docs/adr/0001-xhs-access-throttling.md. Counts live in SQLite so they survive restarts;
only the clock decides when an allowance frees up.
"""

import json
import random
import time
from datetime import datetime, timedelta
from uuid import uuid4

from . import runtime
from .errors import TaskError

HOUR = 3600
IDLE_GAP = 300  # Silence longer than this is not active time.
RESET_AFTER = 20  # Sends since the last freeze that reset the backoff ladder.


class AccessThrottle:
    def __init__(
        self,
        store,
        account,
        *,
        access_per_hour=60,
        send_per_hour=40,
        active_hours=6,
        access_gap=(30, 90),
        send_interval=15,
        send_jitter=(0.8, 1.2),
        ramp_days=3,
        backoff_base=7200,
        backoff_cap=86400,
        clock=time.time,
        sleeper=time.sleep,
        rng=random.uniform,
    ):
        self.store, self.account = store, account
        self.access_per_hour, self.send_per_hour = access_per_hour, send_per_hour
        self.active_seconds = active_hours * HOUR
        self.access_gap, self.send_interval = access_gap, send_interval
        self.send_jitter = send_jitter
        self.ramp_seconds = ramp_days * 86400
        self.backoff_base, self.backoff_cap = backoff_base, backoff_cap
        self.clock, self.sleeper, self.rng = clock, sleeper, rng
        with store.db:
            store.db.execute("""CREATE TABLE IF NOT EXISTS throttle_state (
                account TEXT PRIMARY KEY, value TEXT NOT NULL)""")
            store.db.execute("""CREATE TABLE IF NOT EXISTS throttle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, kind TEXT,
                ref TEXT, at REAL, UNIQUE(account, kind, ref))""")
            store.db.execute(
                "INSERT OR IGNORE INTO throttle_state VALUES(?,?)",
                (
                    account,
                    json.dumps(
                        {
                            "started_at": self.clock(),
                            "ramp_from": self.clock(),
                            "frozen_until": 0,
                            "freeze_count": 0,
                            "last_freeze_at": 0,
                            "reason": None,
                            "day": None,
                            "day_active": 0,
                            "last_touch": 0,
                            "next_access_at": 0,
                            "last_sent": None,
                        }
                    ),
                ),
            )

    # -- state ------------------------------------------------------------------------

    def state(self):
        row = self.store.db.execute(
            "SELECT value FROM throttle_state WHERE account=?", (self.account,)
        ).fetchone()
        return json.loads(row[0])

    def save(self, value):
        self.store.db.execute(
            "UPDATE throttle_state SET value=? WHERE account=?", (json.dumps(value), self.account)
        )

    def factor(self, value, now):
        return 0.5 if now - value["ramp_from"] < self.ramp_seconds else 1.0

    def limit(self, kind, value, now):
        full = self.access_per_hour if kind == "access" else self.send_per_hour
        return max(1, int(full * self.factor(value, now)))

    def used(self, kind, now):
        """Event times inside the sliding hour, oldest first."""
        rows = self.store.db.execute(
            "SELECT at FROM throttle_events WHERE account=? AND kind=? AND at>? ORDER BY at",
            (self.account, kind, now - HOUR),
        ).fetchall()
        return [r[0] for r in rows]

    def roll(self, value, now):
        """Start a fresh day's active-time budget once the calendar date changes."""
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if value["day"] != day:
            value.update(day=day, day_active=0, last_touch=0)

    def touch(self, value, now):
        """Credit active time; idle stretches and day rollovers are not carried over."""
        self.roll(value, now)
        if value["last_touch"] and now - value["last_touch"] <= IDLE_GAP:
            value["day_active"] += now - value["last_touch"]
        value["last_touch"] = now

    def midnight(self, now):
        tomorrow = datetime.fromtimestamp(now).date() + timedelta(days=1)
        return datetime.combine(tomorrow, datetime.min.time()).timestamp()

    # -- freeze -----------------------------------------------------------------------

    def freeze(self, reason):
        """Start (or lengthen) a backoff freeze; returns when it ends."""
        with self.store.db:
            value, now = self.state(), self.clock()
            count = value["freeze_count"] + 1
            until = now + min(self.backoff_base * 2 ** (count - 1), self.backoff_cap)
            value.update(
                frozen_until=until,
                freeze_count=count,
                last_freeze_at=now,
                ramp_from=now,
                reason=reason,
            )
            self.save(value)
            return until

    def release(self):
        """Manual early release; keeps the backoff ladder so a repeat still escalates."""
        with self.store.db:
            value = self.state()
            if value["frozen_until"]:
                value.update(frozen_until=0, reason=None)
                self.save(value)

    # -- gating -----------------------------------------------------------------------

    def wait_for(self, kind, value, now):
        """Earliest time the next `kind` action is allowed, given committed state."""
        until = now
        if value["day_active"] >= self.active_seconds * self.factor(value, now):
            until = max(until, self.midnight(now))
        events = self.used(kind, now)
        limit = self.limit(kind, value, now)
        if len(events) >= limit:
            until = max(until, events[len(events) - limit] + HOUR)
        if kind == "access":
            until = max(until, value["next_access_at"])
        else:
            if value["last_sent"] is not None:
                until = max(until, value["last_sent"] + self.send_interval)
            until = max(until, value.get("next_send_at", 0))
        return until

    def ready(self, kind="send"):
        """Wait until a `kind` action ('send' or 'access') is allowed. No server calls."""
        while True:
            with self.store.db:
                value, now = self.state(), self.clock()
                self.roll(value, now)
                if value["frozen_until"] > now:
                    runtime.block(
                        "pacing_frozen",
                        "账号处于限流冻结，退避结束后再试",
                        kind="frozen",
                    )
                until = self.wait_for(kind, value, now)
                if until <= now:
                    self.touch(value, now)
                    self.save(value)
                    return
            self.rest(until)

    def rest(self, until):
        active = runtime.current()
        item = active.data.pop("item", None) if active else None
        try:
            if active:
                active.operation("account_rest", until - self.clock() + 60)
            while self.clock() < until:
                runtime.check()
                self.sleeper(min(1, until - self.clock()))
        finally:
            if active and item:
                active.data["item"] = item
                active.tick()

    def access(self):
        """Wait for and record one page visit (call right before navigating)."""
        self.ready("access")
        with self.store.db:
            value, now = self.state(), self.clock()
            self.store.db.execute(
                "INSERT INTO throttle_events (account,kind,ref,at) VALUES(?,?,?,?)",
                (self.account, "access", uuid4().hex, now),
            )
            gap = self.rng(*self.access_gap) / self.factor(value, now)
            value["next_access_at"] = now + gap
            self.save(value)

    def reserve(self, request):
        """Record the send in the same transaction as its sending intent (idempotent)."""
        value, sent = self.state(), request["sent_at"]
        existing = self.store.db.execute(
            "SELECT 1 FROM throttle_events WHERE account=? AND kind='send' AND ref=?",
            (self.account, request["request_id"]),
        ).fetchone()
        if existing or sent < value["started_at"]:
            return
        if value["frozen_until"] > sent:
            raise TaskError("pacing_frozen", "账号处于限流冻结，禁止发送", retryable=False)
        if value["last_sent"] is not None and sent < value["last_sent"] + self.send_interval:
            raise TaskError("pacing_conflict", "提交间隔不足，禁止发送", retryable=False)
        value["last_sent"] = sent
        # Spread the hourly allowance evenly instead of spending it in one burst.
        spread = HOUR / self.limit("send", value, sent) * self.rng(*self.send_jitter)
        value["next_send_at"] = sent + max(self.send_interval, spread)
        self.store.db.execute(
            "INSERT INTO throttle_events (account,kind,ref,at) VALUES(?,?,?,?)",
            (self.account, "send", request["request_id"], sent),
        )
        since = self.store.db.execute(
            "SELECT COUNT(*) FROM throttle_events WHERE account=? AND kind='send' AND at>?",
            (self.account, value["last_freeze_at"]),
        ).fetchone()[0]
        if value["freeze_count"] and since >= RESET_AFTER:
            value["freeze_count"] = 0
        self.touch(value, sent)
        self.save(value)

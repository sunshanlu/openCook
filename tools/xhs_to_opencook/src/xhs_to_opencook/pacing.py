"""Durable, bounded account experiments. Slots count sending intents, including retries."""

import json
import time

from . import runtime
from .errors import TaskError


class AccountPacing:
    def __init__(
        self,
        store,
        experiment,
        account,
        *,
        interval=15,
        batch_size=50,
        rest_seconds=900,
        batches=10,
        clock=time.time,
        sleeper=time.sleep,
    ):
        self.store, self.experiment, self.account = store, experiment, account
        self.interval, self.batch_size = interval, batch_size
        self.rest_seconds, self.batches = rest_seconds, batches
        self.clock, self.sleeper = clock, sleeper
        with store.db:
            store.db.execute("""CREATE TABLE IF NOT EXISTS pacing_state (
                experiment TEXT, account TEXT, value TEXT NOT NULL,
                PRIMARY KEY(experiment,account))""")
            store.db.execute("""CREATE TABLE IF NOT EXISTS pacing_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, experiment TEXT, account TEXT,
                occurred_at REAL, kind TEXT, request_id TEXT, value TEXT)""")
            store.db.execute("""CREATE TABLE IF NOT EXISTS pacing_slots (
                experiment TEXT, account TEXT, request_id TEXT, sent_at REAL,
                PRIMARY KEY(experiment,account,request_id))""")
            store.db.execute(
                "INSERT OR IGNORE INTO pacing_state VALUES(?,?,?)",
                (
                    experiment,
                    account,
                    json.dumps(
                        {
                            "started_at": self.clock(),
                        "count": 0,
                            "last_sent": None,
                            "rest_until": None,
                            "blocked": False,
                            "interval": interval,
                            "batch_size": batch_size,
                            "rest_seconds": rest_seconds,
                            "batches": batches,
                        }
                    ),
                ),
            )
        settings = self.state()
        if any(
            settings[k] != v
            for k, v in (
                ("interval", interval),
                ("batch_size", batch_size),
                ("rest_seconds", rest_seconds),
                ("batches", batches),
            )
        ):
            raise TaskError(
                "pacing_config_conflict", "试验参数变化，禁止重置已有计数", retryable=False
            )

    def state(self):
        row = self.store.db.execute(
            "SELECT value FROM pacing_state WHERE experiment=? AND account=?",
            (self.experiment, self.account),
        ).fetchone()
        return json.loads(row[0])

    def save(self, value):
        self.store.db.execute(
            "UPDATE pacing_state SET value=? WHERE experiment=? AND account=?",
            (json.dumps(value), self.experiment, self.account),
        )

    def event(self, kind, request=None, **value):
        self.store.db.execute(
            "INSERT INTO pacing_events "
            "(experiment,account,occurred_at,kind,request_id,value) VALUES(?,?,?,?,?,?)",
            (self.experiment, self.account, self.clock(), kind, request, json.dumps(value)),
        )

    def block(self, reason):
        with self.store.db:
            value = self.state()
            value.update(blocked=True, block_reason=reason)
            self.save(value)
            self.event("frozen", reason=reason)

    def release(self):
        with self.store.db:
            value = self.state()
            if value["blocked"]:
                value.update(blocked=False, block_reason=None)
                self.save(value)
                self.event("manual_release")

    def ready(self):
        """Called before navigation AND before submission; no server calls while waiting."""
        while True:
            with self.store.db:
                value = self.state()
                if value["blocked"]:
                    runtime.block("pacing_frozen", "账号已冻结，等待人工确认", kind="manual")
                if value["count"] >= self.batch_size * self.batches:
                    runtime.block(
                        "experiment_complete", "已达到10批提交上限，停止等待评估", kind="manual"
                    )
                now = self.clock()
                if value["count"] and value["count"] % self.batch_size == 0:
                    if value["rest_until"] is None:
                        value["rest_until"] = now + self.rest_seconds
                        self.save(value)
                        self.event("rest_start", count=value["count"], until=value["rest_until"])
                    if now >= value["rest_until"]:
                        # Avoid starting the same rest again before the next submission.
                        value["rest_until"] = 0
                        self.save(value)
                        self.event("rest_end", count=value["count"])
                    rest = value["rest_until"] or now
                else:
                    rest = now
                until = max(rest, (value["last_sent"] or 0) + self.interval)
            if until <= self.clock():
                return
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

    def reserve(self, request):
        """Commit the slot in the same transaction as the sending intent."""
        value = self.state()
        if request["sent_at"] < value.get("started_at", 0):
            return
        existing = self.store.db.execute(
            "SELECT 1 FROM pacing_slots WHERE experiment=? AND account=? AND request_id=?",
            (self.experiment, self.account, request["request_id"]),
        ).fetchone()
        if existing:
            return
        if value["blocked"] or value["count"] >= self.batch_size * self.batches:
            raise TaskError("pacing_frozen", "账号试验不允许新的提交", retryable=False)
        sent = request["sent_at"]
        if value["last_sent"] is not None and sent < value["last_sent"] + self.interval:
            raise TaskError("pacing_conflict", "提交间隔不足，禁止发送", retryable=False)
        if value["rest_until"] and sent < value["rest_until"]:
            raise TaskError("pacing_conflict", "批间休息尚未结束，禁止发送", retryable=False)
        value.update(count=value["count"] + 1, last_sent=sent, rest_until=None)
        self.store.db.execute(
            "INSERT INTO pacing_slots VALUES(?,?,?,?)",
            (self.experiment, self.account, request["request_id"], sent),
        )
        self.save(value)
        self.event(
            "sending_intent",
            request["request_id"],
            count=value["count"],
            batch=(value["count"] - 1) // self.batch_size + 1,
        )

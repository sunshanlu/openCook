"""Framework-driven single-task execution; journals contain facts, never framework objects.

transitions owns the task phase; py_trees owns the wait phase. A tick runs on the browser
owner thread only, advances at most one action and never sleeps. Restart rebuilds the
executor from the durable record and reconciles an unconfirmed submission before sending.
"""

from copy import deepcopy
from threading import RLock, get_ident
from uuid import uuid4

import py_trees
from transitions import Machine

from . import runtime
from .errors import TaskError
from .task_wait import ObserveAnswer

# Documented normal phases plus the terminal manual-review and blocked exits.
PHASES = (
    "preparing",
    "submitting",
    "waiting",
    "validating",
    "saving",
    "completed",
    "manual_review",
    "blocked",
)
TERMINAL_PHASES = frozenset({"completed", "manual_review", "blocked"})
# A reconstructed task in these phases must reconcile the original request before acting.
RECONCILE_PHASES = frozenset({"submitting", "waiting"})


def new_record(identity, *, request=None, attempt=1):
    """Durable task record seeded from explicit task facts, never from a framework object."""
    request = dict(request or {})
    request.setdefault("request_id", uuid4().hex)
    request.setdefault("status", "prepared")
    return {
        "task_id": identity,
        "phase": "preparing",
        "paused": False,
        "attempt": attempt,
        "request": request,
    }


class TaskExecutor:
    """One owner-thread tick advances one task step without sleeping.

    browser supplies submit/reconcile/observe/cancel_observation; journal supplies load/save.
    validate(answer, record) -> result payload; finalize(record) commits atomically and
    idempotently. Both are injected so tests and the real adapter share this seam.
    """

    def __init__(
        self,
        identity,
        browser,
        journal,
        *,
        clock,
        validate=None,
        finalize=None,
        bootstrap=None,
        stable_seconds=1.0,
    ):
        self.identity, self.browser, self.journal, self.clock = identity, browser, journal, clock
        self.validate, self.finalize, self.bootstrap = validate, finalize, bootstrap
        self.stable_seconds = stable_seconds
        self.owner = get_ident()
        self.lock = RLock()
        self.record = journal.load(identity)
        if self.record is None:
            self.record = (bootstrap(identity) if bootstrap else None) or new_record(identity)
        if self.record.get("phase") not in PHASES:
            raise TaskError(
                "task_phase_unknown",
                f"存储中的任务阶段不可识别：{self.record.get('phase')!r}",
                retryable=False,
            )
        self.needs_reconciliation = self.record["phase"] in RECONCILE_PHASES and (
            self.record["phase"] == "waiting"
            or self.record["request"].get("status") != "prepared"
        )
        self.machine = Machine(
            model=self,
            states=list(PHASES),
            initial=self.record["phase"],
            auto_transitions=False,
            queued=True,
            transitions=[
                ["prepared", "preparing", "submitting"],
                ["submitted", "submitting", "waiting"],
                ["answered", "waiting", "validating"],
                ["validated", "validating", "saving"],
                ["saved", "saving", "completed"],
                ["retry", "waiting", "submitting"],
                [
                    "flagged_review",
                    ["preparing", "submitting", "waiting", "validating", "saving"],
                    "manual_review",
                ],
                [
                    "flagged_blocked",
                    ["preparing", "submitting", "waiting", "validating", "saving"],
                    "blocked",
                ],
            ],
        )
        self.tree = py_trees.trees.BehaviourTree(ObserveAnswer(self))

    def snapshot(self):
        with self.lock:
            return deepcopy(self.record)

    def _save(self):
        self.record["phase"] = self.state
        self.record["updated_at"] = self.clock()
        self.journal.save(self.identity, self.record)

    def tick(self):
        if get_ident() != self.owner:
            raise RuntimeError("Task ticks must run on the browser owner thread")
        with self.lock:
            if runtime.current():
                runtime.check()
            if self.record.get("pause_requested") and not self.record["paused"]:
                self._enact_pause()
            if self.record["paused"]:
                return self.snapshot()
            if self.state in TERMINAL_PHASES:
                self._save()
                return self.snapshot()
            if self.needs_reconciliation:
                self._reconcile()
                if self.needs_reconciliation:
                    self._save()
                    return self.snapshot()
            if self.state == "preparing":
                self.prepared()
            elif self.state == "submitting":
                self._submit()
            elif self.state == "waiting":
                self.tree.tick()
                if self.tree.root.status == py_trees.common.Status.SUCCESS:
                    self.answered()
            elif self.state == "validating":
                self._validate()
            elif self.state == "saving":
                self._finalize()
            self._save()
            return self.snapshot()

    def _reconcile(self):
        snapshot = self.browser.reconcile(deepcopy(self.record["request"]))
        if snapshot.get("submitted"):
            self.record["request"]["status"] = "submitted"
            if self.state == "submitting":
                self.submitted()
            self.needs_reconciliation = False
        elif snapshot.get("unsent"):
            # Positively confirmed never sent: safe to send the same stable request identity.
            self.record["request"]["status"] = "prepared"
            self.needs_reconciliation = False
            if self.state == "waiting":
                self.retry()
        # Otherwise the submission remains unknown; stay put and reconcile again next tick.

    def _submit(self):
        self.record["request"]["status"] = "sending"
        self._save()
        snapshot = self.browser.submit(deepcopy(self.record["request"]))
        if snapshot.get("submitted"):
            self.record["request"]["status"] = "submitted"
            self.submitted()
        else:
            self.needs_reconciliation = True

    def _validate(self):
        try:
            result = self.validate(self.record["answer"], self.record)
        except TaskError as exc:
            self.record["error"] = {"code": exc.code, "message": str(exc)}
            if exc.code == "reference_mismatch":
                self.flagged_blocked()
            else:
                self.flagged_review()
            return
        self.record["result"] = result
        self.record.pop("error", None)
        self.validated()

    def _finalize(self):
        if self.finalize is not None:
            self.finalize(self.record)
        self.saved()

    def pause(self):
        """Signal a pause; the tree and browser are only touched by the owner thread."""
        if get_ident() == self.owner:
            with self.lock:
                self._enact_pause()
                self._save()
            return
        with self.lock:
            self.record["pause_requested"] = True
            self._save()

    def _enact_pause(self):
        if self.state == "waiting":
            self.tree.interrupt()
            self.tree.root.stop(py_trees.common.Status.INVALID)
        self.record["paused"] = True
        self.record["pause_requested"] = False

    def resume(self):
        with self.lock:
            self.record["paused"] = False
            self.record["pause_requested"] = False
            self.needs_reconciliation = self.state in RECONCILE_PHASES
            self._save()

    @property
    def phase(self):
        return self.state if isinstance(self.state, str) else self.state.name

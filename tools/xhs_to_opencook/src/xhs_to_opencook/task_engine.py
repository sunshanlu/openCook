"""Framework-driven single-task execution; journals contain facts, never framework objects.

transitions owns the task phase; py_trees owns the wait phase. A tick runs on the browser
owner thread only, advances at most one action and never sleeps. Restart rebuilds the
executor from the durable record and reconciles an unconfirmed submission before sending.
"""

from copy import deepcopy
from threading import RLock, get_ident
from uuid import uuid4

from transitions import Machine

from . import runtime
from .errors import TaskError
from .task_wait import ANSWER_READY, BLOCKED, NEED_RETRY, NEED_REVIEW, WaitPolicy

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
AUTO_BUDGET_SECONDS = 35 * 60.0
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
        empty_stable_seconds=5.0,
    ):
        self.identity, self.browser, self.journal, self.clock = identity, browser, journal, clock
        self.validate, self.finalize, self.bootstrap = validate, finalize, bootstrap
        self.stable_seconds = stable_seconds
        self.empty_stable_seconds = empty_stable_seconds
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
                ["retry", ["waiting", "validating"], "submitting"],
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
        self.wait = WaitPolicy(self)
        self.record.setdefault("budget", {"auto_seconds": 0.0})
        self.budget_sample = self.record["budget"].get("sampled_at", self.clock())

    def snapshot(self):
        with self.lock:
            return deepcopy(self.record)

    def _save(self):
        self.record["phase"] = self.state
        self.record["updated_at"] = self.clock()
        self.record["budget"]["sampled_at"] = self.budget_sample
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
                self.budget_sample = self.clock()
                return self.snapshot()
            if self.state in TERMINAL_PHASES:
                self._save()
                return self.snapshot()
            elapsed = max(0, self.clock() - self.budget_sample)
            self.record["budget"]["auto_seconds"] += elapsed
            self.budget_sample = self.clock()
            if self.record["budget"]["auto_seconds"] >= AUTO_BUDGET_SECONDS:
                if self.state == "waiting":
                    self._apply_wait_result(self.wait.finalize())
                elif self.state not in {"validating", "saving"}:
                    self.record["resume_phase"] = self.state
                    self.flagged_blocked()
                if self.state not in {"validating", "saving"}:
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
                self._apply_wait_result(self.wait.tick())
            elif self.state == "validating":
                self._validate()
            elif self.state == "saving":
                self._finalize()
            self._save()
            return self.snapshot()

    def _reconcile(self):
        snapshot = self.browser.reconcile(deepcopy(self.record["request"]))
        if snapshot.get("request"):
            self.record["request"].update(snapshot["request"])
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
        if snapshot.get("request"):
            self.record["request"].update(snapshot["request"])
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
                self.request_retry()
            return
        self.record["result"] = result
        self.record.pop("error", None)
        self.validated()

    def _finalize(self):
        try:
            if self.finalize is not None:
                self.finalize(self.record)
        except OSError as exc:
            self.record["save_failures"] = self.record.get("save_failures", 0) + 1
            self.record["error"] = {"code": "save_failed", "message": str(exc)}
            if self.record["save_failures"] >= 3:
                self.record["resume_phase"] = "saving"
                self.flagged_blocked()
            return
        self.saved()

    def request_retry(self):
        """Only the task owner creates a new attempt after a completed answer."""
        request = self.record["request"]
        attempt = self.record.get("attempt", 1)
        if (attempt >= request.get("attempt_limit", 2)
                or self.record.get("budget", {}).get("auto_seconds", 0) >= AUTO_BUDGET_SECONDS):
            self.flagged_review()
            return
        self.record.setdefault("attempts", []).append(deepcopy(request))
        following = {k: v for k, v in request.items() if k in {
            "note_id", "stage", "source", "prompt_version", "raw_directory", "attempt_limit",
        }}
        following.update(request_id=uuid4().hex, status="prepared", attempt=attempt + 1)
        self.record.update(request=following, attempt=attempt + 1)
        for key in ("answer", "result", "wait", "answer_candidate", "answer_since", "empty_since"):
            self.record.pop(key, None)
        self.needs_reconciliation = False
        self.retry()

    def _apply_wait_result(self, result):
        if result == ANSWER_READY:
            self.answered()
        elif result == NEED_RETRY:
            self.request_retry()
        elif result == NEED_REVIEW:
            self.flagged_review()
        elif result == BLOCKED:
            self.record["resume_phase"] = self.state
            self.flagged_blocked()

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
            self.wait.interrupt()
        self.record["paused"] = True
        self.record["pause_requested"] = False

    def resume(self):
        with self.lock:
            self.record["paused"] = False
            self.budget_sample = self.clock()
            self.record["pause_requested"] = False
            self.needs_reconciliation = self.state in RECONCILE_PHASES
            self._save()

    @property
    def phase(self):
        return self.state if isinstance(self.state, str) else self.state.name

"""Wait-phase policy for the framework task engine.

#22: a fixed-priority wait policy with durable deadlines, bounded observation, a 35-minute
automatic budget and an explicit business result that is independent of py_trees node status.
The tree never sends a request; the engine (and #21) owns retry with a fresh request identity.

Priority (first match wins, no fall-through to a lower branch):
identity conflict -> page availability -> request association -> complete answer ->
empty/exception -> normal/extended timeout -> final reconcile.
"""

from copy import deepcopy

import py_trees

from .errors import BrowserUnsupported, TaskError

RUNNING = "running"
ANSWER_READY = "answer_ready"
NEED_RETRY = "need_retry"
NEED_REVIEW = "need_review"
BLOCKED = "blocked"

NORMAL_WAIT_SECONDS = 180.0
EXTENDED_WAIT_SECONDS = 600.0
RECOVERY_LIMIT = 6
RECOVERY_BACKOFF_AFTER = 3
RECOVERY_BACKOFF_SECONDS = 300.0
SHORT_BACKOFF_SECONDS = 5.0
RECONCILE_LIMIT = 6


class ObserveAction(py_trees.behaviour.Behaviour):
    """One bounded observation per tick; node status stays RUNNING, never the business result."""

    def __init__(self, executor):
        super().__init__("bounded observation")
        self.executor = executor

    def update(self):
        record = self.executor.record
        try:
            snapshot = self.executor.browser.observe(deepcopy(record["request"]))
            observable = True
        except (TaskError, OSError) as exc:  # preserve access evidence without guessing
            snapshot = {"error": {"type": type(exc).__name__, "message": str(exc),
                                  "code": getattr(exc, "code", None)},
                        "conflict": isinstance(exc, BrowserUnsupported)}
            observable = False
        record["observation"] = {
            "observable": observable, "observed_at": self.executor.clock(), **snapshot,
        }
        return py_trees.common.Status.RUNNING

    def terminate(self, new_status):
        # Local cancellation is not evidence that remote generation stopped.
        if new_status == py_trees.common.Status.INVALID:
            self.executor.browser.cancel_observation()


class WaitPolicy:
    """Fixed-priority wait decisions; a selected branch never falls through to a lower one."""

    def __init__(self, executor):
        self.executor = executor
        self.tree = py_trees.trees.BehaviourTree(ObserveAction(executor))

    def window(self):
        """Durable normal/extended deadlines and counters, set once and preserved on restart."""
        record = self.executor.record
        wait = record.setdefault("wait", {})
        if "started_at" not in wait:
            now = self.executor.clock()
            wait.update(
                started_at=now,
                normal_until=now + NORMAL_WAIT_SECONDS,
                extended_until=now + NORMAL_WAIT_SECONDS + EXTENDED_WAIT_SECONDS,
                recoveries=0,
                reconciles=0,
                final_done=False,
            )
        return wait

    def interrupt(self):
        self.tree.interrupt()
        self.tree.root.stop(py_trees.common.Status.INVALID)

    def tick(self):
        record = self.executor.record
        wait = self.window()
        self.tree.tick()  # one bounded observation; a page change needs a fresh snapshot
        observation = record.get("observation") or {}
        now = self.executor.clock()

        if observation.get("conflict"):
            record["wait_block"] = {"reason": "identity_conflict", "evidence": observation}
            return BLOCKED
        if not observation.get("observable", False):
            return self._page_branch(record, wait, observation, now)
        matches = int(observation.get("matches", 1 if observation.get("matched") else 0))
        if matches > 1 or observation.get("conflict"):
            record["wait_block"] = {"reason": "identity_conflict", "evidence": observation}
            return BLOCKED
        if matches < 1 or not observation.get("matched", False):
            return self._association_branch(record, wait, observation)

        answer = (observation.get("answer") or "").strip()
        if observation.get("finished") and answer:
            return self._stable(record, "answer", answer, self.executor.stable_seconds,
                                ANSWER_READY, now)
        if observation.get("finished") and not answer:
            return self._stable(record, "empty", "", self.executor.empty_stable_seconds,
                                NEED_RETRY, now)
        self._forget(record)
        if observation.get("error"):
            record["wait_partial"] = {"reason": "interrupted_or_error",
                                      "evidence": observation, "at": now}
            if observation.get("finished"):
                return NEED_RETRY
            if now >= wait["extended_until"]:
                return self.finalize(wait)
            return RUNNING
        if now >= wait["extended_until"]:
            return self.finalize(wait)
        return RUNNING

    def finalize(self, wait=None):
        """Final reconcile at the extended boundary or the automatic-budget edge."""
        record = self.executor.record
        wait = wait or self.window()
        wait["final_done"] = True
        self.tree.tick()  # a complete late answer still wins
        observation = record.get("observation") or {}
        answer = (observation.get("answer") or "").strip()
        if (observation.get("observable", True) and observation.get("matched")
                and not observation.get("conflict") and observation.get("finished") and answer):
            return self._stable(record, "answer", answer, self.executor.stable_seconds,
                                ANSWER_READY, self.executor.clock())
        try:
            snapshot = self.executor.browser.reconcile(deepcopy(record["request"]))
        except (TaskError, OSError) as exc:
            snapshot = {"error": {"type": type(exc).__name__, "message": str(exc)}}
        if snapshot.get("unsent"):
            return NEED_RETRY
        if observation.get("observable", True) and observation.get("finished"):
            record["wait_review"] = {"reason": "ended_without_valid_answer",
                                     "evidence": observation}
            return NEED_REVIEW
        record["wait_block"] = {"reason": "final_reconcile_unknown",
                                "evidence": {"observation": observation, "reconcile": snapshot}}
        return BLOCKED

    def _stable(self, record, kind, value, seconds, ready, now):
        candidate_key, since_key = f"{kind}_candidate", f"{kind}_since"
        other = "empty" if kind == "answer" else "answer"
        record.pop(f"{other}_candidate", None)
        record.pop(f"{other}_since", None)
        if record.get(candidate_key) != value:
            record[candidate_key] = value
            record[since_key] = now
            return RUNNING
        if now - record.get(since_key, now) >= seconds:
            if kind == "answer":
                record["answer"] = value
            record.pop(candidate_key, None)
            record.pop(since_key, None)
            return ready
        return RUNNING

    @staticmethod
    def _forget(record):
        for key in ("answer_candidate", "answer_since", "empty_candidate", "empty_since"):
            record.pop(key, None)

    def _page_branch(self, record, wait, observation, now):
        code = (observation.get("error") or {}).get("code")
        if code in {"login_required", "verification_required", "account_mismatch",
                    "platform_restricted", "pacing_frozen"}:
            record["wait_block"] = {"reason": code, "evidence": observation, "scope": "worker"}
            return BLOCKED
        if now < wait.get("backoff_until", 0):
            return RUNNING
        wait["recoveries"] = wait.get("recoveries", 0) + 1
        if wait["recoveries"] > RECOVERY_LIMIT:
            record["wait_block"] = {"reason": "page_unavailable", "evidence": observation}
            return BLOCKED
        wait["backoff_until"] = now + (
            RECOVERY_BACKOFF_SECONDS if wait["recoveries"] == RECOVERY_BACKOFF_AFTER
            else SHORT_BACKOFF_SECONDS
        )
        record["wait_recovery"] = {"attempts": wait["recoveries"], "at": now}
        recover = getattr(self.executor.browser, "recover", None)
        if recover is not None:
            try:
                recover(deepcopy(record["request"]))
            except (TaskError, OSError) as exc:
                record["wait_recovery"]["error"] = str(exc)
        return RUNNING

    def _association_branch(self, record, wait, observation):
        wait["reconciles"] = wait.get("reconciles", 0) + 1
        try:
            snapshot = self.executor.browser.reconcile(deepcopy(record["request"]))
        except (TaskError, OSError) as exc:
            snapshot = {"error": {"type": type(exc).__name__, "message": str(exc)}}
        if snapshot.get("unsent"):
            return NEED_RETRY
        if snapshot.get("submitted"):
            return RUNNING  # reassociated; a fresh snapshot is taken on the next tick
        if wait["reconciles"] >= RECONCILE_LIMIT:
            record["wait_block"] = {"reason": "request_unconfirmed",
                                    "evidence": {"observation": observation,
                                                 "reconcile": snapshot}}
            return BLOCKED
        return RUNNING

"""Wait-phase policy for the framework task engine.

#20 needs only a bounded single observation that accepts a finished, stable answer.
The full observation/recovery/priority tree, budgets and polling cadence are #22; keeping the
policy here lets #22 extend the wait phase without touching the phase machine or the adapters.
"""

from copy import deepcopy

import py_trees


class ObserveAnswer(py_trees.behaviour.Behaviour):
    """Bounded single observation; the answer must be finished and stable before acceptance."""

    def __init__(self, executor):
        super().__init__("Observe current request")
        self.executor = executor

    def update(self):
        executor = self.executor
        record = executor.record
        snapshot = executor.browser.observe(deepcopy(record["request"]))
        if not snapshot.get("matched"):
            record.pop("answer_candidate", None)
            record.pop("answer_since", None)
            return py_trees.common.Status.RUNNING
        answer = (snapshot.get("answer") or "").strip()
        if not (snapshot.get("finished") and answer):
            record.pop("answer_candidate", None)
            record.pop("answer_since", None)
            return py_trees.common.Status.RUNNING
        if answer != record.get("answer_candidate"):
            record["answer_candidate"] = answer
            record["answer_since"] = executor.clock()
            return py_trees.common.Status.RUNNING
        if executor.clock() - record["answer_since"] >= executor.stable_seconds:
            record["answer"] = answer
            record.pop("answer_candidate", None)
            record.pop("answer_since", None)
            return py_trees.common.Status.SUCCESS
        return py_trees.common.Status.RUNNING

    def terminate(self, new_status):
        # Local cancellation is not evidence that remote generation stopped.
        if new_status == py_trees.common.Status.INVALID:
            self.executor.browser.cancel_observation()

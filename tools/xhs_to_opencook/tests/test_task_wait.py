from copy import deepcopy

from xhs_to_opencook import task_wait
from xhs_to_opencook.task_engine import AUTO_BUDGET_SECONDS, TaskExecutor, new_record


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class Journal:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    def load(self, identity):
        return deepcopy(self.rows.get(identity))

    def save(self, identity, record):
        self.rows[identity] = deepcopy(record)


class Browser:
    def __init__(self, snapshot=None):
        if isinstance(snapshot, Exception):
            self.snapshot = snapshot
        else:
            self.snapshot = dict(snapshot or {"matched": True, "finished": False})
        self.reconcile_result = {"submitted": True}
        self.reconciles = []
        self.cancelled = 0

    def observe(self, request):
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return dict(self.snapshot)

    def reconcile(self, request):
        self.reconciles.append(request["request_id"])
        return dict(self.reconcile_result)

    def submit(self, request):
        return {"submitted": True}

    def cancel_observation(self):
        self.cancelled += 1


def waiting_executor(clock=None, browser=None, *, stable=1.0, empty=5.0):
    clock = clock or Clock()
    browser = browser or Browser()
    record = new_record("classify:video", request={
        "request_id": "r1", "note_id": "a" * 24, "stage": "classify",
        "status": "submitted", "attempt": 1,
    })
    record["phase"] = "waiting"
    journal = Journal({"classify:video": record})
    executor = TaskExecutor("classify:video", browser, journal, clock=clock,
                            stable_seconds=stable, empty_stable_seconds=empty)
    return executor, journal, browser, clock


def test_deadlines_are_set_once_and_are_durable():
    executor, journal, browser, clock = waiting_executor()
    executor.tick()  # engine tick persists the durable window
    wait = executor.snapshot()["wait"]
    assert wait["normal_until"] - wait["started_at"] == task_wait.NORMAL_WAIT_SECONDS
    assert wait["extended_until"] - wait["started_at"] == (
        task_wait.NORMAL_WAIT_SECONDS + task_wait.EXTENDED_WAIT_SECONDS
    )
    started = wait["started_at"]
    clock.value += 50
    executor.tick()
    assert executor.snapshot()["wait"]["started_at"] == started
    # Deadlines survive reconstruction; they are never re-issued or extended.
    restored = TaskExecutor("classify:video", Browser(), journal, clock=Clock(1))
    assert restored.snapshot()["wait"]["extended_until"] == wait["extended_until"]


def test_complete_answer_wins_at_the_normal_boundary():
    clock = Clock(0)
    browser = Browser({"matched": True, "finished": True, "answer": "answer"})
    executor, _, _, _ = waiting_executor(clock, browser)
    assert executor.wait.tick() == task_wait.RUNNING  # first sighting marks the candidate
    clock.value = task_wait.NORMAL_WAIT_SECONDS
    assert executor.wait.tick() == task_wait.ANSWER_READY
    assert executor.snapshot()["answer"] == "answer"


def test_late_answer_is_accepted_during_extended_observation():
    clock = Clock(0)
    browser = Browser({"matched": True, "finished": True, "answer": "late"})
    executor, _, _, _ = waiting_executor(clock, browser)
    executor.wait.tick()
    clock.value = task_wait.NORMAL_WAIT_SECONDS + 300
    assert executor.wait.tick() == task_wait.ANSWER_READY


def test_empty_answer_needs_five_stable_seconds():
    clock = Clock(0)
    browser = Browser({"matched": True, "finished": True, "answer": ""})
    executor, _, _, _ = waiting_executor(clock, browser)
    assert executor.wait.tick() == task_wait.RUNNING
    clock.value = 4
    assert executor.wait.tick() == task_wait.RUNNING
    clock.value = 5
    assert executor.wait.tick() == task_wait.NEED_RETRY
    assert "answer" not in executor.snapshot()


def test_interrupted_partial_answer_is_kept_as_evidence_not_a_result():
    browser = Browser({"matched": True, "finished": False, "answer": "partial",
                       "error": True})
    executor, _, _, _ = waiting_executor(Clock(2), browser)
    assert executor.wait.tick() == task_wait.RUNNING
    record = executor.snapshot()
    assert "answer" not in record
    assert record["wait_partial"]["evidence"]["answer"] == "partial"


def test_identity_conflict_blocks_and_never_falls_through_to_the_answer():
    browser = Browser({"matched": True, "matches": 2, "finished": True, "answer": "wrong"})
    executor, _, _, _ = waiting_executor(Clock(1), browser)
    assert executor.wait.tick() == task_wait.BLOCKED
    assert "answer" not in executor.snapshot()


def test_unassociated_request_reconciles_then_blocks_when_unknown():
    clock = Clock(0)
    browser = Browser({"matched": False, "matches": 0})
    executor, _, _, _ = waiting_executor(clock, browser)
    browser.reconcile_result = {"submitted": True}
    assert executor.wait.tick() == task_wait.RUNNING
    assert browser.reconciles == ["r1"]
    browser.reconcile_result = {}  # cannot confirm either way
    for _ in range(task_wait.RECONCILE_LIMIT - 2):
        assert executor.wait.tick() == task_wait.RUNNING
    assert executor.wait.tick() == task_wait.BLOCKED


def test_unassociated_but_confirmed_unsent_needs_retry():
    browser = Browser({"matched": False, "matches": 0})
    browser.reconcile_result = {"submitted": False, "unsent": True}
    executor, _, _, _ = waiting_executor(Clock(0), browser)
    assert executor.wait.tick() == task_wait.NEED_RETRY


def test_extended_boundary_final_reconcile_blocks_unknown_and_reviews_ended():
    clock = Clock(0)
    browser = Browser({"matched": True, "finished": False})
    executor, _, _, _ = waiting_executor(clock, browser)
    executor.wait.tick()  # establish the durable window
    clock.value = task_wait.NORMAL_WAIT_SECONDS + task_wait.EXTENDED_WAIT_SECONDS
    assert executor.wait.tick() == task_wait.BLOCKED
    assert executor.snapshot()["wait_block"]["reason"] == "final_reconcile_unknown"

    # A request that ended without a valid answer goes to review, not blocked.
    blocked = Browser({"matched": True, "finished": True, "answer": ""})
    other, _, _, _ = waiting_executor(Clock(0), blocked)
    assert other.wait.finalize() == task_wait.NEED_REVIEW


def test_extended_boundary_final_reconcile_confirms_unsent_for_retry():
    clock = Clock(0)
    browser = Browser({"matched": True, "finished": False})
    executor, _, _, _ = waiting_executor(clock, browser)
    executor.wait.tick()  # establish the durable window
    browser.reconcile_result = {"submitted": False, "unsent": True}
    clock.value = task_wait.NORMAL_WAIT_SECONDS + task_wait.EXTENDED_WAIT_SECONDS
    assert executor.wait.tick() == task_wait.NEED_RETRY


def test_interrupt_cancels_local_observation_without_claiming_remote_finish():
    executor, _, browser, _ = waiting_executor()
    executor.wait.tick()
    executor.wait.interrupt()
    assert browser.cancelled == 1


def test_unreadable_page_recovers_then_blocks():
    clock = Clock(0)
    browser = Browser(OSError("page gone"))
    executor, _, _, _ = waiting_executor(clock, browser)
    assert executor.wait.tick() == task_wait.RUNNING
    assert executor.snapshot()["wait_recovery"]["attempts"] == 1
    for _ in range(task_wait.RECOVERY_LIMIT):
        clock.value += task_wait.RECOVERY_BACKOFF_SECONDS + 1
        result = executor.wait.tick()
    assert result == task_wait.BLOCKED


def test_engine_maps_conflict_empty_and_answer_to_phases():
    conflict = Browser({"matched": True, "matches": 2, "finished": True, "answer": "x"})
    executor, _, _, _ = waiting_executor(Clock(0), conflict)
    assert executor.tick()["phase"] == "blocked"

    empty = Browser({"matched": True, "finished": True, "answer": ""})
    clock = Clock(0)
    executor, _, _, _ = waiting_executor(clock, empty)
    executor.tick()
    clock.value = 5
    assert executor.tick()["phase"] == "submitting"  # need_retry hands back to #21

    answer = Browser({"matched": True, "finished": True, "answer": "bound"})
    clock = Clock(0)
    executor, _, _, _ = waiting_executor(clock, answer)
    executor.tick()
    clock.value = 1
    assert executor.tick()["phase"] == "validating"


def test_automatic_budget_accumulates_across_phases_and_blocks():
    clock = Clock(1000)
    journal = Journal()
    executor = TaskExecutor("classify:video", Browser(), journal, clock=clock)
    executor.tick()  # preparing -> submitting; first tick seeds the budget clock
    baseline = executor.snapshot()["budget"]["auto_seconds"]
    clock.value += AUTO_BUDGET_SECONDS
    result = executor.tick()
    assert result["phase"] == "blocked"
    assert result["budget"]["auto_seconds"] >= AUTO_BUDGET_SECONDS
    assert result["budget"]["auto_seconds"] > baseline


def test_budget_excludes_manual_pause_and_survives_restart():
    clock = Clock(0)
    journal = Journal()
    executor = TaskExecutor("classify:video", Browser(), journal, clock=clock)
    executor.tick()  # preparing -> submitting, budget clock seeded at 0
    executor.pause()
    clock.value = 5000
    executor.resume()  # paused interval is not billed
    executor.tick()
    assert executor.snapshot()["budget"]["auto_seconds"] < 60
    # A restart keeps the accumulated budget instead of resetting it.
    clock.value = 5100
    restored = TaskExecutor("classify:video", Browser(), journal, clock=clock)
    restored.tick()
    assert restored.snapshot()["budget"]["auto_seconds"] >= 100

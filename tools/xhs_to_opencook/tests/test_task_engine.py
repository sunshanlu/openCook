from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from xhs_to_opencook.errors import TaskError
from xhs_to_opencook.task_engine import TaskExecutor, new_record


class Clock:
    def __init__(self, value=100.0):
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
    def __init__(self):
        self.sent = []
        self.answer = None
        self.reconciled = []
        self.cancelled = 0
        self.reconcile_result = {"submitted": True}

    def submit(self, request):
        self.sent.append(request["request_id"])
        return {"submitted": True}

    def reconcile(self, request):
        self.reconciled.append(request["request_id"])
        return dict(self.reconcile_result)

    def observe(self, request):
        return {"answer": self.answer, "finished": bool(self.answer), "matched": True}

    def cancel_observation(self):
        self.cancelled += 1


def drive_to_waiting(executor, clock, browser, answer=None):
    executor.tick()  # preparing -> submitting
    executor.tick()  # submit -> waiting
    browser.answer = answer
    return executor


def make(clock=None, journal=None, browser=None, **kwargs):
    clock = clock or Clock()
    journal, browser = journal or Journal(), browser or Browser()
    executor = TaskExecutor("classify:video", browser, journal, clock=clock, **kwargs)
    return executor, journal, browser, clock


def test_answer_requires_finished_stable_observation():
    executor, journal, browser, clock = make(stable_seconds=1.0)
    drive_to_waiting(executor, clock, browser)
    browser.answer = "complete answer"
    assert executor.tick()["phase"] == "waiting"  # first sighting only marks the candidate
    assert executor.tick()["phase"] == "waiting"  # 0s stable is not enough
    clock.value += 1
    result = executor.tick()
    assert result["phase"] == "validating"
    assert result["answer"] == "complete answer"
    assert journal.load("classify:video")["answer"] == "complete answer"


def test_reconstruction_reconciles_before_observation_and_never_resends():
    executor, journal, browser, clock = make()
    drive_to_waiting(executor, clock, browser)
    executor.tick()
    restored, _, _, _ = make(clock=Clock(200), journal=journal, browser=browser)
    assert restored.tick()["phase"] == "waiting"
    assert browser.reconciled == browser.sent
    browser.answer = "late complete answer"
    restored.tick()  # first sighting marks the candidate
    restored.clock.value += 1
    assert restored.tick()["answer"] == "late complete answer"
    assert len(browser.sent) == 1


def test_confirmed_unsent_reconcile_resends_the_same_identity():
    journal, browser, clock = Journal(), Browser(), Clock()
    browser.reconcile_result = {"submitted": False, "unsent": True}
    record = new_record("classify:video", request={
        "request_id": "stable", "note_id": "a" * 24, "stage": "classify",
        "status": "sending", "attempt": 1,
    })
    record["phase"] = "submitting"
    journal.rows["classify:video"] = record
    executor, _, _, _ = make(clock=clock, journal=journal, browser=browser)
    state = executor.tick()
    assert state["phase"] == "waiting"
    assert browser.sent == ["stable"]  # confirmed unsent -> resent with the same request identity
    assert browser.reconciled == ["stable"]


def test_unknown_submission_stays_in_submitting_without_resending():
    journal, browser, clock = Journal(), Browser(), Clock()
    browser.reconcile_result = {"submitted": False}
    record = new_record("classify:video", request={
        "request_id": "stable", "note_id": "a" * 24, "stage": "classify",
        "status": "sending", "attempt": 1,
    })
    record["phase"] = "submitting"
    journal.rows["classify:video"] = record
    executor, _, _, _ = make(clock=clock, journal=journal, browser=browser)
    assert executor.tick()["phase"] == "submitting"
    assert executor.tick()["phase"] == "submitting"
    assert browser.sent == []
    assert browser.reconciled == ["stable", "stable"]


def test_pause_from_other_thread_only_signals_then_owner_thread_cancels():
    executor, journal, browser, clock = make()
    drive_to_waiting(executor, clock, browser)
    executor.tick()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(executor.pause).result()
    assert executor.snapshot()["pause_requested"] is True
    assert browser.cancelled == 0  # the browser was never touched off the owner thread
    assert executor.tick()["paused"] is True
    assert browser.cancelled == 1
    executor.resume()
    assert executor.snapshot()["paused"] is False
    assert executor.snapshot()["pause_requested"] is False


def test_pause_on_owner_thread_cancels_observation_immediately():
    executor, journal, browser, clock = make()
    drive_to_waiting(executor, clock, browser)
    executor.tick()
    executor.pause()
    assert browser.cancelled == 1
    assert executor.snapshot()["paused"] is True


def test_unknown_persisted_phase_fails_closed():
    journal = Journal({"classify:video": {"task_id": "classify:video", "phase": "teleported",
                                          "paused": False, "request": {"request_id": "r"}}})
    with pytest.raises(TaskError, match="不可识别"):
        TaskExecutor("classify:video", Browser(), journal, clock=Clock())


def test_validation_then_saving_commits_once_and_completes():
    calls = []

    def validate(answer, record):
        return {"classification": {"note_id": "a" * 24, "verdict": "tutorial", "reason": "ok"}}

    def finalize(record):
        calls.append(record["request"]["request_id"])

    executor, journal, browser, clock = make(validate=validate, finalize=finalize)
    drive_to_waiting(executor, clock, browser, "bound answer")
    executor.tick()  # first sighting marks the candidate
    clock.value += 1
    assert executor.tick()["phase"] == "validating"
    assert executor.tick()["phase"] == "saving"
    assert calls == []  # not yet committed
    result = executor.tick()
    assert result["phase"] == "completed"
    assert calls == [result["request"]["request_id"]]
    assert result["result"]["classification"]["verdict"] == "tutorial"
    # A terminal task advances no further and never resends.
    assert executor.tick()["phase"] == "completed"
    assert len(browser.sent) == 1


def test_reference_conflict_blocks_and_schema_error_reviews():
    def conflict(answer, record):
        raise TaskError("reference_mismatch", "answer belongs to another request")

    executor, journal, browser, clock = make(validate=conflict)
    drive_to_waiting(executor, clock, browser, "other answer")
    executor.tick()
    clock.value += 1
    assert executor.tick()["phase"] == "validating"
    assert executor.tick()["phase"] == "blocked"

    def invalid(answer, record):
        raise TaskError("json_schema_invalid", "bad structure")

    executor2, journal2, browser2, clock2 = make(validate=invalid)
    drive_to_waiting(executor2, clock2, browser2, "bad answer")
    executor2.tick()
    clock2.value += 1
    assert executor2.tick()["phase"] == "validating"
    assert executor2.tick()["phase"] == "submitting"


def test_task_ticks_reject_other_threads_before_browser_access():
    executor = TaskExecutor("classify:video", Browser(), Journal(), clock=Clock())
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(RuntimeError, match="owner thread"):
            pool.submit(executor.tick).result()
    assert executor.snapshot()["phase"] == "preparing"


def test_invalid_answer_retries_with_new_request_and_preserves_task():
    from xhs_to_opencook.errors import TaskError

    journal, browser = Journal(), Browser()
    calls = []

    def validate(answer, record):
        calls.append(record['request']['request_id'])
        if len(calls) == 1:
            raise TaskError('json_schema_invalid', 'invalid')
        return {'ok': True}

    executor = TaskExecutor('task', browser, journal, clock=lambda: 100,
                            validate=validate, stable_seconds=0)
    browser.answer = 'answer'
    for _ in range(12):
        record = executor.tick()
        if record['phase'] == 'completed':
            break
    assert record['phase'] == 'completed'
    assert len(set(browser.sent)) == 2
    assert record['task_id'] == 'task'
    assert len(record['attempts']) == 1


def test_save_failure_retries_without_another_submission():
    saves = []

    def finalize(record):
        saves.append(1)
        if len(saves) < 3:
            raise OSError('temporary database failure')

    executor, journal, browser, clock = make(finalize=finalize, validate=lambda answer, record: {"ok": True})
    drive_to_waiting(executor, clock, browser, 'answer')
    executor.tick()
    clock.value += 1
    for _ in range(6):
        result = executor.tick()
    assert result['phase'] == 'completed'
    assert len(browser.sent) == 1
    assert len(saves) == 3

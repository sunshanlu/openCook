import json

import pytest

from xhs_to_opencook.chat import ChatSession
from xhs_to_opencook.errors import BrowserUnsupported, TaskError
from xhs_to_opencook.models import Source, parse_bound_answer
from xhs_to_opencook.storage import Store


class Transport:
    def __init__(self):
        self.conversation_id = None
        self.new = 0
        self.sent = []
        self.restore = []
        self.replies = []
        self.clock = 1000
        self.idle = True
        self.failure = None

    def new_chat(self):
        self.new += 1
        self.conversation_id = f"chat-{self.new}"

    def restore_chat(self, cid):
        self.restore.append(cid)
        self.conversation_id = cid

    def begin_request(self, prompt, source, request, persist):
        request.update(status="sending", sent_at=self.clock)
        persist(request)
        if self.failure:
            raise self.failure
        request.update(
            status="submitted", conversation_id=self.conversation_id, round_id=request["request_id"]
        )
        persist(request)
        self.sent.append(dict(request))

    def wait_request(self, request, timeout, persist):
        self.clock += timeout
        value = self.replies.pop(0) if self.replies else "valid"
        if isinstance(value, Exception):
            if getattr(value, 'code', None) == 'empty_answer':
                request['empty_answer_evidence'] = {'finished': True, 'empty_placeholder': True}
                persist(request)
            raise value
        if value == "valid":
            value = json.dumps(
                {
                    "note_id": request["note_id"],
                    "request_id": request["request_id"],
                    "verdict": "tutorial",
                    "reason": "有制作过程",
                }
            )
        request.update(status="answered", answer=value)
        persist(request)
        return value

    def request_snapshot(self, request):
        return {
            "matches": 1,
            "conversation_id": self.conversation_id,
            "round_id": request["request_id"],
        }

    def confirm_idle(self):
        return self.idle

    def stop_generation(self):
        return self.idle


def setup(tmp_path, **kwargs):
    store = Store(tmp_path)
    driver = Transport()
    manager = ChatSession(
        store,
        lambda: driver,
        clock=lambda: driver.clock,
        sleeper=lambda seconds: setattr(driver, "clock", driver.clock + seconds),
        **kwargs,
    )
    return store, driver, manager


def source(number):
    nid = f"{number:024x}"
    return Source(
        note_id=nid,
        url=f"https://www.xiaohongshu.com/explore/{nid}",
        author="author",
        title="title",
    )


def process(store, manager, number, stage="classify"):
    directory = store.attempt_directory(source(number).note_id, 1)
    result = manager.process(source(number), stage, directory)
    manager.accept()
    return result


def test_50_51_boundary_and_restart(tmp_path):
    store, driver, manager = setup(tmp_path)
    for i in range(1, 51):
        process(store, manager, i)
    assert driver.new == 1
    assert manager.batches()[0]["status"] == "sealed"
    assert len(manager.batches()[0]["submitted_notes"]) == 50
    process(store, manager, 51)
    assert driver.new == 2
    manager.loaded_batch = None
    process(store, manager, 52)
    assert driver.restore == ["chat-2"]
    assert len(manager.batches()[1]["submitted_notes"]) == 2
    store.close()


def test_failure_and_repair_count_once(tmp_path):
    store, driver, manager = setup(tmp_path)
    driver.replies = ["{}", "valid"]
    process(store, manager, 1)
    assert len(driver.sent) == 2
    assert driver.sent[1]["repair"]
    assert manager.batches()[0]["submitted_notes"] == [source(1).note_id]
    driver.replies = ["{}", "{}", "{}"]
    with pytest.raises(TaskError):
        process(store, manager, 2)
    assert len(manager.batches()[0]["submitted_notes"]) == 2
    assert [s["attempt"] for s in driver.sent[-3:]] == [1, 1, 2]
    assert sum(s["repair"] for s in driver.sent[-3:]) == 1
    store.close()


def test_phase_isolation_and_capacity_one(tmp_path, answer):
    store, driver, manager = setup(tmp_path, capacity=1)
    process(store, manager, 1)
    driver.replies = [
        json.dumps(
            {"note_id": source(1).note_id, "request_id": "WRONG", "extraction": json.loads(answer)}
        )
    ] * 2
    with pytest.raises(TaskError) as error:
        process(store, manager, 1, "extract")
    assert error.value.code == "reference_mismatch"
    assert [b["stage"] for b in manager.batches()] == ["classify", "extract"]
    assert driver.new == 2
    store.close()


def test_late_answer_keeps_same_request_and_chat(tmp_path):
    store, driver, manager = setup(tmp_path)
    driver.replies = [TaskError("answer_timeout", "no answer"), "valid"]
    process(store, manager, 1)
    assert len(driver.sent) == 1 and driver.new == 1
    assert manager.current["cooldown_until"] == 1780
    store.close()


def test_cooldown_retry_then_block_persists(tmp_path):
    store, driver, manager = setup(tmp_path, timeout=1, cooldown=2)
    driver.replies = [TaskError("answer_timeout", "no answer")] * 8
    with pytest.raises(BrowserUnsupported):
        process(store, manager, 1)
    assert len(driver.sent) == 2 and driver.new == 1
    assert driver.sent[1]["silence_retry"]
    assert manager.pending()["status"] == "blocked"
    manager.loaded_batch = None
    with pytest.raises(BrowserUnsupported):
        process(store, manager, 1)
    assert len(driver.sent) == 2 and driver.new == 1
    store.close()


def test_unknown_submission_never_resent(tmp_path):
    store, driver, manager = setup(tmp_path)
    driver.failure = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        process(store, manager, 1)
    manager.loaded_batch = None
    driver.failure = None
    driver.request_snapshot = lambda request: {"matches": 0}
    with pytest.raises(BrowserUnsupported):
        process(store, manager, 1)
    assert driver.new == 1 and not driver.sent
    assert not manager.batches()[0]["submitted_notes"]
    store.close()


def test_resume_validated_answer_does_not_resend(tmp_path):
    store, driver, manager = setup(tmp_path)
    directory = store.attempt_directory(source(1).note_id, 1)
    manager.process(source(1), "classify", directory)
    manager.loaded_batch = None
    process(store, manager, 1)
    assert len(driver.sent) == 1 and driver.restore == ["chat-1"]
    store.close()


def test_explicit_context_limit_rotates_only_with_evidence(tmp_path):
    store, driver, manager = setup(tmp_path)
    original = driver.wait_request

    def length(request, timeout, persist):
        driver.wait_request = original
        request["context_limit_evidence"] = {"text": "上下文长度达到上限"}
        raise TaskError("context_limit", "上下文长度达到上限")

    driver.wait_request = length
    process(store, manager, 1)
    assert driver.new == 2
    assert manager.batches()[0]["sealed_reason"] == "context_limit"
    assert manager.batches()[0]["evidence"]
    store.close()


@pytest.mark.parametrize("size", [0, -1, 1.5, True])
def test_invalid_capacity(tmp_path, size):
    with pytest.raises(ValueError):
        setup(tmp_path, capacity=size)


def test_bound_export_drops_internal_fields(answer, source):
    obj = {"note_id": source.note_id, "request_id": "token", "extraction": json.loads(answer)}
    result = parse_bound_answer(json.dumps(obj), "extract", source.note_id, "token")
    assert set(result.model_dump()) == {"recipes"}


def test_cooling_deadline_survives_restart_without_reset(tmp_path):
    store, driver, manager = setup(tmp_path, timeout=1, cooldown=2)
    driver.replies = [TaskError("answer_timeout", "no answer"), KeyboardInterrupt()]
    # Transport treats BaseException as a reply; force the interruption at observation.
    original = driver.wait_request
    calls = []

    def observe(request, timeout, persist):
        calls.append(timeout)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return original(request, timeout, persist)

    driver.wait_request = observe
    with pytest.raises(KeyboardInterrupt):
        process(store, manager, 1)
    deadline = manager.pending()["cooldown_until"]
    manager.loaded_batch = None
    driver.clock = deadline + 1
    driver.wait_request = original
    driver.replies = ["valid"]
    process(store, manager, 1)
    assert len(driver.sent) == 2 and driver.new == 1
    assert manager.current["silence_retry"]
    store.close()


def test_pre_submission_failure_does_not_count(tmp_path):
    store, driver, manager = setup(tmp_path)
    driver.begin_request = lambda *args: (_ for _ in ()).throw(
        TaskError("reference_mismatch", "cannot attach", retryable=False)
    )
    with pytest.raises(TaskError):
        process(store, manager, 1)
    assert not manager.batches()[0]["submitted_notes"]
    assert manager.pending() is None
    store.close()


def test_finished_empty_answer_retries_without_cooldown(tmp_path):
    store, driver, manager = setup(tmp_path)
    driver.replies = [TaskError('empty_answer', '（暂无内容）'), 'valid']
    process(store, manager, 1)
    requests = store.stage_rows('chat_requests')
    assert len(driver.sent) == 2 and driver.new == 1
    assert not any('cooldown_until' in r for r in requests)
    assert driver.sent[1]['attempt'] == 2
    assert len(manager.batches()[0]['submitted_notes']) == 1
    store.close()


def test_repeated_empty_answer_blocks_across_restart(tmp_path):
    store, driver, manager = setup(tmp_path)
    driver.replies = [TaskError('empty_answer', '（暂无内容）')] * 3
    with pytest.raises(BrowserUnsupported):
        process(store, manager, 1)
    assert manager.pending()['blocked_reason'] == 'empty_answer_retry_exhausted'
    assert not manager.pending().get('cooldown_until')
    manager.loaded_batch = None
    with pytest.raises(BrowserUnsupported):
        process(store, manager, 1)
    assert len(driver.sent) == 2 and driver.new == 1
    store.close()

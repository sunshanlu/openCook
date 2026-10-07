import csv
import json
import sqlite3

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
    kwargs.setdefault('max_attempts', 2)
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


def test_platform_restriction_preserves_second_attempt_for_resume(tmp_path):
    store, driver, manager = setup(tmp_path)
    driver.replies = [
        TaskError("empty_answer", "（暂无内容）"),
        TaskError("platform_restricted", "访问频繁 300013", retryable=False),
    ]
    with pytest.raises(TaskError) as error:
        process(store, manager, 1)
    assert error.value.code == "platform_restricted"
    assert manager.pending()["status"] == "submitted"
    assert manager.pending()["attempt"] == 2
    assert manager.review_rows() == []
    assert len(driver.sent) == 2
    manager.loaded_batch = None
    assert process(store, manager, 1).verdict == "tutorial"
    assert len(driver.sent) == 2
    assert manager.pending() is None
    store.close()


def test_50_51_boundary_and_restart(tmp_path):
    store, driver, manager = setup(tmp_path, capacity=50)
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


def test_five_video_boundary_by_default(tmp_path):
    store, driver, manager = setup(tmp_path)
    for i in range(1, 6):
        process(store, manager, i)
    assert driver.new == 1
    assert manager.batches()[0]["status"] == "sealed"
    process(store, manager, 6)
    assert driver.new == 2
    assert [len(b["submitted_notes"]) for b in manager.batches()] == [5, 1]
    store.close()


def test_old_large_batch_switches_to_five_on_resume(tmp_path):
    store, driver, manager = setup(tmp_path, capacity=50)
    for i in range(1, 8):
        process(store, manager, i)
    manager.capacity = 5
    manager.loaded_batch = None
    process(store, manager, 8)
    assert driver.new == 2
    assert manager.batches()[0]["status"] == "sealed"
    assert manager.batches()[1]["capacity"] == 5
    store.close()


def test_unsent_request_in_old_large_batch_moves_to_new_chat(tmp_path):
    store, driver, manager = setup(tmp_path, capacity=50)
    for i in range(1, 8):
        process(store, manager, i)

    def interrupt_before_send(*args):
        raise KeyboardInterrupt()

    original = driver.begin_request
    driver.begin_request = interrupt_before_send
    with pytest.raises(KeyboardInterrupt):
        process(store, manager, 8)
    request = manager.pending()
    request_id, old_batch = request["request_id"], request["batch_id"]
    assert request["status"] == "prepared"
    driver.begin_request = original
    manager.capacity = 5
    manager.loaded_batch = None
    process(store, manager, 8)
    saved = store.stage_get("chat_requests", request_id)
    assert saved["status"] == "complete" and saved["attempt"] == 1
    assert saved["batch_id"] != old_batch
    assert saved["conversation_id"] == "chat-2"
    assert [len(b["submitted_notes"]) for b in manager.batches()] == [7, 1]
    store.close()


def test_default_throttle_uses_two_seconds(tmp_path):
    store, driver, manager = setup(tmp_path)
    process(store, manager, 1)
    sent_at = driver.sent[-1]["sent_at"]
    driver.clock = sent_at + 0.5
    manager.throttle()
    assert driver.clock == sent_at + 2
    driver.clock += 10
    before = driver.clock
    manager.throttle()
    assert driver.clock == before
    store.close()


@pytest.mark.parametrize("failures", [1, 2])
def test_pre_send_busy_rotates_then_succeeds_or_reviews(tmp_path, failures):
    store, driver, manager = setup(tmp_path)
    original = driver.begin_request
    calls = []

    def begin(prompt, src, request, persist):
        calls.append(request["conversation_id"])
        if len(calls) <= failures:
            raise TaskError("generation_busy", "上一轮生成未确认结束")
        original(prompt, src, request, persist)

    driver.begin_request = begin
    if failures == 1:
        process(store, manager, 1)
        assert len(driver.sent) == 1 and manager.current["attempt"] == 2
    else:
        with pytest.raises(TaskError) as error:
            process(store, manager, 1)
        assert error.value.code == "manual_review_required"
        assert not driver.sent
        assert manager.review_for("classify", source(1).note_id)["attempt"] == 2
        driver.begin_request = original
        process(store, manager, 2)
    assert calls == ["chat-1", "chat-2"]
    assert manager.batches()[0]["sealed_reason"] == "generation_busy"
    assert manager.pending() is None
    store.close()


@pytest.mark.parametrize("becomes_idle", [True, False])
def test_cached_answer_not_idle_observes_before_recovery(tmp_path, becomes_idle):
    store, driver, manager = setup(tmp_path, timeout=1, cooldown=2)
    directory = store.attempt_directory(source(1).note_id, 1)
    manager.process(source(1), "classify", directory)
    request_id = manager.current["request_id"]
    manager.loaded_batch = None
    driver.idle = False
    original_idle = driver.confirm_idle
    driver.confirm_idle = lambda: becomes_idle and driver.clock >= 1002
    original_new = driver.new_chat

    def new_chat():
        original_new()
        driver.confirm_idle = original_idle
        driver.idle = True

    driver.new_chat = new_chat
    process(store, manager, 1)
    if becomes_idle:
        assert len(driver.sent) == 1 and manager.current["request_id"] == request_id
    else:
        assert len(driver.sent) == 2 and manager.current["attempt"] == 2
        assert manager.current["previous_request_id"] == request_id
    assert driver.clock >= 1002
    assert manager.pending() is None
    store.close()


def test_pre_send_busy_retry_survives_restart_without_history_match(tmp_path):
    store, driver, manager = setup(tmp_path)
    original_begin = driver.begin_request
    driver.begin_request = lambda *args: (_ for _ in ()).throw(
        TaskError("generation_busy", "上一轮生成未确认结束")
    )
    original_new = driver.new_chat

    def interrupt_new():
        if driver.new:
            raise KeyboardInterrupt()
        original_new()

    driver.new_chat = interrupt_new
    with pytest.raises(KeyboardInterrupt):
        process(store, manager, 1)
    assert manager.pending()["status"] == "retry_ready"
    assert "sent_at" not in manager.pending()
    driver.new_chat = original_new
    driver.begin_request = original_begin
    driver.request_snapshot = lambda r: pytest.fail("unsent busy request has no history")
    manager.loaded_batch = None
    process(store, manager, 1)
    assert len(driver.sent) == 1 and driver.new == 2
    assert manager.current["attempt"] == 2
    store.close()


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize("interrupt_at", ["database_commit", "raw_write"])
def test_busy_seal_commit_keeps_recovery_state_after_restart(
    tmp_path, monkeypatch, attempt, interrupt_at,
):
    store, driver, manager = setup(tmp_path)
    original_begin = driver.begin_request
    driver.begin_request = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        process(store, manager, 1)
    request = manager.pending()
    request["attempt"] = attempt
    manager.persist(request)
    driver.begin_request = lambda *args: (_ for _ in ()).throw(
        TaskError("generation_busy", "上一轮仍忙碌")
    )
    original_transition = store.seal_chat_request

    def interrupt_transition(*args):
        original_transition(*args)
        raise KeyboardInterrupt()

    if interrupt_at == "database_commit":
        monkeypatch.setattr(store, "seal_chat_request", interrupt_transition)
    else:
        from xhs_to_opencook import chat

        original_write = chat.write_json

        def interrupt_raw(path, value):
            if value.get("status") in {"retry_ready", "manual_review"}:
                raise KeyboardInterrupt()
            original_write(path, value)

        monkeypatch.setattr(chat, "write_json", interrupt_raw)
    with pytest.raises(KeyboardInterrupt):
        process(store, manager, 1)
    request_id, batch_id = request["request_id"], request["batch_id"]
    store.close()
    resumed = Store(tmp_path)
    saved = resumed.stage_get("chat_requests", request_id)
    assert resumed.stage_get("chat_batches", batch_id)["status"] == "sealed"
    assert saved["status"] == ("retry_ready" if attempt == 1 else "manual_review")
    assert saved["retry_new_chat"] and saved["attempt"] == attempt
    monkeypatch.undo()
    driver.begin_request = original_begin
    driver.request_snapshot = lambda r: pytest.fail("busy request was never submitted")
    recovery = ChatSession(resumed, lambda: driver, clock=lambda: driver.clock,
                           sleeper=lambda s: setattr(driver, "clock", driver.clock + s))
    if attempt == 1:
        process(resumed, recovery, 1)
        assert recovery.current["attempt"] == 2
    else:
        with pytest.raises(TaskError, match="等待人工审核"):
            process(resumed, recovery, 1)
        assert not driver.sent
        process(resumed, recovery, 2)
    assert driver.new == 2 and len(driver.sent) == 1
    resumed.close()


def test_busy_transition_rolls_back_both_records_on_database_failure(tmp_path):
    store, driver, manager = setup(tmp_path)
    driver.begin_request = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        process(store, manager, 1)
    request = manager.pending()
    batch = store.stage_get("chat_batches", request["batch_id"])
    batch.update(status="sealed", sealed_reason="generation_busy")
    request.update(status="retry_ready", retry_new_chat=True, retry_reason="generation_busy")
    store.db.set_authorizer(lambda action, table, *args: sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_INSERT and table == "chat_requests"
                            else sqlite3.SQLITE_OK)
    with pytest.raises(sqlite3.DatabaseError):
        store.seal_chat_request(request, batch)
    store.db.set_authorizer(None)
    store.close()
    resumed = Store(tmp_path)
    assert resumed.stage_get("chat_batches", batch["batch_id"])["status"] == "open"
    assert resumed.stage_get("chat_requests", request["request_id"])["status"] == "prepared"
    resumed.close()


def test_cached_answer_cooling_survives_restart_and_exhausts_to_review(tmp_path):
    store, driver, manager = setup(tmp_path, timeout=1, cooldown=2)
    directory = store.attempt_directory(source(1).note_id, 1)
    manager.process(source(1), "classify", directory)
    request = manager.current
    request["attempt"] = 2
    manager.persist(request)
    driver.idle = False
    original_sleeper = manager.sleeper
    manager.sleeper = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        process(store, manager, 1)
    deadline = manager.pending()["cooldown_until"]
    manager.sleeper = original_sleeper
    manager.loaded_batch = None
    driver.clock = deadline + 1
    with pytest.raises(TaskError) as error:
        process(store, manager, 1)
    assert error.value.code == "manual_review_required"
    assert len(driver.sent) == 1 and manager.pending() is None
    driver.idle = True
    process(store, manager, 2)
    assert driver.new == 2
    store.close()


@pytest.mark.parametrize("status,attempt", [
    ("prepared", 3), ("submitted", 3), ("cooling", 3), ("retry_ready", 2),
])
def test_old_pending_over_new_budget_reviews_without_submission(tmp_path, status, attempt):
    store, driver, manager = setup(tmp_path, max_attempts=30)
    directory = store.attempt_directory(source(1).note_id, 1)
    manager.process(source(1), "classify", directory)
    request = manager.current
    request.update(status=status, attempt=attempt)
    manager.persist(request)
    manager.max_attempts = 2
    manager.loaded_batch = None
    with pytest.raises(TaskError) as error:
        process(store, manager, 1)
    assert error.value.code == "manual_review_required"
    assert len(driver.sent) == 1
    assert manager.review_for("classify", source(1).note_id)["attempt"] == attempt
    assert manager.pending() is None
    assert manager.batches()[0]["status"] == "sealed"
    process(store, manager, 2)
    assert driver.new == 2
    store.close()


@pytest.mark.parametrize("invalid,code", [
    ("{", "json_syntax_invalid"), ("{}", "json_schema_invalid"),
])
def test_invalid_second_attempt_does_not_trigger_third_submission(tmp_path, invalid, code):
    store, driver, manager = setup(tmp_path)
    driver.replies = [TaskError("empty_answer", "暂无内容"), invalid]
    with pytest.raises(TaskError, match="等待人工审核"):
        process(store, manager, 1)
    assert len(driver.sent) == 2
    assert not any(r["repair"] for r in driver.sent)
    review = manager.review_for("classify", source(1).note_id)
    assert review["attempt"] == 2
    assert review["review_error"]["code"] == code
    store.close()


def test_csv_review_preserves_quoted_text_and_requires_explicit_release(tmp_path):
    store, driver, manager = setup(tmp_path)
    driver.replies = [TaskError("empty_answer", "暂无内容")] * 2
    with pytest.raises(TaskError, match="等待人工审核"):
        process(store, manager, 1)
    review = manager.review_for("classify", source(1).note_id)
    review["source"]["title"] = '标题,"含引号"\n第二行'
    manager.persist(review)
    output = tmp_path / "output"
    rows = manager.write_reviews(output, {source(1).note_id})
    assert len(rows) == 1
    path = output / "manual_review.csv"
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as file:
        csv_rows = list(csv.DictReader(file))
    assert len(csv_rows[0]) == 12
    assert "失败原因" not in csv_rows[0]
    assert csv_rows[0]["标题"] == review["source"]["title"]
    assert csv_rows[0]["失败次数"] == "2"
    assert csv_rows[0]["错误码"] == "empty_answer"
    csv_rows[0]["人工审核结论"] = "可重试"
    csv_rows[0]["补全备注"] = '已核对,"原视频"\n稍后补全'
    # Existing exports with the old column still retain human annotations.
    legacy_rows = [dict(csv_rows[0], 失败原因="旧版失败原因")]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(legacy_rows[0]))
        writer.writeheader()
        writer.writerows(legacy_rows)
    manager.write_reviews(output, {source(1).note_id})
    with path.open(encoding="utf-8-sig", newline="") as file:
        assert list(csv.DictReader(file)) == csv_rows
    assert (output / "manual_review_links.txt").read_text().strip() == source(1).url
    driver.replies = ["valid"]
    manager.max_attempts = 30
    with pytest.raises(TaskError, match="等待人工审核"):
        process(store, manager, 1)
    assert len(driver.sent) == 2
    manager.release_review("classify", source(1).note_id)
    process(store, manager, 1)
    assert manager.review_for("classify", source(1).note_id) is None
    manager.write_reviews(output, {source(1).note_id})
    with path.open(encoding="utf-8-sig", newline="") as file:
        released = list(csv.DictReader(file))
    assert released[0]["状态"] == "已解除待审核"
    assert released[0]["人工审核结论"] == "可重试"
    assert released[0]["补全备注"] == csv_rows[0]["补全备注"]
    assert (output / "manual_review_links.txt").read_text() == ""
    store.close()


@pytest.mark.parametrize("invalid", ["{", "{}"])
def test_repair_counts_as_attempt_but_each_chat_counts_note_once(tmp_path, invalid):
    store, driver, manager = setup(tmp_path)
    driver.replies = [invalid, "valid"]
    process(store, manager, 1)
    assert len(driver.sent) == 2
    assert driver.sent[1]["repair"]
    assert manager.batches()[0]["submitted_notes"] == [source(1).note_id]
    driver.replies = [invalid] * 3
    with pytest.raises(TaskError):
        process(store, manager, 2)
    assert [len(b["submitted_notes"]) for b in manager.batches()] == [1, 2, 1]
    assert [s["attempt"] for s in driver.sent[-2:]] == [1, 2]
    assert sum(s["repair"] for s in driver.sent[-2:]) == 1
    assert driver.sent[-2]["conversation_id"] != driver.sent[-1]["conversation_id"]
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
    assert error.value.code == "manual_review_required"
    assert [b["stage"] for b in manager.batches()] == ["classify", "extract", "extract"]
    assert driver.new == 3
    store.close()


@pytest.mark.parametrize("stage", ["classify", "extract"])
@pytest.mark.parametrize("wrong_field", ["note_id", "request_id"])
def test_answer_id_mismatch_retries_then_csv_review_and_continues(
    tmp_path, answer, stage, wrong_field,
):
    store, driver, manager = setup(tmp_path)
    original_wait = driver.wait_request

    def mismatched(request, timeout, persist):
        payload = {
            "note_id": request["note_id"], "request_id": request["request_id"],
        }
        if stage == "classify":
            payload.update(verdict="tutorial", reason="有制作过程")
        else:
            payload["extraction"] = json.loads(answer)
        payload[wrong_field] = source(99).note_id if wrong_field == "note_id" else "old-request"
        driver.replies.insert(0, json.dumps(payload))
        return original_wait(request, timeout, persist)

    driver.wait_request = mismatched
    with pytest.raises(TaskError) as error:
        process(store, manager, 1, stage)
    assert error.value.code == "manual_review_required"
    assert [r["attempt"] for r in driver.sent] == [1, 2]
    assert driver.sent[0]["conversation_id"] != driver.sent[1]["conversation_id"]
    assert manager.pending() is None
    output = tmp_path / "output"
    manager.write_reviews(output, {source(1).note_id})
    with (output / "manual_review.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 1 and rows[0]["错误码"] == "reference_mismatch"
    assert rows[0]["失败次数"] == "2"
    with pytest.raises(TaskError, match="等待人工审核"):
        process(store, manager, 1, stage)
    assert len(driver.sent) == 2
    driver.wait_request = original_wait
    process(store, manager, 2)
    assert manager.current["status"] == "complete"
    store.close()


def test_late_answer_keeps_same_request_and_chat(tmp_path):
    store, driver, manager = setup(tmp_path)
    driver.replies = [TaskError("answer_timeout", "no answer"), "valid"]
    process(store, manager, 1)
    assert len(driver.sent) == 1 and driver.new == 1
    assert manager.current["cooldown_until"] == 1780
    store.close()


@pytest.mark.parametrize("invalid", ["{", "{}"])
def test_cooling_observes_after_invalid_json_without_resubmission(tmp_path, invalid):
    store, driver, manager = setup(tmp_path, timeout=1, cooldown=100)
    driver.replies = [TaskError("answer_timeout", "no answer"), invalid, "valid"]
    process(store, manager, 1)
    assert len(driver.sent) == 1 and driver.new == 1
    assert manager.pending() is None
    store.close()


def test_unconfirmed_generation_recovers_in_new_chat(tmp_path):
    store, driver, manager = setup(tmp_path, timeout=1, cooldown=2)
    driver.idle = False
    original_new = driver.new_chat

    def new_chat():
        original_new()
        if driver.new > 1:
            driver.idle = True

    driver.new_chat = new_chat
    driver.replies = [TaskError("answer_timeout", "no answer")] * 2 + ["valid"]
    process(store, manager, 1)
    assert [r["attempt"] for r in driver.sent] == [1, 2]
    assert [r["conversation_id"] for r in driver.sent] == ["chat-1", "chat-2"]
    assert manager.batches()[0]["sealed_reason"] == "generation_not_confirmed_stopped"
    assert manager.pending() is None
    store.close()


def test_old_generation_block_resumes_attempt_seven_after_restart(tmp_path):
    store, driver, manager = setup(tmp_path, max_attempts=30)
    directory = store.attempt_directory(source(1).note_id, 1)
    manager.process(source(1), "classify", directory)
    request = manager.current
    request.update(
        status="blocked", attempt=6, blocked_reason="generation_not_confirmed_stopped",
        cooldown_until=driver.clock - 1,
    )
    request.pop("answer")
    manager.persist(request)
    manager.loaded_batch = None
    driver.idle = False
    driver.replies = [TaskError("answer_timeout", "no answer"), "valid"]
    process(store, manager, 1)
    assert [r["attempt"] for r in driver.sent] == [1, 7]
    retired = store.stage_get("chat_requests", request["request_id"])
    assert retired["status"] == "abandoned"
    assert retired["superseded_by"] == manager.current["request_id"]
    assert manager.current["previous_request_id"] == request["request_id"]
    assert driver.new == 2 and manager.pending() is None
    store.close()


def test_recovery_intent_survives_interrupt_before_new_chat(tmp_path):
    store, driver, manager = setup(tmp_path, timeout=1, cooldown=2)
    driver.idle = False
    driver.replies = [TaskError("answer_timeout", "no answer")] * 2
    original_new = driver.new_chat

    def interrupt_new():
        if driver.new:
            raise KeyboardInterrupt
        original_new()

    driver.new_chat = interrupt_new
    with pytest.raises(KeyboardInterrupt):
        process(store, manager, 1)
    assert manager.pending()["retry_new_chat"]
    manager.loaded_batch = None
    driver.new_chat = original_new
    driver.replies = ["valid"]
    process(store, manager, 1)
    assert driver.new == 2
    assert [r["attempt"] for r in driver.sent] == [1, 2]
    assert manager.pending() is None
    store.close()


def test_exhausted_stuck_chat_is_sealed_before_next_video(tmp_path):
    store, driver, manager = setup(tmp_path, timeout=1, cooldown=2, max_attempts=1)
    driver.idle = False
    driver.replies = [TaskError("answer_timeout", "no answer")] * 2
    with pytest.raises(TaskError, match="已尝试 1 次"):
        process(store, manager, 1)
    assert manager.pending() is None
    assert manager.batches()[0]["status"] == "sealed"
    driver.replies = ["valid"]
    process(store, manager, 2)
    assert driver.new == 2
    store.close()


def test_unknown_history_during_recovery_still_blocks(tmp_path):
    store, driver, manager = setup(tmp_path, timeout=1, cooldown=2)
    driver.idle = False
    driver.replies = [TaskError("answer_timeout", "no answer")] * 2
    driver.request_snapshot = lambda request: {"matches": 0}
    with pytest.raises(BrowserUnsupported, match="历史中无法确认"):
        process(store, manager, 1)
    assert len(driver.sent) == 1 and driver.new == 1
    assert manager.pending()["status"] == "cooling"
    store.close()


def test_confirmed_empty_history_recovers_persisted_submission(tmp_path):
    store, driver, manager = setup(tmp_path)
    directory = store.attempt_directory(source(1).note_id, 1)
    manager.process(source(1), "classify", directory)
    request = manager.current
    request.update(status="blocked", prompt="saved prompt")
    request.pop("answer")
    manager.persist(request)
    manager.loaded_batch = None

    def missing(cid):
        error = TaskError("history_unavailable", "confirmed empty history", retryable=False)
        error.evidence = {"conversation_id": cid, "items": []}
        raise error

    driver.restore_chat = missing
    process(store, manager, 1)
    assert [r["attempt"] for r in driver.sent] == [1, 2]
    assert driver.new == 2
    assert manager.batches()[0]["sealed_reason"] == "history_unavailable"
    assert manager.pending() is None
    store.close()


def test_cooldown_retry_then_exhaustion_continues_to_next_note(tmp_path):
    store, driver, manager = setup(tmp_path, timeout=1, cooldown=2)
    driver.replies = [TaskError("answer_timeout", "no answer")] * 8
    with pytest.raises(TaskError, match="等待人工审核"):
        process(store, manager, 1)
    assert len(driver.sent) == 2 and driver.new == 2
    assert driver.sent[1]["silence_retry"]
    assert manager.pending() is None
    manager.loaded_batch = None
    with pytest.raises(TaskError, match="等待人工审核"):
        process(store, manager, 1)
    assert len(driver.sent) == 2 and driver.new == 2
    driver.replies = ["valid"]
    process(store, manager, 2)
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
    assert len(driver.sent) == 2 and driver.new == 2
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
    assert len(driver.sent) == 2 and driver.new == 2
    assert not any('cooldown_until' in r for r in requests)
    assert driver.sent[1]['attempt'] == 2
    assert len(manager.batches()[0]['submitted_notes']) == 1
    store.close()


def test_repeated_empty_answer_exhaustion_survives_restart(tmp_path):
    store, driver, manager = setup(tmp_path)
    driver.replies = [TaskError('empty_answer', '（暂无内容）')] * 3
    with pytest.raises(TaskError, match="已尝试 2 次"):
        process(store, manager, 1)
    assert manager.pending() is None
    assert store.stage_rows("chat_requests")[-1]["error"]["code"] == "manual_review_required"
    manager.loaded_batch = None
    with pytest.raises(TaskError, match="等待人工审核"):
        process(store, manager, 1)
    assert len(driver.sent) == 2 and driver.new == 2
    store.close()


def test_30_attempts_limit_and_restart_do_not_reset_count(tmp_path):
    store, driver, manager = setup(tmp_path, max_attempts=30)
    driver.replies = [TaskError('empty_answer', '（暂无内容）')] * 31
    with pytest.raises(TaskError, match="已尝试 30 次"):
        process(store, manager, 1)
    assert [r['attempt'] for r in driver.sent] == list(range(1, 31))
    assert store.stage_rows("chat_requests")[-1]["attempt"] == 30
    assert manager.pending() is None
    assert len(manager.batches()[0]['submitted_notes']) == 1
    assert driver.new == 30
    manager.loaded_batch = None
    with pytest.raises(TaskError, match="等待人工审核"):
        process(store, manager, 1)
    assert len(driver.sent) == 30
    store.close()


def test_higher_limit_resumes_old_blocked_request_at_attempt_three(tmp_path):
    store, driver, manager = setup(tmp_path)
    driver.replies = [TaskError('empty_answer', '（暂无内容）')] * 2
    with pytest.raises(TaskError, match="已尝试 2 次"):
        process(store, manager, 1)
    # Persist the old policy's blocked row to exercise migration on restart.
    blocked = store.stage_rows("chat_requests")[-1]
    blocked.update(status="blocked", blocked_reason="empty_answer_retry_exhausted")
    manager.persist(blocked)
    manager.max_attempts = 30
    manager.loaded_batch = None
    driver.replies = [TaskError('empty_answer', '（暂无内容）'), 'valid']
    process(store, manager, 1)
    assert [r['attempt'] for r in driver.sent] == [1, 2, 3]
    assert driver.new == 3
    store.close()


@pytest.mark.parametrize("succeeds", [False, True])
def test_pre_send_reference_failure_rotates_and_preserves_budget(tmp_path, succeeds):
    store, driver, manager = setup(tmp_path)
    original = driver.begin_request
    failures = []

    def begin(*args):
        failures.append(driver.conversation_id)
        if succeeds and len(failures) == 2:
            return original(*args)
        raise TaskError("reference_mismatch", "引用接口返回空列表")

    driver.begin_request = begin
    driver.request_snapshot = lambda r: pytest.fail("Unsent request has no history")
    if succeeds:
        assert process(store, manager, 1).verdict == "tutorial"
        assert manager.current["attempt"] == 2
    else:
        with pytest.raises(TaskError) as error:
            process(store, manager, 1)
        assert error.value.code == "manual_review_required"
        assert manager.review_rows()[0]["attempt"] == 2
        assert manager.review_rows()[0]["review_error"]["code"] == "reference_mismatch"
        resumed = ChatSession(store, lambda: driver)
        with pytest.raises(TaskError, match="等待人工审核"):
            process(store, resumed, 1)
    assert len(failures) == 2 and driver.new == 2
    assert manager.pending() is None
    assert len(driver.sent) == (1 if succeeds else 0)
    store.close()


def test_legacy_pre_submission_failures_do_not_reset_on_restart(tmp_path):
    store, driver, manager = setup(tmp_path)
    process(store, manager, 1)
    batch_id = manager.batches()[0]["batch_id"]
    for index in range(2):
        failed = {"request_id": f"legacy-{index}", "note_id": source(2).note_id,
                  "stage": "classify", "batch_id": batch_id, "status": "failed",
                  "attempt": 1, "error": {"code": "pre_submission_failed"}}
        manager.persist(failed)
    driver.begin_request = lambda *args: pytest.fail("Legacy failures must not reset the budget")
    resumed = ChatSession(store, lambda: driver)
    with pytest.raises(TaskError) as error:
        process(store, resumed, 2)
    assert error.value.code == "manual_review_required"
    assert len(driver.sent) == 1 and resumed.pending() is None
    assert resumed.review_rows()[0]["attempt"] >= 2
    store.close()


def test_legacy_failures_never_discard_a_submitted_request(tmp_path):
    store, driver, manager = setup(tmp_path)
    process(store, manager, 1)
    for index in range(2):
        manager.persist({"request_id": f"legacy-{index}", "note_id": source(2).note_id,
                         "stage": "classify", "batch_id": manager.batches()[0]["batch_id"],
                         "status": "failed", "attempt": 1,
                         "error": {"code": "pre_submission_failed"}})
    driver.failure = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        original = driver.begin_request
        # Reserve a submitted request from a previous version without invoking recovery.
        request = {"request_id": "already-sent", "note_id": source(2).note_id,
                   "stage": "classify", "batch_id": manager.batches()[0]["batch_id"],
                   "status": "prepared", "attempt": 1}
        original("original", source(2), request, manager.persist)
    driver.failure = None
    resumed = ChatSession(store, lambda: driver, clock=lambda: driver.clock)
    assert process(store, resumed, 2).verdict == "tutorial"
    assert resumed.pending() is None and resumed.review_rows() == []
    assert len(driver.sent) == 1
    store.close()

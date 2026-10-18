import json
from types import SimpleNamespace

from xhs_to_opencook.storage import Store
from xhs_to_opencook.workflow import Workflow

P1 = "1" * 24
N1 = "a" * 24
URL = f"https://www.xiaohongshu.com/explore/{N1}"


class Driver:
    """Deterministic transport for the fixture: journals intent, confirms, then answers."""

    def __init__(self, verdict="tutorial"):
        self.conversation_id = "chat"
        self.verdict = verdict
        self.sent = 0
        self.snapshots = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def begin_request(self, prompt, source, request, persist):
        self.sent += 1
        request.update(status="sending", sent_at=1, prompt=prompt)
        persist(request)
        request.update(status="submitted", conversation_id=self.conversation_id,
                       round_id=request["request_id"])
        persist(request)

    def request_snapshot(self, request):
        self.snapshots += 1
        text = json.dumps({
            "note_id": request["note_id"], "request_id": request["request_id"],
            "verdict": self.verdict, "reason": "视频依据",
        }, ensure_ascii=False)
        return {"matches": 1, "conversation_id": self.conversation_id,
                "round_id": request["request_id"], "text": text, "finished": True}

    def confirm_idle(self):
        return True

    def restore_chat(self, conversation_id):
        self.conversation_id = conversation_id

    def new_chat(self):
        pass


def build(tmp_path, driver, *, engine="v2", queue_job=None):
    store = Store(tmp_path / "state")
    if queue_job:
        store.queue_job = queue_job
    store.collect_note(P1, {"note_id": N1, "url": URL, "type": "video", "title": "标题",
                            "author": "作者"})
    args = SimpleNamespace(
        command="classify", output=tmp_path / "output", interval=10, timeout=180,
        non_interactive=True, max_attempts=2, task_engine=engine, task_stable_seconds=0,
        reclassify=False, refresh=False,
    )
    workflow = Workflow(args, store, {P1: "https://www.xiaohongshu.com/user/profile/" + P1},
                        lambda *a, **kw: driver, sleeper=lambda n: None, clock=lambda: 1000.0)
    return workflow, store


def note(store):
    return store.collected([P1])[0]


def test_v2_classification_completes_with_atomic_result_and_enqueue(tmp_path):
    driver = Driver("tutorial")
    workflow, store = build(tmp_path, driver, queue_job="job")
    workflow.process("classify", [note(store)])
    task = workflow.report["tasks"][-1]
    assert task["status"] == "success" and task["verdict"] == "tutorial"
    assert driver.sent == 1
    classification = store.stage_get("classifications", N1)
    assert classification["status"] == "success"
    assert classification["classification"]["verdict"] == "tutorial"
    # Result, completion marker and tutorial enqueue land together.
    assert store.task_get(N1)["phase"] == "completed"
    assert store.queue_counts("job") == {"ready": 1}
    store.close()


def test_v2_saves_raw_answer_before_validation(tmp_path):
    driver = Driver("non_tutorial")
    workflow, store = build(tmp_path, driver)
    workflow.process("classify", [note(store)])
    record = store.task_get(N1)
    assert record["answer"].startswith('{"note_id"')
    assert record["request"]["request_id"] in record["answer"]
    store.close()


def test_v2_does_not_resend_after_a_committed_transaction(tmp_path):
    driver = Driver("tutorial")
    workflow, store = build(tmp_path, driver, queue_job="job")
    answer = json.dumps({"note_id": N1, "request_id": "r1", "verdict": "tutorial",
                         "reason": "视频依据"}, ensure_ascii=False)
    record = {"task_id": N1, "phase": "saving", "paused": False, "attempt": 1,
              "request": {"request_id": "r1", "note_id": N1, "stage": "classify",
                          "status": "submitted", "attempt": 1},
              "answer": answer,
              "result": {"classification": {"note_id": N1, "verdict": "tutorial",
                                            "reason": "视频依据"}}}
    store.task_save(N1, record)
    committed = {"status": "success", "classification": {"verdict": "tutorial"}}
    store.commit_classification(N1, committed, record, enqueue_job="job")
    workflow.process("classify", [note(store)])
    # The transaction already committed, so a rebuild must not send, re-insert or re-enqueue.
    assert driver.sent == 0
    assert store.task_get(N1)["phase"] == "completed"
    assert store.queue_counts("job") == {"ready": 1}
    store.close()


def test_v2_running_twice_is_idempotent_after_success(tmp_path):
    driver = Driver("tutorial")
    workflow, store = build(tmp_path, driver, queue_job="job")
    workflow.process("classify", [note(store)])
    workflow.process("classify", [note(store)])
    assert driver.sent == 1
    assert store.queue_counts("job") == {"ready": 1}
    store.close()


def test_v2_never_migrates_a_legacy_pending_request(tmp_path):
    driver = Driver("tutorial")
    workflow, store = build(tmp_path, driver)
    store.stage_save("chat_requests", "legacy", {
        "request_id": "legacy", "stage": "classify", "note_id": N1, "batch_id": "b",
        "status": "submitted", "attempt": 1,
    })
    workflow.process("classify", [note(store)])
    task = workflow.report["tasks"][-1]
    assert task["status"] == "skipped" and task["reason"] == "legacy_pending_not_migrated"
    assert driver.sent == 0
    assert store.task_get(N1) is None
    assert store.stage_get("chat_requests", "legacy")["status"] == "submitted"
    store.close()


def test_legacy_engine_is_unaffected_by_the_opt_in(tmp_path):
    driver = Driver("tutorial")
    workflow, store = build(tmp_path, driver, engine="legacy")
    assert workflow.task_engine == "legacy"
    assert driver.sent == 0
    store.close()


def test_corrupt_task_journal_never_becomes_a_new_submission(tmp_path):
    import pytest

    from xhs_to_opencook.errors import TaskError

    driver = Driver()
    workflow, store = build(tmp_path, driver)
    store.db.execute('INSERT INTO import_tasks VALUES (?, ?)', (N1, 'broken json'))
    store.db.commit()
    with pytest.raises(TaskError, match='日志损坏'):
        workflow.process('classify', [note(store)])
    assert driver.sent == 0
    store.close()


def test_other_video_pending_stops_new_task(tmp_path):
    import pytest

    from xhs_to_opencook.errors import TaskError

    driver = Driver()
    workflow, store = build(tmp_path, driver)
    store.stage_save('chat_requests', 'other', {
        'request_id': 'other', 'stage': 'classify', 'note_id': 'b'*24,
        'status': 'submitted', 'attempt': 1,
    })
    with pytest.raises(TaskError, match='原视频'):
        workflow.process('classify', [note(store)])
    assert driver.sent == 0
    store.close()


def test_runtime_pause_persists_task_and_projects_single_budget(tmp_path, monkeypatch):
    import pytest

    from xhs_to_opencook import runtime
    from xhs_to_opencook.storage import write_json
    from xhs_to_opencook.task_engine import TaskExecutor, new_record
    from xhs_to_opencook.task_adapters import StoreJournal

    driver = Driver()
    workflow, store = build(tmp_path, driver)
    active = runtime.WorkerRuntime(tmp_path/'runtime', 'job', clock=lambda: 1000)
    monkeypatch.setattr(runtime, '_worker', active)
    record = new_record(N1, request={'note_id': N1, 'stage': 'classify'})
    executor = TaskExecutor(N1, driver, StoreJournal(store), clock=lambda: 1000,
                            bootstrap=lambda key: record)
    write_json(active.directory/'control.json', {'job_id': 'job', 'action': 'pause'})
    with pytest.raises(KeyboardInterrupt):
        executor.tick()
    assert store.task_get(N1)['paused'] is True
    assert active.data['task_budget_owned'] is True
    assert active.data['deadline'] == 1120
    assert active.items['budgets']['classify:'+N1] == 0
    store.close()

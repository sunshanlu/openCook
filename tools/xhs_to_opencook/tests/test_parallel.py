import json
from types import SimpleNamespace

import pytest

from xhs_to_opencook.chat import ChatSession
from xhs_to_opencook.errors import BrowserUnsupported
from xhs_to_opencook.parallel import ParallelCoordinator, initialize
from xhs_to_opencook.storage import Store


def test_classification_and_enqueue_commit_together(tmp_path):
    producer, reader = Store(tmp_path), Store(tmp_path)
    producer.queue_job, producer.queue_notes = "job", {"a", "b"}
    producer.stage_save(
        "classifications",
        "a",
        {
            "status": "success",
            "classification": {"verdict": "tutorial"},
        },
    )
    assert reader.claim("job", "extract") == "a"
    # Same classification commit cannot enqueue a duplicate.
    producer.stage_save(
        "classifications",
        "a",
        {
            "status": "success",
            "classification": {"verdict": "tutorial"},
        },
    )
    producer.stage_save(
        "classifications",
        "b",
        {
            "status": "success",
            "classification": {"verdict": "unknown"},
        },
    )
    assert reader.queue_counts("job") == {"claimed": 1}
    producer.close()
    reader.close()


def test_claim_survives_restart_and_fifo(tmp_path):
    store = Store(tmp_path)
    store.seed_queue("job", ["a", "b", "a"])
    assert store.claim("job", "old") == "a"
    store.close()
    store = Store(tmp_path)
    store.seed_queue("job", ["c"])
    assert store.claim("job", "new") == "a"
    store.finish_queue("job", "a", "review")
    assert store.claim("job", "new") == "b"
    store.finish_queue("job", "b", "done")
    assert store.claim("job", "new") == "c"
    store.finish_queue("job", "c", "skipped")
    assert store.claim("job", "new") is None
    assert store.queue_counts("job") == {"done": 1, "review": 1, "skipped": 1}
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    store.close()


def test_enqueue_rollback_on_insert_failure(tmp_path):
    store = Store(tmp_path)
    store.queue_job, store.queue_notes = "job", {"a"}
    store.db.execute("""CREATE TRIGGER deny_queue BEFORE INSERT ON extraction_queue
                      BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(Exception, match="injected"):
        store.stage_save(
            "classifications",
            "a",
            {
                "status": "success",
                "classification": {"verdict": "tutorial"},
            },
        )
    store.db.rollback()
    assert store.stage_get("classifications", "a") is None
    store.close()


def test_scoped_pending_and_interval(tmp_path):
    store = Store(tmp_path)
    for stage, sent in [("classify", 95), ("extract", 99)]:
        store.stage_save(
            "chat_requests",
            stage,
            {
                "request_id": stage,
                "stage": stage,
                "status": "submitted",
                "sent_at": sent,
            },
        )
    sleeps = []
    session = ChatSession(
        store, None, stage_scope="classify", interval=10, clock=lambda: 100, sleeper=sleeps.append
    )
    assert session.pending()["stage"] == "classify"
    session.throttle()
    assert sleeps == [5]
    assert len(session.pending_rows()) == 2
    with pytest.raises(BrowserUnsupported):
        ChatSession(store, None).pending()
    store.close()


@pytest.fixture
def coordinator(tmp_path):
    profile = "1" * 24
    args = SimpleNamespace(
        command="pipeline",
        profiles=tmp_path / "profiles",
        state_dir=tmp_path / "state",
        output=tmp_path / "output",
        interval=2,
        timeout=180,
        non_interactive=True,
        skip_collect=True,
        execution="parallel",
    )
    args.profiles.write_text(f"https://www.xiaohongshu.com/user/profile/{profile}")
    store = Store(args.state_dir)
    store.stage_save("profiles", profile, {"status": "complete"})
    store.close()
    directory = args.state_dir / "supervision"
    from xhs_to_opencook.accounts import save_binding
    save_binding(args.state_dir, "classify", "a" * 24)
    save_binding(args.state_dir, "extract", "b" * 24)
    initialize(args, directory)
    c = ParallelCoordinator(directory, clock=lambda: 1000)
    c.outbox.flush = lambda **kw: None
    c.notify = lambda *a, **kw: None
    yield c
    c.store.close()


def test_publish_first_and_every_twenty(coordinator):
    c = coordinator
    count, bundles = [0], []
    c.publisher.snapshot = lambda **kw: (
        bundles.append(count[0]) if kw.get("bundle") else [{} for _ in range(count[0])]
    )
    c.publish()
    assert bundles == []
    count[0], c.next_export = 1, 0
    c.publish()
    assert bundles == [1]
    count[0], c.next_export = 20, 0
    c.publish()
    assert bundles == [1]
    count[0], c.next_export = 21, 0
    c.publish()
    assert bundles == [1, 21]
    c.publish(force=True)
    assert bundles == [1, 21, 21]


def test_export_failure_does_not_advance_checkpoint(coordinator):
    c = coordinator

    def fail(**kw):
        if kw.get("bundle"):
            raise OSError("disk failure")
        return [{}]

    c.publisher.snapshot = fail
    c.publish()
    assert c.job.get("published_success", 0) == 0
    assert c.job["export_error"] == "disk failure"
    c.publisher.snapshot = lambda **kw: [{}]
    c.publish(force=True)
    assert c.job["published_success"] == 1
    assert "export_error" not in c.job


def test_global_wait_broadcasts_and_preserves_terminal(coordinator):
    c = coordinator
    c.job["workers"]["classify"]["status"] = "completed"
    observed = []
    c.stop_workers = lambda: observed.append(
        json.loads((c.directory / "control.json").read_text())["action"]
    )
    c.publish = lambda **kw: None
    c.wait("extract", {"reason": "platform_restricted", "wait_kind": "platform"})
    assert observed == ["pause"]
    assert c.job["next_check"] == 1300
    assert c.job["workers"]["classify"]["status"] == "completed"
    assert c.job["workers"]["extract"]["status"] == "ready"


def test_manual_wait_never_reopens_chrome(coordinator):
    c = coordinator
    c.job.update(status="waiting", next_check=None)
    c.publish = lambda **kw: None
    c.chrome_factory = lambda *a: pytest.fail("manual wait must not relaunch Chrome")
    assert c.step()


def test_local_crash_only_retires_failed_stage(coordinator):
    c = coordinator
    retired = []
    c.retire = retired.append
    c.job["workers"]["extract"]["status"] = "running"
    c.crash("classify", "killed")
    assert retired == ["classify"]
    assert c.job["workers"]["extract"]["status"] == "running"
    assert c.job["workers"]["classify"]["status"] == "ready"
    for _ in range(3):
        c.crash("classify", "killed")
    assert c.job["workers"]["classify"]["status"] == "blocked"
    assert c.job["workers"]["extract"]["status"] == "running"


def test_pause_broadcast_is_observed_before_retire(coordinator):
    from xhs_to_opencook.storage import write_json

    c = coordinator
    write_json(c.directory / "control.json", {"job_id": c.job["job_id"], "action": "pause"})
    retired = []
    c.retire = retired.append
    c.publish = lambda **kw: None
    assert c.step()
    assert retired == ["classify", "extract"]
    assert c.job["status"] == "paused"


def test_complete_requires_both_workers(coordinator):
    c = coordinator
    c.ensure_chrome = lambda stage: True
    c.publish = lambda **kw: None
    c.publisher.report["workflow_status"] = "complete"
    c.job["workers"]["classify"]["status"] = "completed"
    c.job["workers"]["extract"]["status"] = "blocked"
    assert c.step()
    assert c.job["status"] == "running"
    c.job["workers"]["extract"]["status"] = "completed"
    c.job["status"] = "running"
    assert c.step()
    assert c.job["status"] == "completed"


def test_migration_keeps_budgets_and_stage_failure_windows(coordinator):
    from xhs_to_opencook.storage import write_json

    c = coordinator
    write_json(
        c.directory / "items.json",
        {
            "budgets": {"classify:a": 100, "extract:b": 50},
            "seen": ["classify:a", "extract:b"],
            "outcomes": [{"key": "classify:a", "code": "empty"}],
        },
    )
    initialize(c.args, c.directory, job=c.job)
    classify = json.loads((c.directory / "classify/items.json").read_text())
    extract = json.loads((c.directory / "extract/items.json").read_text())
    assert classify["budgets"] == {"classify:a": 100}
    assert extract["budgets"] == {"extract:b": 50}
    assert classify["outcomes"] == [{"key": "classify:a", "code": "empty"}]
    assert extract["outcomes"] == []


def test_launch_records_identity_after_exec(coordinator):
    import time

    from xhs_to_opencook.supervisor import owned_process

    c = coordinator
    c.chromes["classify"] = SimpleNamespace(ensure_window=lambda stage: None)
    # Keep the actual child alive while testing post-exec identity stability.
    import subprocess

    real_popen = subprocess.Popen
    c.spawn = lambda cmd, **kw: real_popen([cmd[0], "-c", "import time;time.sleep(60)"], **kw)
    c.launch("classify")
    try:
        assert owned_process(c.job["workers"]["classify"])
        time.sleep(0.2)
        assert owned_process(c.job["workers"]["classify"])
    finally:
        c.retire("classify")


def test_empty_old_batch_without_pending_rotates_safely(tmp_path):
    from xhs_to_opencook.errors import TaskError
    store = Store(tmp_path)
    store.stage_save('chat_batches', 'old', {'batch_id': 'old', 'stage': 'extract',
                     'status': 'open', 'capacity': 5, 'submitted_notes': [],
                     'conversation_id': 'empty-history'})
    class Driver:
        conversation_id = None
        def restore_chat(self, cid):
            raise TaskError('history_unavailable', 'empty history')
        def new_chat(self):
            pass
    chat = ChatSession(store, lambda: Driver(), stage_scope='extract')
    batch = chat.batch('extract', 'note')
    assert batch['batch_id'] != 'old'
    assert store.stage_get('chat_batches', 'old')['status'] == 'sealed'
    store.close()


def test_empty_history_with_pending_cannot_rotate(tmp_path):
    from xhs_to_opencook.errors import TaskError
    store = Store(tmp_path)
    store.stage_save('chat_batches', 'old', {'batch_id': 'old', 'stage': 'extract',
                     'status': 'open', 'capacity': 5, 'submitted_notes': ['note'],
                     'conversation_id': 'empty-history'})
    store.stage_save('chat_requests', 'pending', {'request_id': 'pending', 'stage': 'extract',
                     'batch_id': 'old', 'note_id': 'note', 'status': 'submitted'})
    class Driver:
        def restore_chat(self, cid):
            raise TaskError('history_unavailable', 'empty history')
        def new_chat(self):
            pytest.fail('unresolved request must not open another conversation')
    chat = ChatSession(store, lambda: Driver(), stage_scope='extract')
    with pytest.raises(TaskError, match='empty history'):
        chat.batch('extract', 'other-note')
    assert store.stage_get('chat_batches', 'old')['status'] == 'open'
    assert len(chat.batches()) == 1
    store.close()


def test_account_wait_only_retires_affected_stage(coordinator):
    c = coordinator
    retired = []
    c.retire = retired.append
    c.publish = lambda **kw: None
    c.job['workers']['classify']['status'] = 'running'
    c.account_wait('extract', {'reason': 'platform_restricted', 'wait_kind': 'platform'})
    assert retired == ['extract']
    assert c.job['workers']['extract']['status'] == 'waiting'
    frozen = c.throttle('extract').state()
    assert frozen['freeze_count'] == 1
    assert c.job['workers']['extract']['next_check'] == frozen['frozen_until']
    assert frozen['frozen_until'] - frozen['last_freeze_at'] == 7200
    assert c.job['workers']['classify']['status'] == 'running'


def test_targeted_resume_does_not_restart_other_stage(coordinator):
    from xhs_to_opencook.storage import write_json
    c = coordinator
    c.job['workers']['extract']['status'] = 'waiting'
    c.job['workers']['classify']['status'] = 'blocked'
    c.ensure_chrome = lambda stage: False
    c.publish = lambda **kw: None
    write_json(c.directory / 'control.json', {'job_id': c.job['job_id'], 'action': 'resume',
                                            'worker': 'extract'})
    c.step()
    assert c.job['workers']['extract']['status'] == 'ready'
    assert c.job['workers']['classify']['status'] == 'blocked'


def test_dead_account_chrome_restarts_only_its_worker(coordinator, monkeypatch):
    c = coordinator
    c.retire = lambda stage: None
    c.playwright = object()
    old = SimpleNamespace(alive=lambda: False, close=lambda: None)
    c.chromes['extract'] = old
    c.chromes['classify'] = SimpleNamespace(alive=lambda: True)
    c.job['workers']['extract']['status'] = 'running'
    c.chrome_factory = lambda *a, **kw: SimpleNamespace(open=lambda: SimpleNamespace())
    assert c.ensure_chrome('extract')
    assert c.job['workers']['extract']['status'] == 'ready'
    assert c.chromes['classify'].alive()


def test_job_next_check_follows_earliest_waiting_stage_and_never_goes_stale(coordinator):
    import time as _time

    c = coordinator
    c.job["next_check_label"] = "2026-10-08 10:54:40"  # Left over from an older job.
    c.job["workers"]["classify"].update(status="running", next_check=None)
    c.job["workers"]["extract"].update(status="waiting", next_check=1_800_000_000.0)
    c.save()
    assert c.job["next_check_label"] == _time.strftime(
        "%Y-%m-%d %H:%M:%S", _time.localtime(1_800_000_000.0)
    )
    c.job["workers"]["extract"].update(status="running", next_check=None)
    c.save()
    assert c.job["next_check_label"] == "按各阶段状态继续"


def test_job_level_wait_time_is_shown_only_while_the_job_is_waiting(coordinator):
    import time as _time

    c = coordinator
    for record in c.job["workers"].values():
        record.update(status="running", next_check=None)
    c.job.update(status="waiting", next_check=1_800_000_000.0)
    c.save()
    assert c.job["next_check_label"] == _time.strftime(
        "%Y-%m-%d %H:%M:%S", _time.localtime(1_800_000_000.0)
    )
    c.job.update(status="running")  # The shared wait ended but left its time behind.
    c.save()
    assert c.job["next_check_label"] == "按各阶段状态继续"

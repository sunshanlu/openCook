import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from xhs_to_opencook import runtime
from xhs_to_opencook.errors import TaskError
from xhs_to_opencook.notifications import Outbox
from xhs_to_opencook.runtime import WorkerRuntime, read_json
from xhs_to_opencook.storage import write_json
from xhs_to_opencook.supervisor import Supervisor, owned_process, process_identity, terminate_owned


def worker(tmp_path, clock):
    return WorkerRuntime(tmp_path, "job", clock=lambda: clock[0], monotonic=lambda: clock[0])


def test_budget_survives_restart_and_excludes_suspension(tmp_path):
    clock = [1000]
    first = worker(tmp_path, clock)
    first.begin_item("classify", "note")
    for _ in range(6):
        clock[0] += 10
        first.tick()
    clock[0] += 3600
    first.tick()
    second = worker(tmp_path, clock)
    second.begin_item("classify", "note")
    assert second.items["budgets"]["classify:note"] == 60
    second.items["budgets"]["classify:note"] = 2100
    with pytest.raises(TaskError, match="35分钟"):
        second.check()


@pytest.mark.parametrize("codes", [["empty"] * 3, ["a", None, "b", None, "c", None,
                                                   "d", None, "e", None]])
def test_circuit_counts_each_video_once(tmp_path, codes):
    clock = [1000]
    active = worker(tmp_path, clock)
    for i, code in enumerate(codes[:-1]):
        active.outcome("classify", str(i), code)
        active.outcome("classify", str(i), code)
    with pytest.raises(TaskError, match="连续失败"):
        active.outcome("classify", str(len(codes) - 1), codes[-1])
    assert active.data["wait_kind"] == "circuit"
    assert len(active.items["seen"]) == len(codes)


def test_probe_failure_waits_without_resetting_budget(tmp_path):
    clock = [1000]
    active = worker(tmp_path, clock)
    active.probing = True
    active.items["budgets"]["classify:n"] = 42
    with pytest.raises(TaskError, match="试探仍失败"):
        active.outcome("classify", "n", "empty")
    assert read_json(active.items_path)["budgets"]["classify:n"] == 42


def test_pause_prevents_new_operations(tmp_path):
    clock = [1000]
    active = worker(tmp_path, clock)
    write_json(tmp_path / "control.json", {"job_id": "job", "action": "pause"})
    with pytest.raises(KeyboardInterrupt):
        active.begin_item("classify", "n")


def test_outbox_retries_same_key_and_coalesces_progress(tmp_path):
    clock = [1000]
    calls = []
    def send(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "ok": True, "data": {"message_id": "message"}
        }) if len(calls) > 1 else "{}")
    box = Outbox(tmp_path, "self", clock=lambda: clock[0], run=send)
    box.enqueue("login", "请登录")
    box.flush()
    assert box.data["pending"][0]["attempts"] == 1
    clock[0] += 60
    box.flush()
    assert not box.data["pending"]
    assert calls[0][calls[0].index("--idempotency-key") + 1] == \
        calls[1][calls[1].index("--idempotency-key") + 1]
    box.enqueue("progress", "旧进度", progress=True)
    box.enqueue("progress", "新进度", progress=True)
    assert len(box.data["pending"]) == 1
    assert box.data["pending"][0]["text"] == "新进度"


def test_outbox_rejects_dry_run_or_missing_receipt(tmp_path):
    result = SimpleNamespace(returncode=0, stdout='{"ok":true,"dry_run":true,"data":{}}')
    box = Outbox(tmp_path, "self", run=lambda *a, **kw: result)
    box.enqueue("complete", "已完成")
    box.flush()
    assert box.data["pending"]


def test_24_hour_wait_restart_and_notification_drain(tmp_path):
    clock = [1000]
    job = {"job_id": "job", "status": "waiting", "output": str(tmp_path / "output"),
           "next_check": None, "state_dir": str(tmp_path), "cwd": str(tmp_path)}
    write_json(tmp_path / "job.json", job)
    for _ in range(24 * 60):
        supervisor = Supervisor(tmp_path, clock=lambda: clock[0])
        supervisor.outbox.flush = lambda **kw: None
        assert supervisor.step()
        clock[0] += 60
    assert read_json(tmp_path / "job.json")["status"] == "waiting"
    supervisor.job.update(status="completed")
    supervisor.outbox.enqueue("complete", "结果")
    assert supervisor.step()
    assert supervisor.job["status"] == "notifications_pending"
    supervisor.outbox.data["pending"] = []
    assert not supervisor.step()


def test_real_owned_process_termination_and_unrelated_process_protection():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                               start_new_session=True)
    try:
        time.sleep(0.1)
        identity = process_identity(process.pid)
        assert not owned_process({"pid": os.getpid(), "identity": "different"})
        terminate_owned({"pid": process.pid, "identity": identity}, grace=0.1)
        process.wait(timeout=5)
        assert process.returncode in (-signal.SIGINT, -signal.SIGKILL, 1)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_unknown_submission_after_invalid_json_continues_without_resend(tmp_path):
    from test_chat import process, setup
    store, driver, manager = setup(tmp_path)
    original = driver.begin_request
    calls = []
    def send(prompt, source, request, persist):
        calls.append(request["request_id"])
        if request["attempt"] == 2:
            request.update(status="sending", sent_at=driver.clock)
            persist(request)
            raise TaskError("submission_unknown", "未找到回执", retryable=False)
        original(prompt, source, request, persist)
    driver.begin_request = send
    driver.replies = ["{"]
    driver.request_snapshot = lambda r: {"matches": 0}
    with pytest.raises(TaskError, match="等待人工审核"):
        process(store, manager, 1)
    assert len(calls) == 2 and manager.pending() is None
    request = manager.review_rows()[0]
    assert request["attempt"] == 2
    process(store, manager, 2)
    with pytest.raises(TaskError, match="等待人工审核"):
        process(store, manager, 1)
    assert len(manager.review_rows()) == 1 and len(calls) == 3
    store.close()


def test_platform_wait_preserves_pending_and_does_not_review(tmp_path, monkeypatch):
    from test_chat import process, setup
    store, driver, manager = setup(tmp_path)
    clock = [1000]
    active = worker(tmp_path / "supervision", clock)
    monkeypatch.setattr(runtime, "_worker", active)
    driver.replies = [TaskError("platform_restricted", "300013", retryable=False)]
    with pytest.raises(TaskError, match="300013"):
        process(store, manager, 1)
    assert manager.pending()["status"] == "submitted"
    assert not manager.review_rows()
    assert active.data["wait_kind"] == "platform"
    store.close()


def test_supervisor_restarts_only_after_heartbeat_or_deadline(tmp_path, monkeypatch):
    from xhs_to_opencook import supervisor as module
    clock = [1000]
    write_json(tmp_path / "job.json", {
        "job_id": "job", "status": "running", "worker": {"pid": 123, "identity": "ours"},
        "output": str(tmp_path / "output"), "last_progress_notice": 1000,
    })
    write_json(tmp_path / "worker.json", {
        "job_id": "job", "heartbeat_at": 1000, "deadline": 1600, "status": "running",
    })
    stopped = []
    monkeypatch.setattr(module, "owned_process", lambda record: True)
    monkeypatch.setattr(module, "terminate_owned", stopped.append)
    active = Supervisor(tmp_path, clock=lambda: clock[0])
    active.outbox.flush = lambda **kw: None
    clock[0] = 1010
    assert active.step() and not stopped
    clock[0] = 1070
    assert active.step() and stopped
    assert active.job["status"] == "ready"


def test_restart_exhaustion_and_manual_pause_are_not_auto_resumed(tmp_path):
    clock = [1000]
    write_json(tmp_path / "job.json", {
        "job_id": "job", "status": "ready", "output": str(tmp_path),
    })
    active = Supervisor(tmp_path, clock=lambda: clock[0])
    for _ in range(4):
        active.crash("crash")
    assert active.job["status"] == "waiting" and active.job["next_check"] is None
    write_json(tmp_path / "control.json", {"job_id": "job", "action": "pause"})
    assert active.step()
    assert active.job["status"] == "paused"
    assert active.step() and active.job["status"] == "paused"


def test_isolation_failure_keeps_request_pending(tmp_path, monkeypatch):
    from playwright.sync_api import Error
    from test_chat import process, setup
    store, driver, manager = setup(tmp_path)
    original = driver.new_chat
    def new_chat():
        if driver.new:
            raise Error("cannot isolate")
        original()
    driver.new_chat = new_chat
    driver.failure = TaskError("submission_unknown", "unknown")
    driver.request_snapshot = lambda r: {"matches": 0}
    clock = [1000]
    active = worker(tmp_path / "supervision", clock)
    monkeypatch.setattr(runtime, "_worker", active)
    manager.sleeper = lambda s: (setattr(driver, "clock", driver.clock + s),
                                 clock.__setitem__(0, clock[0] + s))
    with pytest.raises(TaskError, match="无法隔离"):
        process(store, manager, 1)
    assert manager.pending() and not manager.review_rows()
    assert active.data["wait_kind"] == "manual"
    store.close()


def test_self_recipient_resolution_uses_identity_not_auth_availability(tmp_path):
    response = {"identity": "user", "available": False,
                "onBehalfOf": {"openId": "self"}}
    box = Outbox(tmp_path, run=lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps(response)
    ))
    box.resolve_self()
    assert box.data["recipient"] == "self"


def test_resume_intent_is_consumed_and_does_not_unlock_later_failure(tmp_path, monkeypatch):
    write_json(tmp_path / "job.json", {
        "job_id": "job", "status": "paused", "output": str(tmp_path),
        "last_progress_notice": 1000,
    })
    write_json(tmp_path / "control.json", {"job_id": "job", "action": "resume"})
    active = Supervisor(tmp_path, clock=lambda: 1000)
    monkeypatch.setattr(active, "launch", lambda **kw: active.job.update(status="waiting"))
    active.outbox.flush = lambda **kw: None
    assert active.step()
    assert read_json(tmp_path / "control.json")["action"] == "running"
    active.job.update(status="waiting", next_check=None)
    assert active.step() and active.job["status"] == "waiting"


def test_suspension_extends_operation_deadline_without_spending_budget(tmp_path):
    mono, wall = [1000], [1000]
    active = WorkerRuntime(tmp_path, "job", clock=lambda: wall[0],
                           monotonic=lambda: mono[0])
    active.begin_item("classify", "note")
    deadline = active.data["deadline"]
    mono[0] += 1
    wall[0] += 3601
    active.tick()
    assert active.data["deadline"] == deadline + 3600
    assert active.items["budgets"]["classify:note"] == 1


def test_reference_network_failure_waits_without_consuming_retry(tmp_path, monkeypatch):
    from playwright.sync_api import Error
    from test_chat import process, setup
    store, driver, manager = setup(tmp_path)
    clock = [1000]
    active = worker(tmp_path / "supervision", clock)
    monkeypatch.setattr(runtime, "_worker", active)
    def send(*args):
        try:
            raise Error("net::ERR_CONNECTION_RESET")
        except Error as cause:
            raise TaskError("reference_mismatch", "引用暂未加载") from cause
    driver.begin_request = send
    with pytest.raises(TaskError, match="引用暂未加载"):
        process(store, manager, 1)
    assert manager.pending()["status"] == "prepared"
    assert manager.pending()["attempt"] == 1 and not manager.review_rows()
    assert active.data["wait_kind"] == "platform"
    store.close()


def test_login_wait_is_budget_free_and_resume_preserves_item(tmp_path):
    clock = [1000]
    active = worker(tmp_path, clock)
    active.begin_item("classify", "n")
    checked = []
    active.wait_access(lambda: checked.append(True),
                       sleeper=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert checked == [True]
    assert active.data["status"] == "running" and active.data["item"] == "classify:n"
    assert active.items["budgets"]["classify:n"] == 0

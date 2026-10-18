from types import SimpleNamespace

import pytest

from xhs_to_opencook.cleanup import ChatCleanup
from xhs_to_opencook.errors import TaskError
from xhs_to_opencook.storage import Store


def saved(store, identity, status="complete", *, account="account", conversation=None):
    batch = {"batch_id": identity, "stage": "extract", "account_id": account,
             "status": "sealed", "conversation_id": conversation or identity}
    store.stage_save("chat_batches", identity, batch)
    request = {"request_id": identity, "batch_id": identity, "stage": "extract",
               "account_id": account, "status": status, "note_id": identity,
               "source": {"url": "https://www.xiaohongshu.com/explore/" + "a" * 24}}
    store.stage_save("chat_requests", identity, request)
    return request


def cleaner(store, deleted, **kwargs):
    return ChatCleanup(store, lambda: SimpleNamespace(
        delete_chat=lambda identity: deleted.append(identity) or {"status": "deleted"}),
        account_id="account", stage="extract", clock=lambda: 1000, **kwargs)


def test_cleanup_includes_review_but_protects_unresolved_and_foreign_sessions(tmp_path):
    store, deleted = Store(tmp_path), []
    saved(store, "review", "manual_review")
    saved(store, "complete")
    saved(store, "foreign", account="other")
    unresolved = saved(store, "pending", "submitted")
    cleanup = cleaner(store, deleted)
    assert cleanup.run(limit=10) == []  # Never disturb an active request's page.
    unresolved["status"] = "manual_review"
    store.stage_save("chat_requests", "pending", unresolved)
    cleanup.run(limit=10)
    assert set(deleted) == {"review", "complete", "pending"}
    assert store.stage_get("chat_requests", "review")["status"] == "manual_review"
    assert cleanup.run(limit=10) == []
    store.close()


def test_cleanup_waits_for_all_shared_batches_and_ignores_unknown_ownership(tmp_path):
    store, deleted = Store(tmp_path), []
    saved(store, "a", conversation="shared")
    saved(store, "b", conversation="shared")
    second = store.stage_get("chat_batches", "b")
    second["status"] = "open"
    store.stage_save("chat_batches", "b", second)
    saved(store, "legacy", account=None)
    cleanup = cleaner(store, deleted)
    cleanup.run(limit=10)
    assert deleted == []
    second["status"] = "sealed"
    store.stage_save("chat_batches", "b", second)
    cleanup.run(limit=10)
    assert deleted == ["shared"]
    store.close()


def test_cleanup_failure_is_durable_and_not_retried_immediately(tmp_path):
    store, deleted = Store(tmp_path), []
    saved(store, "old")
    calls = []

    def timeout(identity):
        calls.append(identity)
        raise TaskError("cleanup_unconfirmed", "页面超时")

    cleanup = cleaner(store, deleted)
    cleanup.driver = lambda: SimpleNamespace(delete_chat=timeout)
    assert cleanup.run() == []
    assert cleanup.run() == []
    assert calls == ["old"]
    store.close()
    store = Store(tmp_path)
    record = store.stage_get("chat_batches", "old")["cleanup"]
    assert record["status"] == "deferred" and record["next_attempt_at"] == 1300
    assert cleaner(store, deleted).run() == []
    store.close()


def test_confirmed_missing_is_complete_but_platform_restriction_propagates(tmp_path):
    store, deleted = Store(tmp_path), []
    saved(store, "gone")
    cleanup = cleaner(store, deleted)
    cleanup.driver = lambda: SimpleNamespace(delete_chat=lambda cid: {"status": "missing"})
    assert cleanup.run() == ["gone"]
    saved(store, "restricted")

    def restricted(cid):
        raise TaskError("platform_restricted", "安全限制")

    cleanup.driver = lambda: SimpleNamespace(delete_chat=restricted)
    with pytest.raises(TaskError, match="安全限制"):
        cleanup.run()
    assert "cleanup" not in store.stage_get("chat_batches", "restricted")
    store.close()


def test_cleanup_is_bounded_and_never_runs_at_night(tmp_path):
    store, deleted = Store(tmp_path), []
    saved(store, "a")
    saved(store, "b")
    cleanup = cleaner(store, deleted, pacing=SimpleNamespace(next_open=lambda: 1001))
    assert cleanup.run() == [] and deleted == []
    cleanup.pacing = None
    cleanup.run(limit=1)
    assert len(deleted) == 1
    store.close()

"""Explicit account bindings; no authentication material is recorded here."""

from pathlib import Path

from .errors import TaskError
from .runtime import read_json
from .storage import now, write_json

STAGES = ("classify", "extract")


def profile_path(state, stage):
    return Path(state) / ("chrome" if stage == "classify" else "chrome-extract")


def bindings(state):
    return read_json(Path(state) / "accounts.json")


def save_binding(state, stage, account_id):
    value = bindings(state)
    previous = value.get(stage, {}).get("account_id")
    if previous and previous != account_id:
        raise TaskError("account_mismatch", "账号与已登记身份不同，请登录回原账号", retryable=False)
    other = value.get("extract" if stage == "classify" else "classify", {}).get("account_id")
    if other == account_id:
        raise TaskError("account_mismatch", "分类和提取必须登录不同账号", retryable=False)
    value[stage] = {
        "account_id": account_id,
        "profile": str(profile_path(state, stage)),
        "verified_at": now(),
    }
    write_json(Path(state) / "accounts.json", value)
    return value


def require_bindings(state):
    value = bindings(state)
    ids = [value.get(stage, {}).get("account_id") for stage in STAGES]
    if not all(ids) or ids[0] == ids[1]:
        raise TaskError(
            "account_setup_required", "请先登录并确认分类、提取两个不同账号", retryable=False
        )
    return {stage: value[stage] for stage in STAGES}


def migrate_accounts(store, old_account, new_accounts):
    """Run while stopped. Unresolved old extraction remains bound to its original account."""
    import json

    from .chat import PENDING
    from .models import Extraction, parse_bound_answer

    requests = store.stage_rows("chat_requests")
    batches = store.stage_rows("chat_batches")
    for request in requests:
        if request["stage"] != "extract" or request["status"] not in PENDING:
            continue
        if request.get("account_id", old_account) == new_accounts["extract"]["account_id"]:
            continue
        record = store.get(request["note_id"])
        if request["status"] != "validated" or not record or record.get("status") != "success":
            raise TaskError(
                "account_migration_pending", "旧提取请求须在原账号安全收尾后再迁移", retryable=False
            )
        result = parse_bound_answer(
            request["answer"], "extract", request["note_id"], request["request_id"]
        )
        if result != Extraction.model_validate(record["extraction"]):
            raise TaskError("state_conflict", "已保存菜谱与原请求回答不一致", retryable=False)
    stamp = now()
    with store.db:
        for request in requests:
            request.setdefault("account_id", old_account)
            if (
                request["stage"] == "extract"
                and request["status"] == "validated"
                and request["account_id"] != new_accounts["extract"]["account_id"]
            ):
                request.update(status="complete", completed_by_migration=True)
            store.db.execute(
                "UPDATE chat_requests SET value=? WHERE id=?",
                (json.dumps(request, ensure_ascii=False), request["request_id"]),
            )
        for batch in batches:
            batch.setdefault("account_id", old_account)
            if (
                batch["stage"] == "extract"
                and batch["account_id"] != new_accounts["extract"]["account_id"]
            ):
                batch.update(status="sealed", sealed_reason="account_migration", sealed_at=stamp)
            store.db.execute(
                "UPDATE chat_batches SET value=? WHERE id=?",
                (json.dumps(batch, ensure_ascii=False), batch["batch_id"]),
            )

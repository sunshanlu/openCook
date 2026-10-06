"""Durable sequential chat batches shared by profile workflow and legacy run.

Only the adapter can confirm a submission. Sending intent is journaled before Enter;
unknown intent is reconciled against history, never optimistically resent.
"""

import time
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError

from .errors import BrowserUnsupported, TaskError
from .models import parse_bound_answer
from .prompts import CLASSIFICATION_VERSION, EXTRACTION_VERSION, bound_prompt
from .storage import atomic_write, now, write_json

PENDING = {
    "prepared",
    "sending",
    "submitted",
    "cooling",
    "answered",
    "validated",
    "blocked",
    "retry_ready",
}


class ChatSession:
    def __init__(
        self,
        store,
        driver,
        *,
        capacity=50,
        interval=10,
        timeout=180,
        sleeper=time.sleep,
        clock=time.time,
        cooldown=600,
    ):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("chat batch size must be a positive integer")
        self.store, self.driver = store, driver
        self.capacity, self.interval, self.timeout = capacity, max(10, interval), timeout
        self.sleeper, self.clock, self.cooldown = sleeper, clock, cooldown
        self.loaded_batch = None

    def pending_rows(self):
        return [r for r in self.store.stage_rows("chat_requests") if r["status"] in PENDING]

    def pending(self, stage=None):
        rows = self.pending_rows()
        if stage:
            rows = [r for r in rows if r["stage"] == stage]
        if len(rows) > 1:
            raise BrowserUnsupported("存在多个未决点点请求；停止提交，需核对持久记录")
        return rows[0] if rows else None

    def batches(self):
        return self.store.stage_rows("chat_batches")

    def persist(self, request):
        self.store.stage_save("chat_requests", request["request_id"], request)
        if request.get("conversation_id") and request.get("round_id"):
            batch = self.store.stage_get("chat_batches", request["batch_id"])
            existing = batch.get("conversation_id")
            if existing and existing != request["conversation_id"]:
                raise BrowserUnsupported("请求对应的会话 ID 与批次不同")
            batch["conversation_id"] = request["conversation_id"]
            if request["note_id"] not in batch["submitted_notes"]:
                batch["submitted_notes"].append(request["note_id"])
            self.store.stage_save("chat_batches", batch["batch_id"], batch)
        if request.get("raw_directory"):
            write_json(self.store.directory / request["raw_directory"] / "request.json", request)

    def load(self, batch):
        if self.loaded_batch == batch["batch_id"]:
            return
        driver = self.driver()
        cid = batch.get("conversation_id")
        if cid:
            driver.restore_chat(cid)
        elif any(
            r["batch_id"] == batch["batch_id"] and r["status"] in PENDING - {"prepared"}
            for r in self.store.stage_rows("chat_requests")
        ):
            raise BrowserUnsupported("提交中断且会话 ID 未确认；禁止新建会话或直接重发")
        else:
            driver.new_chat()
        self.loaded_batch = batch["batch_id"]

    def batch(self, stage, note):
        rows = [b for b in self.batches() if b["stage"] == stage]
        batch = rows[-1] if rows else None
        if (
            batch
            and batch["status"] == "open"
            and len(batch["submitted_notes"]) >= batch["capacity"]
        ):
            batch.update(status="sealed", sealed_reason="capacity", sealed_at=now())
            self.store.stage_save("chat_batches", batch["batch_id"], batch)
        if not batch or batch["status"] != "open":
            batch = {
                "batch_id": uuid4().hex,
                "stage": stage,
                "status": "open",
                "capacity": self.capacity,
                "submitted_notes": [],
                "conversation_id": None,
                "created_at": now(),
            }
            self.store.stage_save("chat_batches", batch["batch_id"], batch)
        self.load(batch)
        return batch

    def begin(self, prompt, source, request):
        try:
            self.driver().begin_request(prompt, source, request, self.persist)
        except PlaywrightError as exc:
            self.loaded_batch = None
            raise BrowserUnsupported("提交期间浏览器关闭；保存意图，续跑先核对历史") from exc
        except TaskError:
            if request["status"] == "prepared":
                request.update(status="failed", error={"code": "pre_submission_failed"})
                self.persist(request)
            raise

    def throttle(self):
        sent = [r["sent_at"] for r in self.store.stage_rows("chat_requests") if r.get("sent_at")]
        if sent:
            self.sleeper(max(0, self.interval - (self.clock() - max(sent))))

    def reconcile(self, request):
        batch = self.store.stage_get("chat_batches", request["batch_id"])
        # Repair possible crash between request commit and batch counter commit.
        if not batch.get("conversation_id") and request.get("conversation_id"):
            self.persist(request)
            batch = self.store.stage_get("chat_batches", request["batch_id"])
        self.load(batch)
        snapshot = self.driver().request_snapshot(request)
        if snapshot.get("matches") != 1:
            raise BrowserUnsupported("历史中无法确认未决请求；禁止直接重发或创建额外会话")
        request.update(conversation_id=snapshot["conversation_id"], round_id=snapshot["round_id"])
        if snapshot.get("history_binding"):
            request.update(
                server_message_id=snapshot["history_binding"]["server_message_id"],
                history_binding=snapshot["history_binding"],
                original_round_id=snapshot["original_round_id"],
            )
        self.persist(request)
        return snapshot

    def wait(self, request):
        driver = self.driver()
        if request["status"] in {"answered", "validated"}:
            if not driver.confirm_idle():
                raise BrowserUnsupported("回答虽已缓存，但当前会话生成未确认结束")
            return request["answer"]
        if request["status"] == "retry_ready":
            return None
        if request["status"] == "blocked":
            self.reconcile(request)
            try:
                return driver.wait_request(request, 2, self.persist)
            except TaskError as exc:
                if exc.code not in {"answer_timeout", "empty_answer"}:
                    raise
                raise BrowserUnsupported(
                    "上次无回复重试已耗尽或生成状态不明；本轮停止提交"
                ) from exc
        if request["status"] != "cooling":
            try:
                return driver.wait_request(
                    request,
                    max(0, self.timeout - (self.clock() - request.get("sent_at", self.clock()))),
                    self.persist,
                )
            except TaskError as exc:
                if exc.code == "empty_answer":
                    return self.empty_retry(request)
                if exc.code != "answer_timeout":
                    raise
                request.update(
                    status="cooling",
                    cooldown_until=request.get("sent_at", self.clock())
                    + self.timeout
                    + self.cooldown,
                )
                self.persist(request)
                print(
                    "  当前请求无完整回复，暂停新提交并冷却10分钟，继续观察迟到回答。", flush=True
                )
        # Small waits allow status updates and controlled interrupts throughout cooldown.
        while self.clock() < request["cooldown_until"]:
            remaining = request["cooldown_until"] - self.clock()
            print(
                f"  冷却剩余 {int(remaining)} 秒；会话 {request.get('conversation_id')}", flush=True
            )
            observed_at = self.clock()
            try:
                answer = driver.wait_request(request, min(30, remaining), self.persist)
                # Reject invalid late responses before clearing the cooling state.
                parse_bound_answer(
                    answer, request["stage"], request["note_id"], request["request_id"]
                )
                return answer
            except TaskError as exc:
                if exc.code == "empty_answer":
                    return self.empty_retry(request)
                if exc.code not in {"answer_timeout", "json_invalid", "reference_mismatch"}:
                    raise
                request.update(status="cooling")
                request.pop("answer", None)
                self.persist(request)
                self.sleeper(max(0, min(5, remaining) - (self.clock() - observed_at)))
        if not driver.stop_generation():
            request.update(status="blocked", blocked_reason="generation_not_confirmed_stopped")
            self.persist(request)
            raise BrowserUnsupported("冷却结束，无法确认旧生成已终止；保存进度并停止整批")
        if request.get("silence_retry") or request["attempt"] >= 2:
            request.update(status="blocked", blocked_reason="silence_retry_exhausted")
            self.persist(request)
            raise BrowserUnsupported("同会话重试仍无有效回复；保存进度并停止整批")
        request.update(status="retry_ready", safe_to_retry=True)
        self.persist(request)
        return None

    def empty_retry(self, request):
        # Only explicit placeholder + matched round + finished/idle can bypass cooldown.
        if not request.get("empty_answer_evidence") or not self.driver().confirm_idle():
            raise BrowserUnsupported("空回答缺少结束证据；停止提交")
        if request.get("silence_retry") or request["attempt"] >= 2:
            request.update(status="blocked", blocked_reason="empty_answer_retry_exhausted")
            self.persist(request)
            raise BrowserUnsupported("同会话重试仍为（暂无内容）；保存进度并停止整批")
        request.update(status="retry_ready", safe_to_retry=True, retry_reason="empty_answer")
        self.persist(request)
        print("  当前轮已结束且持续为空，跳过冷却；原会话最多重试一次。", flush=True)
        return None

    def process(self, source, stage, directory):
        pending = self.pending()
        if pending and (pending["stage"] != stage or pending["note_id"] != source.note_id):
            raise BrowserUnsupported("必须先恢复原阶段及原视频的未决请求，不能发送另一视频")
        attempt, repaired, silence_retry = 1, False, False
        self.last_attempt = 1
        batch = None
        predecessor = None
        if pending:
            if pending["status"] != "prepared":
                self.reconcile(pending)
            batch = self.store.stage_get("chat_batches", pending["batch_id"])
            attempt, repaired = pending["attempt"], pending.get("repair_used", False)
            silence_retry = pending.get("silence_retry", False)
            if pending["status"] == "retry_ready":
                attempt += 1
                silence_retry = True
                predecessor = pending
                pending = None
        while attempt <= 2:
            request = pending
            pending = None
            if not request:
                batch = batch or self.batch(stage, source.note_id)
                self.load(batch)
                request = {
                    "request_id": uuid4().hex,
                    "note_id": source.note_id,
                    "stage": stage,
                    "batch_id": batch["batch_id"],
                    "conversation_id": self.driver().conversation_id,
                    "status": "prepared",
                    "attempt": attempt,
                    "repair_used": repaired,
                    "silence_retry": silence_retry,
                    "repair": False,
                    "prompt_version": (
                        CLASSIFICATION_VERSION if stage == "classify" else EXTRACTION_VERSION
                    ),
                    "source": source.model_dump(),
                    "raw_directory": str(directory.relative_to(self.store.directory)),
                }
                if predecessor:
                    predecessor.update(status="no_reply")
                    self.store.replace_chat_request(predecessor, request)
                    predecessor = None
                else:
                    self.persist(request)
                self.throttle()
                self.begin(bound_prompt(source, stage, request["request_id"]), source, request)
            if request["status"] == "prepared":
                self.load(batch)
                self.throttle()
                self.begin(
                    request.get("prompt") or bound_prompt(source, stage, request["request_id"]),
                    source,
                    request,
                )
            try:
                answer = self.wait(request)
                if answer is None:
                    predecessor = request
                    attempt += 1
                    silence_retry = True
                    continue
                atomic_write(
                    directory / ("repair.txt" if request.get("repair") else "answer.txt"),
                    answer.encode(),
                )
                try:
                    result = parse_bound_answer(
                        answer, stage, source.note_id, request["request_id"]
                    )
                except TaskError as invalid:
                    if repaired or invalid.code != "json_invalid":
                        raise
                    repaired = True
                    request.update(status="invalid", repair_used=True)
                    repair = {
                        **request,
                        "request_id": uuid4().hex,
                        "status": "prepared",
                        "repair": True,
                        "repair_used": True,
                    }
                    for key in (
                        "answer",
                        "round_id",
                        "sent_at",
                        "cooldown_until",
                        "server_message_id",
                        "history_binding",
                        "original_round_id",
                        "empty_answer_evidence",
                    ):
                        repair.pop(key, None)
                    repair["conversation_id"] = self.driver().conversation_id
                    repair["prompt"] = bound_prompt(
                        source, stage, repair["request_id"], str(invalid)
                    )
                    self.store.replace_chat_request(request, repair)
                    self.throttle()
                    self.begin(repair["prompt"], source, repair)
                    request = repair
                    answer = self.wait(request)
                    if answer is None:
                        predecessor = request
                        attempt += 1
                        silence_retry = True
                        continue
                    atomic_write(directory / "repair.txt", answer.encode())
                    result = parse_bound_answer(
                        answer, stage, source.note_id, request["request_id"]
                    )
                request.update(status="validated")
                self.persist(request)
                self.current = request
                return result
            except PlaywrightError as exc:
                self.loaded_batch = None
                raise BrowserUnsupported(
                    "浏览器在请求等待期间关闭；保留未决请求，续跑先核对历史"
                ) from exc
            except BrowserUnsupported:
                raise
            except TaskError as exc:
                if request["status"] in {
                    "sending",
                    "submitted",
                    "cooling",
                    "blocked",
                } and exc.code not in {"answer_incomplete", "context_limit"}:
                    raise BrowserUnsupported("当前提交未完成核对；保存未决请求并停止") from exc
                request.update(status="failed", error={"code": exc.code, "message": str(exc)})
                self.persist(request)
                if exc.code == "context_limit":
                    batch = self.store.stage_get("chat_batches", request["batch_id"])
                    batch.update(
                        status="sealed",
                        sealed_reason="context_limit",
                        evidence=request.get("context_limit_evidence"),
                        sealed_at=now(),
                    )
                    self.store.stage_save("chat_batches", batch["batch_id"], batch)
                    batch = None
                self.last_attempt = attempt
                if not exc.retryable or attempt == 2:
                    exc.retryable = False
                    raise
                attempt += 1
        raise BrowserUnsupported("无回复重试次数已耗尽")

    def accept(self):
        # Called only AFTER the legacy recipe/classification cache commit.
        request = self.current
        request.update(status="complete")
        self.persist(request)
        batch = self.store.stage_get("chat_batches", request["batch_id"])
        if len(batch["submitted_notes"]) >= batch["capacity"]:
            batch.update(status="sealed", sealed_reason="capacity", sealed_at=now())
            self.store.stage_save("chat_batches", batch["batch_id"], batch)

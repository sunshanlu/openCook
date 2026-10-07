"""Durable sequential chat batches shared by profile workflow and legacy run.

Only the adapter can confirm a submission. Sending intent is journaled before Enter;
unknown intent is reconciled against history, never optimistically resent.
"""

import csv
import io
import json
import time
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from . import runtime
from .errors import SESSION_BLOCK_CODES, BrowserUnsupported, TaskError
from .models import JSON_ERROR_CODES, parse_bound_answer
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
        capacity=5,
        interval=2,
        timeout=180,
        sleeper=time.sleep,
        clock=time.time,
        cooldown=600,
        max_attempts=2,
        reset_browser=None,
    ):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("chat batch size must be a positive integer")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts <= 0:
            raise ValueError("max attempts must be a positive integer")
        self.max_attempts = max_attempts
        self.store, self.driver = store, driver
        self.capacity, self.interval, self.timeout = capacity, interval, timeout
        self.sleeper, self.clock, self.cooldown = sleeper, clock, cooldown
        self.loaded_batch = None
        self.reset_browser = reset_browser

    def pause(self, seconds):
        runtime.sleep(seconds, self.sleeper)

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

    def review_rows(self):
        return [r for r in self.store.stage_rows("chat_requests")
                if r["status"] == "manual_review"]

    def review_for(self, stage, note_id):
        return next((r for r in self.review_rows()
                     if r["stage"] == stage and r["note_id"] == note_id), None)

    def release_review(self, stage, note_id):
        for request in self.review_rows():
            if request["stage"] == stage and request["note_id"] == note_id:
                request.update(status="review_released", review_released_at=now())
                self.persist(request)

    def write_reviews(self, directory, note_ids):
        rows = [{
            "note_id": r["note_id"], "stage": r["stage"], "url": r["source"]["url"],
            "title": r["source"].get("title"), "author": r["source"].get("author"),
            "status": "awaiting_review", "attempts": r["attempt"],
            "error": r.get("review_error", r.get("error")),
            "request_id": r["request_id"], "raw_directory": r.get("raw_directory"),
        } for r in self.review_rows() if r["note_id"] in note_ids]
        previous = {}
        csv_path = directory / "manual_review.csv"
        if csv_path.exists():
            with csv_path.open(encoding="utf-8-sig", newline="") as file:
                previous = {r["请求ID"]: r for r in csv.DictReader(file)}
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        headers = ["笔记ID", "失败阶段", "标题", "作者", "URL", "状态", "失败次数",
                   "错误码", "请求ID", "原始记录路径", "人工审核结论", "补全备注"]
        writer.writerow(headers)
        for row in rows:
            error = row["error"] or {}
            annotations = previous.pop(row["request_id"], {})
            writer.writerow([
                row["note_id"], "分类" if row["stage"] == "classify" else "提取",
                row["title"], row["author"], row["url"], "待人工审核", row["attempts"],
                error.get("code"), row["request_id"],
                str(self.store.directory / row["raw_directory"]) if row["raw_directory"] else "",
                annotations.get("人工审核结论", ""), annotations.get("补全备注", ""),
            ])
        for row in previous.values():
            if row["笔记ID"] in note_ids:
                row["状态"] = "已解除待审核"
                writer.writerow([row.get(header, "") for header in headers])
        atomic_write(csv_path, buffer.getvalue().encode("utf-8-sig"))
        urls = list(dict.fromkeys(r["url"] for r in rows))
        atomic_write(directory / "manual_review_links.txt",
                     ("\n".join(urls) + ("\n" if urls else "")).encode())
        return rows

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
            and len(batch["submitted_notes"]) >= min(batch["capacity"], self.capacity)
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
        runtime.check()
        try:
            self.driver().begin_request(prompt, source, request, self.persist)
        except PlaywrightError as exc:
            if runtime.current() and runtime.network_error(exc):
                runtime.block("network_unavailable", str(exc))
            self.loaded_batch = None
            raise BrowserUnsupported("提交期间浏览器关闭；保存意图，续跑先核对历史") from exc
        except TaskError as exc:
            if runtime.current() and runtime.network_error(exc):
                runtime.block("network_unavailable", str(exc))
            if self.recoverable_page_error(exc) and request["status"] == "prepared":
                raise
            if (
                exc.code in {"generation_busy", "reference_mismatch"} and exc.retryable
                and request["status"] == "prepared"
                and not (request.get("sent_at") or request.get("round_id"))
            ):
                request.update(
                    review_error={"code": exc.code, "message": str(exc)},
                    retry_new_chat=True, retry_reason=exc.code,
                )
                evidence = {"request_id": request["request_id"], "submitted": False}
                if request["attempt"] >= self.max_attempts:
                    self.exhausted(request, seal_reason=exc.code, seal_evidence=evidence)
                request.update(status="retry_ready", safe_to_retry=False)
                self.seal(request, exc.code, evidence, with_request=True)
                print(f"  {exc.code}，当前请求未发送；封存会话后换新会话重试。", flush=True)
                return
            if request["status"] == "prepared":
                request.update(status="failed", error={"code": "pre_submission_failed"})
                self.persist(request)
            raise

    def throttle(self):
        sent = [r["sent_at"] for r in self.store.stage_rows("chat_requests") if r.get("sent_at")]
        if sent:
            self.pause(max(0, self.interval - (self.clock() - max(sent))))

    def reconcile(self, request):
        runtime.operation("restore_history", 90)
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
            if driver.confirm_idle():
                return request["answer"]
            request.update(
                status="cooling", cached_answer_wait=True,
                cooldown_until=request.get("cooldown_until", self.clock() + self.cooldown),
            )
            self.persist(request)
            print("  回答已缓存，但生成结束状态未确认；冷却观察后恢复或换会话。", flush=True)
        if request["status"] == "retry_ready":
            return None
        if request["status"] == "blocked":
            self.reconcile(request)
            try:
                return driver.wait_request(request, 6, self.persist)
            except TaskError as exc:
                if runtime.current() and runtime.network_error(exc):
                    runtime.block("network_unavailable", str(exc))
                if exc.code not in {"answer_timeout", "empty_answer"}:
                    raise
                if request.get("blocked_reason") in {
                    "silence_retry_exhausted",
                    "empty_answer_retry_exhausted",
                    "generation_not_confirmed_stopped",
                }:
                    if exc.code == "empty_answer":
                        return self.empty_retry(request)
                    request.update(
                        attempt_limit=self.max_attempts, resumed_with_recovery_policy=True
                    )
                    self.persist(request)
                    return self.silence_retry(request)
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
            if request.get("cached_answer_wait"):
                if driver.confirm_idle():
                    request.update(status="answered")
                    self.persist(request)
                    return request["answer"]
                self.pause(min(5, remaining))
                continue
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
                if exc.code not in JSON_ERROR_CODES | {"answer_timeout", "reference_mismatch"}:
                    raise
                request.update(status="cooling")
                request.pop("answer", None)
                self.persist(request)
                self.pause(max(0, min(5, remaining) - (self.clock() - observed_at)))
        if request.get("cached_answer_wait") and driver.confirm_idle():
            request.update(status="answered")
            self.persist(request)
            return request["answer"]
        return self.silence_retry(request)

    def seal(self, request, reason, evidence, *, with_request=False):
        batch = self.store.stage_get("chat_batches", request["batch_id"])
        batch.update(
            status="sealed", sealed_reason=reason, evidence=evidence, sealed_at=now()
        )
        if with_request:
            self.store.seal_chat_request(request, batch)
            if request.get("raw_directory"):
                write_json(
                    self.store.directory / request["raw_directory"] / "request.json", request
                )
        else:
            self.store.stage_save("chat_batches", batch["batch_id"], batch)

    def exhausted(self, request, *, seal_reason=None, seal_evidence=None):
        seal_reason = seal_reason or "manual_review"
        seal_evidence = seal_evidence or request.get("review_error") or request.get("error") or {}
        error = TaskError(
            "manual_review_required",
            f"当前视频 {request['stage']} 已尝试 {request['attempt']} 次；等待人工审核并继续下一条",
            retryable=False,
        )
        request.update(
            status="manual_review",
            review_error=request.get("review_error") or request.get("error"),
            error={"code": error.code, "message": str(error)}, review_requested_at=now(),
        )
        self.last_attempt = request["attempt"]
        if seal_reason is not None:
            self.seal(request, seal_reason, seal_evidence, with_request=True)
        else:
            self.persist(request)
        raise error

    def silence_retry(self, request):
        request["review_error"] = {"code": "answer_timeout", "message": "等待及冷却后无有效回答"}
        rotate = not self.driver().stop_generation()
        if rotate:
            # Recheck the unique submission before isolating it. Never claim it stopped.
            evidence = self.reconcile(request)
            request.update(recovery_evidence=evidence, retry_new_chat=True)
            self.seal(request, "generation_not_confirmed_stopped", evidence)
        else:
            request["retry_new_chat"] = True
            self.seal(request, "no_valid_answer", {"request_id": request["request_id"]})
        if request["attempt"] >= self.max_attempts:
            self.exhausted(request)
        request.update(status="retry_ready", safe_to_retry=False)
        self.persist(request)
        if rotate:
            print(
                f"  旧生成状态不明，已封存会话；换新会话继续第 {request['attempt'] + 1}"
                f"/{self.max_attempts} 次尝试。", flush=True
            )
        return None

    def empty_retry(self, request):
        # Only explicit placeholder + matched round + finished/idle can bypass cooldown.
        if not request.get("empty_answer_evidence") or not self.driver().confirm_idle():
            raise BrowserUnsupported("空回答缺少结束证据；停止提交")
        request["review_error"] = {"code": "empty_answer", "message": "页面已结束但回答为暂无内容"}
        self.seal(request, "empty_answer", request["empty_answer_evidence"])
        if request["attempt"] >= self.max_attempts:
            self.exhausted(request)
        request.update(status="retry_ready", safe_to_retry=False,
                       retry_new_chat=True, retry_reason="empty_answer")
        self.persist(request)
        print(
            f"  当前轮已结束且持续为空，跳过冷却；换新会话继续第 {request['attempt'] + 1}"
            f"/{self.max_attempts} 次尝试。", flush=True
        )
        return None

    @staticmethod
    def recoverable_page_error(error):
        if isinstance(error, TaskError) and error.code in SESSION_BLOCK_CODES:
            return True
        # Adapter mismatches and unknown Playwright errors are bugs, not retry signals.
        cause = error
        while cause is not None:
            if isinstance(cause, PlaywrightTimeoutError):
                return True
            if isinstance(cause, PlaywrightError) and any(text in str(cause) for text in (
                "Timeout", "Execution context was destroyed", "has been closed",
                "net::ERR_", "Target closed",
            )):
                return True
            cause = cause.__cause__
        return False

    def recovery_review(self, source, stage, directory, evidence):
        request = self.pending()
        batches = [b for b in self.batches() if b["stage"] == stage]
        if request and (request["note_id"] != source.note_id or request["stage"] != stage):
            raise BrowserUnsupported("页面恢复的未决请求与当前视频不一致")
        if not request:
            batch = batches[-1] if batches else {
                "batch_id": uuid4().hex, "stage": stage, "status": "open",
                "capacity": self.capacity, "submitted_notes": [], "conversation_id": None,
            }
            if not batches:
                self.store.stage_save("chat_batches", batch["batch_id"], batch)
                batches.append(batch)
            request = {
                "request_id": uuid4().hex, "note_id": source.note_id, "stage": stage,
                "batch_id": batch["batch_id"], "status": "prepared", "attempt": 0,
                "source": source.model_dump(),
                "raw_directory": str(directory.relative_to(self.store.directory)),
            }
        request.update(review_error={
            "code": "page_recovery_exhausted", "message": "页面恢复6次失败，含5分钟等待",
        }, page_recovery=evidence)
        # Isolate every partially opened replacement before processing another video.
        for batch in batches:
            if batch["status"] == "open":
                batch.update(status="sealed", sealed_reason="page_recovery_exhausted",
                             sealed_at=now())
                self.store.stage_save("chat_batches", batch["batch_id"], batch)
        self.loaded_batch = None
        if runtime.current():
            self.isolate_review(source, stage, directory, "page_recovery_exhausted",
                                "页面恢复6次失败，含5分钟等待")
        self.exhausted(request, seal_reason="page_recovery_exhausted", seal_evidence=evidence)

    def isolate_review(self, source, stage, directory, code, message):
        """Retire an uncertain intent only after physically isolating its page."""
        existing = self.review_for(stage, source.note_id)
        if existing:
            self.last_attempt = existing["attempt"]
            raise TaskError("manual_review_required", "当前视频已转人工", retryable=False)
        request = self.pending() or getattr(self, "current", None)
        if not request or request["note_id"] != source.note_id or request["stage"] != stage:
            if request and request["status"] in PENDING:
                raise BrowserUnsupported("隔离请求与当前视频不一致")
            batch = self.batch(stage, source.note_id)
            request = {"request_id": uuid4().hex, "note_id": source.note_id, "stage": stage,
                       "batch_id": batch["batch_id"], "status": "prepared", "attempt": 0,
                       "source": source.model_dump(),
                       "raw_directory": str(directory.relative_to(self.store.directory))}
            self.persist(request)
        driver = self.driver()
        if runtime.current():
            runtime.current().data.pop("item", None)
            runtime.operation("isolate_session", 120)
        if hasattr(driver, "diagnostics"):
            driver.diagnostics(directory / "diagnostics")
        write_json(directory / "isolation.json", {
            "request_id": request["request_id"], "code": code, "message": message,
            "observed_at": now(), "conversation_id": request.get("conversation_id"),
        })
        try:
            if getattr(driver, "chat_page", None):
                driver.chat_page.close()
                driver.chat_page = None
                driver.history_observer = None
            driver.new_chat()
        except (PlaywrightError, TaskError):
            if not self.reset_browser:
                runtime.block("isolation_failed", "旧会话无法隔离", kind="manual")
            self.reset_browser()
            try:
                self.driver().new_chat()
            except (PlaywrightError, TaskError):
                runtime.block("isolation_failed", "专用浏览器重建失败", kind="manual")
        request["review_error"] = {"code": code, "message": message}
        self.loaded_batch = None
        self.exhausted(request, seal_reason=code, seal_evidence={"isolated": True})

    def resolve_unknown(self, source, stage, directory, error):
        request = self.pending()
        if not request:
            raise error
        end = self.clock() + 60
        runtime.operation("reconcile_unknown_submission", 90)
        driver = self.driver()
        if hasattr(driver, "chat_page") and not driver.chat_page \
                and not request.get("conversation_id"):
            self.isolate_review(source, stage, directory, "submission_unknown", str(error))
        while self.clock() < end:
            runtime.check()
            try:
                driver = self.driver()
                if request.get("conversation_id"):
                    snapshot = self.reconcile(request)
                else:
                    snapshot = driver.request_snapshot(request)
                if snapshot.get("matches") == 1 and snapshot.get("conversation_id"):
                    request.update(status="submitted", conversation_id=snapshot["conversation_id"],
                                   round_id=snapshot["round_id"])
                    self.persist(request)
                    return
            except TaskError as exc:
                if exc.code == "runtime_wait":
                    raise
                if exc.code in SESSION_BLOCK_CODES:
                    if runtime.current():
                        runtime.block(exc.code, str(exc))
                    raise
            except PlaywrightError:
                break
            self.pause(min(5, max(0, end - self.clock())))
        self.isolate_review(source, stage, directory, "submission_unknown", str(error))

    def process(self, source, stage, directory):
        """Own all recovery for one video; callers never repeat this operation."""
        path = self.store.directory / "recovery" / f"{stage}-{source.note_id}.json"
        if self.review_for(stage, source.note_id):
            path.unlink(missing_ok=True)
            return self._process(source, stage, directory)
        recovery = json.loads(path.read_text()) if path.exists() else {
            "failures": 0, "wait_until": None,
        }
        while True:
            try:
                runtime.check()
                deadline = recovery.get("wait_until")
                if deadline:
                    while self.clock() < deadline:
                        self.pause(min(5, deadline - self.clock()))
                    recovery["wait_until"] = None
                    write_json(path, recovery)
                if recovery["failures"] >= 6:
                    self.recovery_review(source, stage, directory, recovery)
                driver = self.driver()
                driver.raw_directory = directory
                result = self._process(source, stage, directory)
                if stage == "classify" and result.verdict == "unknown":
                    self.isolate_review(source, stage, directory, "content_unknown",
                                        "无法确认视频内容，等待人工判断")
                path.unlink(missing_ok=True)
                return result
            except TaskError as exc:
                if runtime.current() and exc.code == "login_required":
                    runtime.current().wait_access(
                        lambda: self.driver().ensure_access(self.driver().chat_page), self.sleeper
                    )
                    self.loaded_batch = None
                    continue
                if runtime.current() and exc.code in SESSION_BLOCK_CODES:
                    runtime.block(exc.code, str(exc))
                if exc.code == "item_budget_exhausted":
                    self.isolate_review(source, stage, directory, exc.code, str(exc))
                uncertain = exc.code == "submission_unknown" or (
                    isinstance(exc, BrowserUnsupported) and any(text in str(exc) for text in (
                        "提交状态无法关联", "会话 ID 未确认", "历史中无法确认", "当前用户消息",
                        "同一请求标识", "历史请求消息关联", "当前会话与持久请求不匹配",
                        "当前提交未完成核对",
                    ))
                )
                if uncertain:
                    self.resolve_unknown(source, stage, directory, exc)
                    self.loaded_batch = None
                    continue
                if exc.code == "runtime_wait":
                    raise
                if exc.code == "manual_review_required":
                    path.unlink(missing_ok=True)
                    raise
                if not self.recoverable_page_error(exc):
                    raise
                failure = exc
            except PlaywrightError as exc:
                if not self.recoverable_page_error(exc):
                    raise BrowserUnsupported("未预期的浏览器错误；停止并保存现场") from exc
                failure = exc
            except Exception as exc:
                write_json(directory / "unexpected-error.json", {
                    "type": type(exc).__name__, "message": str(exc),
                })
                raise
            if runtime.current() and runtime.network_error(failure):
                runtime.block("network_unavailable", str(failure))
            self.loaded_batch = None
            recovery.update(failures=recovery["failures"] + 1,
                            last_error={"type": type(failure).__name__, "message": str(failure)},
                            updated_at=now())
            if recovery["failures"] == 3:
                recovery["wait_until"] = self.clock() + 300
                print("  页面恢复连续3次失败；原地等待5分钟后再恢复3次。", flush=True)
            elif recovery["failures"] < 6:
                recovery["wait_until"] = self.clock() + 5
            write_json(path, recovery)
            print(f"  页面恢复 {recovery['failures']}/6：{failure}", flush=True)
            cause = failure
            while cause is not None:
                if self.reset_browser and any(text in str(cause) for text in (
                    "has been closed", "Target closed",
                )):
                    self.reset_browser()
                    break
                cause = cause.__cause__

    def _process(self, source, stage, directory):
        review = self.review_for(stage, source.note_id)
        if review:
            self.last_attempt = review["attempt"]
            raise TaskError("manual_review_required", "当前视频等待人工审核，未重新提交",
                            retryable=False)
        pending = self.pending()
        if pending and (pending["stage"] != stage or pending["note_id"] != source.note_id):
            raise BrowserUnsupported("必须先恢复原阶段及原视频的未决请求，不能发送另一视频")
        # Older versions restarted every unsent failure at attempt 1. Preserve their
        # actual failed preparations instead of repeating them on every full resume.
        legacy_failures = []
        for row in self.store.stage_rows("chat_requests"):
            if row["stage"] != stage or row["note_id"] != source.note_id:
                continue
            if row["status"] in {"complete", "review_released"}:
                legacy_failures = []
            elif (
                row["status"] == "failed"
                and row.get("error", {}).get("code") == "pre_submission_failed"
            ):
                legacy_failures.append(row)
        if legacy_failures and (
            not pending or (
                pending["status"] == "prepared"
                and not (pending.get("sent_at") or pending.get("round_id"))
            )
        ):
            recovery = pending or dict(legacy_failures[-1])
            recovery["attempt"] = max(
                recovery["attempt"], len(legacy_failures) + (1 if pending else 0),
            )
            recovery.setdefault("review_error", {
                "code": "pre_submission_failed", "message": "旧版本已记录发送前失败",
            })
            if recovery["attempt"] >= self.max_attempts:
                self.exhausted(recovery, seal_reason="pre_submission_failed",
                               seal_evidence={"legacy_failed_preparations": len(legacy_failures)})
            if not pending:
                recovery.update(status="retry_ready", retry_new_chat=True,
                                retry_reason="reference_mismatch", safe_to_retry=False)
                self.seal(recovery, "pre_submission_failed", {"submitted": False},
                          with_request=True)
            pending = recovery
        if not pending:
            previous = [
                r for r in self.store.stage_rows("chat_requests")
                if r["stage"] == stage and r["note_id"] == source.note_id
            ]
            latest = previous[-1] if previous else None
            if (
                latest and latest["status"] == "failed"
                and latest.get("error", {}).get("code") == "attempts_exhausted"
            ):
                if latest["attempt"] >= self.max_attempts:
                    self.exhausted(latest)
                pending = latest
                pending.update(status="blocked", blocked_reason="silence_retry_exhausted")
                pending.pop("error", None)
                self.persist(pending)
        attempt, repaired, silence_retry = 1, False, False
        self.last_attempt = 1
        batch = None
        predecessor = None
        if pending:
            if pending["attempt"] > self.max_attempts or (
                pending["status"] == "retry_ready" and pending["attempt"] >= self.max_attempts
            ):
                self.seal(pending, "attempt_limit", {"attempt_limit": self.max_attempts})
                pending.setdefault("review_error", {
                    "code": "attempts_exhausted", "message": "旧请求次数已达到本轮上限",
                })
                self.exhausted(pending)
            unsent_busy_retry = (
                pending["status"] == "retry_ready"
                and pending.get("retry_reason") in {"generation_busy", "reference_mismatch"}
                and not pending.get("sent_at") and not pending.get("round_id")
            )
            if pending["status"] != "prepared" and not unsent_busy_retry:
                try:
                    self.reconcile(pending)
                except TaskError as exc:
                    if exc.code != "history_unavailable" or not (
                        pending.get("conversation_id") and pending.get("round_id")
                        and pending.get("sent_at") and pending.get("prompt")
                    ):
                        raise
                    pending.update(retry_new_chat=True, recovery_evidence=exc.evidence)
                    self.seal(pending, "history_unavailable", exc.evidence)
                    if pending["attempt"] >= self.max_attempts:
                        self.exhausted(pending)
                    pending.update(status="retry_ready", safe_to_retry=False)
                    self.persist(pending)
                    print(
                        f"  旧会话历史确认不可用，封存后换新会话继续第 {pending['attempt'] + 1}"
                        f"/{self.max_attempts} 次尝试。", flush=True
                    )
            batch = self.store.stage_get("chat_batches", pending["batch_id"])
            if (pending["status"] == "prepared"
                    and pending["note_id"] not in batch["submitted_notes"]
                    and len(batch["submitted_notes"]) >= self.capacity):
                self.seal(pending, "capacity", {"capacity": self.capacity})
                batch = self.batch(stage, source.note_id)
                pending.update(batch_id=batch["batch_id"],
                               conversation_id=self.driver().conversation_id)
                self.persist(pending)
            attempt, repaired = pending["attempt"], pending.get("repair_used", False)
            silence_retry = pending.get("silence_retry", False)
            if pending["status"] == "retry_ready":
                attempt += 1
                silence_retry = True
                predecessor = pending
                pending = None
        while attempt <= self.max_attempts:
            self.last_attempt = attempt
            request = pending
            pending = None
            if not request:
                if predecessor and predecessor.get("retry_new_chat"):
                    batch = None
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
                    "attempt_limit": self.max_attempts,
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
                    request["previous_request_id"] = predecessor["request_id"]
                    predecessor.update(
                        status="abandoned" if predecessor.get("retry_new_chat") else "no_reply",
                        superseded_by=request["request_id"],
                    )
                    self.store.replace_chat_request(predecessor, request)
                    self.persist(predecessor)
                    self.persist(request)
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
                    if repaired or invalid.code not in JSON_ERROR_CODES:
                        raise
                    if attempt >= self.max_attempts:
                        raise
                    attempt += 1
                    self.last_attempt = attempt
                    repaired = True
                    request.update(status="invalid", repair_used=True,
                                   error={"code": invalid.code, "message": str(invalid)})
                    self.seal(request, "request_failed", request["error"])
                    batch = self.batch(stage, source.note_id)
                    repair = {
                        **request,
                        "request_id": uuid4().hex,
                        "batch_id": batch["batch_id"],
                        "previous_request_id": request["request_id"],
                        "status": "prepared",
                        "attempt": attempt,
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
                        "error",
                        "cached_answer_wait",
                    ):
                        repair.pop(key, None)
                    repair["conversation_id"] = self.driver().conversation_id
                    repair["prompt"] = bound_prompt(
                        source, stage, repair["request_id"], str(invalid)
                    )
                    request["superseded_by"] = repair["request_id"]
                    self.store.replace_chat_request(request, repair)
                    self.persist(request)
                    self.persist(repair)
                    request = repair
                    self.throttle()
                    self.begin(repair["prompt"], source, repair)
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
                if exc.code in SESSION_BLOCK_CODES:
                    # Keep any submitted intent for reconciliation after access is restored.
                    # A session restriction must never exhaust one video's attempt budget.
                    raise
                if exc.code == "manual_review_required":
                    raise
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
                if not exc.retryable or attempt >= self.max_attempts:
                    if exc.code != "manual_review_required" and attempt >= self.max_attempts:
                        if exc.code != "context_limit":
                            self.seal(request, "request_failed", request["error"])
                        self.exhausted(request)
                    exc.retryable = False
                    raise
                if exc.code != "context_limit":
                    self.seal(request, "request_failed", request["error"])
                batch = None
                attempt += 1
        self.seal(request, "attempt_limit", {"attempt_limit": self.max_attempts})
        self.exhausted(request)

    def accept(self):
        runtime.operation("save_result", 120)
        # Called only AFTER the legacy recipe/classification cache commit.
        request = self.current
        request.update(status="complete")
        self.persist(request)
        batch = self.store.stage_get("chat_batches", request["batch_id"])
        if len(batch["submitted_notes"]) >= batch["capacity"]:
            batch.update(status="sealed", sealed_reason="capacity", sealed_at=now())
            self.store.stage_save("chat_batches", batch["batch_id"], batch)

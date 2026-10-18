"""Retire only owned, settled conversations; local request and review records survive."""

from playwright.sync_api import Error as PlaywrightError

from . import runtime
from .errors import SESSION_BLOCK_CODES, TaskError
from .storage import now

SETTLED = {"complete", "manual_review", "review_released", "failed", "invalid",
           "abandoned", "no_reply"}
REMOVED = {"deleted", "missing"}


class ChatCleanup:
    def __init__(self, store, driver, *, account_id, stage, clock, pacing=None):
        self.store, self.driver = store, driver
        self.account_id, self.stage = account_id, stage
        self.clock, self.pacing = clock, pacing

    def run(self, *, limit=1):
        """Process a bounded oldest-first batch without touching an active request's page."""
        if not self.account_id or not self.stage:
            return []
        if self.pacing and self.pacing.next_open() > self.clock():
            return []
        requests = self.store.stage_rows("chat_requests")
        if any(r.get("account_id") == self.account_id and r.get("status") not in SETTLED
               for r in requests):
            return []
        batches = self.store.stage_rows("chat_batches")
        candidates = []
        seen = set()
        for batch in batches:
            cid = batch.get("conversation_id")
            if not cid or cid in seen or batch.get("account_id") != self.account_id:
                continue
            seen.add(cid)
            related = [b for b in batches if b.get("conversation_id") == cid]
            if any(b.get("stage") != self.stage or b.get("account_id") != self.account_id
                   or b.get("status") != "sealed" for b in related):
                continue
            if any(b.get("cleanup", {}).get("status") in REMOVED for b in related):
                continue
            if any(b.get("cleanup", {}).get("next_attempt_at", 0) > self.clock()
                   for b in related):
                continue
            identities = {b["batch_id"] for b in related}
            bound = [r for r in requests if r.get("batch_id") in identities
                     or r.get("conversation_id") == cid]
            if not bound or any(r.get("account_id") != self.account_id
                                or r.get("stage") != self.stage
                                or r.get("status") not in SETTLED
                                or not r.get("source", {}).get("url") for r in bound):
                continue
            candidates.append((cid, related))
        completed = []
        for cid, related in candidates[:limit]:
            driver = self.driver()
            if not hasattr(driver, "delete_chat"):
                break
            active = runtime.current()
            item = active.data.pop("item", None) if active else None
            try:
                runtime.operation("cleanup_chat", 120)
                outcome = driver.delete_chat(cid)
                if outcome.get("status") not in REMOVED:
                    raise TaskError("cleanup_unconfirmed", "无法确认会话已删除", retryable=False)
                record = {**outcome, "at": now()}
                completed.append(cid)
            except (TaskError, PlaywrightError) as error:
                if getattr(error, "code", None) in SESSION_BLOCK_CODES | {
                    "account_mismatch", "pacing_frozen", "runtime_wait",
                }:
                    raise
                record = {"status": "deferred", "at": now(),
                          "next_attempt_at": self.clock() + 300,
                          "error": {"code": getattr(error, "code", "browser_error"),
                                    "message": runtime.redact(error)}}
            finally:
                if active and item:
                    active.data["item"] = item
                runtime.operation("processing", 180)
            for batch in related:
                batch["cleanup"] = record
                self.store.stage_save("chat_batches", batch["batch_id"], batch)
        return completed

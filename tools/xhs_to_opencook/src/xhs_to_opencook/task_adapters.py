"""Real adapters binding the framework task engine to Store and the existing chat transport.

The engine sees a small Browser/Journal surface; these adapters translate it onto the
already-audited ChatSession submission, history reconciliation and Store persistence so the
new path reuses one request-identity and throttle contract instead of a second writer.
"""

from playwright.sync_api import Error as PlaywrightError

from .models import parse_bound_answer
from .prompts import CLASSIFICATION_VERSION


class StoreJournal:
    """Durable task journal backed by the Store import_tasks table (never framework objects)."""

    def __init__(self, store):
        self.store = store

    def load(self, identity):
        return self.store.task_get(identity)

    def save(self, identity, record):
        self.store.task_save(identity, record)


class ChatBrowserAdapter:
    """Engine Browser protocol over ChatSession and the live driver primitives."""

    def __init__(self, chats, driver, source, *, stage="classify"):
        self.chats, self.driver, self.source, self.stage = chats, driver, source, stage

    def submit(self, request):
        from .prompts import bound_prompt

        if not request.get("batch_id"):
            batch = self.chats.batch(self.stage, request["note_id"])
            request["batch_id"] = batch["batch_id"]
        prompt = request.get("prompt") or bound_prompt(
            self.source, self.stage, request["request_id"]
        )
        self.chats.begin(prompt, self.source, request)
        if request.get("status") == "submitted":
            return {"submitted": True, "request": request}
        return {
            "submitted": False,
            "request": request,
            "unsent": request.get("status") in {"prepared", "retry_ready"},
        }

    def reconcile(self, request):
        saved = self.chats.store.stage_get("chat_requests", request["request_id"])
        if saved:
            request.update(saved)
        if request.get("status") == "prepared" and not request.get("sent_at"):
            return {"unsent": True, "request": request}
        self.chats.reconcile(request)
        return {"submitted": True, "request": request}

    def observe(self, request):
        snapshot = self.driver.request_snapshot(request)
        if snapshot.get("matches") != 1:
            return {"matched": False}
        text = (snapshot.get("text") or "").strip()
        if text.strip("（）() ") == "暂无内容":
            text = ""
        return {"matched": True, "matches": 1, "finished": bool(snapshot.get("finished")),
                "answer": text, "error": snapshot.get("error"),
                "empty_placeholder": snapshot.get("empty_placeholder")}

    def persist_request(self, request):
        if not request.get("batch_id") and not request.get("sent_at"):
            self.chats.store.stage_save("chat_requests", request["request_id"], request)
            return
        self.chats.persist(request)

    def recover(self, request):
        saved = self.chats.store.stage_get("chat_requests", request["request_id"])
        if saved:
            request.update(saved)
        if request.get("conversation_id"):
            self.driver.restore_chat(request["conversation_id"])
        elif request.get("status") == "prepared" and not request.get("sent_at"):
            self.driver.new_chat()
            return {"unsent": True, "request": request}
        else:
            return self.reconcile(request)
        return self.reconcile(request)

    def cancel_observation(self):
        # Stopping local polling never claims the remote generation ended.
        return None


def classification_validator(identity, prompt_version=CLASSIFICATION_VERSION):
    """Strict ownership plus schema validation; reference conflict is a hard block."""

    def validate(answer, record):
        request_id = record["request"]["request_id"]
        result = parse_bound_answer(answer, "classify", identity, request_id)
        return {
            "classification": result.model_dump(),
            "prompt_version": prompt_version,
        }

    return validate


def classification_commit(store, identity, source, driver, *,
                          prompt_version=CLASSIFICATION_VERSION):
    """Commit result + completion + tutorial enqueue atomically and idempotently."""

    def finalize(record):
        from .storage import now

        classification = record["result"]["classification"]
        store.commit_classification(
            identity,
            {
                "status": "success",
                "source": source.model_dump(),
                "classification": classification,
                "conversation_id": getattr(driver, "conversation_id", None),
                "raw_directory": record["request"].get("raw_directory"),
                "classified_at": now(),
                "prompt_version": prompt_version,
            },
            record,
            enqueue_job=store.queue_job,
        )

    return finalize


def extraction_validator(identity):
    def validate(answer, record):
        from .prompts import EXTRACTION_VERSION

        result = parse_bound_answer(answer, "extract", identity, record["request"]["request_id"])
        return {"extraction": result.model_dump(), "prompt_version": EXTRACTION_VERSION}

    return validate


def extraction_commit(store, identity, source, driver):
    def finalize(record):
        from .errors import TaskError
        from .export import convert, cover_jpeg
        from .models import Extraction
        from .prompts import EXTRACTION_VERSION
        from .storage import atomic_write, now

        extraction = record["result"]["extraction"]
        if "cover_path" not in record:
            record["cover_path"] = None
            record["image_error"] = {"code": "image_missing", "message": "未提供封面"}
            if source.cover_url:
                try:
                    path = store.directory / "covers" / (identity + ".jpg")
                    atomic_write(path, cover_jpeg(driver.download_cover(source.cover_url)))
                    record.update(cover_path=str(path), image_error=None)
                except (TaskError, OSError, PlaywrightError) as exc:
                    record["image_error"] = {"code": "image_failed", "message": str(exc)}
        store.commit_extraction(identity, {
            "status": "success", "source": source.model_dump(), "extraction": extraction,
            "export_names": [
                r["name"] for r in convert(source, Extraction.model_validate(extraction))
            ],
            "conversation_id": getattr(driver, "conversation_id", None),
            "raw_directory": record["request"].get("raw_directory"),
            "classified_at": now(), "prompt_version": EXTRACTION_VERSION,
            "cover_path": record["cover_path"], "image_error": record["image_error"],
        }, record)

    return finalize

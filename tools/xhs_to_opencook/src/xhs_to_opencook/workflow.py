"""Persistent profile collection, classification, extraction and cumulative export."""

import os
import shutil
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta

from playwright.sync_api import Error as PlaywrightError

from . import runtime
from .browser import SiteBrowser
from .chat import ChatSession
from .errors import SESSION_BLOCK_CODES, BrowserUnsupported, TaskError
from .export import convert, cover_jpeg, estimates, write_outputs
from .links import note_id, read_profiles, validate_url
from .models import JSON_ERROR_CODES, Classification, Extraction, Source, ends_unfinished
from .prompts import (
    CLASSIFICATION_VERSION,
    EXTRACTION_VERSION,
)
from .storage import Store, atomic_write, now, state_lock, write_json


def classification_record(store, identity):
    record = store.stage_get("classifications", identity)
    if not record or record.get("status") != "success":
        return record, None
    try:
        result = Classification.model_validate(record["classification"])
        if result.note_id != identity:
            return None, None
        return record, result
    except (ValueError, KeyError, TypeError):
        return None, None


RECONCILE_DAYS = 30


def incremental_base(previous, notes, force_full=False, clock=None):
    """When the last complete pass began, if an incremental pass is allowed now; else None.

    A full pass is required for a first run, an unfinished one, a forced one, or when the last
    full pass is older than RECONCILE_DAYS (to catch deletions and edits the shortcut misses).
    """
    if force_full or not notes or previous.get("status") != "complete":
        return None
    full_at = previous.get("full_at") or (
        previous.get("finished_at") if previous.get("end_signal") == "user_posted.has_more=false"
        else None
    )
    if not full_at:
        return None
    age = (datetime.now(UTC) if clock is None else clock) - datetime.fromisoformat(full_at)
    return full_at if age < timedelta(days=RECONCILE_DAYS) else None


def collected_source(note, *records):
    """Build the submission source from collection and saved stage metadata."""
    identity = note["note_id"]
    metadata = {}
    for record in (*records, note):
        saved = (record or {}).get("source")
        if saved:
            if saved.get("note_id") != identity:
                raise TaskError("reference_mismatch", "缓存来源 ID 与采集笔记不同", retryable=False)
            metadata.update({k: v for k, v in saved.items() if v})
    metadata.update({k: note[k] for k in ("title", "author", "cover_url") if note.get(k)})
    url = note["url"]
    validate_url(url)
    if note_id(url) != identity:
        raise TaskError("reference_mismatch", "采集 URL 与笔记 ID 不同", retryable=False)
    return Source(
        note_id=identity,
        url=url,
        title=metadata.get("title") or f"标题未记录（{identity}）",
        author=metadata.get("author") or f"作者未记录（{note['profile_ids'][0]}）",
        cover_url=metadata.get("cover_url"),
    )


class Workflow:
    def __init__(self, args, store, profiles, browser_factory, sleeper, clock=time.time):
        self.args, self.store, self.profiles = args, store, profiles
        self.output = args.output.resolve()
        self.browser_factory, self.sleeper = browser_factory, sleeper
        self.browser = None
        self.last_submit = None
        self.chats = ChatSession(
            store,
            self.driver,
            reset_browser=self.close,
            capacity=getattr(args, "chat_batch_size", 5),
            max_attempts=getattr(args, "max_attempts", 2),
            interval=args.interval,
            timeout=args.timeout,
            sleeper=sleeper,
            clock=clock,
            stage_scope=getattr(args, "worker_stage", None),
            account_id=getattr(args, "account_id", None),
        )
        self.code = 0
        self.report = {
            "started_at": now(),
            "command": args.command,
            "status": "running",
            "tasks": [],
            "profiles": [],
        }

    def driver(self):
        if self.browser is None:
            self.browser = self.browser_factory(
                self.store.directory / "chrome",
                timeout=self.args.timeout,
                interactive=not self.args.non_interactive,
            ).__enter__()
        return self.browser

    def close(self):
        if self.browser:
            self.browser.__exit__(None, None, None)
            self.browser = None
            self.chats.loaded_batch = None

    def submit(self, prompt, source, *, repair=False):
        if self.last_submit is not None:
            self.sleeper(max(0, self.args.interval - (time.monotonic() - self.last_submit)))
        self.last_submit = time.monotonic()
        return self.driver().ask(prompt, source, repair=repair)

    def raw(self, identity, stage, attempt):
        directory = self.store.attempt_directory(identity, attempt) / stage
        directory.mkdir()
        return directory

    def archive(self, identity):
        origin = self.store.directory / "raw" / identity
        if origin.is_dir():
            shutil.copytree(origin, self.output / "raw" / identity, dirs_exist_ok=True)

    def record_error(self, task, error):
        failure = error if isinstance(error, TaskError) else TaskError("browser_error", str(error))
        task.update(status="failed", error={"code": failure.code, "message": str(failure)})
        if failure.code == "manual_review_required":
            task["status"] = "awaiting_review"
        self.code = max(self.code, 2)
        print(f"  {failure.code}: {failure}", file=sys.stderr, flush=True)
        if isinstance(failure, BrowserUnsupported) or failure.code in SESSION_BLOCK_CODES:
            raise failure

    def snapshot(self, *, bundle=False):
        notes = self.store.collected(list(self.profiles))
        profile_rows = []
        for identity, url in self.profiles.items():
            profile_rows.append(
                {
                    "profile_id": identity,
                    "url": url,
                    **(self.store.stage_get("profiles", identity) or {"status": "not_collected"}),
                }
            )
        videos = [n for n in notes if n.get("type") == "video"]
        reviews = self.chats.write_reviews(self.output, {n["note_id"] for n in videos})
        self.report.update(manual_review=reviews, manual_review_count=len(reviews))
        tutorials, records, states = [], [], []
        used = set()
        for note in videos:
            identity = note["note_id"]
            classified, decision = classification_record(self.store, identity)
            extraction_run = self.store.stage_get("extraction_runs", identity)
            record = self.store.get(identity)
            states.append(
                {
                    "note_id": identity,
                    "profile_ids": note["profile_ids"],
                    "classification": classified or {"status": "pending"},
                    "extraction": extraction_run
                    or {"status": record.get("status", "pending") if record else "pending"},
                }
            )
            if not decision or decision.verdict != "tutorial":
                continue
            tutorials.append(note["url"])
            if record and record.get("status") == "success":
                # Persist final import names across batches and legacy caches.
                changed = False
                for i, name in enumerate(record["export_names"]):
                    if name.casefold() in used:
                        record["export_names"][i] = f"{name} [{identity}]"
                        changed = True
                    used.add(record["export_names"][i].casefold())
                if changed:
                    self.store.save(identity, record)
                records.append(record)
        write_json(self.output / "collection.json", {"profiles": profile_rows, "notes": notes})
        atomic_write(
            self.output / "video_links.txt",
            ("\n".join(n["url"] for n in videos) + ("\n" if videos else "")).encode(),
        )
        atomic_write(
            self.output / "tutorial_links.txt",
            ("\n".join(tutorials) + ("\n" if tutorials else "")).encode(),
        )
        self.report.update(
            profiles=profile_rows,
            notes=states,
            video_count=len(videos),
            tutorial_count=len(tutorials),
            incomplete_profiles=[
                p["profile_id"] for p in profile_rows if p["status"] != "complete"
            ],
            unknown_type_count=sum(n.get("type") not in {"video", "normal"} for n in notes),
            chat_batches=self.chats.batches(),
            pending_requests=self.chats.pending_rows(),
            verification="format_and_reference_only",
            collection_verification="observed_pagination_end_only",
            classification_counts={
                key: sum(
                    (item["classification"].get("classification") or {}).get("verdict") == key
                    for item in states
                )
                for key in ("tutorial", "non_tutorial", "unknown")
            },
            pending_classification_count=sum(
                item["classification"].get("status") != "success" for item in states
            ),
            pending_extraction_count=len(tutorials) - len(records),
            extraction_success_count=sum(
                item["extraction"].get("status") == "success" for item in states
            ),
            recipe_count=sum(len(record["export_names"]) for record in records),
        )
        self.report["workflow_status"] = (
            "pending"
            if any(
                (
                    self.report["incomplete_profiles"],
                    self.report["unknown_type_count"],
                    self.report["pending_classification_count"],
                    self.report["classification_counts"]["unknown"],
                    self.report["pending_extraction_count"],
                )
            )
            else "complete"
        )
        if bundle:
            for identity in {n["note_id"] for n in notes}:
                self.archive(identity)
            write_outputs(self.output, records, self.report)
        else:
            recipes = [
                r
                for record in records
                for r in convert(
                    Source.model_validate(record["source"]),
                    Extraction.model_validate(record["extraction"]),
                    record["export_names"],
                )
            ]
            for record in records:
                source = record["source"]
                if source.get("cover_url"):
                    for recipe in recipes:
                        if recipe["name"] in record["export_names"]:
                            recipe["image"] = [source["cover_url"]]
            self.report["recipe_count"] = len(recipes)
            write_json(self.output / "recipes.json", recipes)
            write_json(self.output / "report.json", self.report)
        return records

    def collect(self):
        for identity, url in self.profiles.items():
            if runtime.current() and f"collect:{identity}" in runtime.current().items["seen"]:
                continue
            task = {"stage": "collect", "profile_id": identity, "status": "running"}
            self.report["tasks"].append(task)
            previous = self.store.stage_get("profiles", identity) or {}
            full_at = incremental_base(previous, self.store.collected([identity]),
                                       getattr(self.args, "full_collect", False))
            self.store.stage_save(
                "profiles", identity,
                {"url": url, "status": "running", "started_at": now(), "full_at": full_at},
            )
            print(f"采集博主 {identity}", flush=True)
            try:
                directory = self.store.directory / "raw" / identity / now().replace(":", "-")
                directory.mkdir(parents=True)
                self.driver().raw_directory = directory

                def save(batch, profile=identity):
                    for note in batch:
                        self.store.collect_note(profile, note)
                    self.snapshot()

                if runtime.current():
                    runtime.current().begin_item("collect", identity)
                if os.environ.get("XHS_RECOVERY_PROBE"):
                    try:
                        self.driver().probe()
                    except (TaskError, PlaywrightError) as error:
                        runtime.block(getattr(error, "code", "probe_failed"), str(error),
                                      kind=runtime.wait_kind(error))
                    os.environ.pop("XHS_RECOVERY_PROBE", None)
                attempt = 0
                while attempt < 6:
                    try:
                        runtime.operation("collect_profile", 35 * 60)
                        extra = (
                            {"known": {n["note_id"] for n in self.store.collected([identity])}}
                            if full_at else {}
                        )
                        result = self.driver().collect_profile(url, save, **extra)
                        break
                    except (TaskError, PlaywrightError) as error:
                        if runtime.current() and runtime.network_error(error):
                            runtime.block("network_unavailable", str(error))
                        if runtime.current() and getattr(error, "code", None) == "login_required":
                            runtime.current().wait_access(
                                lambda: self.driver().ensure_access(self.driver().page),
                                self.sleeper
                            )
                            continue
                        if isinstance(error, TaskError) and error.code in SESSION_BLOCK_CODES:
                            runtime.block(error.code, str(error))
                        if not self.chats.recoverable_page_error(error):
                            raise
                        if attempt == 5:
                            result = {"status": "incomplete", "reason": str(error)}
                            break
                        runtime.sleep(300 if attempt == 2 else 5, self.sleeper)
                        attempt += 1
                        self.close()
                task.update(result)
                if result["status"] != "complete":
                    self.code = max(self.code, 2)
                if result["status"] == "complete" and result.get("end_signal") != (
                    "incremental_known_run"
                ):
                    full_at = now()  # A full pass resets the 30-day reconciliation clock.
                self.store.stage_save(
                    "profiles", identity,
                    {"url": url, **result, "finished_at": now(),
                     "full_at": full_at if result["status"] == "complete" else None},
                )
                if runtime.current():
                    runtime.current().outcome("collect", identity,
                                              None if result["status"] == "complete"
                                              else "collection_incomplete")
                # Missing card types stay explicit; collection never opens note pages.
                for note in self.store.collected([identity]):
                    if note.get("type") in {"video", "normal"}:
                        continue
                    self.store.collect_note(identity, {
                        **note, "type": "unknown", "type_error": {
                            "code": "type_unknown", "message": "主页卡片未提供笔记类型",
                        },
                    })
                    self.code = max(self.code, 2)
            except (TaskError, PlaywrightError) as exc:
                if runtime.current() and isinstance(exc, TaskError) and exc.code in {
                    "runtime_wait", *SESSION_BLOCK_CODES
                }:
                    raise
                self.store.stage_save(
                    "profiles", identity, {"url": url, "status": "incomplete", "reason": str(exc)}
                )
                self.record_error(task, exc)
            except (KeyboardInterrupt, EOFError):
                self.store.stage_save(
                    "profiles", identity, {"url": url, "status": "incomplete", "reason": "用户中断"}
                )
                raise
            finally:
                self.snapshot()

    def process(self, stage, notes=None):
        runtime.operation(f"stage_{stage}", 300)
        if runtime.current():
            runtime.current().data["stage"] = stage
        if notes is None:
            notes = [
                n for n in self.store.collected(list(self.profiles)) if n.get("type") == "video"
            ]
        pending = self.chats.pending()
        if pending:
            if pending["stage"] != stage or pending["note_id"] not in {n["note_id"] for n in notes}:
                raise BrowserUnsupported("未决请求不在当前阶段或博主范围；请恢复原命令")
            notes.sort(key=lambda n: n["note_id"] != pending["note_id"])
        for index, note in enumerate(notes, 1):
            identity = note["note_id"]
            classified, decision = classification_record(self.store, identity)
            if runtime.current() and not pending \
                    and f"{stage}:{identity}" in runtime.current().items["seen"]:
                continue
            table = "classifications" if stage == "classify" else "extraction_runs"
            task = {"stage": stage, "note_id": identity, "status": "running", "attempts": 0}
            self.report["tasks"].append(task)
            if self.chats.review_for(stage, identity):
                if getattr(self.args, "retry_review", False):
                    self.chats.release_review(stage, identity)
                else:
                    task.update(status="awaiting_review", reason="manual_review_required")
                    self.code = max(self.code, 2)
                    continue
            cache = self.store.get(identity)
            committed = (decision and stage == "classify") or (
                stage == "extract" and cache and cache.get("status") == "success"
            )
            if committed and pending and pending["note_id"] == identity \
                    and pending["status"] == "validated":
                # A result commit can precede its request acknowledgement across a crash.
                self.chats.current = pending
                self.chats.accept()
                pending = None
                if runtime.current():
                    runtime.current().outcome(stage, identity)
            if (
                stage == "classify"
                and not (pending and pending["note_id"] == identity)
                and decision
                and decision.verdict != "unknown"
                and not getattr(
                    self.args,
                    "reclassify",
                    False,
                )
            ):
                task.update(status="cached", verdict=decision.verdict)
                continue
            if stage == "extract":
                if (not decision or decision.verdict != "tutorial") and not (
                    pending and pending["note_id"] == identity
                ):
                    task.update(status="skipped", reason="not_confirmed_tutorial")
                    continue
                if (
                    cache
                    and not (pending and pending["note_id"] == identity)
                    and cache.get("status") == "success"
                    and not getattr(
                        self.args,
                        "refresh",
                        False,
                    )
                ):
                    task.update(status="cached")
                    continue
            directory = None
            self.store.stage_save(table, identity, {"status": "running", "started_at": now()})
            print(f"[{index}/{len(notes)}] {stage} {identity}", flush=True)
            try:
                directory = self.raw(identity, stage, 1)
                source = (
                    Source.model_validate(pending["source"])
                    if pending and pending["note_id"] == identity
                    else collected_source(note, cache, classified)
                )
                if source.note_id != identity:
                    raise TaskError("reference_mismatch", "笔记 ID 发生变化")
                write_json(directory / "source.json", source.model_dump())
                self.store.alias(source.url, identity)
                for profile in note["profile_ids"]:
                    self.store.collect_note(
                        profile, {**note, "url": source.url, "source": source.model_dump()}
                    )
                active = runtime.current()
                if active:
                    active.begin_item(stage, identity)
                    if os.environ.get("XHS_RECOVERY_PROBE"):
                        try:
                            if self.chats.pending():
                                self.driver().ensure_access(self.driver().chat_page,
                                                            require_account=True)
                                self.driver().wait_editor_ready()
                            else:
                                self.driver().probe(source)
                        except (TaskError, PlaywrightError) as error:
                            runtime.block(getattr(error, "code", "probe_failed"), str(error),
                                          kind=runtime.wait_kind(error))
                        os.environ.pop("XHS_RECOVERY_PROBE", None)
                result = self.chats.process(source, stage, directory)
                task["attempts"] = self.chats.current["attempt"]
                driver = self.driver()
                common = {
                    "status": "success",
                    "source": source.model_dump(),
                    "conversation_id": driver.conversation_id,
                    "raw_directory": str(directory.relative_to(self.store.directory)),
                    "classified_at": now(),
                    "prompt_version": (
                        CLASSIFICATION_VERSION
                        if stage == "classify"
                        else EXTRACTION_VERSION
                    ),
                }
                if stage == "classify":
                    record = {
                        **common,
                        "classification": result.model_dump(),
                        "prompt_version": CLASSIFICATION_VERSION,
                    }
                    self.store.stage_save(table, identity, record)
                    task.update(verdict=result.verdict, reason=result.reason)
                    if result.verdict == "unknown":
                        self.code = max(self.code, 2)
                else:
                    cover, error = None, None
                    if source.cover_url:
                        try:
                            image = cover_jpeg(driver.download_cover(source.cover_url))
                            cover = self.store.directory / "covers" / f"{identity}.jpg"
                            atomic_write(cover, image)
                        except (TaskError, PlaywrightError, OSError) as exc:
                            error = {"code": "image_failed", "message": str(exc)}
                    else:
                        error = {"code": "image_missing", "message": "未提供封面"}
                    record = {
                        **common,
                        "extraction": result.model_dump(),
                        "export_names": [r["name"] for r in convert(source, result)],
                        "cover_path": str(cover) if cover else None,
                        "image_error": error,
                    }
                    self.store.save(identity, record)
                    self.store.stage_save(table, identity, common)
                    task.update(estimates=estimates(result), image_error=error)
                write_json(directory / "result.json", record)
                self.chats.accept()
                pending = None
                task["status"] = "success"
                if active:
                    active.data["probe_succeeded"] = True
                    active.outcome(stage, identity)
            except (TaskError, PlaywrightError) as exc:
                if isinstance(exc, TaskError) and exc.code == "runtime_wait":
                    raise
                failure = (
                    exc if isinstance(exc, TaskError) else TaskError("browser_error", str(exc))
                )
                pending = self.chats.pending()
                self.store.stage_save(
                    table,
                    identity,
                    {
                        "status": "failed",
                        "error": {"code": failure.code, "message": str(failure)},
                        "raw_directory": str(directory.relative_to(self.store.directory))
                        if directory
                        else None,
                    },
                )
                task["attempts"] = getattr(self.chats, "last_attempt", 0)
                if directory:
                    write_json(directory / "error.json", {
                        "code": failure.code, "message": str(failure),
                    })
                self.record_error(task, failure)
                if runtime.current() and failure.code == "manual_review_required":
                    review = self.chats.review_for(stage, identity)
                    review_code = ((review.get("review_error") or {}).get("code")
                                   or "manual_review_required")
                    # A recipe longer than the reply ceiling is the video's problem, not the
                    # account's, so it must not trip the account-level circuit.
                    too_long = (stage == "extract" and review_code == "json_syntax_invalid"
                                and ends_unfinished(review.get("answer") or ""))
                    if too_long:
                        task["too_long"] = True
                    runtime.current().outcome(stage, identity, review_code, neutral=too_long,
                                              answered=review_code in JSON_ERROR_CODES)
                if failure.code != "manual_review_required" or pending:
                    raise BrowserUnsupported("当前视频未安全结束；保存现场并停止") from exc
            except (KeyboardInterrupt, EOFError):
                task["status"] = "interrupted"
                self.store.stage_save(
                    table,
                    identity,
                    {
                        "status": "interrupted",
                        "raw_directory": str(directory.relative_to(self.store.directory))
                        if directory
                        else None,
                    },
                )
                raise
            except Exception as exc:
                error = {"code": "unexpected_error", "type": type(exc).__name__,
                         "message": str(exc)}
                task.update(status="failed", error=error)
                self.store.stage_save(table, identity, {"status": "failed", "error": error})
                if directory:
                    write_json(directory / "unexpected-error.json", error)
                    if self.browser:
                        self.browser.diagnostics(directory / "diagnostics")
                raise
            finally:
                self.archive(identity)
                self.snapshot()
            if self.chats.pending():
                raise BrowserUnsupported("视频处理结束后仍有未决请求；禁止进入下一视频")


def run_workflow(args, *, browser_factory=SiteBrowser, sleeper=time.sleep, clock=time.time):
    if getattr(args, "execution", "serial") == "parallel":
        from .parallel import run_parallel
        return run_parallel(args)
    profiles = read_profiles(args.profiles)
    state, output = args.state_dir.resolve(), args.output.resolve()
    if output == state or output in state.parents or state in output.parents:
        raise TaskError("output_invalid", "输出目录与状态目录不能互相包含", retryable=False)
    with state_lock(state), ExitStack() as stack:
        store = Store(state)
        stack.callback(store.close)
        flow = Workflow(args, store, profiles, browser_factory, sleeper, clock)
        stack.callback(flow.close)
        try:
            if args.command not in {"collect", "pipeline"} and any(
                not store.stage_get("profiles", identity) for identity in profiles
            ):
                raise TaskError(
                    "collection_required", "请先采集所有指定博主的主页", retryable=False
                )
            if getattr(args, "skip_collect", False) and any(
                not store.stage_get("profiles", identity) for identity in profiles
            ):
                raise TaskError("collection_required", "缓存续跑前须有博主采集记录",
                                retryable=False)
            if args.command in {"collect", "pipeline"} and not getattr(args, "skip_collect", False):
                flow.collect()
            if args.command in {"classify", "pipeline"}:
                flow.process("classify")
            if args.command in {"extract", "pipeline"}:
                flow.process("extract")
            flow.report["status"] = "partial" if flow.code else "complete"
            if flow.report.get("manual_review_count"):
                flow.code = max(2, flow.code)
                flow.report["status"] = "partial"
        except (KeyboardInterrupt, EOFError):
            flow.report["status"] = "interrupted"
            flow.code = 130
        except Exception as exc:
            flow.report.update(
                status="blocked",
                error={
                    "code": exc.code if isinstance(exc, TaskError) else "runtime_error",
                    "message": str(exc),
                },
            )
            flow.code = 1
            if runtime.current() and runtime.current().data["status"] != "waiting":
                runtime.current().data["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            flow.report["finished_at"] = now()
            if runtime.current():
                runtime.current().data.update(operation="export_outputs",
                                              deadline=time.time() + 300)
            for export_attempt in range(3):
                try:
                    flow.snapshot(
                        bundle=args.command in {"extract", "pipeline", "export"}
                        or flow.code in {1, 130}
                    )
                    break
                except OSError:
                    if not runtime.current() or export_attempt == 2:
                        raise
                    # Local export retries never re-enter video processing.
                    time.sleep(1)
            if flow.code in (0, 2) and (flow.report.get("manual_review_count")
                                      or (args.command in {"pipeline", "export"}
                                          and flow.report.get("workflow_status") != "complete")):
                flow.code = 2
                flow.report["status"] = "partial"
                write_json(output / "report.json", flow.report)
            if flow.report["incomplete_profiles"] and flow.code == 0:
                flow.code = 2
                flow.report["status"] = "partial"
                write_json(output / "report.json", flow.report)
        print(f"已保存 {flow.report['recipe_count']} 道菜谱：{output}", flush=True)
        return flow.code

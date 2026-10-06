"""Persistent profile collection, classification, extraction and cumulative export."""

import shutil
import sys
import time
from contextlib import ExitStack

from playwright.sync_api import Error as PlaywrightError

from .browser import SiteBrowser
from .chat import ChatSession
from .errors import BrowserUnsupported, TaskError
from .export import convert, cover_jpeg, estimates, write_outputs
from .links import read_profiles
from .models import Classification, Extraction, Source
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


class Workflow:
    def __init__(self, args, store, profiles, browser_factory, sleeper):
        self.args, self.store, self.profiles = args, store, profiles
        self.output = args.output.resolve()
        self.browser_factory, self.sleeper = browser_factory, sleeper
        self.browser = None
        self.last_submit = None
        self.chats = ChatSession(
            store,
            self.driver,
            capacity=getattr(args, "chat_batch_size", 50),
            interval=args.interval,
            timeout=args.timeout,
            sleeper=sleeper,
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
        self.driver().raw_directory = directory
        return directory

    def archive(self, identity):
        origin = self.store.directory / "raw" / identity
        if origin.is_dir():
            shutil.copytree(origin, self.output / "raw" / identity, dirs_exist_ok=True)

    def record_error(self, task, error):
        failure = error if isinstance(error, TaskError) else TaskError("browser_error", str(error))
        task.update(status="failed", error={"code": failure.code, "message": str(failure)})
        self.code = max(self.code, 2)
        print(f"  {failure.code}: {failure}", file=sys.stderr, flush=True)
        if isinstance(failure, BrowserUnsupported) or failure.code == "login_required":
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
            task = {"stage": "collect", "profile_id": identity, "status": "running"}
            self.report["tasks"].append(task)
            self.store.stage_save(
                "profiles", identity, {"url": url, "status": "running", "started_at": now()}
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

                result = self.driver().collect_profile(url, save)
                task.update(result)
                if result["status"] != "complete":
                    self.code = max(self.code, 2)
                self.store.stage_save(
                    "profiles", identity, {"url": url, **result, "finished_at": now()}
                )
                # Detail verification uses a different page so profile scrolling is undisturbed.
                for note in self.store.collected([identity]):
                    if note.get("type") in {"video", "normal"}:
                        continue
                    try:
                        source = self.driver().open_source(note["url"])
                        self.store.collect_note(
                            identity, {**note, "type": "video", "source": source.model_dump()}
                        )
                    except TaskError as exc:
                        if isinstance(exc, BrowserUnsupported) or exc.code == "login_required":
                            raise
                        self.store.collect_note(
                            identity,
                            {
                                **note,
                                "type": "normal" if exc.code == "not_video" else "unknown",
                                "type_error": {"code": exc.code, "message": str(exc)},
                            },
                        )
                        if exc.code != "not_video":
                            self.code = max(self.code, 2)
            except (TaskError, PlaywrightError) as exc:
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
            table = "classifications" if stage == "classify" else "extraction_runs"
            task = {"stage": stage, "note_id": identity, "status": "running", "attempts": 0}
            self.report["tasks"].append(task)
            cache = self.store.get(identity)
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
                for attempt in (1, 2):
                    task["attempts"] = attempt
                    try:
                        driver = self.driver()
                        directory = self.raw(identity, stage, attempt)
                        source = (
                            Source.model_validate(pending["source"])
                            if pending and pending["note_id"] == identity
                            else driver.open_source(note["url"])
                        )
                        if source.note_id != identity:
                            raise TaskError("reference_mismatch", "笔记 ID 发生变化")
                        write_json(directory / "source.json", source.model_dump())
                        self.store.alias(source.url, identity)
                        for profile in note["profile_ids"]:
                            self.store.collect_note(
                                profile, {**note, "url": source.url, "source": source.model_dump()}
                            )
                        result = self.chats.process(source, stage, directory)
                        task["attempts"] = max(attempt, self.chats.current["attempt"])
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
                        break
                    except (TaskError, PlaywrightError) as exc:
                        task["attempts"] = max(
                            task["attempts"], getattr(self.chats, "last_attempt", 1)
                        )
                        failure = (
                            exc
                            if isinstance(exc, TaskError)
                            else TaskError("browser_error", str(exc))
                        )
                        if directory:
                            write_json(
                                directory / "error.json",
                                {"code": failure.code, "message": str(failure)},
                            )
                            self.driver().diagnostics(directory / "diagnostics")
                        if isinstance(exc, PlaywrightError) and "has been closed" in str(exc):
                            self.close()
                        if not failure.retryable or attempt == 2:
                            raise failure from None
                        print(f"  {failure.code}，重试一次", flush=True)
            except (TaskError, PlaywrightError) as exc:
                failure = (
                    exc if isinstance(exc, TaskError) else TaskError("browser_error", str(exc))
                )
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
                self.record_error(task, failure)
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
            finally:
                self.archive(identity)
                self.snapshot()


def run_workflow(args, *, browser_factory=SiteBrowser, sleeper=time.sleep):
    profiles = read_profiles(args.profiles)
    state, output = args.state_dir.resolve(), args.output.resolve()
    if output == state or output in state.parents or state in output.parents:
        raise TaskError("output_invalid", "输出目录与状态目录不能互相包含", retryable=False)
    with state_lock(state), ExitStack() as stack:
        store = Store(state)
        stack.callback(store.close)
        flow = Workflow(args, store, profiles, browser_factory, sleeper)
        stack.callback(flow.close)
        try:
            if args.command not in {"collect", "pipeline"} and any(
                not store.stage_get("profiles", identity) for identity in profiles
            ):
                raise TaskError(
                    "collection_required", "请先采集所有指定博主的主页", retryable=False
                )
            if args.command in {"collect", "pipeline"}:
                flow.collect()
            if args.command in {"classify", "pipeline"}:
                flow.process("classify")
            if args.command in {"extract", "pipeline"}:
                flow.process("extract")
            flow.report["status"] = "partial" if flow.code else "complete"
        except (KeyboardInterrupt, EOFError):
            flow.report["status"] = "interrupted"
            flow.code = 130
        except (TaskError, PlaywrightError, OSError) as exc:
            flow.report.update(
                status="blocked",
                error={
                    "code": exc.code if isinstance(exc, TaskError) else "runtime_error",
                    "message": str(exc),
                },
            )
            flow.code = 1
        finally:
            flow.report["finished_at"] = now()
            flow.snapshot(
                bundle=args.command in {"extract", "pipeline", "export"} or flow.code in {1, 130}
            )
            if flow.report["incomplete_profiles"] and flow.code == 0:
                flow.code = 2
                flow.report["status"] = "partial"
                write_json(output / "report.json", flow.report)
        print(f"已保存 {flow.report['recipe_count']} 道菜谱：{output}", flush=True)
        return flow.code

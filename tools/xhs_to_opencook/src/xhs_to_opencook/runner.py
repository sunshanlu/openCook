import shutil
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from .browser import SiteBrowser
from .chat import ChatSession
from .errors import SESSION_BLOCK_CODES, BrowserUnsupported, TaskError
from .export import convert, cover_jpeg, estimates, write_outputs
from .links import note_id, read_links, validate_url
from .models import Extraction, Source
from .prompts import EXTRACTION_VERSION
from .storage import Store, atomic_write, now, state_lock, write_json


def run(args, *, browser_factory=SiteBrowser, sleeper=time.sleep, clock=time.time) -> int:
    links = read_links(args.links)
    if not links:
        raise TaskError("input_empty", "链接文件中没有视频链接", retryable=False)
    output = args.output.resolve()
    state = args.state_dir.resolve()
    if output == state or output in state.parents or state in output.parents:
        raise TaskError("output_invalid", "输出目录与状态目录不能互相包含", retryable=False)
    report = {"started_at": now(), "status": "running", "tasks": []}
    results = []
    seen = set()
    seen_urls = set()
    browser = None
    exit_code = 0
    with state_lock(state), ExitStack() as stack:
        store = Store(state)
        stack.callback(store.close)

        def get_browser():
            nonlocal browser
            if browser is None:
                browser = stack.enter_context(
                    browser_factory(
                        state / "chrome",
                        timeout=args.timeout,
                        interactive=not args.non_interactive,
                    )
                )
            return browser

        def reset_browser():
            nonlocal browser
            if browser:
                browser.__exit__(None, None, None)
                browser = None

        chats = ChatSession(
            store,
            get_browser,
            reset_browser=reset_browser,
            capacity=getattr(args, "chat_batch_size", 5),
            max_attempts=getattr(args, "max_attempts", 2),
            interval=args.interval,
            timeout=args.timeout,
            sleeper=sleeper,
            clock=clock,
        )
        review_scope = {identity for url in links
                        if (identity := note_id(url) or store.resolve(url))}
        pending = chats.pending()
        if pending:
            if pending["stage"] != "extract":
                raise BrowserUnsupported("必须先恢复分类阶段未决请求")
            pending_url = pending["source"]["url"]
            if pending_url not in links and not any(
                note_id(u) == pending["note_id"] for u in links
            ):
                raise BrowserUnsupported("未决请求不在当前链接清单；请恢复原命令")
            links.sort(key=lambda u: note_id(u) != pending["note_id"] and u != pending_url)

        def archive_raw(record):
            relative = record.get("raw_directory")
            if relative:
                relative = Path(relative).parent
                original = state / relative
                destination = output / relative
                if original.is_dir():
                    shutil.copytree(original, destination, dirs_exist_ok=True)

        def accept(record):
            # Persist collision resolution, including when a cached recipe joins a new batch.
            used = {name.casefold() for item in results for name in item["export_names"]}
            names = record["export_names"]
            for i, name in enumerate(names):
                if name.casefold() in used:
                    names[i] = f"{name} [{record['source']['note_id']}]"
                used.add(names[i].casefold())
            store.save(record["source"]["note_id"], record)
            results.append(record)
            archive_raw(record)

        try:
            for index, url in enumerate(links, 1):
                task = {"input_url": url, "status": "running", "attempts": 0}
                report["tasks"].append(task)
                source = None
                attempt_dir = None
                latest_raw = None
                record = None
                identity = None
                print(f"[{index}/{len(links)}] 处理视频", flush=True)
                try:
                    validate_url(url)
                    if url in seen_urls:
                        task.update(status="skipped", reason="duplicate_link")
                        continue
                    seen_urls.add(url)
                    identity = note_id(url) or store.resolve(url)
                    if identity and chats.review_for("extract", identity):
                        if getattr(args, "retry_review", False):
                            chats.release_review("extract", identity)
                        else:
                            task.update(status="awaiting_review", note_id=identity,
                                        reason="manual_review_required")
                            seen.add(identity)
                            exit_code = 2
                            continue
                    if identity in seen:
                        task.update(status="skipped", reason="duplicate_note", note_id=identity)
                        continue
                    if identity:
                        try:
                            record = store.get(identity)
                            if record and record.get("status") == "success":
                                Source.model_validate(record["source"])
                                Extraction.model_validate(record["extraction"])
                        except (ValueError, KeyError):
                            record = None
                            task["cache_warning"] = "缓存校验失败，将重新处理"
                    if (
                        record
                        and record.get("status") == "success"
                        and not args.refresh
                        and not (pending and pending["note_id"] == identity)
                    ):
                        seen.add(identity)
                        accept(record)
                        task.update(
                            status="cached",
                            note_id=identity,
                            names=record["export_names"],
                            estimates=estimates(Extraction.model_validate(record["extraction"])),
                            image_error=record.get("image_error"),
                            conversation_id=record.get("conversation_id"),
                            raw_directory=f"raw/{identity}",
                        )
                        continue

                    failure = None
                    driver = get_browser()
                    source = (
                        Source.model_validate(pending["source"])
                        if pending
                        and (
                            note_id(url) == pending["note_id"]
                            or url == pending["source"]["url"]
                        )
                        else driver.open_source(url)
                    )
                    identity = source.note_id
                    review_scope.add(identity)
                    task["note_id"] = identity
                    task["source"] = source.model_dump()
                    store.alias(url, identity)
                    if chats.review_for("extract", identity):
                        if getattr(args, "retry_review", False):
                            chats.release_review("extract", identity)
                        else:
                            raise TaskError(
                                "manual_review_required", "当前视频等待人工审核",
                                retryable=False,
                            )
                    if identity in seen:
                        task.update(status="skipped", reason="duplicate_note")
                        continue
                    # A previously unknown short URL may resolve to an existing success.
                    resolved_cache = store.get(identity)
                    if (
                        resolved_cache
                        and resolved_cache.get("status") == "success"
                        and not args.refresh
                        and not (pending and pending["note_id"] == identity)
                    ):
                        Source.model_validate(resolved_cache["source"])
                        Extraction.model_validate(resolved_cache["extraction"])
                        record = resolved_cache
                        task["status"] = "cached"
                        seen.add(identity)
                        accept(record)
                        continue
                    attempt_dir = store.attempt_directory(identity, 1)
                    latest_raw = attempt_dir
                    write_json(attempt_dir / "source.json", source.model_dump())
                    driver.raw_directory = attempt_dir
                    extraction = chats.process(source, "extract", attempt_dir)
                    task["attempts"] = chats.current["attempt"]
                    image_path, image_error = None, None
                    if source.cover_url:
                        try:
                            image = cover_jpeg(driver.download_cover(source.cover_url))
                            image_path = state / "covers" / f"{identity}.jpg"
                            atomic_write(image_path, image)
                        except (TaskError, PlaywrightError, OSError) as exc:
                            image_error = {"code": "image_failed", "message": str(exc)}
                    else:
                        image_error = {"code": "image_missing", "message": "笔记未提供封面"}
                    # Refresh can change dish count/names; that is a new name-based import.
                    names = [r["name"] for r in convert(source, extraction)]
                    record = {
                        "status": "success",
                        "source": source.model_dump(),
                        "extraction": extraction.model_dump(),
                        "prompt_version": EXTRACTION_VERSION,
                        "export_names": names,
                        "cover_path": str(image_path) if image_path else None,
                        "image_error": image_error,
                        "conversation_id": driver.conversation_id,
                        "raw_directory": str(attempt_dir.relative_to(state)),
                    }
                    write_json(attempt_dir / "result.json", record)
                    store.save(identity, record)
                    chats.accept()
                    pending = None
                    task["status"] = "success"
                    if task["status"] == "skipped":
                        continue
                    if not record or record.get("status") != "success":
                        raise failure or TaskError("browser_error", "没有生成菜谱结果")
                    seen.add(identity)
                    accept(record)
                    task.update(
                        names=record["export_names"],
                        conversation_id=record.get("conversation_id"),
                        estimates=estimates(Extraction.model_validate(record["extraction"])),
                        image_error=record.get("image_error"),
                        raw_directory=f"raw/{identity}",
                    )
                    print(f"  {len(record['export_names'])} 道菜谱已保存", flush=True)
                except TaskError as exc:
                    pending = chats.pending()
                    task["attempts"] = getattr(chats, "last_attempt", 0)
                    task.update(status="failed", error={"code": exc.code, "message": str(exc)})
                    if exc.code == "manual_review_required":
                        task["status"] = "awaiting_review"
                    exit_code = 2
                    if identity:
                        seen.add(identity)
                        failed = {"status": "failed", "error": task["error"]}
                        if latest_raw:
                            failed["raw_directory"] = str(latest_raw.relative_to(state))
                            archive_raw(failed)
                            task["raw_directory"] = f"raw/{identity}"
                        if not (store.get(identity) or {}).get("status") == "success":
                            store.save(identity, failed)
                    print(f"  {exc.code}: {exc}", file=sys.stderr, flush=True)
                    if (isinstance(exc, BrowserUnsupported) or exc.code in SESSION_BLOCK_CODES
                            or pending):
                        report["status"] = "blocked"
                        exit_code = 1
                        break
                finally:
                    # Every completed item also produces usable partial outputs.
                    report.update(
                        chat_batches=chats.batches(), pending_requests=chats.pending_rows()
                    )
                    reviews = chats.write_reviews(output, review_scope)
                    report.update(manual_review=reviews, manual_review_count=len(reviews))
                    write_outputs(output, results, report)
            if report["status"] == "running":
                report["status"] = "partial" if exit_code else "complete"
        except (KeyboardInterrupt, EOFError):
            report["status"] = "interrupted"
            for task in report["tasks"]:
                if task["status"] == "running":
                    task["status"] = "interrupted"
            if latest_raw:
                interrupted = {
                    "status": "interrupted",
                    "raw_directory": str(
                        latest_raw.relative_to(state),
                    ),
                }
                archive_raw(interrupted)
                if identity and (store.get(identity) or {}).get("status") != "success":
                    store.save(identity, interrupted)
            exit_code = 130
        except (PlaywrightError, OSError) as exc:
            report["status"] = "blocked"
            report["error"] = {"code": "runtime_error", "message": str(exc)}
            for task in report["tasks"]:
                if task["status"] == "running":
                    task.update(status="failed", error=report["error"])
            exit_code = 1
        finally:
            report["finished_at"] = now()
            report["unprocessed_count"] = len(links) - len(report["tasks"])
            write_outputs(output, results, report)
    print(f"已导出 {sum(len(r['export_names']) for r in results)} 道菜谱：{output / 'recipes.zip'}")
    return exit_code

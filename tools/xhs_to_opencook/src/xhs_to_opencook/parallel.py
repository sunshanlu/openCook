"""Durable two-stage coordinator. Only this process publishes shared output files."""

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from playwright.sync_api import sync_playwright

from . import runtime
from .accounts import profile_path, require_bindings
from .errors import TaskError
from .links import read_profiles
from .notifications import Outbox, summary
from .parallel_browser import AttachedBrowser, ChromeOwner
from .storage import Store, state_lock, write_json
from .supervisor import (
    cleanup_children,
    owned_process,
    process_identity,
    redact,
    rotate,
    terminate_owned,
)
from .throttle import AccessThrottle
from .workflow import Workflow, classification_record

STAGES = ("classify", "extract")
TERMINAL = {"completed", "partial"}


def gated(browser, flow):
    """Count every page visit the browser makes against the account's access allowance."""
    browser.visit_gate = flow.chats.pacing
    return browser


def worker(directory, stage):
    directory = Path(directory)
    job = runtime.read_json(directory / "job.json")
    args = SimpleNamespace(**job["options"])
    args.state_dir, args.output, args.profiles = map(
        Path,
        (
            args.state_dir,
            args.output,
            args.profiles,
        ),
    )
    args.worker_stage = stage
    args.account_id = job.get("accounts", {}).get(stage, {}).get("account_id")
    args.non_interactive = True
    profiles = read_profiles(args.profiles)
    store = Store(args.state_dir)
    store.queue_job = job["job_id"] if stage == "classify" else None
    notes = [n for n in store.collected(list(profiles)) if n.get("type") == "video"]
    by_id = {n["note_id"]: n for n in notes}
    store.queue_notes = set(by_id)
    flow = Workflow(
        args,
        store,
        profiles,
        lambda profile, **kw: gated(
            AttachedBrowser(
                profile, directory=directory / stage, stage=stage, account_id=args.account_id, **kw
            ),
            flow,
        ),
        time.sleep,
    )
    flow.chats.pacing = AccessThrottle(store, args.account_id, **job.get("throttle", {}))
    # Workers publish no common files. Progress derives from the committed business DB.
    flow.snapshot = lambda **kw: None
    flow.archive = lambda identity: None
    active = runtime.start_worker()
    code, error = 1, None
    try:
        with state_lock(directory / stage):
            if stage == "classify":
                flow.process(stage, notes)
            else:
                while True:
                    runtime.check()
                    pending = flow.chats.pending()
                    identity = pending["note_id"] if pending else store.claim(job["job_id"], stage)
                    if identity:
                        if identity not in by_id:
                            raise TaskError(
                                "state_conflict", "提取任务不在原博主范围", retryable=False
                            )
                        before = len(flow.report["tasks"])
                        flow.process(stage, [by_id[identity]])
                        if flow.chats.pending():
                            raise TaskError("state_conflict", "未决提取尚未收尾", retryable=False)
                        if len(flow.report["tasks"]) == before:
                            store.finish_queue(job["job_id"], identity, "done")
                            continue
                        task = flow.report["tasks"][-1]
                        status = (
                            "review"
                            if task["status"] == "awaiting_review"
                            else ("skipped" if task["status"] == "skipped" else "done")
                        )
                        store.finish_queue(job["job_id"], identity, status)
                    else:
                        state = runtime.read_json(directory / "job.json")
                        if state.get("workers", {}).get("classify", {}).get("status") in TERMINAL:
                            break
                        if active:
                            active.data.pop("item", None)
                        runtime.operation("waiting_for_tutorial", 60)
                        runtime.sleep(1)
            code = flow.code
    except (KeyboardInterrupt, EOFError):
        code = 130
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if active:
            if getattr(exc, "code", None) in {
                "account_mismatch",
                "account_identity_unavailable",
                "login_required",
            }:
                active.data.update(
                    status="waiting",
                    reason=exc.code,
                    message=str(exc),
                    wait_kind="access" if exc.code == "login_required" else "manual",
                )
            import sqlite3

            active.data["error_scope"] = (
                "shared" if isinstance(exc, (OSError, sqlite3.Error)) else "local"
            )
        print(runtime.redact(error), file=sys.stderr, flush=True)
    finally:
        flow.close()
        if active:
            active.finish(code, error)
        store.close()
    return code


class ParallelCoordinator:
    def __init__(self, directory, *, clock=time.time, chrome_factory=ChromeOwner):
        self.directory, self.clock = Path(directory), clock
        self.job = runtime.read_json(self.directory / "job.json")
        configured = require_bindings(Path(self.job["state_dir"]))
        if any(
            self.job.get("accounts", {}).get(s, {}).get("account_id") != configured[s]["account_id"]
            for s in STAGES
        ):
            raise TaskError(
                "account_mismatch", "任务账号绑定与登录配置不符，保持停止", retryable=False
            )
        self.children = {}
        self.spawn = subprocess.Popen
        self.chromes = {}
        self.playwright = None
        self.chrome_factory = chrome_factory
        self.outbox = Outbox(self.directory)
        self.last_loop = clock()

        self.args = SimpleNamespace(**self.job["options"])
        for key in ("profiles", "state_dir", "output"):
            setattr(self.args, key, Path(getattr(self.args, key)))
        self.store = Store(self.args.state_dir)
        self.publisher = Workflow(
            self.args, self.store, read_profiles(self.args.profiles), AttachedBrowser, time.sleep
        )
        self.next_export = 0

    def throttle(self, stage):
        return AccessThrottle(
            self.store, self.job["accounts"][stage]["account_id"], **self.job.get("throttle", {})
        )

    def save(self):
        self.job.update(supervisor_pid=os.getpid(), supervisor_heartbeat_at=self.clock())
        self.job["queue"] = self.store.queue_counts(self.job["job_id"])
        self.job["throttle_progress"] = {s: self.throttle(s).state() for s in STAGES}
        for record in self.job["workers"].values():
            record["next_check_label"] = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record["next_check"]))
                if record.get("next_check")
                else "等待处理或运行中"
            )
        if self.job.get("next_check"):
            self.job["next_check_label"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.job["next_check"])
            )
        self.job["action_hint"] = (
            "处理问题后执行 resume"
            if self.job["status"] == "waiting" and not self.job.get("next_check")
            else "可用 status 查询两阶段进度"
        )
        write_json(self.directory / "job.json", self.job)

    def notify(self, key, progress=False):
        self.outbox.enqueue(key, summary(self.job, self.publisher.report), progress=progress)

    def publish(self, force=False):
        if not force and self.clock() < self.next_export:
            return
        self.next_export = self.clock() + 5
        try:
            self.publisher.report.update(
                status=self.job["status"],
                workers=self.job["workers"],
                queue=self.store.queue_counts(self.job["job_id"]),
            )
            records = self.publisher.snapshot()
            count = len(records)
            last = self.job.get("published_success", 0)
            if force or (count > last and (last == 0 or count - last >= 20)):
                self.publisher.snapshot(bundle=True)
                self.job["published_success"] = count
            self.job.pop("export_error", None)
        except OSError as error:
            self.job["export_error"] = redact(error)
            self.next_export = self.clock() + 5
        self.save()

    def launch(self, stage, probe=False):
        self.chromes[stage].ensure_window(stage)
        folder = self.directory / stage
        folder.mkdir(exist_ok=True)
        (folder / "worker.json").unlink(missing_ok=True)
        env = dict(
            os.environ,
            XHS_RUNTIME_DIR=str(folder),
            XHS_CONTROL_DIR=str(self.directory),
            XHS_JOB_ID=self.job["job_id"],
            XHS_PARALLEL_STAGE=stage,
            PYTHONUNBUFFERED="1",
        )
        env.pop("XHS_RECOVERY_PROBE", None)
        if probe:
            env["XHS_RECOVERY_PROBE"] = "1"
        child = self.spawn(
            [
                sys.executable,
                "-m",
                "xhs_to_opencook.parallel",
                "worker",
                str(self.directory),
                stage,
            ],
            cwd=self.job["cwd"],
            env=env,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        os.set_blocking(child.stdout.fileno(), False)
        self.children[stage] = child
        identity = None
        end = time.monotonic() + 2
        while child.poll() is None and time.monotonic() < end:
            following = process_identity(child.pid)
            if following and following == identity:
                break
            identity = following
            time.sleep(0.1)
        self.job["workers"][stage].update(
            pid=child.pid,
            identity=identity,
            status="running",
            started_at=self.clock(),
            probing=probe,
        )
        self.save()

    def retire(self, stage):
        record = self.job["workers"][stage]
        terminate_owned(record)
        child = self.children.pop(stage, None)
        if child:
            child.wait(timeout=10)
            self.drain(stage, child)
            child.stdout.close()
        cleanup_children(runtime.read_json(self.directory / stage / "worker.json"))
        record.pop("pid", None)
        record.pop("identity", None)

    def drain(self, stage, child):
        for _ in range(16):
            chunk = child.stdout.read(65536)
            if not chunk:
                break
            path = self.directory / f"{stage}.log"
            rotate(path)
            with path.open("ab") as stream:
                stream.write(redact(chunk.decode(errors="replace")).encode())

    def stop_workers(self):
        for stage in STAGES:
            self.retire(stage)

    def wait(self, stage, data):
        # Broadcast stop before waiting for either worker's shutdown.
        write_json(
            self.directory / "control.json", {"job_id": self.job["job_id"], "action": "pause"}
        )
        self.stop_workers()
        kind = data.get("wait_kind", "platform")
        count = self.job.get("wait_attempt", 0)
        delay = (
            300
            if kind == "access"
            else (
                (1800 if not count else 3600)
                if kind == "circuit"
                else (300, 900, 1800, 3600)[min(count, 3)]
            )
        )
        self.job.update(
            status="waiting",
            trigger_stage=stage,
            reason=data.get("reason"),
            message=data.get("message", "等待恢复"),
            wait_kind=kind,
            wait_attempt=count + 1,
            next_check=self.clock() + delay,
        )
        if kind == "manual" or (kind == "circuit" and self.job.get("probe_failures", 0) >= 3):
            self.job["next_check"] = None
        for record in self.job["workers"].values():
            if record["status"] not in TERMINAL and record["status"] != "blocked":
                record["status"] = "ready"
        write_json(
            self.directory / "control.json", {"job_id": self.job["job_id"], "action": "running"}
        )
        self.save()
        self.publish(force=True)
        self.notify(f"waiting:{self.job.get('wait_episode', 0)}:{self.job.get('wait_attempt', 0)}")

    def crash(self, stage, message):
        self.retire(stage)
        record = self.job["workers"][stage]
        restarts = [t for t in record.get("restarts", []) if self.clock() - t < 900]
        if len(restarts) >= 3:
            record.update(status="blocked", message=message)
            self.notify(f"blocked:{stage}:{self.job['job_id']}")
        else:
            record.update(status="ready", restarts=restarts + [self.clock()], message=message)

    def account_wait(self, stage, data):
        self.retire(stage)
        record = self.job["workers"][stage]
        kind = data.get("wait_kind", "platform")
        attempt = record.get("wait_attempt", 0)
        delay = (
            300
            if kind == "access"
            else (1800 if not attempt else 3600)
            if kind == "circuit"
            else 300
            if kind == "frozen"
            else (300, 900, 1800, 3600)[min(attempt, 3)]
        )
        record.update(
            status="waiting",
            reason=data.get("reason"),
            message=data.get("message", "账号等待恢复"),
            wait_kind=kind,
            wait_attempt=attempt + 1,
            next_check=self.clock() + delay,
        )
        if kind == "manual" or (kind == "circuit" and record.get("probe_failures", 0) >= 3):
            record["next_check"] = None
        if record.get("reason") == "platform_restricted":
            # Backoff freeze: probe again only after the doubled rest has elapsed.
            record["next_check"] = self.throttle(stage).freeze("platform_restricted")
        elif record.get("reason") == "pacing_frozen":
            record["next_check"] = self.throttle(stage).state()["frozen_until"]
        self.save()
        self.notify(f"waiting:{stage}:{record['wait_attempt']}:{self.job.get('wait_episode', 0)}")
        self.publish(force=True)

    def ensure_chrome(self, stage):
        owner = self.chromes.get(stage)
        if owner and owner.alive():
            return True
        record = self.job["workers"][stage]
        self.retire(stage)
        if owner:
            owner.close()
            recent = [t for t in record.get("chrome_restarts", []) if self.clock() - t < 900]
            if len(recent) >= 3:
                self.account_wait(
                    stage,
                    {
                        "reason": "browser_restart_exhausted",
                        "message": "本账号Chrome恢复次数耗尽",
                        "wait_kind": "manual",
                    },
                )
                return False
            record["chrome_restarts"] = recent + [self.clock()]
            record["status"] = "ready"
        if self.playwright is None:
            self.playwright = sync_playwright().start()
        try:
            self.chromes[stage] = self.chrome_factory(
                profile_path(self.args.state_dir, stage),
                self.directory / stage,
                playwright=self.playwright,
            ).open()
            return True
        except Exception as error:
            self.account_wait(
                stage,
                {"reason": "browser_unavailable", "message": str(error), "wait_kind": "manual"},
            )
            return False

    def probe(self, stage):
        if not self.ensure_chrome(stage):
            return
        owner = self.chromes[stage]
        owner.ensure_window(stage)
        with AttachedBrowser(
            profile_path(self.args.state_dir, stage),
            directory=self.directory / stage,
            stage=stage,
            playwright=self.playwright,
            account_id=self.job["accounts"][stage]["account_id"],
            timeout=self.args.timeout,
            interactive=False,
        ) as browser:
            browser.verify_account()
            pending = self.publisher.chats.pending(stage)
            if pending:
                from .models import Source

                browser.verify_reference(
                    Source.model_validate(pending["source"]), browser.wait_editor_ready()
                )
        self.launch(stage, probe=True)

    def step(self):
        now = self.clock()
        resumed = now - self.last_loop > 60
        self.last_loop = now
        control = runtime.read_json(self.directory / "control.json")
        action = control.get("action") if control.get("job_id") == self.job["job_id"] else None
        target = control.get("worker")
        if action in {"pause", "stop"}:
            self.stop_workers()
            for record in self.job["workers"].values():
                if record["status"] not in TERMINAL:
                    record["status"] = "paused"
            self.job["status"] = "stopped" if action == "stop" else "paused"
            self.publish(force=True)
            return action != "stop"
        if action == "resume":
            if target and self.job["status"] in {"stopped", "paused"}:
                for stage, record in self.job["workers"].items():
                    if stage != target and record["status"] not in TERMINAL:
                        record["status"] = "paused"
            if self.job["status"] not in {"completed", "partial", "notifications_pending"}:
                for stage, record in self.job["workers"].items():
                    if (
                        (not target or target == stage)
                        and record["status"] != "running"
                        and record["status"] not in TERMINAL
                    ):
                        self.throttle(stage).release()
                        record.update(
                            status="ready",
                            restarts=[],
                            wait_attempt=0,
                            probe_failures=0,
                            probing=False,
                            next_check=None,
                        )
                self.job.update(
                    status="running",
                    wait_attempt=0,
                    probe_failures=0,
                    reason=None,
                    message="从账号断点恢复",
                    wait_episode=self.job.get("wait_episode", 0) + 1,
                )
            write_json(
                self.directory / "control.json", {"job_id": self.job["job_id"], "action": "running"}
            )
        if self.job["status"] in {"completed", "partial", "notifications_pending"}:
            if self.job.get("export_error"):
                self.publish(force=True)
                return True
            self.outbox.flush(still_actionable=False)
            if self.outbox.data["pending"]:
                self.job["status"] = "notifications_pending"
                self.save()
                return True
            return False
        if self.job["status"] == "paused":
            self.save()
            self.outbox.flush(still_actionable=False)
            return True
        # This state is reserved for shared network/storage/coordinator faults.
        if self.job["status"] == "waiting":
            if self.job.get("next_check") and now >= self.job["next_check"]:
                self.job.update(status="running", reason=None)
                for record in self.job["workers"].values():
                    if record["status"] == "ready":
                        record["probing"] = True
            else:
                self.publish()
                self.outbox.flush()
                return True
        for stage in STAGES:
            record = self.job["workers"][stage]
            if record["status"] in TERMINAL | {"blocked", "paused"}:
                continue
            if record["status"] == "waiting":
                if not record.get("next_check") or now < record["next_check"]:
                    continue
                try:
                    self.probe(stage)
                except Exception as error:
                    record["probe_failures"] = record.get("probe_failures", 0) + 1
                    self.account_wait(
                        stage,
                        {
                            "reason": getattr(error, "code", "probe_failed"),
                            "message": str(error),
                            "wait_kind": record.get("wait_kind", "manual"),
                        },
                    )
                continue
            if not self.ensure_chrome(stage):
                continue
            if record["status"] == "ready":
                self.launch(stage, probe=record.get("probing", False))
            child = self.children.get(stage)
            if child:
                self.drain(stage, child)
            data = runtime.read_json(self.directory / stage / "worker.json")
            record["progress"] = data
            code = child.poll() if child else data.get("exit_code")
            if code == 1 and data.get("error_scope") == "shared":
                self.wait(
                    stage,
                    {
                        "reason": "storage_failure",
                        "message": data.get("error"),
                        "wait_kind": "manual",
                    },
                )
                return True
            if data.get("status") == "waiting":
                if record.get("probing"):
                    record["probe_failures"] = record.get("probe_failures", 0) + 1
                if data.get("reason") == "network_unavailable":
                    self.wait(stage, data)
                    return True
                self.account_wait(stage, data)
                continue
            if code is not None or not owned_process(record):
                self.retire(stage)
                if code in (0, 2):
                    record["status"] = "partial" if code else "completed"
                    self.notify(f"stage:{self.job['job_id']}:{stage}")
                elif code == 1 and data.get("error"):
                    if any(
                        x in data["error"]
                        for x in ("account_mismatch", "account_identity_unavailable")
                    ):
                        self.account_wait(
                            stage,
                            {
                                "reason": "account_mismatch",
                                "message": data["error"],
                                "wait_kind": "manual",
                            },
                        )
                    else:
                        record.update(status="blocked", message=data["error"])
                        self.notify(f"blocked:{stage}:{self.job['job_id']}")
                else:
                    self.crash(stage, "工作进程异常退出")
                continue
            if record.get("probing") and data.get("probe_succeeded"):
                record.update(probing=False, probe_failures=0, wait_attempt=0, reason=None)
            if resumed:
                record["grace_until"] = now + 60
            deadline = data.get("deadline", record["started_at"] + 60)
            stale = now - data.get("heartbeat_at", record["started_at"]) > 60
            if (stale or now > deadline) and now >= record.get("grace_until", 0):
                self.crash(stage, "心跳或操作截止时间超时")
        if all(r["status"] in TERMINAL for r in self.job["workers"].values()):
            self.publish(force=True)
            partial = self.publisher.report.get("workflow_status") != "complete" or any(
                r["status"] == "partial" for r in self.job["workers"].values()
            )
            self.job.update(
                status="partial" if partial else "completed",
                finished_at=now,
                message="自动处理已结束",
            )
            self.publish(force=True)
            self.notify(f"complete:{self.job['job_id']}")
        else:
            self.job.update(status="running", message="双账号处理；各阶段状态见workers")
            self.publish()
        if now - self.job.get("last_progress_notice", 0) >= 1800:
            self.notify("progress", progress=True)
            self.job["last_progress_notice"] = now
        self.outbox.flush()
        self.save()
        return True

    def run(self):
        try:
            with state_lock(self.args.state_dir), state_lock(self.directory):
                for stage in STAGES:
                    self.retire(stage)
                    record = self.job["workers"][stage]
                    if record["status"] == "running":
                        record["status"] = "ready"
                cleanup_children(runtime.read_json(self.directory / "browser-processes.json"))
                for stage in STAGES:
                    cleanup_children(
                        runtime.read_json(self.directory / stage / "browser-processes.json")
                    )
                while True:
                    try:
                        if not self.step():
                            break
                    except Exception as error:
                        self.wait(
                            "classify",
                            {
                                "reason": "coordinator_error",
                                "message": f"{type(error).__name__}: {error}",
                                "wait_kind": "manual",
                            },
                        )
                    time.sleep(1)
        except KeyboardInterrupt:
            self.stop_workers()
            self.job["status"] = "paused"
            self.publish(force=True)
        finally:
            self.stop_workers()
            for owner in self.chromes.values():
                owner.close()
            if self.playwright:
                self.playwright.stop()
            self.store.close()
        return (
            2
            if self.job["status"] == "partial"
            else (0 if self.job["status"] == "completed" else 130)
        )


def initialize(args, directory, *, job=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    options = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}
    for key in ("state_dir", "output", "profiles"):
        options[key] = str(Path(options[key]).resolve())
    value = {
        **(job or {}),
        "version": 3,
        "accounts": require_bindings(args.state_dir),
        "execution": "parallel",
        "job_id": (job or {}).get("job_id", uuid4().hex),
        "status": "ready",
        "state_dir": options["state_dir"],
        "output": options["output"],
        "cwd": (job or {}).get("cwd", str(Path.cwd())),
        "options": options,
        "workers": {s: {"status": "ready", "restarts": []} for s in STAGES},
        "created_at": time.time(),
        "stage": "classify + extract",
    }
    value["throttle"] = {
        k: v
        for k, v in (
            ("access_per_hour", getattr(args, "access_per_hour", None)),
            ("send_per_hour", getattr(args, "send_per_hour", None)),
            ("active_hours", getattr(args, "active_hours", None)),
        )
        if v is not None
    }
    with StoreContext(args.state_dir) as store:
        from .accounts import migrate_accounts

        migrate_accounts(store, value["accounts"]["classify"]["account_id"], value["accounts"])
        profiles = read_profiles(args.profiles)
        if any(not store.stage_get("profiles", p) for p in profiles):
            raise TaskError("collection_required", "请先采集所有指定博主", retryable=False)
        notes = store.collected(list(profiles))
        ids = []
        for note in notes:
            _, decision = classification_record(store, note["note_id"])
            if note.get("type") == "video" and decision and decision.verdict == "tutorial":
                ids.append(note["note_id"])
        if not getattr(args, "reclassify", False):
            store.seed_queue(value["job_id"], ids)
        # Incremental migration preserves per-stage budgets and failure outcomes.
        previous = runtime.read_json(directory / "items.json")
        for stage in STAGES:
            folder = directory / stage
            folder.mkdir(exist_ok=True)
            if previous and not (folder / "items.json").exists():
                write_json(
                    folder / "items.json",
                    {
                        "budgets": {
                            k: v
                            for k, v in previous.get("budgets", {}).items()
                            if k.startswith(stage + ":")
                        },
                        "outcomes": [
                            v
                            for v in previous.get("outcomes", [])
                            if v["key"].startswith(stage + ":")
                        ],
                        "seen": [k for k in previous.get("seen", []) if k.startswith(stage + ":")],
                    },
                )
    write_json(directory / "job.json", value)
    write_json(directory / "control.json", {"job_id": value["job_id"], "action": "running"})
    return value


class StoreContext(Store):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def run_parallel(args):
    state, output = args.state_dir.resolve(), args.output.resolve()
    if output == state or output in state.parents or state in output.parents:
        raise TaskError("output_invalid", "输出目录与状态目录不能互相包含", retryable=False)
    directory = state / "supervision"
    job = runtime.read_json(directory / "job.json")
    if job and job.get("status") not in {"completed", "partial", "stopped"}:
        raise TaskError("state_busy", "已有任务，请使用 status/pause/resume/stop", retryable=False)
    if not getattr(args, "skip_collect", False):
        from .workflow import run_workflow

        collect_args = SimpleNamespace(**vars(args))
        collect_args.command, collect_args.execution = "collect", "serial"
        if run_workflow(collect_args) not in (0, 2):
            return 1
    with state_lock(state):
        initialize(args, directory)
    return ParallelCoordinator(directory).run()


if __name__ == "__main__":
    if sys.argv[1] == "worker":
        sys.exit(worker(sys.argv[2], sys.argv[3]))
    sys.exit(ParallelCoordinator(Path(sys.argv[1])).run())

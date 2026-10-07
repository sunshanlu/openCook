"""Single-worker supervision in the current macOS GUI session."""

import hashlib
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from .notifications import Outbox, summary
from .runtime import read_json, redact
from .storage import state_lock, write_json

WORK_COMMANDS = {"run", "pipeline", "collect", "classify", "extract", "export"}


def process_identity(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,lstart=,command="],
                            capture_output=True, text=True, timeout=5)
    value = result.stdout.strip()
    return value.split(maxsplit=1)[1] \
        if result.returncode == 0 and value and not value.startswith("Z") else None


def owned_process(record):
    return bool(record.get("pid") and record.get("identity")
                and process_identity(record["pid"]) == record["identity"])


def rotate(path, limit=20 * 1024 * 1024):
    if not path.exists() or path.stat().st_size < limit:
        return
    oldest = path.with_suffix(".log.5")
    oldest.unlink(missing_ok=True)
    for number in range(4, 0, -1):
        source = path.with_suffix(f".log.{number}")
        if source.exists():
            source.rename(path.with_suffix(f".log.{number + 1}"))
    path.rename(path.with_suffix(".log.1"))


def terminate_owned(record, *, grace=30):
    if not owned_process(record):
        return
    pid = record["pid"]
    # The worker owns a new session; never kill an unrelated process group.
    if os.getpgid(pid) != pid:
        raise RuntimeError("工作进程组身份不符，禁止清理")
    os.kill(pid, signal.SIGINT)
    end = time.monotonic() + grace
    while owned_process(record) and time.monotonic() < end:
        time.sleep(0.25)
    if owned_process(record):
        os.killpg(pid, signal.SIGKILL)
    end = time.monotonic() + 10
    while owned_process(record) and time.monotonic() < end:
        time.sleep(0.1)
    if owned_process(record):
        raise RuntimeError("旧工作进程未退出")


def cleanup_children(worker):
    """Only descendants journaled by this worker, with unchanged startup identity."""
    for child in reversed(worker.get("children", [])):
        if owned_process(child):
            try:
                os.kill(child["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
    end = time.monotonic() + 5
    while time.monotonic() < end and any(owned_process(c) for c in worker.get("children", [])):
        time.sleep(0.1)
    for child in worker.get("children", []):
        if owned_process(child):
            try:
                os.kill(child["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


class Supervisor:
    def __init__(self, directory, *, clock=time.time):
        self.directory, self.clock = Path(directory), clock
        self.path = self.directory / "job.json"
        self.job = read_json(self.path)
        self.outbox = Outbox(self.directory)
        self.child = None
        self.last_loop = clock()

    def save(self):
        self.job["supervisor_pid"] = os.getpid()
        self.job["supervisor_heartbeat_at"] = self.clock()
        write_json(self.path, self.job)

    def report(self):
        return read_json(Path(self.job["output"]) / "report.json")

    def notify(self, key, *, progress=False):
        self.outbox.enqueue(key, summary(self.job, self.report()), progress=progress)

    def wait(self, reason, message, kind):
        self.job.update(status="waiting", reason=reason, message=redact(message), wait_kind=kind)
        if kind == "manual":
            self.job.update(next_check=None, action_hint="处理问题后执行 resume")
        elif kind == "circuit":
            count = self.job.get("probe_failures", 0)
            if count >= 3:
                return self.wait(reason, message, "manual")
            self.job.update(next_check=self.clock() + (1800 if not count else 3600),
                            action_hint="程序将检查恢复；持续失败会等待人工")
        else:
            attempt = self.job.get("wait_attempt", 0)
            delay = 300 if kind == "access" else (300, 900, 1800, 3600)[min(attempt, 3)]
            self.job.update(next_check=self.clock() + delay, wait_attempt=attempt + 1,
                            action_hint="请处理登录或验证码，完成后程序自动继续"
                            if kind == "access" else "等待服务恢复，无需重启")
        self.job["next_check_label"] = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.job["next_check"]))
            if self.job.get("next_check") else "等待人工确认"
        )
        self.save()
        self.notify(f"waiting:{reason}:{self.job.get('wait_episode', 0)}")

    def launch(self, *, probe=False):
        try:
            with state_lock(Path(self.job["state_dir"])):
                pass
        except Exception as error:
            self.wait("state_unavailable", str(error), "manual")
            return
        self.directory.joinpath("worker.json").unlink(missing_ok=True)
        command = [sys.executable, "-m", "xhs_to_opencook.cli", *self.job["argv"]]
        if "--non-interactive" not in command:
            command.append("--non-interactive")
        env = dict(os.environ, XHS_RUNTIME_DIR=str(self.directory), XHS_JOB_ID=self.job["job_id"],
                   PYTHONUNBUFFERED="1")
        if probe:
            env["XHS_RECOVERY_PROBE"] = "1"
        self.child = subprocess.Popen(command, cwd=self.job["cwd"], env=env,
                                      start_new_session=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT)
        time.sleep(0.1)
        os.set_blocking(self.child.stdout.fileno(), False)
        identity = process_identity(self.child.pid)
        self.job.update(worker={"pid": self.child.pid, "identity": identity}, status="running",
                        started_worker_at=self.clock(), probing=probe,
                        message="恢复检查中" if probe else "正常处理", action_hint="无需操作")
        self.save()

    def drain_log(self):
        if not self.child or not self.child.stdout:
            return
        path = self.directory / "runtime.log"
        # Bound each iteration so verbose logging cannot starve supervision.
        for _ in range(16):
            chunk = self.child.stdout.read(65536)
            if not chunk:
                break
            rotate(path)
            with path.open("ab") as stream:
                stream.write(redact(chunk.decode(errors="replace")).encode())

    def crash(self, message):
        restarts = [t for t in self.job.get("restarts", []) if self.clock() - t < 900]
        if len(restarts) >= 3:
            self.wait("restart_exhausted", message, "manual")
            return
        restarts.append(self.clock())
        self.job.update(restarts=restarts, status="ready", message=message)
        self.save()

    def complete_worker(self, code, worker):
        self.job.pop("worker", None)
        if self.child and self.child.stdout:
            self.child.stdout.close()
        self.child = None
        if code is not None and code < 0:
            self.crash("工作进程异常退出")
            return
        if worker.get("status") == "waiting":
            if worker.get("wait_kind") == "access" and code == 130:
                self.job["status"] = "paused"
                self.save()
                return
            kind = worker.get("wait_kind", "manual")
            if self.job.get("probing"):
                self.job["probe_failures"] = self.job.get("probe_failures", 0) + 1
            self.wait(worker.get("reason", "worker_blocked"),
                      worker.get("message", "工作进程等待处理"), kind)
        elif code in (0, 2):
            self.job.update(status="partial" if code else "completed", finished_at=self.clock(),
                            message="自动处理已结束；仍有缺口" if code else "自动处理已完成")
            self.save()
            self.notify(f"complete:{self.job['job_id']}")
        elif code == 130:
            self.job["status"] = "paused"
            self.save()
        elif worker.get("status") == "finished" and (worker.get("error") or code == 1):
            self.wait("runtime_error", worker.get("error", "程序或状态异常，请检查日志"), "manual")
        else:
            self.crash("工作进程退出但未保存最终状态")

    def step(self):
        now = self.clock()
        # After machine suspension re-check process/journal before enforcing old deadlines.
        resumed = now - self.last_loop > 60
        self.last_loop = now
        control = read_json(self.directory / "control.json")
        action = control.get("action") if control.get("job_id") == self.job["job_id"] else None
        if action == "resume":
            if self.job["status"] in {"paused", "waiting", "stopped"}:
                self.job.update(status="ready", probe_failures=0, wait_attempt=0, restarts=[],
                                wait_episode=self.job.get("wait_episode", 0) + 1)
            write_json(self.directory / "control.json", {"job_id": self.job["job_id"],
                                                         "action": "running"})
        if now - self.job.get("last_progress_notice", 0) >= 1800 \
                and self.job["status"] in {"waiting", "waiting_access", "paused"}:
            self.notify("progress", progress=True)
            self.job["last_progress_notice"] = now
        if action in {"pause", "stop"}:
            if self.job.get("worker"):
                terminate_owned(self.job["worker"])
                if self.child:
                    self.child.wait(timeout=10)
                self.job.pop("worker", None)
                self.child = None
                cleanup_children(read_json(self.directory / "worker.json"))
            self.job["status"] = "stopped" if action == "stop" else "paused"
            self.save()
            return action != "stop"
        if self.job["status"] in {"completed", "partial", "notifications_pending"}:
            self.outbox.flush(still_actionable=False)
            if self.outbox.data["pending"]:
                self.job["status"] = "notifications_pending"
                self.save()
                return True
            return False
        if self.job["status"] == "paused":
            self.outbox.flush(still_actionable=False)
            self.save()
            return True
        if self.job["status"] == "waiting":
            self.outbox.flush()
            if self.job.get("next_check") and now >= self.job["next_check"]:
                self.launch(probe=True)
            self.save()
            return True
        if self.job["status"] == "ready":
            self.launch()
        self.drain_log()
        worker = read_json(self.directory / "worker.json")
        record = self.job.get("worker", {})
        alive = owned_process(record)
        code = self.child.poll() if self.child else None
        if not alive or code is not None:
            if self.child:
                self.drain_log()
            cleanup_children(worker)
            self.complete_worker(code if code is not None else worker.get("exit_code"), worker)
            return True
        if worker.get("job_id") == self.job["job_id"]:
            if resumed:
                self.job["resume_grace_until"] = now + 60
            if worker.get("status") == "waiting" and worker.get("wait_kind") == "access":
                self.job.update(status="waiting_access", message=worker["message"],
                                reason="login_required",
                                action_hint="请在专用Chrome完成登录或验证码，随后自动续跑")
                self.notify(f"waiting:login:{self.job.get('wait_episode', 0)}")
            elif worker.get("status") == "running" and self.job.get("reason") == "login_required":
                self.job.update(status="running", message="登录恢复，继续处理", reason=None,
                                action_hint="无需操作")
                self.job["wait_episode"] = self.job.get("wait_episode", 0) + 1
            if worker.get("stage") and worker["stage"] != self.job.get("stage"):
                previous = self.job.get("stage")
                self.job["stage"] = worker["stage"]
                if previous:
                    self.notify(f"stage:{self.job['job_id']}:{previous}")
            if self.job.get("probing") and worker.get("probe_succeeded"):
                self.job.update(probing=False, probe_failures=0, wait_attempt=0)
            stale = now - worker.get("heartbeat_at", now) > 60
            deadline = worker.get("deadline")
            expired = deadline is not None and now > deadline
            if (stale or expired) and now >= self.job.get("resume_grace_until", 0):
                terminate_owned(record)
                if self.child:
                    self.child.wait(timeout=10)
                self.job.pop("worker", None)
                self.child = None
                cleanup_children(worker)
                self.crash("工作进程心跳或操作截止时间超时")
        elif now - self.job.get("started_worker_at", now) > 60:
            terminate_owned(record)
            if self.child:
                self.child.wait(timeout=10)
            self.child = None
            self.job.pop("worker", None)
            self.crash("工作进程未建立心跳")
        if now - self.job.get("last_progress_notice", 0) >= 1800:
            self.notify("progress", progress=True)
            self.job["last_progress_notice"] = now
        self.outbox.flush(still_actionable=self.job["status"] in {"waiting", "waiting_access"})
        self.save()
        return True

    def run(self):
        with state_lock(self.directory):
            if self.job.get("worker") and owned_process(self.job["worker"]):
                # Recover supervisor crash: retire the owned orphan before replacement.
                terminate_owned(self.job["worker"])
                self.job.pop("worker", None)
                if self.job["status"] in {"running", "waiting_access"}:
                    self.job["status"] = "ready"
            elif self.job["status"] in {"running", "waiting_access"}:
                self.job["status"] = "ready"
            cleanup_children(read_json(self.directory / "worker.json"))
            while self.step():
                time.sleep(1)
            self.save()
            rotate(self.directory / "supervisor.log")


def launchd(directory, job):
    label = job["label"]
    domain = f"gui/{os.getuid()}"
    plist = directory / "supervisor.plist"
    data = {
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "xhs_to_opencook.supervisor", str(directory)],
        "WorkingDirectory": job["cwd"],
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        "KeepAlive": {"SuccessfulExit": False}, "RunAtLoad": True, "ThrottleInterval": 30,
        "StandardOutPath": str(directory / "supervisor.log"),
        "StandardErrorPath": str(directory / "supervisor.log"),
    }
    plist.write_bytes(plistlib.dumps(data))
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True,
                   capture_output=True, text=True)


def command(argv, state_default, *, parser):
    action = argv[0]
    if action == "supervise":
        worker_argv = argv[1:]
        if not worker_argv or worker_argv[0] not in WORK_COMMANDS:
            parser.error("supervise 后需指定 run/pipeline/collect/classify/extract/export")
        args = parser.parse_args(worker_argv)
        state = args.state_dir.resolve()
    else:
        import argparse
        controls = argparse.ArgumentParser(prog=f"xhs-to-opencook {action}")
        controls.add_argument("--state-dir", type=Path, default=state_default)
        state = controls.parse_args(argv[1:]).state_dir.resolve()
    directory = state / "supervision"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "job.json"
    job = read_json(path)
    if action == "status":
        identity = process_identity(job.get("supervisor_pid", 0))
        print(json.dumps({**job, "supervisor_alive": bool(identity and str(directory) in identity),
                          "worker_alive": owned_process(job.get("worker", {}))},
                         ensure_ascii=False, indent=2))
        return 0
    if action in {"pause", "stop", "resume"}:
        if not job:
            parser.error("没有已托管任务")
        if action == "resume" and job["status"] in {
            "completed", "partial", "notifications_pending"
        }:
            print("本批自动处理已结束，请用 supervise 显式启动新任务。")
            return 0
        if action == "resume":
            old_control = read_json(directory / "control.json")
            if job["status"] in {"running", "waiting_access"} \
                    and old_control.get("action") not in {"pause", "stop"}:
                print("任务正在运行，无需恢复。")
                return 0
        write_json(directory / "control.json", {"job_id": job["job_id"], "action": action})
        if action == "resume":
            supervisor_identity = process_identity(job.get("supervisor_pid", 0))
            if supervisor_identity and str(directory) in supervisor_identity:
                print("已请求 resume，监督器将从原断点继续。")
                return 0
            # A stopped GUI job needs an explicit restart, never an automatic one.
            domain = f"gui/{os.getuid()}/{job['label']}"
            result = subprocess.run(["launchctl", "kickstart", "-k", domain],
                                    capture_output=True, text=True)
            if result.returncode:
                launchd(directory, job)
        print(f"已请求 {action}，使用 status 查看结果。")
        return 0
    if job and job.get("status") not in {"completed", "partial", "stopped"}:
        parser.error("已有任务，请先使用 status/pause/resume/stop")
    with state_lock(state), state_lock(directory):
        label = "com.opencook.xhs." + hashlib.sha256(str(state).encode()).hexdigest()[:12]
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                       capture_output=True, text=True)
        job = {"version": 1, "job_id": uuid4().hex, "label": label, "status": "ready",
               "argv": worker_argv, "state_dir": str(state), "output": str(args.output.resolve()),
               "cwd": str(Path.cwd()), "created_at": time.time(), "last_progress_notice": 0,
               "probe_failures": 0, "restarts": []}
        # Previous jobs retain their attempt/time journals; a new job gets a new item scope.
        if (directory / "items.json").exists():
            (directory / "items.json").rename(directory / f"items-{job['job_id']}.json")
        for name in ("worker.json", "control.json"):
            (directory / name).unlink(missing_ok=True)
        write_json(path, job)
        launchd(directory, job)
    print(f"已启动托管任务 {job['job_id']}；用 status/pause/resume/stop 控制。")
    return 0


if __name__ == "__main__":
    Supervisor(Path(sys.argv[1])).run()

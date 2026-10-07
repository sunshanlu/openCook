"""Worker-side checkpoints. Business state remains in Store, never in this journal."""

import json
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import TaskError
from .storage import write_json


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {} if default is None else default


def redact(value):
    return re.sub(
        r'(?i)((?:xsec_token|access_token|authorization|cookie|password|secret)'
        r'[\s"\x27:=]+)[^\s&"\x27<>]+', r'\1<REDACTED>', str(value)
    )


def network_error(error):
    cause = error
    while cause:
        if any(text in str(cause) for text in (
            "net::ERR_", "ECONNRESET", "ECONNREFUSED", "ENOTFOUND", "Connection refused"
        )):
            return True
        cause = cause.__cause__
    return False


def wait_kind(error):
    code = getattr(error, "code", None)
    if code == "login_required":
        return "access"
    if code == "platform_restricted" or network_error(error):
        return "platform"
    return "circuit"


class WorkerRuntime:
    def __init__(self, directory, job_id, *, clock=time.time, monotonic=time.monotonic):
        self.directory = Path(directory)
        self.job_id, self.clock, self.monotonic = job_id, clock, monotonic
        self.path = self.directory / "worker.json"
        self.items_path = self.directory / "items.json"
        self.items = read_json(self.items_path, {"budgets": {}, "outcomes": [], "seen": []})
        self.data = {"job_id": job_id, "pid": os.getpid(), "status": "running",
                     "operation": "starting", "deadline": clock() + 60}
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.last_tick = monotonic()
        self.last_wall = clock()
        self.thread = None
        self.probing = os.environ.get("XHS_RECOVERY_PROBE") == "1"

    def start(self):
        self.tick()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()
        return self

    def _heartbeat(self):
        while not self.closed.wait(10):
            try:
                self.register_children()
                self.tick()
            except (OSError, subprocess.TimeoutExpired):
                # Surface journal failures on the main thread; do not silently lose control.
                self.data["journal_failed"] = True

    def register_children(self):
        result = subprocess.run(["ps", "-axo", "pid=,ppid=,stat=,lstart=,command="],
                                capture_output=True, text=True, timeout=5)
        rows = []
        for line in result.stdout.splitlines():
            fields = line.strip().split(maxsplit=3)
            if len(fields) == 4 and fields[0].isdigit() and fields[1].isdigit():
                rows.append((int(fields[0]), int(fields[1]), fields[2], fields[3]))
        owned = {os.getpid()}
        descendants = []
        changed = True
        while changed:
            changed = False
            for pid, parent, state, identity in rows:
                if parent in owned and pid not in owned and not state.startswith("Z"):
                    owned.add(pid)
                    descendants.append({"pid": pid, "identity": identity})
                    changed = True
        with self.lock:
            self.data["children"] = descendants

    def tick(self):
        with self.lock:
            current = self.monotonic()
            elapsed = current - self.last_tick
            self.last_tick = current
            wall_elapsed = self.clock() - self.last_wall
            self.last_wall = self.clock()
            suspension = wall_elapsed - elapsed
            if suspension > 30 and self.data.get("deadline"):
                self.data["deadline"] += suspension
            key = self.data.get("item")
            # Long monotonic gaps are machine suspension, not active processing time.
            if key and 0 <= elapsed <= 30:
                self.items["budgets"][key] = self.items["budgets"].get(key, 0) + elapsed
                write_json(self.items_path, self.items)
            self.data["heartbeat_at"] = self.clock()
            write_json(self.path, self.data)

    def check(self):
        control = read_json(self.directory / "control.json")
        if control.get("job_id") == self.job_id and control.get("action") in {"pause", "stop"}:
            raise KeyboardInterrupt()
        if self.data.get("journal_failed"):
            raise OSError("运行状态写入失败")
        key = self.data.get("item")
        if key and self.items["budgets"].get(key, 0) >= 35 * 60:
            raise TaskError("item_budget_exhausted", "单条主动处理已达到35分钟", retryable=False)

    def begin_item(self, stage, identity):
        self.check()
        self.tick()
        self.data.update(item=f"{stage}:{identity}", stage=stage)
        # The chat recovery owner must receive budget exhaustion, not its caller.
        self.data.update(operation="prepare", deadline=self.clock() + 120)
        self.tick()

    def operation(self, name, seconds):
        with self.lock:
            self.data.update(operation=name, deadline=self.clock() + seconds)
            self.tick()
        self.check()

    @contextmanager
    def waiting(self, seconds, name="cooling"):
        self.operation(name, seconds + 30)
        try:
            yield
        finally:
            self.operation("processing", 180)

    def outcome(self, stage, identity, code=None):
        key = f"{stage}:{identity}"
        with self.lock:
            self.tick()
            self.data.pop("item", None)
            if self.probing:
                self.probing = False
                if code:
                    self.items["seen"].append(key)
                    write_json(self.items_path, self.items)
                    self.block("probe_failed", "恢复试探仍失败，继续全局等待", kind="circuit")
                self.items["outcomes"] = []
                self.data["probe_succeeded"] = True
            if key in self.items["seen"]:
                return
            self.items["seen"].append(key)
            self.items["outcomes"] = (self.items["outcomes"] + [{"key": key, "code": code}])[-10:]
            write_json(self.items_path, self.items)
        recent = self.items["outcomes"]
        same = len(recent) >= 3 and code and all(x["code"] == code for x in recent[-3:])
        if same or (len(recent) == 10 and sum(bool(x["code"]) for x in recent) >= 5):
            self.block("circuit_open", "连续失败，等待恢复检查", kind="circuit")

    def wait_access(self, checker, sleeper=time.sleep):
        item = self.data.pop("item", None)
        self.data.update(status="waiting", reason="login_required", wait_kind="access",
                         message="请在专用Chrome完成登录或验证码")
        while True:
            self.operation("waiting_for_login", 420)
            self.data["next_check"] = self.clock() + 300
            self.tick()
            end = self.monotonic() + 300
            while self.monotonic() < end:
                self.check()
                sleeper(min(1, end - self.monotonic()))
            try:
                checker()
                break
            except TaskError as error:
                if error.code != "login_required":
                    raise
        self.data.update(status="running", message="登录恢复，继续核对原请求")
        if item:
            self.data["item"] = item
        self.operation("processing", 180)

    def block(self, code, message, *, kind=None):
        if kind is None:
            kind = "access" if code == "login_required" else "platform"
        self.data.update(status="waiting", reason=code, message=redact(message), wait_kind=kind)
        self.data.pop("item", None)
        self.tick()
        raise TaskError("runtime_wait", message, retryable=False)

    def finish(self, code, error=None):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.data["exit_code"] = code
        if self.data["status"] != "waiting":
            self.data.update(status="finished", exit_code=code)
            if error:
                self.data["error"] = redact(error)
        self.tick()


_worker = None


def current():
    return _worker


def start_worker():
    global _worker
    path, identity = os.environ.get("XHS_RUNTIME_DIR"), os.environ.get("XHS_JOB_ID")
    if path and identity:
        _worker = WorkerRuntime(path, identity).start()
    else:
        _worker = None
    return _worker


def check():
    if _worker:
        _worker.check()


def operation(name, seconds=180):
    if _worker:
        _worker.operation(name, seconds)


def block(code, message, *, kind=None):
    if _worker:
        _worker.block(code, message, kind=kind)
    raise TaskError(code, message, retryable=False)


def sleep(seconds, sleeper=time.sleep):
    if not _worker:
        return sleeper(seconds)
    with _worker.waiting(seconds):
        end = _worker.monotonic() + seconds
        while _worker.monotonic() < end:
            check()
            sleeper(min(1, max(0, end - _worker.monotonic())))

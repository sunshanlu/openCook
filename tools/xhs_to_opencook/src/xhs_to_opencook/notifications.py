"""A persistent, independent Feishu outbox; delivery never blocks business progress."""

import json
import shutil
import subprocess
import time
from uuid import uuid4

from .runtime import read_json, redact
from .storage import write_json


class Outbox:
    def __init__(self, directory, recipient=None, *, clock=time.time, run=subprocess.run):
        self.path = directory / "outbox.json"
        self.clock, self.run = clock, run
        self.data = read_json(self.path, {"recipient": recipient, "pending": [], "events": []})
        self.data.setdefault("pending", [])
        self.data.setdefault("events", [])
        self.cli = shutil.which("lark-cli") or "/opt/homebrew/bin/lark-cli"

    @property
    def enabled(self):
        return self.data.get("enabled", True) is not False

    def resolve_self(self):
        if self.data.get("recipient"):
            return
        result = self.run([self.cli, "whoami", "--as", "user"],
                          capture_output=True, text=True, timeout=10)
        identity = json.loads(result.stdout)
        def open_id(value):
            if isinstance(value, dict):
                for key in ("openId", "open_id"):
                    if value.get(key):
                        return value[key]
                for child in value.values():
                    found = open_id(child)
                    if found:
                        return found
            return None
        recipient = open_id(identity)
        if result.returncode or not recipient:
            raise ValueError("无法从飞书CLI确认本人接收地址")
        self.data["recipient"] = recipient
        self.save()

    def save(self):
        write_json(self.path, self.data)

    def enqueue(self, key, text, *, progress=False):
        if not self.enabled:
            return
        if not progress and key in self.data["events"]:
            return
        if progress:
            self.data["pending"] = [m for m in self.data["pending"] if not m["progress"]]
        else:
            self.data["events"].append(key)
        self.data["pending"].append({
            "key": key, "text": redact(text), "progress": progress, "uuid": uuid4().hex,
            "attempts": 0, "created_at": self.clock(), "next_at": self.clock(),
        })
        self.save()

    def flush(self, *, still_actionable=True):
        if not self.enabled:
            if self.data["pending"]:
                self.data["pending"] = []
                self.save()
            return
        for message in list(self.data["pending"]):
            if message["next_at"] > self.clock():
                continue
            try:
                self.resolve_self()
                if self.clock() - message["created_at"] >= 3600 and message["attempts"]:
                    if message["progress"]:
                        self.data["pending"].remove(message)
                        self.save()
                        continue
                    if not still_actionable and message["key"].startswith("waiting:"):
                        self.data["pending"].remove(message)
                        self.save()
                        continue
                    message.update(uuid=uuid4().hex, created_at=self.clock())
                    if not message["text"].startswith("提醒："):
                        message["text"] = "提醒：该问题或结果仍待查看。\n" + message["text"]
                result = self.run([
                    self.cli, "im", "+messages-send", "--as", "bot",
                    "--user-id", self.data["recipient"], "--text", message["text"],
                    "--idempotency-key", message["uuid"], "--format", "json",
                ], capture_output=True, text=True, timeout=10)
                reply = json.loads(result.stdout or "{}")
                data = reply.get("data", {})
                if result.returncode or reply.get("ok") is not True or not data.get("message_id"):
                    raise ValueError("飞书未返回确认送达的消息ID")
                self.data["pending"].remove(message)
                self.data["last_delivery"] = {"message_id": data["message_id"],
                                              "delivered_at": self.clock()}
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                delays = (60, 300, 900, 3600)
                message["next_at"] = self.clock() + delays[min(message["attempts"], 3)]
                message["attempts"] += 1
                message["last_error"] = redact(error)
            self.save()
            # Bound CLI time per supervisor iteration; progress/control must stay responsive.
            break


def summary(job, report):
    counts = report.get("classification_counts", {})
    classified = counts.get("tutorial", 0) + counts.get("non_tutorial", 0)
    extracted = report.get("extraction_success_count", 0)
    workers = job.get("workers", {})
    detail = "\n".join(
        f"{stage}：{value.get('status')}；{value.get('reason') or value.get('message') or '正常'}；"
        f"下次检查：{value.get('next_check_label', value.get('next_check', '按状态继续'))}"
        for stage, value in workers.items()
    )
    return (
        f"{detail}\n" if detail else ""
    ) + (
        f"openCook：{job['status']}\n"
        f"阶段：{job.get('stage', '准备')}\n"
        f"分类：{classified}/{report.get('video_count', '待确定')}\n"
        f"解析：{extracted}/{report.get('tutorial_count', '待确定')}\n"
        f"待人工：{report.get('manual_review_count', 0)}\n"
        f"原因：{job.get('message', '正常处理')}\n"
        f"下次检查：{job.get('next_check_label', '按运行状态继续')}\n"
        f"下一步：{job.get('action_hint', '无需操作，可用 status 查询')}"
    )

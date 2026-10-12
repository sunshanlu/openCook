"""One Chrome owner, with stage-owned windows and independently attached CDP clients."""

import socket
import time
from contextlib import suppress
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .browser import SiteBrowser
from .errors import TaskError
from .runtime import WorkerRuntime, read_json
from .storage import write_json


class ChromeOwner:
    def __init__(self, profile, directory, *, playwright=None):
        self.profile, self.directory = Path(profile), Path(directory)
        self.playwright = self.context = None
        self.borrowed_playwright = playwright

    def open(self):
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.playwright = self.borrowed_playwright or sync_playwright().start()
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                str(self.profile),
                channel="chrome",
                headless=False,
                no_viewport=True,
                args=[f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"],
            )
            self.processes = WorkerRuntime(self.directory, "browser-owner")
            self.processes.register_children()
            children = self.processes.data.get("children", [])
            roots = {
                c["pid"] for c in children if f"--user-data-dir={self.profile}" in c["identity"]
            }
            # Shared Playwright node is excluded; only this Chrome tree is owned.
            rows = self.context.pages[0].context.browser.new_browser_cdp_session()
            try:
                chrome_pids = {
                    r["id"] for r in rows.send("SystemInfo.getProcessInfo")["processInfo"]
                }
            finally:
                rows.detach()
            self.browser_children = [c for c in children if c["pid"] in chrome_pids | roots]
            write_json(
                self.directory / "browser-processes.json",
                {
                    "children": self.browser_children,
                },
            )
            endpoint = f"http://127.0.0.1:{port}"
            previous = read_json(self.directory / "browser.json")
            self.generation = previous.get("generation", 0) + 1
            # Workers never infer ownership from the order of context.pages.
            write_json(
                self.directory / "browser.json",
                {
                    "endpoint": endpoint,
                    "generation": self.generation,
                    "windows": {},
                },
            )
            return self
        except BaseException:
            self.close()
            raise

    def ensure_window(self, stage):
        state = read_json(self.directory / "browser.json")
        cdp = self.context.new_cdp_session(self.context.pages[0])
        try:
            targets = cdp.send("Target.getTargets")["targetInfos"]
            target = state["windows"].get(stage)
            if target and any(t["targetId"] == target for t in targets):
                return
            target = cdp.send(
                "Target.createTarget",
                {
                    "url": f"about:blank#opencook-{self.generation}-{stage}",
                    "newWindow": True,
                },
            )["targetId"]
            state["windows"][stage] = target
            write_json(self.directory / "browser.json", state)
        finally:
            cdp.detach()

    def alive(self):
        try:
            self.context.pages[0].evaluate("1")
            return True
        except (PlaywrightError, IndexError, AttributeError):
            return False

    def close(self):
        if self.context:
            with suppress(PlaywrightError):
                self.context.close()
            self.context = None
        if self.playwright:
            if not self.borrowed_playwright:
                self.playwright.stop()
            self.playwright = None


class AttachedBrowser(SiteBrowser):
    def __init__(
        self, profile, *, directory, stage, playwright=None, account_id=None, gate=None, **kwargs
    ):
        super().__init__(profile, **kwargs)
        self.visit_gate = gate  # Set before __enter__, which may navigate while attaching.
        self.directory, self.stage = Path(directory), stage
        self.remote = None
        self.borrowed_playwright = playwright
        self.expected_account = account_id

    def __enter__(self):
        state = read_json(self.directory / "browser.json")
        target = state.get("windows", {}).get(self.stage)
        if not target:
            raise TaskError("browser_unavailable", "本阶段窗口尚未准备", retryable=False)
        self.playwright = self.borrowed_playwright or sync_playwright().start()
        try:
            self.remote = self.playwright.chromium.connect_over_cdp(state["endpoint"])
            self.context = self.remote.contexts[0]
            end = time.monotonic() + 10
            while time.monotonic() < end:
                for page in self.context.pages:
                    session = self.context.new_cdp_session(page)
                    try:
                        identity = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
                    finally:
                        session.detach()
                    if identity == target:
                        self.page = self.chat_page = page
                        self.generation = state["generation"]
                        if self.expected_account:
                            if page.url.startswith("about:"):
                                from .browser import CHAT

                                self.visit(page, CHAT, wait_until="domcontentloaded")
                            self.verify_account()
                        return self
                time.sleep(0.1)
            raise TaskError("browser_unavailable", "找不到本阶段拥有的Chrome窗口", retryable=False)
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def verify_account(self):
        if self.expected_account and self.account_identity() != self.expected_account:
            raise TaskError("account_mismatch", "当前登录账号与本阶段绑定不符", retryable=False)

    def restore_chat(self, conversation_id):
        self.verify_account()
        return super().restore_chat(conversation_id)

    def begin_request(self, prompt, source, request, persist):
        self.verify_account()
        if self.expected_account and request.get("account_id") != self.expected_account:
            raise TaskError("account_mismatch", "请求不属于当前账号", retryable=False)
        return super().begin_request(prompt, source, request, persist)

    def prepare_chat_page(self):
        if not self.chat_page or self.chat_page.is_closed():
            raise TaskError("browser_unavailable", "本阶段Chrome窗口已关闭", retryable=False)
        super().prepare_chat_page()

    def __exit__(self, *args):
        # Never close the shared default context or a window on worker shutdown.
        if self.remote:
            with suppress(PlaywrightError):
                self.remote.close()
            self.remote = None
        self.context = self.chat_page = None
        if self.playwright:
            if not self.borrowed_playwright:
                self.playwright.stop()
            self.playwright = None

    def isolate_owned_page(self):
        # Navigating away isolates late DOM updates without relinquishing target ownership.
        self.chat_page.goto("about:blank", wait_until="domcontentloaded")
        self.history_observer = None
        self.history_messages = {}

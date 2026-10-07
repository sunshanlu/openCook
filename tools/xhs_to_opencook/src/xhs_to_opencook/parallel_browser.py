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
    def __init__(self, profile, directory):
        self.profile, self.directory = Path(profile), Path(directory)
        self.playwright = self.context = None

    def open(self):
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.playwright = sync_playwright().start()
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
            self.browser_children = self.processes.data.get("children", [])
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
            self.playwright.stop()
            self.playwright = None


class AttachedBrowser(SiteBrowser):
    def __init__(self, profile, *, directory, stage, playwright=None, **kwargs):
        super().__init__(profile, **kwargs)
        self.directory, self.stage = Path(directory), stage
        self.remote = None
        self.borrowed_playwright = playwright

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
                        return self
                time.sleep(0.1)
            raise TaskError("browser_unavailable", "找不到本阶段拥有的Chrome窗口", retryable=False)
        except BaseException:
            self.__exit__(None, None, None)
            raise

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

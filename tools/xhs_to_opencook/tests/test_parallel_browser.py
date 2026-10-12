from xhs_to_opencook.parallel_browser import AttachedBrowser, ChromeOwner


def test_two_cdp_windows_disconnect_and_cookie_cover(tmp_path):
    directory = tmp_path / "runtime"
    directory.mkdir()
    owner = ChromeOwner(tmp_path / "chrome", directory).open()
    try:
        owner.ensure_window("classify")
        owner.ensure_window("extract")
        first = AttachedBrowser(
            tmp_path, directory=directory, stage="classify", playwright=owner.playwright
        ).__enter__()
        second = AttachedBrowser(
            tmp_path, directory=directory, stage="extract", playwright=owner.playwright
        ).__enter__()
        try:
            first.chat_page.set_content("<p>classification</p>")
            second.chat_page.set_content("<p>extraction</p>")
            assert first.chat_page.inner_text("p") == "classification"
            assert second.chat_page.inner_text("p") == "extraction"

            def window_id(browser):
                session = browser.context.new_cdp_session(browser.chat_page)
                value = session.send("Browser.getWindowForTarget")["windowId"]
                session.detach()
                return value

            assert window_id(first) != window_id(second)
            first.__exit__(None, None, None)
            assert owner.alive()
            assert second.chat_page.inner_text("p") == "extraction"
            first = AttachedBrowser(
                tmp_path, directory=directory, stage="classify", playwright=owner.playwright
            ).__enter__()
            assert first.chat_page.inner_text("p") == "classification"
            first.isolate_owned_page()
            assert second.chat_page.inner_text("p") == "extraction"
        finally:
            first.__exit__(None, None, None)
            second.__exit__(None, None, None)
    finally:
        owner.close()


def test_independent_process_kill_does_not_close_other_window(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time

    directory = tmp_path / "runtime"
    directory.mkdir()
    owner = ChromeOwner(tmp_path / "chrome", directory).open()
    children = []
    try:
        for stage in ("classify", "extract"):
            owner.ensure_window(stage)
            script = """
import sys,time
from pathlib import Path
from xhs_to_opencook.parallel_browser import AttachedBrowser
p=Path(sys.argv[1]);s=sys.argv[2]
b=AttachedBrowser(p,directory=p,stage=s).__enter__()
b.chat_page.set_content('<p>'+s+'</p>')
(p/(s+'.ready')).write_text('ready')
time.sleep(60)
"""
            children.append(subprocess.Popen([sys.executable, "-c", script, str(directory), stage]))
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if all((directory / (s + ".ready")).exists() for s in ("classify", "extract")):
                break
            time.sleep(0.1)
        assert all((directory / (s + ".ready")).exists() for s in ("classify", "extract"))
        os.kill(children[0].pid, signal.SIGKILL)
        children[0].wait(timeout=5)
        with AttachedBrowser(
            tmp_path, directory=directory, stage="extract", playwright=owner.playwright
        ) as browser:
            assert browser.chat_page.inner_text("p") == "extract"
        assert children[1].poll() is None
        assert owner.alive()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        owner.close()


def test_attached_cover_request(tmp_path, image_bytes):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/webp")
            self.end_headers()
            self.wfile.write(image_bytes)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    directory = tmp_path / "runtime"
    directory.mkdir()
    owner = ChromeOwner(tmp_path / "chrome", directory).open()
    try:
        owner.ensure_window("extract")
        with AttachedBrowser(
            tmp_path, directory=directory, stage="extract", playwright=owner.playwright
        ) as browser:
            assert (
                browser.download_cover(f"http://127.0.0.1:{server.server_port}/cover")
                == image_bytes
            )
    finally:
        owner.close()
        server.shutdown()
        server.server_close()


def test_two_profiles_have_separate_cookies_and_local_close(tmp_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as engine:
        first = ChromeOwner(tmp_path / 'chrome-a', tmp_path / 'a', playwright=engine).open()
        second = ChromeOwner(tmp_path / 'chrome-b', tmp_path / 'b', playwright=engine).open()
        try:
            first.context.add_cookies([{'name': 'identity', 'value': 'A',
                                       'domain': 'example.test', 'path': '/'}])
            second.context.add_cookies([{'name': 'identity', 'value': 'B',
                                        'domain': 'example.test', 'path': '/'}])
            assert first.context.cookies()[0]['value'] == 'A'
            assert second.context.cookies()[0]['value'] == 'B'
            assert not ({c['pid'] for c in first.browser_children}
                        & {c['pid'] for c in second.browser_children})
            first.close()
            assert second.alive()
        finally:
            first.close()
            second.close()


def test_attach_navigation_is_counted_by_the_gate(tmp_path):
    from xhs_to_opencook.parallel_browser import AttachedBrowser

    calls = []

    class Gate:
        def access(self):
            calls.append("access")

    class Page:
        def goto(self, url, **options):
            calls.append("goto")

    browser = AttachedBrowser(tmp_path / "profile", directory=tmp_path, stage="classify",
                              gate=Gate())
    browser.visit(Page(), "https://www.xiaohongshu.com/", wait_until="domcontentloaded")
    assert calls == ["access", "goto"]  # Counted before the navigation happens.

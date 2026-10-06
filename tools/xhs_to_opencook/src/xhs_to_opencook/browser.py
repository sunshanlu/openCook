"""The site adapter. All live selectors and response lifecycle checks live here."""

import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from .errors import BrowserUnsupported, TaskError
from .links import note_id, validate_url
from .models import Source
from .storage import atomic_write, write_json

HOME = "https://www.xiaohongshu.com/explore"
CHAT = "https://www.xiaohongshu.com/ai_chat"
EDITOR = 'textarea[name="aiSearchTextarea"]:not([aria-hidden="true"]):visible'


class SiteBrowser:
    def __init__(self, profile: Path, *, timeout: float = 180, interactive: bool = True):
        self.profile = profile
        self.timeout = timeout
        self.interactive = interactive
        self.context = None
        self.chat_page = None
        self.conversation_id = None
        self.playwright = None
        self.raw_directory = None
        self.verified_note_id = None

    def __enter__(self):
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.playwright = sync_playwright().start()
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                str(self.profile), channel="chrome", headless=False,
                viewport={"width": 1440, "height": 1000},
            )
            self.context.set_default_timeout(8000)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            return self
        except BaseException:
            self.playwright.stop()
            raise

    def __exit__(self, *args):
        if self.context:
            with suppress(PlaywrightError):
                self.context.close()
            self.context = None
        if self.playwright:
            self.playwright.stop()
            self.playwright = None

    def _pause(self, message: str):
        if not self.interactive:
            raise TaskError("login_required", message, retryable=False)
        input(f"\n{message}。请在专用 Chrome 中处理，完成后回到终端按 Enter；Ctrl-C 退出。\n")

    def login(self):
        self.page.goto(HOME, wait_until="domcontentloaded")
        self.page.bring_to_front()
        self._pause("请登录小红书；不要关闭这个 Chrome 窗口")
        self.ensure_access(self.page, require_account=True)

    def ensure_access(self, page: Page, *, require_account: bool = False) -> bool:
        """Never solve challenges or dismiss login by pretending a page is accessible."""
        paused = False
        while True:
            state = page.evaluate("""() => {
                const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight
                    || el.getClientRects().length);
                const challenge = [...document.querySelectorAll('iframe')].some(el =>
                    visible(el) && /captcha|verify|challenge/i.test(el.src));
                const dialog = [...document.querySelectorAll(
                    '.login-container, .login-modal, .captcha-container, [role="dialog"]'
                )].filter(visible).map(el => el.innerText || '').join(' ');
                const text = (document.body?.innerText || '').slice(-12000);
                const login = /扫码登录|手机号登录|登录后查看|登录后即可|登录后使用/.test(dialog)
                    || /扫码登录|手机号登录|登录后查看|登录后即可/.test(text);
                const verify = challenge || /请完成验证|安全验证|拖动滑块|点击完成验证/.test(dialog)
                    || /请完成验证|拖动滑块|访问异常|异常访问|安全验证/.test(text)
                    || /captcha|verify|challenge/i.test(location.pathname);
                const account = [...document.querySelectorAll('a[href*="/user/profile/"]')]
                    .some(el => visible(el) && el.innerText.trim() === '我');
                return {login, verify, account};
            }""")
            if state["verify"] or state["login"] or (require_account and not state["account"]):
                self._pause("检测到验证码或需要登录，请手动完成")
                paused = True
                page.wait_for_timeout(500)
                continue
            return paused

    def open_source(self, url: str) -> Source:
        validate_url(url)
        if self.page.is_closed():
            self.page = self.context.new_page()
        self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        self.ensure_access(self.page)
        expected = note_id(url)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self.ensure_access(self.page)
            resolved = note_id(self.page.url)
            data = self.page.evaluate("""expected => {
                const map = window.__INITIAL_STATE__?.note?.noteDetailMap || {};
                for (const item of Object.values(map)) {
                    const n = item?.note;
                    if (!n || (expected && n.noteId !== expected)) continue;
                    const im = n.imageList?.[0];
                    const cover = im?.urlDefault || im?.infoList?.find(
                        x => x.imageScene === 'WB_DFT')?.url;
                    return {note_id: n.noteId, title: n.title || n.desc?.split('\\n')[0],
                        author: n.user?.nickname, cover_url: cover || null, type: n.type};
                }
                return null;
            }""", expected or resolved)
            if data and resolved and data.get("note_id") == resolved:
                validate_url(self.page.url)
                if expected and expected != resolved:
                    raise TaskError("link_invalid", "笔记跳转到了不同的视频", retryable=False)
                if data.pop("type", None) != "video":
                    raise TaskError("not_video", "目标笔记不是视频", retryable=False)
                try:
                    return Source(url=self.page.url, **data)
                except ValueError as exc:
                    raise BrowserUnsupported("无法读取笔记作者、标题或 ID；请检查站点适配") from exc
            text = self.page.locator("body").inner_text()
            if any(x in text for x in ("笔记已删除", "笔记不存在", "无法查看", "内容不可见")):
                raise TaskError("link_invalid", "笔记不可访问或已删除", retryable=False)
            self.page.wait_for_timeout(250)
        if not note_id(self.page.url):
            raise TaskError("link_invalid", "短链接未解析到视频笔记", retryable=False)
        raise BrowserUnsupported("未找到当前笔记数据；请更新页面适配后再运行批量任务")

    def new_chat(self):
        if self.chat_page and not self.chat_page.is_closed():
            self.chat_page.close()
        self.chat_page = self.context.new_page()
        self.chat_page.goto(CHAT, wait_until="domcontentloaded")
        self.ensure_access(self.chat_page)
        editor = self.chat_page.locator(EDITOR)
        try:
            editor.wait_for(state="visible", timeout=20000)
        except PlaywrightError as exc:
            self.ensure_access(self.chat_page)
            raise BrowserUnsupported("找不到点点输入框；请检查页面版本") from exc
        new_button = self.chat_page.locator(
            '[title="新建会话"]:visible, svg:has(title:text-is("新建会话")):visible'
        )
        if new_button.count():
            new_button.first.click()
        # Navigating to /ai_chat alone may restore the latest conversation.
        try:
            self.chat_page.wait_for_function("""() =>
                !new URL(location.href).searchParams.get('conversationId') &&
                document.querySelectorAll('.ai-message, .user-message__text-wrapper').length === 0
            """, timeout=10000)
        except PlaywrightError as exc:
            raise BrowserUnsupported("无法确认新建了空白对话；停止以防混入其他视频") from exc
        self.conversation_id = None
        self.verified_note_id = None

    def diagnostics(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        for name, page in (("note", self.page), ("chat", self.chat_page)):
            if page and not page.is_closed():
                with suppress(PlaywrightError):
                    page.screenshot(path=str(directory / f"{name}.png"), full_page=True)
                    (directory / f"{name}.html").write_text(page.content(), encoding="utf-8")

    def ask(self, prompt: str, source: Source, *, repair: bool = False) -> str:
        page = self.chat_page
        self.ensure_access(page)
        editor = page.locator(EDITOR)
        editor.wait_for(state="visible", timeout=20000)
        if repair:
            current = parse_qs(urlsplit(page.url).query).get("conversationId", [None])[0]
            if self.verified_note_id != source.note_id or current != self.conversation_id:
                raise TaskError("reference_mismatch", "修正请求的对话或视频来源发生变化")
        else:
            # The real site only attaches note cards on paste, not textarea.fill().
            # Observe the response generated by that UI action; do not call private APIs.
            try:
                with page.expect_response(
                    lambda r: urlsplit(r.url).path == "/api/sns/agentspark/web/note/detail",
                    timeout=20000,
                ) as fetched:
                    editor.evaluate("""(el, url) => {
                        const data = new DataTransfer();
                        data.setData('text/plain', url);
                        el.dispatchEvent(new ClipboardEvent('paste', {
                            clipboardData: data, bubbles: true, cancelable: true
                        }));
                    }""", source.url)
                response = fetched.value
                payload = response.json()
                data = payload.get("data", payload)
                details = data.get("content_detail_list", data.get("contentDetailList", []))
                ids = [entry.get("id") for entry in details]
                if not response.ok or ids != [source.note_id]:
                    raise TaskError("reference_mismatch", "点点引用响应未对应唯一的目标视频")
                card = page.locator('.xhs-ai-selected-note-card:visible')
                card.wait_for(state="visible", timeout=10000)
                if card.count() != 1:
                    raise TaskError("reference_mismatch", "输入中包含零个或多个笔记引用")
                self.verified_note_id = source.note_id
                if self.raw_directory:
                    write_json(self.raw_directory / "reference.json", {
                        "expected_note_id": source.note_id, "selected_note_ids": ids,
                        "card_title": card.inner_text(), "verified_at": time.time(),
                    })
            except TaskError:
                raise
            except (PlaywrightError, ValueError, TypeError, AttributeError) as exc:
                self.ensure_access(page)
                raise TaskError("reference_mismatch", "链接未形成可确认的视频引用") from exc

        before = page.locator(".ai-message").count()
        users_before = page.locator(".user-message__text-wrapper").count()
        editor.fill(prompt)
        editor.press("Enter")
        started = time.monotonic()
        saw_answer = False
        previous = None
        finished_since = None
        turn = "repair" if repair else "answer"
        while time.monotonic() - started < self.timeout:
            paused_at = time.monotonic()
            if self.ensure_access(page):
                started += time.monotonic() - paused_at
            snapshot = page.evaluate("""before => {
                const messages = [...document.querySelectorAll('.ai-message')];
                const last = messages[messages.length - 1];
                const content = last?.querySelector('.xhs-ai-md-container');
                // A fenced JSON code block exposes only its actual code, not copy controls.
                const codes = content ? [...content.querySelectorAll('pre code')] : [];
                const text = codes.length === 1 ? codes[0].textContent : content?.innerText;
                const error = last?.querySelector('.interrupt, [class*="timeout"]');
                return {count: messages.length, finished: !!last?.classList.contains(
                    'ai-message-finished'), text: text || '', error: !!error,
                    users: document.querySelectorAll('.user-message__text-wrapper').length};
            }""", before)
            if snapshot["count"] > before and snapshot["users"] > users_before:
                saw_answer = True
                text = snapshot["text"].strip()
                if self.raw_directory and text != previous:
                    atomic_write(self.raw_directory / f"{turn}-partial.txt", text.encode())
                if snapshot["error"]:
                    raise TaskError("answer_incomplete", "点点显示超时或中断，拒绝导出部分回答")
                if snapshot["finished"] and text:
                    if finished_since is None or text != previous:
                        finished_since = time.monotonic()
                    if finished_since is not None and time.monotonic() - finished_since >= 1:
                        current = parse_qs(urlsplit(page.url).query).get(
                            "conversationId", [None],
                        )[0]
                        if not current:
                            raise BrowserUnsupported("回答完成但缺少对话 ID，无法记录来源")
                        if repair and current != self.conversation_id:
                            raise TaskError("reference_mismatch", "修正过程中对话 ID 发生变化")
                        self.conversation_id = current
                        return text
                else:
                    finished_since = None
                previous = text
            page.wait_for_timeout(250)
        if not saw_answer:
            raise TaskError("answer_timeout", "提交后未出现新的点点回答")
        raise TaskError("answer_timeout", "未等到 ai-message-finished 完成标记，拒绝导出部分回答")

    def download_cover(self, url: str) -> bytes:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
            raise TaskError("image_failed", "封面地址不是有效的 http(s) URL")
        response = self.context.request.get(url, timeout=30000)
        try:
            if not response.ok:
                raise TaskError("image_failed", f"封面请求返回 HTTP {response.status}")
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > 20 * 1024 * 1024:
                raise TaskError("image_failed", "封面文件超过 20 MiB")
            return response.body()
        finally:
            response.dispose()

"""The site adapter. All live selectors and response lifecycle checks live here."""

import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect, sync_playwright

from . import runtime
from .errors import BrowserUnsupported, TaskError
from .links import note_id, profile_id, validate_url
from .models import Source
from .storage import atomic_write, write_json

HOME = "https://www.xiaohongshu.com/explore"
CHAT = "https://www.xiaohongshu.com/ai_chat"
EDITOR = 'textarea[name="aiSearchTextarea"]:not([aria-hidden="true"]):visible'


def profile_note_metadata(card: dict) -> dict:
    """Read metadata already delivered in a profile's note card."""
    user = card.get("user") or {}
    cover = card.get("cover") or {}
    images = cover.get("info_list") or cover.get("infoList") or []
    cover_url = cover.get("url_default") or cover.get("urlDefault") or next(
        (image.get("url") for image in images
         if (image.get("image_scene") or image.get("imageScene")) == "WB_DFT"),
        None,
    ) or cover.get("url") or cover.get("url_pre")
    return {
        "title": card.get("display_title") or card.get("title") or "",
        "author": user.get("nickname") or user.get("nick_name") or "",
        "author_id": user.get("user_id") or user.get("userId"),
        "cover_url": cover_url,
    }


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
        self.history_messages = {}
        self.history_observer = None

    def __enter__(self):
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.playwright = sync_playwright().start()
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                str(self.profile),
                channel="chrome",
                headless=False,
                # Headed Chrome must use its real content area. An emulated viewport
                # taller than the native window puts the bottom composer off screen.
                no_viewport=True,
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

    def account_identity(self, page=None):
        page = page or self.chat_page or self.page
        self.ensure_access(page, require_account=True)
        links = page.evaluate("""() => [...document.querySelectorAll('a[href*="/user/profile/"]')]
            .filter(el => (el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                && el.innerText.trim() === '我')
            .map(el => new URL(el.getAttribute('href'), location.href).href)""")
        try:
            identities = {profile_id(url) for url in links}
        except TaskError as error:
            raise TaskError("account_identity_unavailable", "无法确认登录用户ID",
                            retryable=False) from error
        if len(identities) != 1:
            raise TaskError("account_identity_unavailable", "无法唯一确认登录用户ID",
                            retryable=False)
        return identities.pop()

    def ensure_access(self, page: Page, *, require_account: bool = False) -> bool:
        """Never solve challenges or dismiss login by pretending a page is accessible."""
        runtime.check()
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
                // Restriction pages must stop the batch, not become bad-note failures.
                // Match the dedicated error route, never words in ordinary note content.
                const restricted = location.pathname === '/website-login/error'
                    || location.pathname.startsWith('/website-login/error/');
                const login = /扫码登录|手机号登录|登录后查看|登录后即可|登录后使用/.test(dialog)
                    || /扫码登录|手机号登录|登录后查看|登录后即可/.test(text);
                const verify = challenge || /请完成验证|安全验证|拖动滑块|点击完成验证/.test(dialog)
                    || /请完成验证|拖动滑块|访问异常|异常访问|安全验证/.test(text)
                    || /captcha|verify|challenge/i.test(location.pathname);
                const account = [...document.querySelectorAll('a[href*="/user/profile/"]')]
                    .some(el => visible(el) && el.innerText.trim() === '我');
                return {login, verify, account, restricted};
            }""")
            if state["restricted"]:
                raise TaskError(
                    "platform_restricted",
                    "平台安全限制页面（访问频繁可能显示300013）；已停止整批，请等待限制解除",
                    retryable=False,
                )
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
            data = self.page.evaluate(
                """expected => {
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
            }""",
                expected or resolved,
            )
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

    def wait_editor_ready(self):
        """Wait for the live composer, including replacement after chat navigation."""
        page = self.chat_page
        print("  等待点点输入框加载并可编辑…", flush=True)
        deadline = time.monotonic() + 60
        editor = page.locator(EDITOR)
        try:
            while time.monotonic() < deadline:
                self.ensure_access(page)
                remaining = max(1, int((deadline - time.monotonic()) * 1000))
                editor.wait_for(state="visible", timeout=remaining)
                expect(editor).to_be_editable(timeout=remaining)
                handle = editor.element_handle()
                page.wait_for_timeout(500)
                # A navigation can replace the visible input during the settle delay.
                if editor.count() != 1 or not editor.is_editable():
                    continue
                if not handle.evaluate("el => el.isConnected"):
                    continue
                if editor.evaluate("""el => {
                    const box = el.getBoundingClientRect();
                    const composer = el.closest('.wendian-wrapper');
                    const button = composer?.querySelector('.submit-button-wrapper');
                    const send = button?.getBoundingClientRect();
                    const inView = rect => rect && rect.width > 0 && rect.height > 0 &&
                        rect.top >= 0 && rect.left >= 0 && rect.bottom <= innerHeight &&
                        rect.right <= innerWidth;
                    const hit = document.elementFromPoint(
                        box.left + box.width / 2, box.top + box.height / 2);
                    return inView(box) && inView(send) && hit === el;
                }"""):
                    return editor
                page.wait_for_timeout(250)
        except (PlaywrightError, AssertionError) as exc:
            self.ensure_access(page)
            raise BrowserUnsupported("点点输入框未加载完成或不可编辑；未填写、未发送") from exc
        raise BrowserUnsupported("点点输入框未在可见窗口内稳定就绪；未填写、未发送")

    def prepare_chat_page(self):
        # Reuse the tab: opening a new target can raise headed Chrome on macOS.
        if self.chat_page and self.history_observer:
            self.chat_page.remove_listener("response", self.history_observer)
        self.history_observer = None
        if not self.chat_page or self.chat_page.is_closed():
            self.chat_page = self.context.new_page()
        self.history_messages = {}

    def new_chat(self):
        self.prepare_chat_page()
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
            self.chat_page.wait_for_function(
                """() =>
                !new URL(location.href).searchParams.get('conversationId') &&
                document.querySelectorAll('.ai-message, .user-message__text-wrapper').length === 0
            """,
                timeout=10000,
            )
        except PlaywrightError as exc:
            raise BrowserUnsupported("无法确认新建了空白对话；停止以防混入其他视频") from exc
        self.conversation_id = None
        self.verified_note_id = None
        self.wait_editor_ready()

    def collect_profile(self, url: str, save_batch) -> dict:
        """Observe only pagination triggered by ordinary page navigation and scrolling."""
        identity = profile_id(url)
        page = self.context.new_page()
        pagination = {"observed": False, "has_more": None, "error": None}
        pending = []
        observed_pages = 0

        def observe(response):
            nonlocal observed_pages
            parts = urlsplit(response.url)
            if (
                parts.hostname not in {"www.xiaohongshu.com", "edith.xiaohongshu.com"}
                or parts.path != "/api/sns/web/v1/user_posted"
                or parse_qs(parts.query).get("user_id") != [identity]
            ):
                return
            try:
                payload = response.json()
                data = payload.get("data", {})
                if (
                    not response.ok
                    or payload.get("success") is False
                    or not isinstance(data.get("notes"), list)
                    or not isinstance(data.get("has_more"), bool)
                ):
                    pagination["error"] = "主页分页响应失败或结构改变"
                    return
                batch = []
                observed_pages += 1
                if self.raw_directory:
                    write_json(
                        self.raw_directory / "profile-pages" / f"{observed_pages:04d}.json",
                        data,
                    )
                for note in data["notes"]:
                    card = note.get("note_card", note)
                    nid = note.get("note_id", card.get("note_id"))
                    token = note.get("xsec_token", card.get("xsec_token"))
                    if not nid or not note_id(f"https://www.xiaohongshu.com/explore/{nid}"):
                        pagination["error"] = "主页分页缺少有效笔记 ID"
                        return
                    from urllib.parse import urlencode

                    link = f"https://www.xiaohongshu.com/user/profile/{identity}/{nid}"
                    if token:
                        link += "?" + urlencode({"xsec_token": token, "xsec_source": "pc_user"})
                    batch.append(
                        {
                            "note_id": nid,
                            "url": link,
                            "type": card.get("type"),
                            **profile_note_metadata(card),
                        }
                    )
                pending.extend(batch)
                pagination.update(observed=True, has_more=data["has_more"])
            except (ValueError, TypeError, AttributeError, PlaywrightError):
                pagination["error"] = "无法读取主页分页响应"

        page.on("response", observe)
        seen = set()
        stagnant = 0
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
            self.ensure_access(page)
            while True:
                self.ensure_access(page)
                if profile_id(page.url) != identity:
                    raise TaskError("profile_invalid", "主页跳转到其他博主", retryable=False)
                active_tab = page.evaluate("""() => {
                    const tab = window.__INITIAL_STATE__?.user?.activeTab;
                    return (tab?._value ?? tab)?.query ?? null;
                }""")
                if active_tab is not None and active_tab != "note":
                    raise BrowserUnsupported("主页已切换到非笔记标签；停止以免采集收藏内容")
                author = page.evaluate("""() => {
                    const unwrap = v => v?._value ?? v?.value ?? v;
                    const data = unwrap(window.__INITIAL_STATE__?.user?.userPageData);
                    return unwrap(data?.basicInfo)?.nickname
                        || document.querySelector('.user-name')?.innerText?.trim() || '';
                }""")
                cards = page.locator("section.note-item").evaluate_all("""els => els.map(e => ({
                    note_id: e.dataset.noteId, url: e.querySelector('a.cover')?.href,
                    type: e.querySelector('.play-icon') ? 'video' : null,
                    title: e.querySelector('.title')?.innerText || '',
                    author: e.querySelector('.author .name, .author-wrapper .name')
                        ?.innerText?.trim() || '',
                    cover_url: e.querySelector('a.cover img')?.currentSrc
                        || e.querySelector('a.cover img')?.src || null
                }))""")
                batch = {n["note_id"]: n for n in pending}
                pending.clear()
                for card in cards:
                    nid = card.get("note_id")
                    if nid and card.get("url") and note_id(card["url"]) == nid:
                        previous = batch.get(nid, {})
                        batch[nid] = {
                            **{k: v for k, v in card.items() if v},
                            **{k: v for k, v in previous.items() if v},
                        }
                for card in batch.values():
                    card["author"] = card.get("author") or author
                    card["author_id"] = card.get("author_id") or identity
                if batch:
                    save_batch(list(batch.values()))
                new = set(batch) - seen
                seen.update(batch)
                print(f"  已观察 {len(seen)} 条笔记", flush=True)
                stagnant = 0 if new else stagnant + 1
                if pagination["error"]:
                    return {
                        "status": "incomplete",
                        "reason": pagination["error"],
                        "observed_notes": len(seen),
                    }
                if pagination["observed"] and pagination["has_more"] is False:
                    return {
                        "status": "complete",
                        "end_signal": "user_posted.has_more=false",
                        "observed_notes": len(seen),
                    }
                if stagnant >= 3:
                    return {
                        "status": "incomplete",
                        "reason": "连续三轮无新增，未确认末页",
                        "observed_notes": len(seen),
                    }
                page.evaluate("""() => {
                    window.scrollTo(0, Math.max(document.body.scrollHeight,
                        document.documentElement.scrollHeight));
                }""")
                page.wait_for_timeout(2500)
        finally:
            page.remove_listener("response", observe)
            if self.raw_directory and not page.is_closed():
                with suppress(PlaywrightError):
                    page.screenshot(path=str(self.raw_directory / "profile.png"), timeout=5000)
            with suppress(PlaywrightError):
                page.close()

    def diagnostics(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        for name, page in (("note", self.page), ("chat", self.chat_page)):
            if page and not page.is_closed():
                with suppress(PlaywrightError):
                    page.screenshot(
                        path=str(directory / f"{name}.png"), full_page=False, timeout=5000
                    )
                    (directory / f"{name}.html").write_text(
                        runtime.redact(page.content()), encoding="utf-8"
                    )

    def restore_chat(self, conversation_id: str):
        self.prepare_chat_page()
        empty_history = []

        def observe_history(response):
            if urlsplit(response.url).path != "/api/sns/web/v1/dqa/history/detail":
                return
            try:
                if not response.ok:
                    return
                payload = response.json()
                items = payload.get("data", {}).get("items")
                requested = parse_qs(urlsplit(response.url).query).get("conversation_id")
                if (
                    requested == [conversation_id] and payload.get("success") is True
                    and payload.get("code") == 0 and items == []
                ):
                    empty_history.append({
                        "conversation_id": conversation_id, "items": [],
                        "observed_url": response.url, "observed_at": time.time(),
                    })
                for item in items or []:
                    message = item.get("message", {})
                    question = message.get("question", "")
                    if message.get("id") and question:
                        self.history_messages[question] = {
                            "server_message_id": message["id"],
                            "text": message.get("simple_data", {})
                            .get("base_info", {})
                            .get("text", ""),
                            "observed_url": response.url,
                        }
            except (ValueError, TypeError, PlaywrightError):
                pass

        self.chat_page.on("response", observe_history)
        self.history_observer = observe_history
        try:
            self.chat_page.goto(
                CHAT + "?conversationId=" + conversation_id, wait_until="domcontentloaded"
            )
            self.ensure_access(self.chat_page)
            self.wait_editor_ready()
            if empty_history and self.chat_page.locator("[data-round-id]").count() == 0:
                error = TaskError(
                    "history_unavailable", "旧会话历史接口确认无消息", retryable=False
                )
                error.evidence = empty_history[-1]
                raise error
            self.chat_page.wait_for_selector("[data-round-id]", timeout=20000)
            current = parse_qs(urlsplit(self.chat_page.url).query).get("conversationId", [None])[0]
            if current != conversation_id:
                raise BrowserUnsupported("历史会话恢复后 ID 不匹配；停止提交")
            self.conversation_id = conversation_id
            self.verified_note_id = None
        except BaseException:
            self.chat_page.remove_listener("response", observe_history)
            self.history_observer = None
            raise

    def request_snapshot(self, request: dict) -> dict:
        self.ensure_access(self.chat_page)
        snapshot = self.chat_page.evaluate(
            """request => {
            const cid = new URL(location.href).searchParams.get('conversationId');
            const rounds = [...document.querySelectorAll('[data-round-id]')];
            const matches = rounds.filter(el => {
                const user = el.querySelector('.user-message__text-wrapper');
                return user?.innerText.includes(request.request_id);
            });
            if (matches.length !== 1) return {conversation_id:cid, matches:matches.length};
            const round = matches[0];
            const ai = round.querySelector('.ai-message');
            const content = ai?.querySelector('.xhs-ai-md-container');
            const codes = content ? [...content.querySelectorAll('pre code')] : [];
            const text = codes.length === 1 ? codes[0].textContent : content?.innerText;
            const placeholder = [...(ai?.querySelectorAll('*') || [])].some(el =>
                !el.children.length && (el.innerText || '').trim()
                    .replace(/^[（(]\\s*|\\s*[）)]$/g, '') === '暂无内容');
            const user = round.querySelector('.user-message__text-wrapper')?.innerText || '';
            return {conversation_id:cid, matches:1, round_id:round.dataset.roundId,
                user_text:user, text:text || '', empty_placeholder:placeholder,
                finished:!!ai?.classList.contains(
                'ai-message-finished'), error:!!ai?.querySelector('.interrupt, [class*="timeout"]'),
                context_limit:
                /上下文.{0,12}(上限|限制|过长)|对话.{0,8}(长度|太长)/.test(text || '')};
        }""",
            request,
        )
        if (
            request.get("conversation_id")
            and snapshot.get("conversation_id") != request["conversation_id"]
        ):
            if self.raw_directory:
                write_json(
                    self.raw_directory / "conversation-mismatch.json",
                    {
                        "expected_conversation_id": request["conversation_id"],
                        "actual_conversation_id": snapshot.get("conversation_id"),
                        "request_id": request["request_id"],
                        "observed_at": time.time(),
                    },
                )
            raise BrowserUnsupported("当前会话与持久请求不匹配")
        if snapshot.get("matches", 0) > 1:
            raise BrowserUnsupported("同一请求标识对应多个消息，无法安全恢复")
        if snapshot.get("matches") == 1:
            if request.get("prompt") and snapshot["user_text"] != request["prompt"]:
                raise BrowserUnsupported("当前用户消息与已保存的提交文本不一致")
            if request["note_id"] not in snapshot["user_text"]:
                raise BrowserUnsupported("当前用户消息未绑定目标笔记")
            if request.get("round_id") and snapshot["round_id"] != request["round_id"]:
                history = self.history_messages.get(snapshot["user_text"])
                if (
                    not snapshot["round_id"].startswith("history-")
                    or not history
                    or snapshot["user_text"] != request.get("prompt")
                    or (
                        request.get("server_message_id")
                        and request["server_message_id"] != history["server_message_id"]
                    )
                ):
                    raise BrowserUnsupported("历史请求消息关联无法通过页面加载数据确认")
                snapshot["history_binding"] = history
                snapshot["original_round_id"] = request["round_id"]
        return snapshot

    def confirm_idle(self) -> bool:
        # The live composer switches its icon from arrow_top to pause while generating.
        return self.chat_page.evaluate("""() => {
            const rounds = [...document.querySelectorAll('[data-round-id]')];
            if (!rounds.length) return true;
            const ai = rounds[rounds.length-1].querySelector('.ai-message');
            const icons = [...document.querySelectorAll(
                '.input-container .submit-button-wrapper use')];
            const send = icons.some(el => (el.getAttribute('href') || '').endsWith('#arrow_top'));
            return !!ai?.classList.contains('ai-message-finished') && send;
        }""")

    def stop_generation(self) -> bool:
        # Fail closed until the stop control has been verified for this page version.
        return self.confirm_idle()

    def verify_reference(self, source, editor):
        page = self.chat_page
        # Only removable cards in the composer are current input references.
        composer = editor.locator('xpath=ancestor::*[contains(@class,"wendian-wrapper")][1]')
        if composer.count() != 1:
            raise BrowserUnsupported("无法定位唯一输入引用区")
        removers = composer.locator(".xhs-ai-selected-note-remove:visible")
        while removers.count():
            removers.first.click()
        editor.fill("")
        try:
            with page.expect_response(
                lambda r: urlsplit(r.url).path == "/api/sns/agentspark/web/note/detail",
                timeout=20000,
            ) as fetched:
                editor.evaluate(
                    """(el, url) => {
                    const data = new DataTransfer(); data.setData('text/plain', url);
                    el.dispatchEvent(new ClipboardEvent('paste', {
                        clipboardData:data, bubbles:true, cancelable:true}));
                }""",
                    source.url,
                )
            response = fetched.value
            payload = response.json()
            data = payload.get("data", payload)
            details = data.get("content_detail_list", data.get("contentDetailList", []))
            ids = [entry.get("id") for entry in details]
            evidence = {
                "expected_note_id": source.note_id,
                "selected_note_ids": ids,
                "response_status": response.status,
                "response_code": payload.get("code"),
                "verified_at": time.time(),
            }
            if self.raw_directory:
                write_json(self.raw_directory / "reference.json", evidence)
            if not response.ok or ids != [source.note_id]:
                raise TaskError("reference_mismatch", "引用接口未返回唯一目标视频；当前请求未发送")
            card = composer.locator(".xhs-ai-selected-note-card:visible")
            card.wait_for(state="visible", timeout=10000)
            if not response.ok or ids != [source.note_id] or card.count() != 1:
                raise TaskError("reference_mismatch", "当前输入引用未对应唯一目标视频")
            if card.count() != 1:
                raise TaskError("reference_mismatch", "等待引用稳定后目标卡片不唯一")
            self.verified_note_id = source.note_id
            evidence = {
                "expected_note_id": source.note_id,
                "selected_note_ids": ids,
                "card_title": card.inner_text(),
                "verified_at": time.time(),
            }
            if self.raw_directory:
                write_json(self.raw_directory / "reference.json", evidence)
        except TaskError:
            raise
        except (PlaywrightError, ValueError, TypeError, AttributeError) as exc:
            self.ensure_access(page)
            raise TaskError("reference_mismatch", "链接未形成可确认的视频引用") from exc
        return composer, evidence

    def probe(self, source=None):
        runtime.operation("recovery_probe", 120)
        self.new_chat()
        self.ensure_access(self.chat_page, require_account=True)
        if source:
            self.verify_reference(source, self.wait_editor_ready())

    def begin_request(self, prompt: str, source: Source, request: dict, persist):
        runtime.operation("prepare_submission", 120)
        page = self.chat_page
        self.ensure_access(page)
        editor = self.wait_editor_ready()
        current = parse_qs(urlsplit(page.url).query).get("conversationId", [None])[0]
        if request.get("conversation_id") and current != request["conversation_id"]:
            raise BrowserUnsupported("发送前会话 ID 与批次不同")
        if not self.confirm_idle():
            raise TaskError("generation_busy", "上一轮生成未确认结束；当前请求尚未发送")
        composer, evidence = self.verify_reference(source, editor)
        print("  已确认视频引用，填写请求…", flush=True)
        editor.fill(prompt)
        page.wait_for_timeout(500)
        if editor.input_value() != prompt:
            raise BrowserUnsupported("输入文本尚未稳定；停止提交")
        submit = composer.locator(".submit-button-wrapper:visible")
        if submit.count() != 1:
            raise BrowserUnsupported("无法确认唯一发送控件")
        page.wait_for_function(
            """() => {
            const editor = [...document.querySelectorAll('textarea[name="aiSearchTextarea"]')]
                .find(el => el.getAttribute('aria-hidden') !== 'true');
            const wrapper = editor?.closest('.wendian-wrapper');
            const button = wrapper?.querySelector('.submit-button-wrapper');
            return button && !button.classList.contains('disabled') &&
                !button.classList.contains('submitting');
        }""",
            timeout=15000,
        )
        # Durable intent BEFORE Enter. A crash here requires history reconciliation.
        request.update(status="sending", prompt=prompt, reference=evidence, sent_at=time.time())
        persist(request)
        runtime.check()
        runtime.operation("confirm_submission", 30)
        editor.press("Enter")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            snapshot = self.request_snapshot(request)
            if snapshot.get("matches") == 1 and snapshot.get("conversation_id"):
                request.update(
                    status="submitted",
                    conversation_id=snapshot["conversation_id"],
                    round_id=snapshot["round_id"],
                )
                self.conversation_id = snapshot["conversation_id"]
                persist(request)
                print("  已确认请求发送，等待点点回答…", flush=True)
                return
            page.wait_for_timeout(250)
        raise TaskError("submission_unknown", "提交状态无法关联到唯一用户消息；先核对再隔离",
                        retryable=False)

    def wait_request(
        self, request: dict, timeout: float, persist, *, empty_grace: float | None = 5
    ) -> str:
        runtime.operation("wait_answer", timeout + 30)
        started = time.monotonic()
        previous, finished_since = None, None
        empty_since = None
        while time.monotonic() - started < timeout:
            snapshot = self.request_snapshot(request)
            if snapshot.get("matches") == 1:
                if not request.get("round_id"):
                    request.update(
                        round_id=snapshot["round_id"],
                        conversation_id=snapshot["conversation_id"],
                        status="submitted",
                    )
                    self.conversation_id = snapshot["conversation_id"]
                    persist(request)
                text = snapshot["text"].strip()
                if text.strip("（）() ") == "暂无内容":
                    text = ""
                if self.raw_directory and text != previous:
                    atomic_write(
                        self.raw_directory
                        / ("repair-partial.txt" if request.get("repair") else "answer-partial.txt"),
                        text.encode(),
                    )
                if snapshot.get("context_limit"):
                    request["context_limit_evidence"] = snapshot
                    persist(request)
                    raise TaskError("context_limit", "点点明确提示上下文长度限制")
                if snapshot["error"]:
                    raise TaskError("answer_incomplete", "点点显示超时或中断，拒绝部分回答")
                if (
                    empty_grace is not None
                    and snapshot.get("empty_placeholder")
                    and snapshot["finished"]
                    and not text
                    and self.confirm_idle()
                ):
                    empty_since = empty_since or time.monotonic()
                    if time.monotonic() - empty_since >= empty_grace:
                        request["empty_answer_evidence"] = {
                            **snapshot,
                            "observed_at": time.time(),
                            "stable_seconds": empty_grace,
                        }
                        persist(request)
                        raise TaskError("empty_answer", "当前轮已结束且持续显示（暂无内容）")
                else:
                    empty_since = None
                if snapshot["finished"] and text:
                    if finished_since is None or text != previous:
                        finished_since = time.monotonic()
                    if time.monotonic() - finished_since >= 1:
                        request.update(status="answered", answer=text)
                        persist(request)
                        return text
                else:
                    finished_since = None
                previous = text
            self.chat_page.wait_for_timeout(250)
        raise TaskError("answer_timeout", "当前关联请求未收到完整回答")

    def ask(self, prompt: str, source: Source, *, repair: bool = False) -> str:
        # Compatibility entry point; all production commands use ChatSession below.
        from uuid import uuid4

        request = {
            "request_id": uuid4().hex,
            "note_id": source.note_id,
            "conversation_id": self.conversation_id,
            "repair": repair,
        }
        prompt = prompt + f"\n目标 note_id={source.note_id}；request_id={request['request_id']}"
        self.begin_request(prompt, source, request, lambda value: None)
        return self.wait_request(request, self.timeout, lambda value: None)

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

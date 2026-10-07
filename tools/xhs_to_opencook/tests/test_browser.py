"""Real Chrome against a local routed UI fixture, never the user's profile or the live site."""

import json

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from xhs_to_opencook.browser import CHAT, SiteBrowser
from xhs_to_opencook.errors import BrowserUnsupported, TaskError

HTML = """<!doctype html><body>
<div class="wendian-wrapper input-container">
<textarea name="aiSearchTextarea" aria-hidden="true"></textarea>
<textarea name="aiSearchTextarea" id="editor"></textarea>
<button title="新建会话" onclick="document.querySelectorAll('.ai-message,
 .user-message__text-wrapper, [data-round-id]').forEach(e=>e.remove());
 history.replaceState({},'', '/ai_chat');">
 新建会话</button>
<div id="cards"></div>
<div class="submit-button-wrapper"><svg><use href="#arrow_top"></use></svg></div>
</div><div id="messages"></div>
<script>
window.mode='finish'; window.reply='{}';
editor.addEventListener('input',()=>document.querySelector('.submit-button-wrapper')
 .classList.toggle('disabled',!editor.value));
editor.addEventListener('paste',async e=>{
 e.preventDefault();
 const r=await fetch('/api/sns/agentspark/web/note/detail',{method:'POST'});
 await r.json();
 cards.innerHTML='<div class="xhs-ai-selected-note-card">fixture video</div>';
});
editor.addEventListener('keydown',e=>{
 if(e.key!=='Enter') return;
 const round=document.createElement('div'); round.dataset.roundId=crypto.randomUUID();
 messages.append(round);
 const user=document.createElement('div'); user.className='user-message__text-wrapper';
 user.innerText=editor.value; round.append(user);
 const ai=document.createElement('div'); ai.className='ai-message';
 ai.innerHTML='<div class="xhs-ai-md-container"><pre><code></code></pre></div>';
 ai.querySelector('code').textContent=window.reply; round.append(ai);
 history.replaceState({},'', '/ai_chat?conversationId=fixture-chat');
 if(mode==='finish') ai.classList.add('ai-message-finished');
 if(mode==='delay') setTimeout(()=>ai.classList.add('ai-message-finished'),700);
 if(mode==='interrupt') {ai.classList.add('ai-message-finished');
 ai.innerHTML+='<div class="interrupt">中断</div>';} cards.innerHTML='';
});
</script></body>"""


@pytest.fixture(scope="module")
def chrome():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except PlaywrightError as exc:
            pytest.skip(f"Chrome unavailable: {exc}")
        yield browser
        browser.close()


@pytest.fixture
def driver(chrome, tmp_path, source, answer):
    context = chrome.new_context()
    context.route(CHAT + "*", lambda route: route.fulfill(content_type="text/html", body=HTML))
    context.route(
        "**/api/sns/agentspark/web/note/detail",
        lambda route: route.fulfill(
            json={"data": {"content_detail_list": [{"id": source.note_id}]}},
        ),
    )
    driver = SiteBrowser(tmp_path, timeout=3, interactive=False)
    driver.context = context
    driver.page = context.new_page()
    driver.new_chat()
    driver.chat_page.evaluate("reply=>window.reply=reply", answer)
    driver.raw_directory = tmp_path / "raw"
    yield driver
    context.close()


def test_finished_signal_and_same_conversation_repair(driver, source, answer):
    assert driver.ask("test", source) == answer
    assert driver.conversation_id == "fixture-chat"
    reference = json.loads((driver.raw_directory / "reference.json").read_text())
    assert reference["selected_note_ids"] == [source.note_id]
    assert driver.ask("repair", source, repair=True) == answer


def test_pre_send_busy_is_classified_without_sending(driver, source):
    driver.chat_page.evaluate("""() => {
        messages.innerHTML = '<div data-round-id="old"><div class="ai-message"></div></div>';
    }""")
    request = {"status": "prepared", "request_id": "current", "note_id": source.note_id}
    with pytest.raises(TaskError) as error:
        driver.begin_request("test", source, request, lambda r: None)
    assert error.value.code == "generation_busy"
    assert request["status"] == "prepared" and "sent_at" not in request
    assert driver.chat_page.locator('.user-message__text-wrapper').count() == 0
    request["conversation_id"] = "different-chat"
    with pytest.raises(BrowserUnsupported, match="发送前会话 ID 与批次不同"):
        driver.begin_request("test", source, request, lambda r: None)


def test_stable_partial_without_signal_times_out(driver, source):
    driver.timeout = 0.7
    driver.chat_page.evaluate("window.mode='partial'")
    with pytest.raises(TaskError) as error:
        driver.ask("test", source)
    assert error.value.code == "answer_timeout"
    assert (driver.raw_directory / "answer-partial.txt").is_file()


def test_restore_confirmed_empty_history_reports_unavailable(driver):
    driver.chat_page.close()
    driver.context.route(
        "**/api/sns/web/v1/dqa/history/detail?*",
        lambda route: route.fulfill(json={"success": True, "code": 0, "data": {"items": []}}),
    )
    body = HTML.replace(
        "window.mode='finish';",
        "fetch('/api/sns/web/v1/dqa/history/detail?conversation_id=missing-chat');"
        "window.mode='finish';",
    )
    driver.context.route(CHAT + "*", lambda route: route.fulfill(body=body))
    with pytest.raises(TaskError) as error:
        driver.restore_chat("missing-chat")
    assert error.value.code == "history_unavailable"
    assert error.value.evidence["conversation_id"] == "missing-chat"


def test_finish_marker_after_text_stops_changing(driver, source, answer):
    driver.chat_page.evaluate("window.mode='delay'")
    assert driver.ask("test", source) == answer


def test_prior_finished_answer_not_accepted(driver, source):
    driver.chat_page.evaluate("""() => {
        messages.innerHTML='<div class="ai-message ai-message-finished">old answer</div>';
        window.mode='partial';
    }""")
    driver.timeout = 0.7
    with pytest.raises(TaskError) as error:
        driver.ask("test", source)
    assert error.value.code == "answer_timeout"


def test_interruption_rejected(driver, source):
    driver.chat_page.evaluate("window.mode='interrupt'")
    with pytest.raises(TaskError) as error:
        driver.ask("test", source)
    assert error.value.code == "answer_incomplete"


def test_reference_mismatch_prevents_submission(driver, source):
    driver.context.route(
        "**/api/sns/agentspark/web/note/detail",
        lambda route: route.fulfill(
            json={"data": {"content_detail_list": [{"id": "000000000000000000000000"}]}},
        ),
    )
    with pytest.raises(TaskError) as error:
        driver.ask("test", source)
    assert error.value.code == "reference_mismatch"
    assert driver.chat_page.locator(".user-message__text-wrapper").count() == 0


def test_repair_requires_original_conversation(driver, source, answer):
    assert driver.ask("test", source) == answer
    driver.chat_page.evaluate("history.replaceState({},'', '/ai_chat?conversationId=other')")
    with pytest.raises(TaskError) as error:
        driver.ask("repair", source, repair=True)
    assert error.value.code == "page_unsupported"


def test_new_chat_clears_history(driver):
    driver.new_chat()
    assert "conversationId" not in driver.chat_page.url
    assert driver.chat_page.locator(".ai-message").count() == 0


def test_submission_does_not_activate_window(driver, source, answer, monkeypatch):
    def unexpected_activation():
        pytest.fail("Routine submission must not bring Chrome to the foreground")

    monkeypatch.setattr(driver.chat_page, "bring_to_front", unexpected_activation)
    assert driver.ask("background submission", source) == answer


def test_chat_rotation_reuses_page_and_clears_history(driver, source, answer, monkeypatch):
    assert driver.ask("first", source) == answer
    page = driver.chat_page
    pages = driver.context.pages
    driver.history_messages = {"first": {"server_message_id": "old"}}

    def unexpected_activation():
        pytest.fail("Chat rotation must not bring Chrome to the foreground")

    monkeypatch.setattr(page, "bring_to_front", unexpected_activation)
    driver.new_chat()
    assert driver.chat_page is page
    assert driver.context.pages == pages
    assert driver.history_messages == {}
    assert driver.chat_page.locator("[data-round-id]").count() == 0
    driver.chat_page.evaluate("reply=>window.reply=reply", answer)
    assert driver.ask("second", source) == answer


def test_restore_reuses_page_and_removes_history_observer(driver, monkeypatch):
    page = driver.chat_page
    driver.context.route(
        "**/api/sns/web/v1/dqa/history/detail?*",
        lambda route: route.fulfill(json={"success": True, "code": 0, "data": {"items": []}}),
    )
    body = HTML.replace(
        "window.mode='finish';",
        "fetch('/api/sns/web/v1/dqa/history/detail?conversation_id=missing-chat');"
        "window.mode='finish';",
    )
    driver.context.route(CHAT + "*", lambda route: route.fulfill(body=body))
    removed = []
    original = page.remove_listener

    def remove_listener(event, listener):
        removed.append(event)
        original(event, listener)

    monkeypatch.setattr(page, "remove_listener", remove_listener)
    with pytest.raises(TaskError) as error:
        driver.restore_chat("missing-chat")
    assert error.value.code == "history_unavailable"
    assert driver.chat_page is page
    assert removed == ["response"]


def test_reused_page_keeps_current_history_listener_until_navigation(driver, monkeypatch):
    body = HTML.replace(
        "window.mode='finish';",
        "messages.innerHTML='<div data-round-id=old>original</div>';"
        "fetch('/api/sns/web/v1/dqa/history/detail?conversation_id=restored');"
        "window.mode='finish';",
    )
    driver.context.route(CHAT + "*", lambda route: route.fulfill(body=body))
    driver.context.route(
        "**/api/sns/web/v1/dqa/history/detail?*",
        lambda route: route.fulfill(json={"data": {"items": [{"message": {
            "id": "server-old", "question": "original",
            "simple_data": {"base_info": {"text": "answer"}},
        }}]}}),
    )
    page = driver.chat_page
    removed = []
    original = page.remove_listener

    def remove_listener(event, listener):
        removed.append(listener)
        original(event, listener)

    monkeypatch.setattr(page, "remove_listener", remove_listener)
    driver.restore_chat("restored")
    page.wait_for_function("document.readyState === 'complete'")
    page.wait_for_timeout(100)
    assert driver.history_messages["original"]["server_message_id"] == "server-old"
    observer = driver.history_observer
    assert observer is not None and removed == []
    driver.context.route(CHAT + "*", lambda route: route.fulfill(body=HTML))
    driver.new_chat()
    assert driver.chat_page is page
    assert removed == [observer]
    assert driver.history_observer is None
    assert driver.history_messages == {}


def test_noninteractive_login_requires_action(driver):
    driver.chat_page.evaluate("document.body.innerHTML='<div>扫码登录</div>'")
    with pytest.raises(TaskError) as error:
        driver.ensure_access(driver.chat_page)
    assert error.value.code == "login_required"


def test_unsupported_editor_fails_closed(driver):
    driver.context.route(CHAT + "*", lambda route: route.fulfill(body="<body>Changed</body>"))
    # Keep this adapter-contract test quick without changing production timeouts.
    original = driver.context.new_page

    def new_page():
        page = original()
        page.locator = lambda *a, **kw: MissingEditor()
        return page

    class MissingEditor:
        def wait_for(self, **kw):
            raise PlaywrightError("missing editor")

    driver.context.new_page = new_page
    with pytest.raises(BrowserUnsupported):
        driver.new_chat()


def test_collection_observes_only_matching_profile_pagination(driver, source):
    profile = "1" * 24
    url = f"https://www.xiaohongshu.com/user/profile/{profile}"
    script = """<body><script>
      fetch('https://edith.xiaohongshu.com/api/sns/web/v1/user_posted?user_id=PROFILE');
    </script></body>""".replace("PROFILE", profile)
    driver.context.route(url, lambda route: route.fulfill(body=script, content_type="text/html"))
    driver.context.route(
        "**/api/sns/web/v1/user_posted?*",
        lambda route: route.fulfill(
            json={
                "success": True,
                "data": {
                    "has_more": False,
                    "notes": [
                        {
                            "note_id": source.note_id,
                            "type": "video",
                            "xsec_token": "fixture",
                            "display_title": source.title,
                        }
                    ],
                },
            }
        ),
    )
    batches = []
    result = driver.collect_profile(url, lambda notes: batches.extend(notes))
    assert result["status"] == "complete"
    assert result["end_signal"] == "user_posted.has_more=false"
    assert batches[0]["type"] == "video"
    assert "xsec_token=fixture" in batches[0]["url"]


def test_collection_wrong_profile_and_stagnation_not_complete(driver):
    profile = "2" * 24
    url = f"https://www.xiaohongshu.com/user/profile/{profile}"
    driver.context.route(
        url, lambda route: route.fulfill(content_type="text/html", body="<body>没有更多了</body>")
    )
    result = driver.collect_profile(url, lambda notes: None)
    assert result["status"] == "incomplete"
    assert result["observed_notes"] == 0


def test_collection_bad_response_is_incomplete(driver):
    profile = "3" * 24
    url = f"https://www.xiaohongshu.com/user/profile/{profile}"
    driver.context.route(
        url,
        lambda route: route.fulfill(
            body=f"""<script>
      fetch('/api/sns/web/v1/user_posted?user_id={profile}');</script>""",
            content_type="text/html",
        ),
    )
    driver.context.route(
        "**/api/sns/web/v1/user_posted?*",
        lambda route: route.fulfill(json={"success": False, "data": {}}),
    )
    assert driver.collect_profile(url, lambda notes: None)["status"] == "incomplete"


def test_same_chat_next_video_ignores_historical_cards(driver, source, answer):
    from xhs_to_opencook.models import Source

    assert driver.ask("first", source) == answer
    driver.chat_page.evaluate("""() => {
        const card=document.createElement('div');card.className='xhs-ai-selected-note-card';
        card.innerText='historical video';messages.firstChild.append(card);
    }""")
    second = Source(**{**source.model_dump(), "note_id": "1" * 24})
    driver.context.route(
        "**/api/sns/agentspark/web/note/detail",
        lambda route: route.fulfill(
            json={"data": {"content_detail_list": [{"id": second.note_id}]}}
        ),
    )
    assert driver.ask("second", second) == answer
    assert driver.conversation_id == "fixture-chat"
    assert driver.chat_page.locator("[data-round-id]").count() == 2


def test_wrong_round_id_fails_closed(driver, source, answer):
    assert driver.ask("first", source) == answer
    with pytest.raises(BrowserUnsupported):
        driver.request_snapshot(
            {"request_id": "missing", "note_id": source.note_id, "conversation_id": "other"}
        )


def test_editor_waits_for_rerender_and_editability(driver):
    driver.chat_page.evaluate("""() => {
        editor.readOnly = true;
        setTimeout(() => {
            const replacement = editor.cloneNode(true);
            editor.replaceWith(replacement);
            setTimeout(() => { replacement.readOnly = false; }, 400);
        }, 400);
    }""")
    editor = driver.wait_editor_ready()
    assert editor.is_editable()
    assert driver.chat_page.locator('.user-message__text-wrapper').count() == 0


def test_editor_waits_until_composer_enters_viewport(driver):
    driver.chat_page.evaluate("""() => {
        const composer = editor.closest('.wendian-wrapper');
        composer.style.position = 'absolute';
        composer.style.top = `${innerHeight + 100}px`;
        setTimeout(() => { composer.style.top = '20px'; }, 900);
    }""")
    editor = driver.wait_editor_ready()
    box = editor.bounding_box()
    assert box['y'] + box['height'] <= driver.chat_page.viewport_size['height']
    assert driver.chat_page.locator('.user-message__text-wrapper').count() == 0


def test_editor_waits_until_loading_overlay_is_removed(driver):
    driver.chat_page.evaluate("""() => {
        const overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;background:white';
        overlay.id = 'loading-overlay';document.body.append(overlay);
        setTimeout(() => overlay.remove(), 900);
    }""")
    assert driver.wait_editor_ready().is_editable()
    assert driver.chat_page.locator('#loading-overlay').count() == 0
    assert driver.chat_page.locator('.user-message__text-wrapper').count() == 0


def test_explicit_finished_empty_placeholder_can_fail_fast(driver, source):
    driver.chat_page.evaluate("""() => {
        window.reply = '';editor.addEventListener('keydown', e => {
            if(e.key !== 'Enter') return;
            const ai = messages.lastChild.querySelector('.ai-message');
            ai.innerHTML += '<div class="empty">（暂无内容）</div>';
        });
    }""")
    request = {'request_id': 'empty-test', 'note_id': source.note_id}
    driver.begin_request(f"{source.note_id} empty-test", source, request, lambda r: None)
    assert driver.request_snapshot(request)['empty_placeholder']
    with pytest.raises(TaskError) as error:
        driver.wait_request(request, 3, lambda r: None, empty_grace=0.3)
    assert error.value.code == 'empty_answer'
    assert request['empty_answer_evidence']['finished']


def test_placeholder_without_generation_finish_does_not_fail_fast(driver, source):
    driver.chat_page.evaluate("""() => {
        window.reply = '';window.mode = 'partial';
        editor.addEventListener('keydown', e => {
            if(e.key === 'Enter') messages.lastChild.querySelector('.ai-message')
                .innerHTML += '<div>（暂无内容）</div>';
        });
    }""")
    request = {'request_id': 'partial-test', 'note_id': source.note_id}
    driver.begin_request(f"{source.note_id} partial-test", source, request, lambda r: None)
    with pytest.raises(TaskError) as error:
        driver.wait_request(request, 0.7, lambda r: None, empty_grace=0.1)
    assert error.value.code == 'answer_timeout'

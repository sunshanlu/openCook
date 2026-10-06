"""Real Chrome against a local routed UI fixture, never the user's profile or the live site."""

import json

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from xhs_to_opencook.browser import CHAT, SiteBrowser
from xhs_to_opencook.errors import BrowserUnsupported, TaskError

HTML = """<!doctype html><body>
<textarea name="aiSearchTextarea" aria-hidden="true"></textarea>
<textarea name="aiSearchTextarea" id="editor"></textarea>
<button title="新建会话" onclick="document.querySelectorAll('.ai-message,
 .user-message__text-wrapper').forEach(e=>e.remove());history.replaceState({},'', '/ai_chat');">
 新建会话</button>
<div id="cards"></div><div id="messages"></div>
<script>
window.mode='finish'; window.reply='{}';
editor.addEventListener('paste',async e=>{
 e.preventDefault();
 const r=await fetch('/api/sns/agentspark/web/note/detail',{method:'POST'});
 await r.json();
 cards.innerHTML='<div class="xhs-ai-selected-note-card">fixture video</div>';
});
editor.addEventListener('keydown',e=>{
 if(e.key!=='Enter') return;
 const user=document.createElement('div'); user.className='user-message__text-wrapper';
 user.innerText=editor.value; messages.append(user);
 const ai=document.createElement('div'); ai.className='ai-message';
 ai.innerHTML='<div class="xhs-ai-md-container"><pre><code></code></pre></div>';
 ai.querySelector('code').textContent=window.reply; messages.append(ai);
 history.replaceState({},'', '/ai_chat?conversationId=fixture-chat');
 if(mode==='finish') ai.classList.add('ai-message-finished');
 if(mode==='delay') setTimeout(()=>ai.classList.add('ai-message-finished'),700);
 if(mode==='interrupt') ai.innerHTML+='<div class="interrupt">中断</div>';
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
    context.route("**/api/sns/agentspark/web/note/detail", lambda route: route.fulfill(
        json={"data": {"content_detail_list": [{"id": source.note_id}]}},
    ))
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


def test_stable_partial_without_signal_times_out(driver, source):
    driver.timeout = 0.7
    driver.chat_page.evaluate("window.mode='partial'")
    with pytest.raises(TaskError) as error:
        driver.ask("test", source)
    assert error.value.code == "answer_timeout"
    assert (driver.raw_directory / "answer-partial.txt").is_file()


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
    driver.context.route("**/api/sns/agentspark/web/note/detail", lambda route: route.fulfill(
        json={"data": {"content_detail_list": [{"id": "000000000000000000000000"}]}},
    ))
    with pytest.raises(TaskError) as error:
        driver.ask("test", source)
    assert error.value.code == "reference_mismatch"
    assert driver.chat_page.locator(".user-message__text-wrapper").count() == 0


def test_repair_requires_original_conversation(driver, source, answer):
    assert driver.ask("test", source) == answer
    driver.chat_page.evaluate("history.replaceState({},'', '/ai_chat?conversationId=other')")
    with pytest.raises(TaskError) as error:
        driver.ask("repair", source, repair=True)
    assert error.value.code == "reference_mismatch"


def test_new_chat_clears_history(driver):
    driver.new_chat()
    assert "conversationId" not in driver.chat_page.url
    assert driver.chat_page.locator(".ai-message").count() == 0


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

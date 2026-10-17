import pytest
from playwright.sync_api import sync_playwright

from xhs_to_opencook.browser import SiteBrowser
from xhs_to_opencook.errors import TaskError


@pytest.fixture
def access_browser(tmp_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        driver = SiteBrowser(tmp_path / "chrome", interactive=False)
        driver.page = driver.chat_page = page
        yield driver, page
        browser.close()


def test_note_text_and_hidden_dialog_do_not_require_login(access_browser):
    driver, page = access_browser
    page.set_content('''<body>
      <a href="/user/profile/65e1e13a000000000500b6d7">我</a>
      <article>扫码登录 手机号登录 安全验证 拖动滑块</article>
      <div class="login-modal" style="display:none">扫码登录</div>
    </body>''')
    assert driver.ensure_access(page, require_account=True) is False


@pytest.mark.parametrize(("markup", "code"), [
    ('<div class="login-modal">扫码登录</div>', 'login_required'),
    ('<div class="captcha-container">拖动滑块完成验证</div>', 'verification_required'),
])
def test_visible_access_controls_have_distinct_evidence(access_browser, markup, code):
    driver, page = access_browser
    page.set_content(markup)
    with pytest.raises(TaskError) as failure:
        driver.ensure_access(page, require_account=True)
    assert failure.value.code == code
    assert failure.value.evidence['matched']


@pytest.mark.parametrize('appears', [True, False])
def test_missing_account_marker_waits_without_claiming_login_failure(access_browser, appears):
    original, page = access_browser
    clock, waits = [0], []

    def wait(seconds):
        clock[0] += seconds
        waits.append(seconds)
        if appears and clock[0] == 10:
            page.set_content('<a href="/user/profile/' + '1' * 24 + '">我</a>')

    driver = SiteBrowser(original.profile, interactive=False,
                         access_clock=lambda: clock[0], access_wait=wait)
    page.set_content('<body>页面加载中</body>')
    if appears:
        assert driver.ensure_access(page, require_account=True) is False
        assert waits == [5, 5]
    else:
        with pytest.raises(TaskError) as failure:
            driver.ensure_access(page, require_account=True)
        assert failure.value.code == 'page_state_unconfirmed'
        assert clock[0] == 30
        evidence = list((driver.profile.parent / 'access-diagnostics').glob('*/evidence.json'))
        assert len(evidence) == 1


def test_snapshot_failure_does_not_replace_access_error(access_browser, monkeypatch):
    driver, page = access_browser
    page.set_content('<div class="captcha-container">安全验证</div>')

    def failed_screenshot(**kwargs):
        raise OSError('disk unavailable')

    monkeypatch.setattr(page, 'screenshot', failed_screenshot)
    with pytest.raises(TaskError) as failure:
        driver.ensure_access(page)
    assert failure.value.code == 'verification_required'


def test_opacity_hidden_login_is_not_visible(access_browser):
    driver, page = access_browser
    page.set_content('<div style="opacity:0"><div class="login-modal">扫码登录</div></div>')
    assert driver.ensure_access(page) is False


def test_ordinary_dialog_keywords_are_not_authentication_controls(access_browser):
    driver, page = access_browser
    page.set_content('<div role="dialog">教程介绍扫码登录和安全验证的用法</div>')
    assert driver.ensure_access(page) is False


def test_diagnostic_evidence_redacts_url_credentials(access_browser):
    driver, page = access_browser
    page.route('https://example.test/**', lambda route: route.fulfill(
        content_type='text/html; charset=utf-8',
        body='<div class="login-modal">扫码登录</div><script>secret="sensitive"</script>'
    ))
    page.goto('https://example.test/ai_chat?xsec_token=sensitive')
    with pytest.raises(TaskError) as failure:
        driver.ensure_access(page)
    assert failure.value.evidence['url'] == 'https://example.test/ai_chat'
    path = driver.profile.parent / 'access-diagnostics'
    assert 'sensitive' not in next(path.glob('*/page.html')).read_text()


def test_committed_navigation_timeout_checks_same_page_without_navigating_again(
    access_browser, monkeypatch,
):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    driver, page = access_browser
    page.route('https://example.test/**', lambda route: route.fulfill(
        content_type='text/html; charset=utf-8',
        body='<a href="/user/profile/' + '1' * 24 + '">我</a>',
    ))
    page.goto('https://example.test/ai_chat')
    navigations = []

    def timeout(url, **kwargs):
        navigations.append(url)
        raise PlaywrightTimeoutError('navigation timeout after commit')

    monkeypatch.setattr(page, 'goto', timeout)
    assert driver.visit(page, 'https://example.test/ai_chat') is None
    assert navigations == ['https://example.test/ai_chat']


def test_recovered_access_saves_a_fresh_snapshot_when_the_same_error_returns(access_browser):
    driver, page = access_browser
    for _ in range(2):
        page.set_content('<div class="login-modal">扫码登录</div>')
        with pytest.raises(TaskError):
            driver.ensure_access(page)
        page.set_content('<body>已恢复</body>')
        assert driver.ensure_access(page) is False
    assert len(list((driver.profile.parent / 'access-diagnostics').glob('*/evidence.json'))) == 2


def test_corrupt_diagnostic_journal_does_not_block_access(access_browser):
    driver, page = access_browser
    directory = driver.profile.parent / 'access-diagnostics'
    directory.mkdir()
    (directory / 'last-chrome.json').write_text('broken JSON')
    page.set_content('<body>可访问</body>')
    assert driver.ensure_access(page) is False

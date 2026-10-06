import json
from types import SimpleNamespace

import pytest
from chat_adapter import install
from playwright.sync_api import Error as PlaywrightError

from xhs_to_opencook.errors import BrowserUnsupported, TaskError
from xhs_to_opencook.models import Source
from xhs_to_opencook.runner import run
from xhs_to_opencook.storage import Store


class FakeBrowser:
    def __init__(self, source, answer, image):
        self.source, self.answer, self.image = source, answer, image
        self.conversation_id = "fixture-chat"
        self.asks, self.opens, self.repairs = 0, 0, 0
        self.failure = None
        self.cover_failure = False

    def __enter__(self):
        install(self)
        return self

    def __exit__(self, *args):
        pass

    def open_source(self, url):
        self.opens += 1
        if "bad" in url:
            raise TaskError("link_invalid", "不存在", retryable=False)
        return self.source

    def new_chat(self):
        pass

    def ask(self, prompt, source, *, repair=False):
        self.asks += 1
        self.repairs += int(repair)
        if self.failure:
            raise self.failure
        return self.answer

    def download_cover(self, url):
        if self.cover_failure:
            raise TaskError("image_failed", "封面不可访问")
        return self.image

    def diagnostics(self, directory):
        pass


@pytest.fixture
def setup(tmp_path, source, answer, image_bytes):
    args = SimpleNamespace(
        links=tmp_path / "links.txt",
        output=tmp_path / "output",
        state_dir=tmp_path / "state",
        timeout=180,
        interval=10,
        non_interactive=True,
        refresh=False,
    )
    args.links.write_text(source.url)
    driver = FakeBrowser(source, answer, image_bytes)
    return args, driver


def execute(args, driver):
    code = run(args, browser_factory=lambda *a, **kw: driver, sleeper=lambda seconds: None)
    return code, json.loads((args.output / "report.json").read_text())


def test_duplicates_shortlinks_failure_continues_and_offline_cache(setup):
    args, driver = setup
    url = driver.source.url
    args.links.write_text(f"https://xhslink.com/bad\n{url}\n{url}\nhttps://xhslink.com/a/alias")
    code, report = execute(args, driver)
    assert code == 2
    assert [t["status"] for t in report["tasks"]] == ["failed", "success", "skipped", "skipped"]
    assert driver.asks == 1
    assert report["tasks"][1]["estimates"][0]["estimated"] is True
    assert list((args.output / "raw").rglob("answer.txt"))
    args.links.write_text(f"https://xhslink.com/a/alias\n{url}")

    def unexpected_browser(*a, **kw):
        pytest.fail("successful cache must not start Chrome")

    assert run(args, browser_factory=unexpected_browser) == 0
    assert json.loads((args.output / "report.json").read_text())["tasks"][0]["status"] == "cached"
    args.refresh = True
    execute(args, driver)
    assert driver.asks == 2


def test_one_repair_two_attempts_and_retry_next_run(setup):
    args, driver = setup
    good = driver.answer
    driver.answer = "{}"
    code, report = execute(args, driver)
    assert code == 2
    assert driver.asks == 3 and driver.repairs == 1
    assert report["tasks"][0]["attempts"] == 2
    assert report["tasks"][0]["error"]["code"] == "json_invalid"
    driver.answer = good
    assert execute(args, driver)[0] == 0


def test_image_failure_is_text_success(setup):
    args, driver = setup
    driver.cover_failure = True
    code, report = execute(args, driver)
    assert code == 0 and report["recipe_count"] == 1
    assert report["tasks"][0]["image_error"]["code"] == "image_failed"


def test_adapter_failure_stops_batch(setup):
    args, driver = setup
    args.links.write_text(driver.source.url + "\nhttps://xhslink.com/a/other")
    driver.failure = BrowserUnsupported("页面已变更")
    code, report = execute(args, driver)
    assert code == 1 and report["status"] == "blocked"
    assert driver.asks == 1 and report["unprocessed_count"] == 1


def test_interrupt_archives_partial_and_resumes(setup):
    args, driver = setup
    original = driver.ask

    def interrupt(*a, **kw):
        (driver.raw_directory / "answer-partial.txt").write_text("partial")
        raise KeyboardInterrupt

    driver.ask = interrupt
    code, report = execute(args, driver)
    assert code == 130 and report["tasks"][0]["status"] == "interrupted"
    assert list((args.output / "raw").rglob("answer-partial.txt"))
    driver.ask = original
    assert execute(args, driver)[0] == 0


def test_corrupt_cache_reprocessed(setup):
    args, driver = setup
    assert execute(args, driver)[0] == 0
    store = Store(args.state_dir)
    store.db.execute("UPDATE records SET value='invalid json'")
    store.db.commit()
    store.close()
    assert execute(args, driver)[0] == 0
    assert driver.asks == 2


def test_names_collision_persisted(setup):
    args, driver = setup
    first = driver.source
    second = Source(**{**first.model_dump(), "note_id": "12345678000000001c02c2e0"})
    second.url = f"https://www.xiaohongshu.com/explore/{second.note_id}"
    args.links.write_text(first.url + "\n" + second.url)
    driver.open_source = lambda url: first if url == first.url else second
    assert execute(args, driver)[0] == 0
    names = [r["name"] for r in json.loads((args.output / "recipes.json").read_text())]
    assert len(set(names)) == 2 and second.note_id in names[1]
    args.links.write_text(second.url)
    assert execute(args, driver)[0] == 0
    assert json.loads((args.output / "recipes.json").read_text())[0]["name"] == names[1]


def test_closed_browser_stops_then_reconciles_on_resume(setup):
    args, driver = setup
    original = driver.ask
    calls = []

    def ask(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise PlaywrightError("Target page, context or browser has been closed")
        return original(*a, **kw)

    driver.ask = ask
    factories = []

    def factory(*a, **kw):
        factories.append(1)
        return driver

    assert run(args, browser_factory=factory, sleeper=lambda _: None) == 1
    assert len(factories) == 1 and len(calls) == 1
    assert run(args, browser_factory=factory, sleeper=lambda _: None) == 0
    assert len(factories) == 2 and len(calls) == 2
    report = json.loads((args.output / "report.json").read_text())
    assert report["tasks"][0]["attempts"] == 1
    assert report["tasks"][0]["raw_directory"].startswith("raw/")

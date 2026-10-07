import csv
import json
from types import SimpleNamespace

import pytest
from chat_adapter import install

from xhs_to_opencook.errors import BrowserUnsupported, TaskError
from xhs_to_opencook.links import profile_id, read_profiles
from xhs_to_opencook.models import Source, parse_classification
from xhs_to_opencook.storage import Store, atomic_write
from xhs_to_opencook.workflow import run_workflow

P1, P2 = "1" * 24, "2" * 24
N1, N2, N3 = "a" * 24, "b" * 24, "c" * 24


class Browser:
    def __init__(self, answer, image):
        self.answer, self.image = answer, image
        self.conversation_id = "chat"
        self.events = []
        self.verdicts = {N1: "tutorial", N2: "non_tutorial", N3: "unknown"}
        self.notes = {P1: [N1, N2, N3], P2: [N1]}
        self.replies = []
        self.failure = None
        self.incomplete = False
        self.opens = []

    def __enter__(self):
        install(self)
        return self

    def __exit__(self, *args):
        pass

    def collect_profile(self, url, save):
        self.events.append("collect")
        for identity in self.notes[profile_id(url)]:
            save(
                [
                    {
                        "note_id": identity,
                        "url": f"https://www.xiaohongshu.com/explore/{identity}",
                        "type": "video",
                    }
                ]
            )
        if self.failure:
            raise self.failure
        return {"status": "incomplete" if self.incomplete else "complete", "observed_notes": 3}

    def open_source(self, url):
        identity = url.split("/")[-1]
        self.opens.append(identity)
        return Source(
            note_id=identity,
            url=url,
            author="same name",
            title="note",
            cover_url="https://example.test/image",
        )

    def new_chat(self):
        self.events.append("new_chat")

    def ask(self, prompt, source, *, repair=False):
        stage = "classify" if "verdict" in prompt else "extract"
        self.events.append(stage)
        if self.failure:
            raise self.failure
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        if stage == "classify":
            return json.dumps(
                {
                    "note_id": source.note_id,
                    "verdict": self.verdicts[source.note_id],
                    "reason": "视频依据",
                }
            )
        return self.answer

    def download_cover(self, url):
        return self.image

    def diagnostics(self, directory):
        pass


@pytest.fixture
def setup(tmp_path, answer, image_bytes):
    args = SimpleNamespace(
        command="pipeline",
        profiles=tmp_path / "profiles.txt",
        state_dir=tmp_path / "state",
        output=tmp_path / "output",
        timeout=180,
        interval=10,
        non_interactive=True,
        reclassify=False,
        refresh=False,
        max_attempts=2,
    )
    args.profiles.write_text(
        f"https://www.xiaohongshu.com/user/profile/{P1}\n"
        f"https://www.xiaohongshu.com/user/profile/{P2}\n"
    )
    return args, Browser(answer, image_bytes)


def execute(args, browser):
    ticks = [1000.0]
    code = run_workflow(args, browser_factory=lambda *a, **kw: browser,
                        sleeper=lambda n: ticks.__setitem__(0, ticks[0] + n),
                        clock=lambda: ticks[0])
    return code, json.loads((args.output / "report.json").read_text())


def test_profile_pipeline_never_opens_collected_note_pages(setup, monkeypatch):
    args, browser = setup

    def unexpected_open(url):
        pytest.fail("Collected video URLs must be submitted directly to Diandian")

    monkeypatch.setattr(browser, "open_source", unexpected_open)
    code, report = execute(args, browser)
    assert code == 2  # The fixture includes an unknown classification.
    assert browser.events.count("classify") == 3
    assert browser.events.count("extract") == 1
    assert report["recipe_count"] == 1
    store = Store(args.state_dir)
    saved = store.get(N1)["source"]
    assert saved["note_id"] == N1
    assert saved["author"].startswith("作者未记录")
    assert saved["cover_url"] is None
    store.close()


def test_extraction_uses_saved_classification_source_without_note_navigation(setup, monkeypatch):
    args, browser = setup
    args.command = "collect"
    execute(args, browser)
    store = Store(args.state_dir)
    saved = browser.open_source(f"https://www.xiaohongshu.com/explore/{N1}")
    store.stage_save("classifications", N1, {
        "status": "success", "source": saved.model_dump(),
        "classification": {"note_id": N1, "verdict": "tutorial", "reason": "制作过程"},
    })
    store.close()
    args.command = "extract"

    def unexpected_open(url):
        pytest.fail("Extraction should reuse the classification source")

    monkeypatch.setattr(browser, "open_source", unexpected_open)
    code, report = execute(args, browser)
    assert code == 0 and report["recipe_count"] == 1
    store = Store(args.state_dir)
    assert store.get(N1)["source"] == saved.model_dump()
    store.close()


def test_collected_metadata_survives_recollection_and_reaches_export(setup, monkeypatch):
    args, browser = setup
    args.command = "collect"
    execute(args, browser)
    store = Store(args.state_dir)
    for profile in (P1, P2):
        store.collect_note(profile, {
            "note_id": N1, "url": f"https://www.xiaohongshu.com/explore/{N1}",
            "type": "video", "title": "主页标题", "author": "主页作者",
            "author_id": profile, "cover_url": "https://example.test/image",
        })
        store.collect_note(profile, {
            "note_id": N1, "url": f"https://www.xiaohongshu.com/explore/{N1}",
            "type": None, "title": "", "author": "", "cover_url": None,
        })
    store.close()
    args.command = "pipeline"
    monkeypatch.setattr(browser, "open_source", lambda url: pytest.fail("No detail navigation"))
    code, report = execute(args, browser)
    assert code == 2 and report["recipe_count"] == 1
    store = Store(args.state_dir)
    record = store.get(N1)
    assert record["source"]["title"] == "主页标题"
    assert record["source"]["author"] == "主页作者"
    assert record["source"]["cover_url"] == "https://example.test/image"
    assert record["cover_path"] and record["image_error"] is None
    store.close()


@pytest.mark.parametrize("field", ["source", "url"])
def test_cached_identity_mismatch_is_rejected_before_submission(setup, field):
    args, browser = setup
    args.command = "collect"
    execute(args, browser)
    store = Store(args.state_dir)
    note = {"note_id": N1, "url": f"https://www.xiaohongshu.com/explore/{N1}"}
    if field == "source":
        note["source"] = browser.open_source(
            f"https://www.xiaohongshu.com/explore/{N2}"
        ).model_dump()
    else:
        note["url"] = f"https://www.xiaohongshu.com/explore/{N2}"
    store.collect_note(P1, note)
    store.collect_note(P2, note)
    store.close()
    args.command = "classify"
    _, report = execute(args, browser)
    assert report["tasks"][0]["error"]["code"] == "reference_mismatch"
    assert report["status"] == "blocked"
    assert browser.events.count("classify") == 0  # Identity contradictions stop the stage.


def test_empty_answer_exhaustion_continues_classification_and_extraction(setup, monkeypatch):
    from xhs_to_opencook.workflow import Workflow

    args, browser = setup
    args.max_attempts = 2
    browser.verdicts[N2] = "tutorial"
    browser.verdicts[N3] = "non_tutorial"
    original_driver = Workflow.driver

    def driver(flow):
        transport = original_driver(flow)
        if not getattr(transport, "empty_injected", False):
            original_wait = transport.wait_request

            def wait(request, timeout, persist):
                if request["note_id"] == N1 and request["stage"] == "classify":
                    request["empty_answer_evidence"] = {"finished": True}
                    persist(request)
                    raise TaskError("empty_answer", "（暂无内容）")
                return original_wait(request, timeout, persist)

            transport.wait_request = wait
            transport.empty_injected = True
        return transport

    monkeypatch.setattr(Workflow, "driver", driver)
    code, report = execute(args, browser)
    assert code == 2 and report["status"] == "partial"
    assert not report["pending_requests"]
    tasks = {(t.get("stage"), t.get("note_id")): t for t in report["tasks"]}
    failure = tasks["classify", N1]
    assert failure["attempts"] == 2
    assert failure["error"]["code"] == "manual_review_required"
    assert tasks["classify", N2]["status"] == "success"
    assert "extract" in browser.events
    store = Store(args.state_dir)
    requests = [r for r in store.stage_rows("chat_requests") if r["note_id"] == N1]
    assert [r["attempt"] for r in requests] == [1, 2]
    store.close()
    assert report["manual_review_count"] == 1
    with (args.output / "manual_review.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert [(r["笔记ID"], r["失败阶段"]) for r in rows] == [(N1, "分类")]
    args.command = "classify"

    def unexpected_browser(*a, **kw):
        pytest.fail("review tasks and successful cache must not submit again")

    assert run_workflow(args, browser_factory=unexpected_browser) == 2
    monkeypatch.setattr(Workflow, "driver", original_driver)
    del browser.empty_injected
    args.retry_review = True
    assert execute(args, browser)[0] == 0
    with (args.output / "manual_review.csv").open(encoding="utf-8-sig", newline="") as file:
        assert list(csv.DictReader(file))[0]["状态"] == "已解除待审核"


@pytest.mark.parametrize("scenario", ["pre_send_busy", "cached_answer_busy", "old_over_budget"])
def test_recovery_failures_review_and_continue_full_stage(setup, monkeypatch, scenario):
    from test_chat import Transport

    from xhs_to_opencook.chat import ChatSession
    from xhs_to_opencook.workflow import Workflow

    args, browser = setup
    browser.verdicts[N3] = "non_tutorial"
    args.command = "collect"
    assert execute(args, browser)[0] == 0
    args.command = "classify"
    if scenario != "pre_send_busy":
        store = Store(args.state_dir)
        transport = Transport()
        manager = ChatSession(store, lambda: transport, max_attempts=30,
                              clock=lambda: transport.clock, sleeper=lambda s: None)
        src = browser.open_source(f"https://www.xiaohongshu.com/explore/{N1}")
        manager.process(src, "classify", store.attempt_directory(N1, 1))
        request = manager.current
        request.update(attempt=3 if scenario == "old_over_budget" else 2,
                       status="submitted" if scenario == "old_over_budget" else "validated")
        manager.persist(request)
        store.close()

    original_init, original_driver = Workflow.__init__, Workflow.driver
    clock = [1000]

    def initialize(flow, *a, **kw):
        original_init(flow, *a, **kw)
        flow.chats.clock = lambda: clock[0]
        flow.chats.sleeper = lambda s: clock.__setitem__(0, clock[0] + s)
        flow.chats.cooldown = 2

    def driver(flow):
        transport = original_driver(flow)
        if not getattr(transport, "recovery_injected", False):
            transport.recovery_injected = True
            if scenario == "pre_send_busy":
                original_begin = transport.begin_request

                def begin(prompt, src, request, persist):
                    if request["note_id"] == N1:
                        raise TaskError("generation_busy", "上一轮生成未确认结束")
                    original_begin(prompt, src, request, persist)

                transport.begin_request = begin
            elif scenario == "cached_answer_busy":
                transport.confirm_idle = lambda: False
                transport.stop_generation = lambda: False
        return transport

    monkeypatch.setattr(Workflow, "__init__", initialize)
    monkeypatch.setattr(Workflow, "driver", driver)
    code, report = execute(args, browser)
    assert code == 2 and report["status"] == "partial"
    assert report["manual_review_count"] == 1 and not report["pending_requests"]
    tasks = {t["note_id"]: t for t in report["tasks"]}
    assert tasks[N1]["status"] == "awaiting_review"
    assert tasks[N2]["status"] == "success" and tasks[N3]["status"] == "success"
    with (args.output / "manual_review.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 1 and len(rows[0]) == 12
    assert rows[0]["笔记ID"] == N1


def test_phase_order_scope_and_offline_export(setup):
    args, browser = setup
    code, report = execute(args, browser)
    assert code == 2 and report["recipe_count"] == 1
    assert browser.events.count("classify") == 3
    assert browser.events.index("extract") > max(
        i for i, x in enumerate(browser.events) if x == "classify"
    )
    assert browser.events.count("new_chat") == 3
    assert report["classification_counts"] == {"tutorial": 1, "non_tutorial": 1, "unknown": 0}
    assert report["manual_review_count"] == 1
    collection = json.loads((args.output / "collection.json").read_text())
    assert collection["notes"][0]["profile_ids"] == [P1, P2]
    assert len((args.output / "tutorial_links.txt").read_text().splitlines()) == 1
    args.command = "export"

    def unexpected(*a, **kw):
        pytest.fail("export must work offline")

    assert run_workflow(args, browser_factory=unexpected) == 2
    assert (args.output / "recipes.zip").is_file()
    browser.notes[P2] = [N2]
    args.command = "collect"
    execute(args, browser)
    # Historical membership is retained when a later traversal misses a note.
    args.profiles.write_text(f"https://www.xiaohongshu.com/user/profile/{P2}")
    args.command = "export"
    assert run_workflow(args, browser_factory=unexpected) == 0
    assert len(json.loads((args.output / "recipes.json").read_text())) == 1


def test_cache_unknown_frozen_refresh_and_reclassification_excludes(setup):
    args, browser = setup
    execute(args, browser)
    browser.events.clear()
    args.command = "classify"
    execute(args, browser)
    assert browser.events.count("classify") == 0
    args.command = "extract"
    execute(args, browser)
    assert browser.events.count("extract") == 0
    args.refresh = True
    execute(args, browser)
    assert browser.events.count("extract") == 1
    args.command, args.reclassify = "classify", True
    browser.verdicts[N1] = "non_tutorial"
    execute(args, browser)
    args.command = "export"
    assert execute(args, browser)[1]["recipe_count"] == 0
    store = Store(args.state_dir)
    assert store.get(N1)["status"] == "success"
    store.close()


def test_partial_collection_continues_other_profiles_and_ai(setup):
    args, browser = setup
    browser.incomplete = True
    code, report = execute(args, browser)
    assert code == 2 and report["recipe_count"] == 1
    assert report["incomplete_profiles"] == [P1, P2]
    assert browser.events.count("collect") == 2


def test_collection_required_and_invalid_profiles(setup):
    args, browser = setup
    args.command = "classify"
    assert execute(args, browser)[1]["error"]["code"] == "collection_required"
    with pytest.raises(TaskError):
        profile_id(f"https://www.xiaohongshu.com/user/profile/{P1}/{N1}")
    args.profiles.write_text("\ufeff\n" + f"https://www.xiaohongshu.com/user/profile/{P1}\n" * 2)
    assert len(read_profiles(args.profiles)) == 1


def test_legacy_cache_classified_then_reused_and_failure_preserves_it(setup):
    args, browser = setup
    execute(args, browser)
    store = Store(args.state_dir)
    store.db.execute("DELETE FROM classifications")
    store.db.commit()
    store.close()
    browser.events.clear()
    execute(args, browser)
    assert browser.events.count("classify") == 2
    assert browser.events.count("extract") == 0
    args.command, args.refresh = "extract", True
    browser.failure = TaskError("json_schema_invalid", "invalid")
    assert execute(args, browser)[0] == 2
    store = Store(args.state_dir)
    assert store.get(N1)["status"] == "success"
    assert store.stage_get("classifications", N1)["classification"]["verdict"] == "tutorial"
    assert store.stage_get("extraction_runs", N1)["status"] == "failed"
    store.close()


def test_single_repair_two_attempts_and_mismatch(setup):
    args, browser = setup
    browser.notes = {P1: [N1], P2: []}
    browser.replies = ["{}", "{}", "{}"]
    code, report = execute(args, browser)
    assert code == 2 and browser.events.count("classify") == 2
    assert report["recipe_count"] == 0
    with pytest.raises(TaskError) as error:
        parse_classification(json.dumps({"note_id": N2, "verdict": "tutorial", "reason": "x"}), N1)
    assert error.value.code == "reference_mismatch"


def test_interrupt_archives_and_packages_resumes(setup):
    args, browser = setup
    browser.replies = [KeyboardInterrupt()]
    code, report = execute(args, browser)
    assert code == 130 and report["status"] == "interrupted"
    assert (args.output / "recipes.zip").is_file()
    assert execute(args, browser)[1]["recipe_count"] == 1


def test_unsupported_stops_all_ai(setup):
    args, browser = setup
    args.command = "collect"
    execute(args, browser)
    args.command = "classify"
    browser.failure = BrowserUnsupported("changed")
    code, report = execute(args, browser)
    assert code == 1 and report["status"] == "blocked"
    assert browser.events.count("classify") == 1


def test_platform_restriction_exhausts_to_review_and_continues(setup, monkeypatch):
    args, browser = setup
    args.command = "collect"
    execute(args, browser)
    args.command = "classify"

    def restricted(prompt, source, *, repair=False):
        browser.events.append("classify")
        raise TaskError("platform_restricted", "访问频繁 300013", retryable=False)

    monkeypatch.setattr(browser, "ask", restricted)
    code, report = execute(args, browser)
    assert code == 2 and report["status"] == "partial"
    assert browser.opens == [] and browser.events.count("classify") == 18
    assert report["pending_requests"] == []
    assert report["manual_review_count"] == 3
    assert (args.output / "manual_review.csv").is_file()


def test_atomic_replace_failure_preserves_previous(tmp_path, monkeypatch):
    import os

    path = tmp_path / "recipes.zip"
    path.write_bytes(b"old")

    def fail(*a):
        raise OSError("replacement failed")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        atomic_write(path, b"new")
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        '{"note_id":"x"}',
        "not json",
        '{"note_id":"' + N1 + '","verdict":"tutorial","verdict":"unknown","reason":"x"}',
    ],
)
def test_invalid_classification_json(text):
    with pytest.raises(TaskError):
        parse_classification(text, N1)


def test_unknown_type_is_reported_without_opening_note_and_normal_skipped(setup):
    args, browser = setup
    original = browser.collect_profile

    def collect(url, save):
        def unknown(notes):
            for note in notes:
                note["type"] = None if note["note_id"] == N1 else "normal"
            save(notes)

        return original(url, unknown)

    browser.collect_profile = collect
    code, report = execute(args, browser)
    assert code == 2 and report["recipe_count"] == 0
    assert browser.opens == []
    notes = json.loads((args.output / "collection.json").read_text())["notes"]
    assert next(n for n in notes if n["note_id"] == N1)["type"] == "unknown"


def test_submit_interval_applies_to_repair(setup):
    from xhs_to_opencook.workflow import Workflow

    args, browser = setup
    store = Store(args.state_dir)
    sleeps = []
    flow = Workflow(args, store, {P1: "profile"}, lambda *a, **kw: browser, sleeps.append)
    source = browser.open_source(f"https://www.xiaohongshu.com/explore/{N1}")
    flow.submit("verdict", source)
    flow.submit("verdict", source, repair=True)
    assert len(sleeps) == 1 and 9 < sleeps[0] <= 10
    flow.close()
    store.close()


@pytest.mark.parametrize("exhausts", [False, True])
def test_full_workflow_handles_empty_reply_then_navigation_failure(setup, monkeypatch, exhausts):
    from playwright.sync_api import Error as PlaywrightError

    from xhs_to_opencook.workflow import Workflow

    args, browser = setup
    browser.verdicts[N3] = "non_tutorial"
    args.command = "collect"
    execute(args, browser)
    args.command = "classify"
    original = Workflow.driver
    calls = []
    sends = []

    def driver(flow):
        transport = original(flow)
        if not getattr(transport, "navigation_injected", False):
            new_chat = transport.new_chat
            begin, wait = transport.begin_request, transport.wait_request

            def new():
                calls.append(1)
                if (exhausts and 2 <= len(calls) <= 7) or (not exhausts and len(calls) == 2):
                    raise PlaywrightError("Page.goto: Timeout 8000ms exceeded")
                new_chat()

            def send(prompt, source, request, persist):
                sends.append((source.note_id, request["attempt"]))
                begin(prompt, source, request, persist)

            def reply(request, timeout, persist):
                if request["note_id"] == N1 and request["attempt"] == 1:
                    request['empty_answer_evidence'] = {'finished': True, 'empty_placeholder': True}
                    persist(request)
                    raise TaskError("empty_answer", "（暂无内容）")
                return wait(request, timeout, persist)

            transport.new_chat, transport.begin_request, transport.wait_request = new, send, reply
            transport.navigation_injected = True
        return transport

    monkeypatch.setattr(Workflow, 'driver', driver)
    code, report = execute(args, browser)
    assert code == (2 if exhausts else 0)
    assert report['status'] == ('partial' if exhausts else 'complete')
    assert report['pending_requests'] == []
    assert sends[0] == (N1, 1)
    if exhausts:
        assert sends[1][0] == N2
        assert report['manual_review_count'] == 1
        assert report['tasks'][0]['status'] == 'awaiting_review'
    else:
        assert sends[1] == (N1, 2) and sends[2][0] == N2
        assert report['manual_review_count'] == 0
    assert not any(task.get('error', {}).get('code') == 'page_unsupported'
                   for task in report['tasks'])


def test_outer_workflow_never_reinvokes_inner_after_unexpected_bug(setup, monkeypatch):
    from xhs_to_opencook.chat import ChatSession

    args, browser = setup
    args.command = 'collect'
    execute(args, browser)
    args.command = 'classify'
    calls = []

    def broken(*args):
        calls.append(1)
        raise TaskError('unexpected_error', '状态矛盾')

    monkeypatch.setattr(ChatSession, 'process', broken)
    code, report = execute(args, browser)
    assert code == 1 and report['status'] == 'blocked'
    assert len(calls) == 1 and len(report['tasks']) == 1


def test_unknown_python_bug_marks_current_video_failed_and_stops(setup, monkeypatch):
    from xhs_to_opencook.chat import ChatSession

    args, browser = setup
    args.command = 'collect'
    execute(args, browser)
    args.command = 'classify'
    calls = []

    def broken(*args):
        calls.append(1)
        raise RuntimeError('unexpected state')

    monkeypatch.setattr(ChatSession, 'process', broken)
    code, report = execute(args, browser)
    assert code == 1 and report['status'] == 'blocked'
    assert len(calls) == 1 and report['tasks'][0]['status'] == 'failed'
    assert report['tasks'][0]['error']['code'] == 'unexpected_error'
    store = Store(args.state_dir)
    assert store.stage_get('classifications', N1)['status'] == 'failed'
    store.close()

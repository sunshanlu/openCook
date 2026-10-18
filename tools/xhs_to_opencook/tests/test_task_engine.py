from copy import deepcopy

from xhs_to_opencook.task_engine import TaskExecutor


class Journal:
    def __init__(self):
        self.rows = {}

    def load(self, identity):
        return deepcopy(self.rows.get(identity))

    def save(self, identity, record):
        self.rows[identity] = deepcopy(record)


class Browser:
    def __init__(self):
        self.sent = []
        self.answer = None
        self.reconciled = []
        self.cancelled = 0

    def submit(self, request):
        self.sent.append(request['request_id'])
        return {'submitted': True}

    def reconcile(self, request):
        self.reconciled.append(request['request_id'])
        return {'submitted': True}

    def observe(self, request):
        return {'answer': self.answer, 'finished': bool(self.answer), 'matched': True}

    def cancel_observation(self):
        self.cancelled += 1


def test_task_delivers_answer_without_restarting_submission():
    journal, browser = Journal(), Browser()
    executor = TaskExecutor('classify:video', browser, journal, clock=lambda: 100)
    assert executor.tick()['phase'] == 'submitting'
    assert executor.tick()['phase'] == 'waiting'
    assert executor.tick()['phase'] == 'waiting'
    browser.answer = 'complete answer'
    result = executor.tick()
    assert result['phase'] == 'validating'
    assert result['answer'] == 'complete answer'
    assert len(browser.sent) == 1
    assert journal.load('classify:video')['answer'] == 'complete answer'


def test_reconstruction_reconciles_before_observation_and_never_resends():
    journal, browser = Journal(), Browser()
    executor = TaskExecutor('extract:video', browser, journal, clock=lambda: 100)
    executor.tick()
    executor.tick()
    restored = TaskExecutor('extract:video', browser, journal, clock=lambda: 200)
    assert restored.tick()['phase'] == 'waiting'
    assert browser.reconciled == browser.sent
    browser.answer = 'late complete answer'
    assert restored.tick()['answer'] == 'late complete answer'
    assert len(browser.sent) == 1


def test_pause_cancels_local_observation_and_resume_reconciles():
    journal, browser = Journal(), Browser()
    executor = TaskExecutor('classify:video', browser, journal, clock=lambda: 100)
    executor.tick()
    executor.tick()
    executor.tick()
    executor.pause()
    browser.answer = 'answer during pause'
    assert executor.tick()['phase'] == 'waiting'
    assert browser.cancelled == 1
    executor.resume()
    assert executor.tick()['phase'] == 'waiting'
    assert executor.tick()['answer'] == 'answer during pause'
    assert len(browser.sent) == 1


def test_reconstruction_before_send_continues_prepared_submission():
    journal, browser = Journal(), Browser()
    first = TaskExecutor('classify:video', browser, journal, clock=lambda: 100)
    first.tick()
    restored = TaskExecutor('classify:video', browser, journal, clock=lambda: 200)
    assert restored.tick()['phase'] == 'waiting'
    assert len(browser.sent) == 1
    assert browser.reconciled == []


def test_task_ticks_reject_other_threads_before_browser_access():
    from concurrent.futures import ThreadPoolExecutor

    import pytest

    journal, browser = Journal(), Browser()
    executor = TaskExecutor('classify:video', browser, journal, clock=lambda: 100)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(RuntimeError, match='owner thread'):
            pool.submit(executor.tick).result()
    assert browser.sent == []

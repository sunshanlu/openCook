import json

from xhs_to_opencook.access import AccessDiagnostics


class SnapshotPage:
    def screenshot(self, *, path, **kwargs):
        from pathlib import Path
        Path(path).write_bytes(b'image' * 10)

    def content(self):
        return '<body>page</body>'


def test_snapshot_deduplicates_across_restart_and_records_changed_conditions(tmp_path):
    diagnostics = AccessDiagnostics(tmp_path, clock=lambda: 1000)
    state = {'url': 'https://example.test/ai_chat', 'matched': [], 'account': False}
    first = diagnostics.record('chrome', 'page_state_unconfirmed', state, SnapshotPage())
    resumed = AccessDiagnostics(tmp_path, clock=lambda: 1001)
    assert resumed.record('chrome', 'page_state_unconfirmed', state, SnapshotPage()) == first
    state['matched'] = [{'kind': 'login', 'text': '扫码登录'}]
    second = resumed.record('chrome', 'login_required', state, SnapshotPage())
    assert second != first
    assert len(list(tmp_path.glob('*/evidence.json'))) == 2


def test_retention_removes_expired_snapshots_and_evicts_oldest_for_capacity(tmp_path):
    clock = [1000]
    diagnostics = AccessDiagnostics(tmp_path, clock=lambda: clock[0], retention_seconds=100)
    for scope in ['chrome', 'chrome-extract']:
        diagnostics.record(scope, 'login_required', {'url': 'about:blank'}, SnapshotPage())
    clock[0] = 1101
    diagnostics.record('chrome', 'verification_required', {'url': 'about:blank'}, SnapshotPage())
    assert len(list(tmp_path.glob('*/evidence.json'))) == 1
    diagnostics.max_bytes = 1
    diagnostics.prune()
    assert list(tmp_path.glob('*/evidence.json')) == []


def test_capture_failure_is_saved_without_replacing_access_state(tmp_path):
    class BrokenPage(SnapshotPage):
        def screenshot(self, **kwargs):
            raise OSError('capture unavailable')

    diagnostics = AccessDiagnostics(tmp_path, clock=lambda: 1000)
    path = diagnostics.record('chrome', 'login_required', {'url': 'about:blank'}, BrokenPage())
    assert path is not None
    evidence = json.loads(next(tmp_path.glob('*/evidence.json')).read_text())
    assert evidence['code'] == 'login_required'
    assert evidence['capture_errors'][0]['artifact'] == 'page.png'


def test_normal_recovery_removes_expired_evidence_without_another_failure(tmp_path):
    clock = [1000]
    diagnostics = AccessDiagnostics(tmp_path, clock=lambda: clock[0], retention_seconds=100)
    diagnostics.record('chrome', 'login_required', {'url': 'about:blank'}, SnapshotPage())
    clock[0] = 1101
    diagnostics.resolved('chrome')
    assert list(tmp_path.glob('*/evidence.json')) == []

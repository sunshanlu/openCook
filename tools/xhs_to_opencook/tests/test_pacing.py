import pytest

from xhs_to_opencook.chat import ChatSession
from xhs_to_opencook.errors import TaskError
from xhs_to_opencook.pacing import AccountPacing
from xhs_to_opencook.storage import Store


def test_interval_rest_and_limit_survive_restart(tmp_path):
    store = Store(tmp_path)
    clock = [1000.0]
    gate = AccountPacing(store, 'experiment', 'account', interval=15, batch_size=2,
                         batches=2, rest_seconds=900, clock=lambda: clock[0],
                         sleeper=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    for index in range(2):
        gate.ready()
        with store.db:
            gate.reserve({'request_id': str(index), 'sent_at': clock[0]})
    assert clock[0] == 1015
    store.close()
    store = Store(tmp_path)
    gate = AccountPacing(store, 'experiment', 'account', interval=15, batch_size=2,
                         batches=2, rest_seconds=900, clock=lambda: clock[0],
                         sleeper=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    gate.ready()
    assert clock[0] == 1915
    gate.ready()
    assert clock[0] == 1915  # Same completed rest cannot restart on repeated preparation.
    for index in range(2, 4):
        gate.ready()
        with store.db:
            gate.reserve({'request_id': str(index), 'sent_at': clock[0]})
    with pytest.raises(TaskError) as failure:
        gate.ready()
    assert failure.value.code == 'experiment_complete'
    assert gate.state()['count'] == 4
    store.close()


def test_atomic_sending_intent_and_idempotent_slot(tmp_path):
    store = Store(tmp_path)
    gate = AccountPacing(store, 'experiment', 'account')
    chat = ChatSession(store, None, pacing=gate)
    request = {'request_id': 'r', 'status': 'sending', 'sent_at': 1000}
    chat.persist(request)
    chat.persist(request)
    assert gate.state()['count'] == 1
    store.db.execute("""CREATE TRIGGER deny_request BEFORE INSERT ON chat_requests
        BEGIN SELECT RAISE(ABORT,'injected'); END""")
    with pytest.raises(Exception, match='injected'):
        chat.persist({'request_id': 'r2', 'status': 'sending', 'sent_at': 1020})
    assert gate.state()['count'] == 1
    assert store.db.execute('select count(*) from pacing_slots').fetchone()[0] == 1
    store.close()


def test_frozen_gate_makes_no_browser_calls_and_keeps_quota(tmp_path):
    store = Store(tmp_path)
    gate = AccountPacing(store, 'experiment', 'account')
    gate.block('platform_restricted')
    chat = ChatSession(store, lambda: pytest.fail('frozen account accessed'), pacing=gate)
    with pytest.raises(TaskError):
        chat.batch('classify', 'note')
    gate.release()
    assert gate.state()['count'] == 0
    assert not gate.state()['blocked']
    store.close()


def test_account_quotas_are_independent(tmp_path):
    store = Store(tmp_path)
    first = AccountPacing(store, 'experiment', 'A')
    second = AccountPacing(store, 'experiment', 'B')
    with store.db:
        first.reserve({'request_id': 'r', 'sent_at': 1000})
    first.block('platform_restricted')
    assert second.state()['count'] == 0
    assert not second.state()['blocked']
    store.close()


def test_rest_does_not_spend_item_budget(tmp_path, monkeypatch):
    from xhs_to_opencook import runtime
    store = Store(tmp_path / 'store')
    clock = [1000.0]
    active = runtime.WorkerRuntime(tmp_path / 'runtime', 'job', clock=lambda: clock[0],
                                   monotonic=lambda: clock[0])
    active.begin_item('classify', 'note')
    monkeypatch.setattr(runtime, '_worker', active)
    gate = AccountPacing(store, 'experiment', 'account', batch_size=1, batches=2,
                         clock=lambda: clock[0], sleeper=lambda seconds:
                         clock.__setitem__(0, clock[0] + seconds))
    with store.db:
        gate.reserve({'request_id': 'r', 'sent_at': clock[0]})
    gate.ready()
    assert active.items['budgets']['classify:note'] == 0
    assert active.data['item'] == 'classify:note'
    assert gate.state()['rest_until'] == 0
    store.close()


def test_complete_experiment_blocks_before_driver(tmp_path):
    store = Store(tmp_path)
    gate = AccountPacing(store, 'experiment', 'account', batch_size=1, batches=1)
    with store.db:
        gate.reserve({'request_id': 'r', 'sent_at': 1000})
    chat = ChatSession(store, lambda: pytest.fail('completed trial accessed platform'), pacing=gate)
    with pytest.raises(TaskError) as failure:
        chat.process(None, 'classify', tmp_path)
    assert failure.value.code == 'experiment_complete'
    store.close()


def test_trial_initialization_pauses_other_stage(tmp_path):
    from types import SimpleNamespace

    from xhs_to_opencook.accounts import save_binding
    from xhs_to_opencook.parallel import initialize
    state = tmp_path / 'state'
    profile = '1' * 24
    store = Store(state)
    store.stage_save('profiles', profile, {'status': 'complete'})
    store.close()
    save_binding(state, 'classify', 'a' * 24)
    save_binding(state, 'extract', 'b' * 24)
    profiles = tmp_path / 'profiles'
    profiles.write_text(f'https://www.xiaohongshu.com/user/profile/{profile}')
    args = SimpleNamespace(state_dir=state, profiles=profiles, output=tmp_path / 'output',
        interval=15, trial_stage='classify', trial_batch_size=50, trial_rest=900,
        trial_batches=10)
    job = initialize(args, state / 'supervision')
    assert job['workers']['extract']['status'] == 'paused'
    assert job['experiment']['settings'] == {
        'interval': 15, 'batch_size': 50, 'rest_seconds': 900, 'batches': 10}

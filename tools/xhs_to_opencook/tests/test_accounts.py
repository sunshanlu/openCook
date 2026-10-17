import json

import pytest

from xhs_to_opencook.accounts import migrate_accounts, require_bindings, save_binding
from xhs_to_opencook.chat import ChatSession
from xhs_to_opencook.errors import TaskError
from xhs_to_opencook.storage import Store

A, B = 'a' * 24, 'b' * 24


def test_distinct_bindings_and_no_silent_rebind(tmp_path):
    save_binding(tmp_path, 'classify', A)
    with pytest.raises(TaskError):
        save_binding(tmp_path, 'extract', A)
    with pytest.raises(TaskError):
        require_bindings(tmp_path)
    save_binding(tmp_path, 'extract', B)
    assert require_bindings(tmp_path)['extract']['account_id'] == B
    with pytest.raises(TaskError):
        save_binding(tmp_path, 'extract', 'c' * 24)


def test_foreign_pending_and_batch_rejected_before_browser(tmp_path):
    store = Store(tmp_path)
    store.stage_save('chat_requests', 'r', {'stage': 'extract', 'status': 'submitted',
                     'request_id': 'r', 'account_id': A})
    session = ChatSession(store, lambda: pytest.fail('must not access browser'),
                          stage_scope='extract', account_id=B)
    with pytest.raises(TaskError, match='其他账号'):
        session.pending()
    with pytest.raises(TaskError, match='其他账号'):
        session.load({'batch_id': 'b', 'account_id': A})
    store.close()


def test_unresolved_migration_leaves_everything_unchanged(tmp_path):
    store = Store(tmp_path)
    request = {'stage': 'extract', 'status': 'submitted', 'note_id': 'n', 'request_id': 'r'}
    store.stage_save('chat_requests', 'r', request)
    before = store.stage_get('chat_requests', 'r')
    with pytest.raises(TaskError, match='原账号'):
        migrate_accounts(store, A, {'classify': {'account_id': A}, 'extract': {'account_id': B}})
    assert store.stage_get('chat_requests', 'r') == before
    store.close()


def test_validated_committed_migration_no_resend(tmp_path, source, payload):
    store = Store(tmp_path)
    store.save(source.note_id, {'status': 'success', 'source': source.model_dump(),
                               'extraction': payload, 'export_names': ['腌萝卜']})
    store.stage_save('chat_batches', 'batch', {'batch_id': 'batch', 'stage': 'extract',
                     'status': 'open', 'capacity': 5, 'submitted_notes': [source.note_id]})
    store.stage_save('chat_requests', 'request', {'request_id': 'request', 'stage': 'extract',
                     'status': 'validated', 'note_id': source.note_id, 'attempt': 2,
                     'batch_id': 'batch', 'answer': json.dumps({'note_id': source.note_id,
                     'request_id': 'request', 'extraction': payload})})
    migrate_accounts(store, A, {'classify': {'account_id': A}, 'extract': {'account_id': B}})
    row = store.stage_get('chat_requests', 'request')
    assert row['status'] == 'complete'
    assert row['attempt'] == 2
    assert row['account_id'] == A
    assert store.stage_get('chat_batches', 'batch')['status'] == 'sealed'
    store.close()


def test_new_batches_bound_and_ignore_foreign_history(tmp_path):
    store = Store(tmp_path)
    store.stage_save('chat_batches', 'old', {'batch_id': 'old', 'stage': 'extract',
                     'status': 'open', 'account_id': A, 'submitted_notes': [], 'capacity': 5})
    class Driver:
        def new_chat(self):
            pass
        def restore_chat(self, cid):
            pytest.fail('foreign history must not restore')
    chat = ChatSession(store, lambda: Driver(), stage_scope='extract', account_id=B)
    batch = chat.batch('extract', 'note')
    assert batch['account_id'] == B
    assert batch['batch_id'] != 'old'
    store.close()

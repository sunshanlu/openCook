import json

from test_task_dispatch import N1, Driver, build, note


class ExtractionDriver(Driver):
    def request_snapshot(self, request):
        return {'matches': 1, 'finished': True, 'text': json.dumps({
            'note_id': N1, 'request_id': request['request_id'],
            'extraction': {'recipes': [{'name': '炒蛋', 'ingredients': [
                {'name': '鸡蛋', 'quantity': 2, 'unit': '个',
                 'original_amount': '2个', 'estimated': False}],
                'steps': ['打散鸡蛋', '炒熟'], 'servings': 1}]},
        })}


def test_extraction_commits_result_and_task_without_resending(tmp_path):
    driver = ExtractionDriver()
    flow, store = build(tmp_path, driver, queue_job='job')
    store.stage_save('classifications', N1, {'status': 'success', 'classification': {
        'note_id': N1, 'verdict': 'tutorial', 'reason': '教程'},
        'source': {'note_id': N1, 'url': note(store)['url']}})
    flow.process('extract', [note(store)])
    assert flow.report['tasks'][-1]['status'] == 'success'
    assert store.get(N1)['status'] == 'success'
    assert store.task_get('extract:'+N1)['phase'] == 'completed'
    flow.process('extract', [note(store)])
    assert driver.sent == 1
    store.close()

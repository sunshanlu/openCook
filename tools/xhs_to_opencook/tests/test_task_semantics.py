import json

import pytest

from xhs_to_opencook.errors import TaskError
from xhs_to_opencook.task_adapters import extraction_validator

N = 'a' * 24


def answer(*, original='3平勺', unit='克', estimated=False, total=None, steps=None):
    return json.dumps({'note_id': N, 'request_id': 'r', 'extraction': {'recipes': [{
        'name': '腌萝卜', 'ingredients': [{'name': '盐', 'quantity': 30, 'unit': unit,
        'original_amount': original, 'estimated': estimated, 'purpose': '杀水'}],
        'steps': steps or ['拌匀'], 'total_time': total,
    }]}}, ensure_ascii=False)


@pytest.mark.parametrize('text', [
    answer(),
    answer(original='适口量'),
    answer(original='3平勺+10平勺', unit='勺'),
    answer(unit='勺', total='PT12H30M', steps=['冰箱腌制最少一夜']),
])
def test_explicit_semantic_conflicts_fail_validation(text):
    with pytest.raises(TaskError, match='语义'):
        extraction_validator(N)(text, {'request': {'request_id': 'r'}})


def test_original_spoon_unit_and_estimated_ambiguous_amount_are_accepted():
    validator = extraction_validator(N)
    validator(answer(unit='勺'), {'request': {'request_id': 'r'}})
    validator(answer(original='适量', estimated=True), {'request': {'request_id': 'r'}})

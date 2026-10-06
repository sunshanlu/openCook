import io
import json

import pytest
from PIL import Image

from xhs_to_opencook.models import Source


@pytest.fixture
def source():
    return Source(
        note_id="6ac482bf000000001c02c2e0",
        url="https://www.xiaohongshu.com/explore/6ac482bf000000001c02c2e0",
        title="腌萝卜", author="村驴", cover_url="https://example.test/cover.webp",
    )


@pytest.fixture
def payload():
    return {"recipes": [{
        "name": "腌萝卜", "ingredients": [
            {"name": "白糖", "quantity": 3, "unit": "勺", "original_amount": "三平勺",
             "estimated": False, "purpose": "杀水用"},
            {"name": "白糖", "quantity": 10, "unit": "勺", "original_amount": "十平勺",
             "estimated": False, "purpose": "料汁用"},
            {"name": "姜", "quantity": 10, "unit": "g", "original_amount": "一小块",
             "estimated": True, "purpose": ""},
        ], "steps": ["拌匀", "冷藏一夜"], "notes": ["用干净容器"],
    }]}


@pytest.fixture
def answer(payload):
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture
def image_bytes():
    image = io.BytesIO()
    Image.new("RGB", (30, 20), "green").save(image, "WEBP")
    return image.getvalue()

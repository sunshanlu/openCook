import copy
import io
import json
import zipfile

import pytest
from PIL import Image

from xhs_to_opencook.errors import TaskError
from xhs_to_opencook.export import convert, cover_jpeg, estimates, write_outputs
from xhs_to_opencook.links import note_id, read_links, validate_url
from xhs_to_opencook.models import Extraction, parse_answer


def test_strict_json_and_fence(answer):
    assert parse_answer(answer) == parse_answer(f"```json\n{answer}\n```")


@pytest.mark.parametrize("invalid", [
    '{"recipes": [], "recipes": []}', '{"recipes": NaN}', '```json\n{}',
    '说明\n{}', '{"recipes": [', '{"recipes": []}',
])
def test_invalid_json_never_reconstructed(invalid):
    with pytest.raises(TaskError, match="JSON"):
        parse_answer(invalid)


@pytest.mark.parametrize("field,value", [
    ("name", " "), ("quantity", 0), ("quantity", -1), ("quantity", True),
    ("quantity", "30"), ("quantity", float("inf")), ("estimated", "false"),
    ("estimated", None), ("original_amount", ""), ("unit", ""),
])
def test_invalid_ingredients(payload, field, value):
    payload["recipes"][0]["ingredients"][0][field] = value
    with pytest.raises(TaskError):
        parse_answer(json.dumps(payload))


@pytest.mark.parametrize("change", ["missing_amount", "missing_estimate", "no_steps", "bad_time"])
def test_missing_fields_and_nonstandard_time(payload, change):
    recipe = payload["recipes"][0]
    if change == "missing_amount":
        del recipe["ingredients"][0]["original_amount"]
    elif change == "missing_estimate":
        del recipe["ingredients"][0]["estimated"]
    elif change == "no_steps":
        recipe["steps"] = [" "]
    else:
        recipe["total_time"] = "PT2H+1夜"
    with pytest.raises(TaskError):
        parse_answer(json.dumps(payload))


def test_conversion_keeps_purposes_estimates_and_multi_dish(source, payload):
    payload["recipes"].append(copy.deepcopy(payload["recipes"][0]))
    extraction = Extraction.model_validate(payload)
    recipes = convert(source, extraction)
    assert recipes[0]["name"] != recipes[1]["name"]
    assert recipes[0]["cookbook"] == "村驴"
    assert [i["name"] for i in recipes[0]["openCookIngredients"]] == [
        "白糖（杀水用）", "白糖（料汁用）", "姜",
    ]
    assert "identifier" not in recipes[0]
    assert "totalTime" not in recipes[0]
    assert recipes[0]["recipeInstructions"][1]["text"] == "冷藏一夜"
    assert any("估算用量｜姜" in note for note in recipes[0]["openCookNotes"])
    assert any(source.url in note for note in recipes[0]["openCookNotes"])
    assert len(estimates(extraction)) == 2


def test_repeated_rows_are_not_merged(source, payload):
    payload["recipes"][0]["ingredients"].append(payload["recipes"][0]["ingredients"][0])
    rows = convert(source, Extraction.model_validate(payload))[0]["openCookIngredients"]
    assert rows[-1]["name"] == "白糖（杀水用）（第2项）"


def test_bundle_remote_and_local_images(tmp_path, source, payload, image_bytes):
    path = tmp_path / "cover.jpg"
    path.write_bytes(cover_jpeg(image_bytes))
    extraction = Extraction.model_validate(payload)
    record = {"source": source.model_dump(), "extraction": extraction.model_dump(),
              "export_names": ["实际缓存名称"], "cover_path": str(path)}
    output = tmp_path / "out"
    write_outputs(output, [record], {"tasks": []})
    remote = json.loads((output / "recipes.json").read_text())
    assert remote[0]["image"] == [source.cover_url]
    assert remote[0]["name"] == "实际缓存名称"
    with zipfile.ZipFile(output / "recipes.zip") as archive:
        bundled = json.loads(archive.read("recipes.json"))
        ref = bundled[0]["image"][0]
        assert ref == f"images/{source.note_id}.jpg"
        with Image.open(io.BytesIO(archive.read(ref))) as image:
            assert image.format == "JPEG"
    path.write_text("<html>登录</html>")
    report = {"tasks": [{"note_id": source.note_id, "status": "cached"}]}
    write_outputs(output, [record], report)
    with zipfile.ZipFile(output / "recipes.zip") as archive:
        assert "image" not in json.loads(archive.read("recipes.json"))[0]
    assert report["tasks"][0]["image_error"]["code"] == "image_failed"
    assert json.loads((output / "report.json").read_text())["recipe_count"] == 1


def test_html_cover_rejected():
    with pytest.raises(TaskError):
        cover_jpeg(b"<html>not an image</html>")


def test_links_allowlist_bom_and_paths(tmp_path, source):
    path = tmp_path / "links.txt"
    path.write_text(f"\ufeff\n{source.url}\n \nhttps://xhslink.com/a/test\n", encoding="utf-8")
    assert read_links(path) == [source.url, "https://xhslink.com/a/test"]
    assert note_id(source.url) == source.note_id
    assert note_id(f"https://www.xiaohongshu.com/discovery/item/{source.note_id}") == source.note_id
    for url in ["http://xhslink.com/a", "https://xhslink.com.evil/a",
                "https://name@xhslink.com/a", "https://xhslink.com:90/a"]:
        with pytest.raises(TaskError):
            validate_url(url)

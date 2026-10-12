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


@pytest.mark.parametrize("invalid,code", [
    ('{"recipes": [], "recipes": []}', "json_syntax_invalid"),
    ('{"recipes": NaN}', "json_syntax_invalid"),
    ('```json\n{}', "json_syntax_invalid"),
    ('```python\n{}\n```', "json_syntax_invalid"),
    ('说明\n{}', "json_syntax_invalid"),
    ('{"recipes": [', "json_syntax_invalid"),
    ('{"recipes": []}', "json_schema_invalid"),
    ('{}', "json_schema_invalid"),
    ('null', "json_schema_invalid"),
])
def test_invalid_json_never_reconstructed(invalid, code):
    with pytest.raises(TaskError, match="JSON") as error:
        parse_answer(invalid)
    assert error.value.code == code


@pytest.mark.parametrize("field,value", [
    ("name", " "), ("quantity", 0), ("quantity", -1), ("quantity", True),
    ("quantity", "30"), ("quantity", float("inf")), ("estimated", "false"),
    ("estimated", None), ("original_amount", ""), ("unit", ""),
])
def test_invalid_ingredients(payload, field, value):
    payload["recipes"][0]["ingredients"][0][field] = value
    with pytest.raises(TaskError) as error:
        parse_answer(json.dumps(payload))
    expected = "json_syntax_invalid" if value == float("inf") else "json_schema_invalid"
    assert error.value.code == expected


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
    with pytest.raises(TaskError) as error:
        parse_answer(json.dumps(payload))
    assert error.value.code == "json_schema_invalid"


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


def cut(answer, marker, keep=0):
    """The answer truncated just after the first `marker` (plus `keep` more characters)."""
    return answer[: answer.index(marker) + len(marker) + keep]


@pytest.mark.parametrize("marker,keep", [
    ('"notes":["用干净容器"', 0),  # complete value, only brackets missing
    ('"notes":["用干净容器",', 3),  # cut inside a later optional string
    ('"notes":[', 0),  # empty optional list left open
])
def test_truncated_optional_tail_is_closed(payload, marker, keep):
    payload["recipes"][0]["notes"].append("再加一条注意事项")
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    recipe = parse_answer(cut(text, marker, keep)).recipes[0]
    assert [step for step in recipe.steps] == ["拌匀", "冷藏一夜"]
    assert len(recipe.ingredients) == 3
    assert "再加一条注意事项" not in recipe.notes


def test_truncated_fence_and_description_are_salvaged(payload):
    payload["recipes"][0]["description"] = "清爽开胃"
    payload["recipes"][0]["servings"] = 4
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    truncated = "```json\n" + cut(text, '"description":"清爽开胃"')
    recipe = parse_answer(truncated).recipes[0]
    assert recipe.description == "清爽开胃"
    assert recipe.servings is None


@pytest.mark.parametrize("marker,keep", [
    ('"steps":["拌匀"', 0),  # steps list cut: a step would silently vanish
    ('"steps":["拌匀",', 2),
    ('"ingredients":[{"name":"白糖"', 0),  # ingredients list cut
    ('"original_amount":"三平勺","estimated":false', 0),  # inside an ingredient
    ('"name":"腌萝卜"', 0),  # required lists not started yet
])
def test_truncation_in_core_content_is_never_closed(payload, marker, keep):
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with pytest.raises(TaskError) as error:
        parse_answer(cut(text, marker, keep))
    assert error.value.code == "json_syntax_invalid"


def test_second_recipe_cut_off_is_not_silently_dropped(payload):
    payload["recipes"].append(copy.deepcopy(payload["recipes"][0]))
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    first_end = text.index('"用干净容器"]}') + len('"用干净容器"]}')
    with pytest.raises(TaskError):
        parse_answer(text[: first_end + 1])  # ends right after the comma before recipe two
    with pytest.raises(TaskError):
        parse_answer(text[: first_end + 40])


def test_truncated_number_is_dropped_not_guessed(payload):
    payload["recipes"][0]["servings"] = 45
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    recipe = parse_answer(cut(text, '"servings":4')).recipes[0]  # could have been 45
    assert recipe.servings is None  # dropped, never read as 4


def test_repair_is_extraction_only(payload):
    from xhs_to_opencook.models import Classification, parse_json_model

    with pytest.raises(TaskError) as error:
        parse_json_model('{"note_id":"6ac482bf000000001c02c2e0",', Classification)
    assert error.value.code == "json_syntax_invalid"


@pytest.mark.parametrize("text,expected", [
    ('{"recipes":[{"name":"x","steps":["一步', True),  # cut inside a string
    ('{"recipes":[{"name":"x","steps":["a","b"]', True),  # cut with objects still open
    ('```json\n{"recipes":[{"name":"x"', True),  # unclosed fence and brackets
    ('{"recipes":[{"name":"x" "steps":[]}]}', False),  # malformed but complete
    ('{"recipes":[]}', False),
    ('{"a":"含\\"引号\\"的字符串"}', False),  # escaped quotes do not end the string
    ('{"a":"末尾是转义\\\\"}', False),
    ('', False),
])
def test_ends_unfinished_tells_cut_off_from_malformed(text, expected):
    from xhs_to_opencook.models import ends_unfinished

    assert ends_unfinished(text) is expected


@pytest.mark.parametrize("tail", ["}", "]}", " }\n", "}}]"])
def test_stray_closers_after_a_complete_document_are_dropped(answer, tail):
    assert parse_answer(answer + tail) == parse_answer(answer)


@pytest.mark.parametrize("tail", ['{"recipes": []}', "说明", ',"x":1', "}说明", "\n```"])
def test_anything_but_closers_after_the_document_is_still_rejected(answer, tail):
    with pytest.raises(TaskError) as error:
        parse_answer(answer + tail)
    assert error.value.code == "json_syntax_invalid"


def test_stray_closers_are_only_repaired_for_extraction(answer):
    from xhs_to_opencook.models import Classification, parse_json_model

    text = '{"note_id":"6ac482bf000000001c02c2e0","verdict":"tutorial","reason":"做法"}}'
    with pytest.raises(TaskError) as error:
        parse_json_model(text, Classification)
    assert error.value.code == "json_syntax_invalid"
    assert parse_json_model(text, Classification, repair=True).verdict == "tutorial"


def test_stray_closers_keep_duplicate_key_protection(payload):
    text = json.dumps(payload, ensure_ascii=False).replace('"name"', '"name": "x", "name"', 1)
    with pytest.raises(TaskError):
        parse_answer(text + "}")


def test_a_complete_document_with_trailing_text_is_never_trimmed_as_truncated(payload):
    payload["recipes"][0]["notes"] = ["不能被悄悄丢掉"]
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with pytest.raises(TaskError):
        parse_answer(text + "以上是菜谱")

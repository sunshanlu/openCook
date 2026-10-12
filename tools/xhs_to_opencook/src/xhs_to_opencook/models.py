import json
import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator

from .errors import TaskError

JSON_ERROR_CODES = frozenset({"json_syntax_invalid", "json_schema_invalid"})

Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
DURATION = re.compile(
    r"^PT(?:\d+H(?:\d+M)?(?:\d+(?:\.\d+)?S)?|\d+M(?:\d+(?:\.\d+)?S)?|\d+(?:\.\d+)?S)$",
)
Duration = Annotated[str, StringConstraints(pattern=DURATION.pattern)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ingredient(Model):
    name: Text
    quantity: float = Field(gt=0, allow_inf_nan=False)
    unit: Text
    original_amount: Text = Field(description="视频原文用量；缺失写未说明，不可改写成估算值")
    estimated: StrictBool = Field(description="缺失或模糊用量、非精确单位换算为true；明确值为false")
    purpose: str = Field(default="", description="用途/阶段；同食材不同阶段分开列，不合计")

    @field_validator("quantity", mode="before")
    @classmethod
    def numeric_quantity(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("quantity must be a JSON number")
        if not math.isfinite(value):
            raise ValueError("quantity must be finite")
        return value


class Recipe(Model):
    name: Text
    ingredients: list[Ingredient] = Field(min_length=1)
    steps: list[Text] = Field(min_length=1)
    notes: list[Text] = Field(default_factory=list)
    description: str = ""
    servings: int | None = Field(default=None, strict=True, gt=0)
    prep_time: Duration | None = Field(default=None, description="仅视频明确的标准时长，否则null")
    cook_time: Duration | None = Field(default=None, description="仅视频明确的标准时长，否则null")
    total_time: Duration | None = Field(default=None, description="含一夜/至熟等文字时必须null")

    @field_validator("prep_time", "cook_time", "total_time")
    @classmethod
    def duration(cls, value):
        if value is not None and not DURATION.fullmatch(value):
            raise ValueError("use PT durations only when explicitly stated; otherwise omit")
        return value


class Extraction(Model):
    recipes: list[Recipe] = Field(min_length=1)


class Source(Model):
    note_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{24}$")]
    url: Text
    title: Text
    author: Text
    cover_url: str | None = None


class Classification(Model):
    note_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{24}$")]
    verdict: Literal["tutorial", "non_tutorial", "unknown"]
    reason: Text


def parse_classification(text: str, expected: str) -> Classification:
    result = parse_json_model(text, Classification)
    if result.note_id != expected:
        raise TaskError("reference_mismatch", "分类结果的笔记 ID 与目标不同")
    return result


def parse_answer(text: str) -> Extraction:
    return parse_json_model(text, Extraction, repair=True)


# Cutting inside these arrays would leave a valid-looking recipe that silently lost content.
CORE_ARRAYS = frozenset({"recipes", "ingredients", "steps"})
CORE_FIELDS = frozenset({"ingredients", "steps"})


def salvage_truncated(payload: str) -> str | None:
    """Close a JSON text cut off mid-way, dropping the unfinished tail.

    Only a cut that falls in optional recipe fields is salvaged. A cut inside the recipe
    list, ingredients or steps returns None, because closing those would hide missing content.
    """
    stack, in_string, escaped, start = [], False, False, 0
    commas = []  # (index, closers, safe) for every comma outside strings
    for index, char in enumerate(payload):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                top = stack[-1] if stack else None
                if top and top["type"] == "{" and top["expect_key"]:
                    top["key"] = json.loads(payload[start : index + 1])
            continue
        if char == '"':
            in_string, start = True, index
        elif char in "{[":
            stack.append({"type": char, "key": None, "expect_key": char == "{"})
        elif char in "}]":
            if not stack:
                return None
            stack.pop()
        elif char == ":" and stack:
            stack[-1]["expect_key"] = False
        elif char == ",":
            if stack and stack[-1]["type"] == "{":
                stack[-1]["expect_key"] = True
            commas.append((index, _closers(stack), _cut_is_safe(stack)))
    candidates = []
    tail = payload.rstrip()
    ends_cleanly = tail and (tail[-1] in '"}]' or tail.endswith(("true", "false", "null")))
    if stack and not in_string and ends_cleanly:
        candidates.append((len(tail), _closers(stack), _cut_is_safe(stack)))
    candidates += [(index, closers, safe) for index, closers, safe in reversed(commas)]
    for end, closers, safe in candidates:
        try:
            json.loads(payload[:end] + closers)
        except ValueError:
            continue
        return payload[:end] + closers if safe else None
    return None


def ends_unfinished(text: str) -> bool:
    """True when the answer simply stops: a string, object or list is still open at the end.

    That is the signature of a reply cut off at the output ceiling, as opposed to a reply
    that is malformed somewhere in the middle.
    """
    try:
        payload = _unfence(text, lenient=True)
    except TaskError:
        return False
    depth, in_string, escaped = 0, False, False
    for char in payload:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return bool(payload.strip()) and (in_string or depth > 0)


def _closers(stack):
    return "".join("}" if frame["type"] == "{" else "]" for frame in reversed(stack))


def _cut_is_safe(stack):
    # The innermost object's key names the field just finished, not a container we are inside.
    keys = [frame["key"] for frame in stack[:-1] if frame["type"] == "{"]
    if any(key in CORE_FIELDS for key in keys):
        return False
    return not (stack and stack[-1]["type"] == "[" and keys and keys[-1] in CORE_ARRAYS)


def _unfence(text: str, lenient: bool) -> str:
    payload = text.strip()
    if payload.startswith("```"):
        lines = payload.splitlines()
        closed = len(lines) > 1 and lines[-1].strip() == "```"
        if lines[0].strip().lower() not in {"```", "```json"} or not (closed or lenient):
            raise TaskError("json_syntax_invalid", "JSON 代码围栏不完整或语言不支持")
        payload = "\n".join(lines[1:-1] if closed else lines[1:])
    return payload


def parse_json_model(text: str, model, *, repair: bool = False):
    """Parse actual JSON (optionally fenced); never invent missing fields.

    With repair, a text cut off inside optional fields is closed locally (see
    salvage_truncated); anything else still fails with the original error.
    """

    def strict(payload):
        def reject_constant(value):
            raise ValueError(f"non-finite JSON number: {value}")

        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        return json.loads(payload, parse_constant=reject_constant, object_pairs_hook=unique_keys)

    try:
        payload = _unfence(text, lenient=False)
        obj = strict(payload)
    except (TaskError, ValueError, TypeError) as exc:
        failure = exc
        if isinstance(exc, TaskError):
            if exc.code != "json_syntax_invalid" or not repair:
                raise
        else:
            failure = TaskError("json_syntax_invalid", f"JSON 格式或语法错误：{exc}")
        if not repair:
            raise failure from exc
        try:
            repaired = salvage_truncated(_unfence(text, lenient=True))
            if repaired is None:
                raise failure from exc
            return model.model_validate(strict(repaired))
        except (TaskError, ValueError, TypeError):
            raise failure from exc
    try:
        return model.model_validate(obj)
    except (ValueError, TypeError) as exc:
        raise TaskError("json_schema_invalid", f"JSON 数据结构校验失败：{exc}") from exc


class BoundClassification(Classification):
    request_id: Text


class BoundExtraction(Model):
    note_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{24}$")]
    request_id: Text
    extraction: Extraction


def parse_bound_answer(text: str, stage: str, note: str, request: str):
    model = BoundClassification if stage == "classify" else BoundExtraction
    result = parse_json_model(text, model, repair=stage != "classify")
    if result.note_id != note or result.request_id != request:
        raise TaskError("reference_mismatch", "回答的笔记 ID 或请求标识与当前提交不同")
    if stage == "classify":
        return Classification.model_validate(result.model_dump(exclude={"request_id"}))
    return result.extraction

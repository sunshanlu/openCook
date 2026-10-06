import json
import math
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator

from .errors import TaskError

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


def parse_answer(text: str) -> Extraction:
    """Only parse actual JSON (optionally fenced); never reconstruct missing fields."""
    payload = text.strip()
    if payload.startswith("```"):
        lines = payload.splitlines()
        if lines[0].strip().lower() not in {"```", "```json"} or lines[-1].strip() != "```":
            raise TaskError("json_invalid", "JSON 代码围栏不完整或语言不支持")
        payload = "\n".join(lines[1:-1])

    def reject_constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        obj = json.loads(payload, parse_constant=reject_constant, object_pairs_hook=unique_keys)
        return Extraction.model_validate(obj)
    except (ValueError, TypeError) as exc:
        raise TaskError("json_invalid", f"菜谱 JSON 校验失败：{exc}") from exc

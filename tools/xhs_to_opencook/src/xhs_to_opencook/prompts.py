import json

from .models import Source

CLASSIFICATION_VERSION = 3
EXTRACTION_VERSION = 3


def classification_prompt(source: Source) -> str:
    return f"""请打开并引用这个视频笔记：{source.url}
目标笔记ID：{source.note_id}；作者：{source.author}；标题：{source.title}
仅根据这个笔记的视频和作者配文判断：是否为食物或饮品的制作教程？
有制作过程，能提取食材和操作步骤则 verdict=tutorial，包括烘焙、饮品、酱料、腌菜、
多道菜合集；用量不全仍算教程。只有成品展示、吃播、探店、商品推荐、食材知识，
没有制作教程则 verdict=non_tutorial。无法读取视频或依据不足必须 verdict=unknown，
不得仅凭标题推断，不得把读取失败当作非教程。reason 简要说明依据，
note_id 必须原样输出目标 ID。reason 不超过40个汉字，不列举食材和做法。
只输出判断结果的紧凑单行 JSON，完整闭合引号和括号，不要 Markdown。
结果格式如下（替换 verdict 和 reason 为实际判断，不要输出格式说明）：
{{"note_id":"{source.note_id}","verdict":"tutorial","reason":"简短判断依据"}}"""


def classification_repair_prompt(error: str) -> str:
    return (
        "上一条分类 JSON 无效。请修正同一视频的完整 JSON，包含 note_id、verdict、reason。"
        "verdict 只能为 tutorial/non_tutorial/unknown；无法读取或依据不足使用 unknown。"
        "不要提取菜谱，不要编造判断依据。reason 不超过40个汉字，输出紧凑完整JSON。错误："
        + error[:3000]
    )


def recipe_prompt(source: Source) -> str:
    return (
        "仅根据当前引用视频及作者配文提取全部菜谱，忽略历史视频和评论。多菜按出现顺序分开，"
        "每菜食材、调料、步骤完整；保留火候、时间及注意事项。同食材不同用途分条，purpose写用途。"
        "明确用量保留原数值和单位（勺/粒不换算），estimated=false；缺失、适量、模糊范围或"
        "大小换算给合理估计，estimated=true。quantity必须为正数；original_amount原样保留用量，"
        "缺失写未说明。只估算用量，不添加食材、操作、营养、份数或时间。"
        "可选description、servings、prep_time/cook_time/total_time；份数须为明确正整数，"
        "时间只填明确PT时长，否则省略或null；一夜/至熟留在steps或notes。"
        "无法读取或无菜谱就说明，禁止编造。"
        "输出务必精简以免被截断：每步不超过50字，notes最多3条且每条不超过30字，"
        "description不超过40字，purpose不超过10字，不写寒暄和解释。"
        "字段顺序固定：name、ingredients、steps在前，notes、description及时间在后。"
        "只输出完整紧凑JSON，无Markdown；"
    )


def repair_prompt(error: str) -> str:
    return (
        "修正当前视频JSON，返回完整结果；保持原食材、步骤、原始用量和估算标记，禁止编造。"
        "时间不明确或非PT格式用null，文字时间留在steps/notes。只输出JSON。错误："
        + error[:400]
    )


def bound_prompt(source: Source, stage: str, request: str, error: str | None = None) -> str:
    if stage == "classify":
        example = json.dumps(
            {
                "note_id": source.note_id,
                "request_id": request,
                "verdict": "tutorial/non_tutorial/unknown",
                "reason": "简短判断依据",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        instruction = (
            "只判断当前引用视频及作者配文，忽略历史内容。能看到制作过程并提取食材、步骤"
            "为tutorial（含烘焙、饮品、酱料、腌菜、多菜合集；用量不全也算）；"
            "仅展示、吃播、探店、推荐或知识为non_tutorial；无法读取或依据不足为unknown，"
            "不凭标题猜测。只输出JSON，verdict选一个，reason不超过40字，"
            "原样回显note_id和request_id："
        )
        if error:
            instruction = "修正上一条分类JSON，不编造视频内容。" + instruction
        return instruction + example
    example = json.dumps(
        {
            "note_id": source.note_id,
            "request_id": request,
            "extraction": {
                "recipes": [{
                    "name": "菜名", "ingredients": [{
                        "name": "食材", "quantity": 1, "unit": "单位",
                        "original_amount": "原文用量", "estimated": False, "purpose": "用途",
                    }], "steps": ["步骤"], "notes": ["注意事项"],
                }],
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (repair_prompt(error) if error else recipe_prompt(source)) + (
        "原样回显note_id和request_id；按此完整结构替换菜谱内容，括号保持配对：" + example
    )

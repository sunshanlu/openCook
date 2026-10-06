import json

from .models import Extraction, Source

CLASSIFICATION_VERSION = 2


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
    schema = json.dumps(Extraction.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    return f"""请打开并引用这个视频笔记：
{source.url}

目标笔记ID：{source.note_id}；作者：{source.author}；标题：{source.title}
仅根据这个笔记的视频和作者配文总结菜谱。不要引用其他笔记或评论区的配方。
一条视频有多道菜时，按视频出现顺序分别输出，每道菜的食材和步骤独立完整。
全部食材、调料都要列出；步骤按顺序，保留原文明确的火候、时间及注意事项。
明确用量保持原数量与单位。缺失用量、“适量”或模糊范围，请明确给出合理估计用量：
quantity 必须是大于0的数字，unit 写对应单位，estimated 必须为 true。
视频明确给出的单值用量 estimated 为 false；original_amount 原样保留原文用量，
原文没说用量写“未说明”。purpose 写用途（如“杀水用”“料汁用”），没有则写空字符串。
同一种食材用于不同阶段时必须拆成独立条目，例如杀水白糖和料汁白糖不得合计成一条。
原文是“3勺”“6粒”时保留 quantity=3/unit="勺"、quantity=6/unit="粒"，不要换算成克；
“两指宽一块”等大小描述若估成克数，必须 estimated=true，不得当作明确克数。
只允许估计用量。不得自行添加食材、操作、营养、份数或时间。
servings 和 prep_time/cook_time/total_time 只有来源明确才能填写，否则省略或 null；
时间须用 PT 格式。“一夜”“至熟”等文字放在 steps 或 notes，不换算成小时。
不要输出“PT2H+1夜”“PT24H+”等无效时间。无法给出完整标准时长就省略时间字段。
如无法读取视频或不是菜谱，说明无法读取，不得编造 JSON。
输出严格符合下面 JSON Schema 的 JSON 对象，不要说明文字，不要 Markdown 围栏：
使用紧凑 JSON（不缩进），完整闭合所有括号。每个食材必须有全部必填字段。
{schema}"""


def repair_prompt(error: str) -> str:
    return (
        "上一条 JSON 未通过校验。请修正同一视频的完整结果，只输出 JSON 对象。"
        "不要新增原视频未出现的食材或做法；只估算缺失和模糊用量，保留 estimated 标记。"
        "时间字段只能为null或纯PT数字H/M/S。若错误包含time，把相应时间字段改为null；"
        "尤其含一夜、+或文字的时间禁止放入time字段，保留在steps/notes。"
        "不要把回答放进文本字段，不要省略已给出的菜谱或步骤。校验错误：\n" + error[:3000]
    )


def bound_prompt(source: Source, stage: str, request: str, error: str | None = None) -> str:
    binding = (
        f"本次唯一请求标识 request_id={request}；目标 note_id={source.note_id}。"
        "仅处理当前引用的视频，忽略历史视频、历史回答和历史配方。"
        "必须原样回显这两个标识。\n"
    )
    if stage == "classify":
        prompt = classification_repair_prompt(error) if error else classification_prompt(source)
        if not error:
            prompt = prompt.split("结果格式如下", 1)[0]
        example = json.dumps(
            {
                "note_id": source.note_id,
                "request_id": request,
                "verdict": "tutorial",
                "reason": "简短判断依据",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return binding + prompt + "\n最终只输出以下结构的完整JSON（替换实际判断）：" + example
    prompt = repair_prompt(error) if error else recipe_prompt(source)
    return (
        binding
        + prompt
        + (
            '\n将菜谱JSON放入内部外层包装，最终格式为：{"note_id":"'
            + source.note_id
            + '","request_id":"'
            + request
            + '","extraction":{"recipes":[...]}}。'
            "extraction须符合上述菜谱格式；不要把包装字段写入菜谱。"
        )
    )

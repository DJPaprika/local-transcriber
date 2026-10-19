"""LLM 任务：翻译、摘要、问答。

翻译的做法：把字幕按「条数 + 字符数」分批，让模型按编号逐条译。
回来之后按编号对回原来的时间轴，所以字幕条数不会变。
"""

from __future__ import annotations

import re

from .client import ChatClient

TRANSLATE_SYSTEM = """你是专业的字幕翻译。严格遵守以下规则：

1. 只输出译文，不解释、不加引号、不加任何注释；
2. 严格一条对一条，编号与原文一致，不合并、不拆分、不省略、不新增；
3. 每条译文尽量不超过原文长度的两倍——字幕要能一眼读完；
4. 口语化、自然，符合目标语言的说话习惯，不要书面腔；
5. 人名、机构名等专有名词保留原形或沿用其惯用译名，全篇保持一致；
6. 若某条是纯符号或无实义内容，原样输出该条，不要留空；
7. 不要输出编号以外的任何内容（不要"以下是翻译"之类的开场白）。"""

SUMMARIZE_SYSTEM = """你是一名通用内容摘要编辑。输入是任意领域音视频的内容——可能直接是自动转写稿，也可能是按段落整理好的笔记（访谈、讲座、会议、庭审、教程、播客都可能）。

只依据给出的内容，不补充里面没有的信息。

分三部分写，用【】小标题。除「转写存疑处」要求对着原文核验外，其余两部分写成连贯的话就行，不必凑条目、不必编号：

【一句话结论】
一句话，说清这段内容**是什么、谁在讲、核心主张或结果是什么**。有判决、决定、具体数字就写进去。
不要拿某一个数字或设定充当结论——那是细节，不是结论。

【内容大意】
把这段内容的**结构**讲清楚：分几个部分、每部分讲了什么、各自的结论是什么。
按内容自然分段，不要逐条罗列，**也不要重复【一句话结论】里已经说过的话**——那里是结论，这里是展开。
篇幅和素材成比例：十分钟以上的内容至少写四五段，短内容两三段也够，别一句话打发。
**原文点了名的东西（物品、人名、地名、品牌、数字），在【内容大意】里要点名**，不要用"各种""一些""等"笼统带过——读者要的是具体信息，不是"介绍了若干物品"。
- 原文里的具体数字、时间、人名、机构名，务必保留。
- 但**不要替原文做统计**：「共 N 种」「共 N 项」「总共 N 个」这类汇总数字，只有原文自己明确数过才能写；原文没有数，就只列举、不报总数。乱报一个数比不报更糟。
- 有两个以上说话人时，转述观点必须写明是谁说的（作者、投稿者、受访者、提问者……）；判断不出来就写"有人提出"，不要硬派给某个人。
- 拿不准的用"疑似""据称"，不要写成定论。

【转写存疑处】
只在确实发现语音识别出错时才写：专有名词或数字明显是同音错字、句子里混进乱码、一句话读不通。
每条附上对应原文；有几处写几处，一处都没有就只写「无」。
不要为了凑数把正常句子抄一遍，这里也不放内容本身的疑问。"""

# 下面这个「专职校对」的提示词暂时不接入主流程——试过拆开单独跑，
# 分段会让它失去上下文、只剩同音瞎猜（误报一堆），整篇又会陷入重复循环。
# 7B 只有在「先读懂全文、顺手校对」的模式下才偶尔可靠，所以存疑检测仍然
# 留在 SUMMARIZE_SYSTEM 里。详见 docs/summary-experiments.md。
PROOF_SYSTEM = """你是语音转写稿的校对员。输入是自动语音识别（ASR）出来的中文稿，可能有同音错字、专有名词误识别、数字错误、句子断裂。

逐句扫过去，只报**确实可能识别错了**的地方：

- 同音或近音字写错：「三支箭」写成「三支件」、「口粮」写成「狗粮」、「理论家」写成「理论」；
- 专有名词、人名、地名、机构名、书名、术语、诗句写错；
- 数字、金额、单位、年份写错；
- 上下文明显接不上：一句话被切断成两截、词不达意、混进乱码。

**不要报**这些：
- 内容本身的疑问、观点对不对、信息缺失；
- 口语、方言、语气词、重复啰嗦的句子——那是原话，不是错误；
- 你觉得"表达不好"的地方；
- **意思没变的替换**：把「重新增长」改成「恢复增长」、把「我们」改成「你们」，意思没变就不算错误，不要报。

**宁缺毋滥**：只报你有把握确实写错的地方。拿不准的、只是"可能"的，一律不报。漏报没人受损，错报会让读者连对的地方也不敢信。

每条按下面的格式，一条一段：

- 原文：「……」（照抄出错片段及其前后几个字）
  疑似：「……」（给出你认为正确的写法）
  理由：一句话说明为什么可疑

有几处写几处，一处都没有就只写一行「无」。不要输出其它内容、不要加开场白。"""

MAP_SYSTEM = """你在为后面的摘要做准备：把这段转写稿里的**具体信息**抄录成笔记。

当成做笔记，不是写摘要。要求：

- 一条一行，短句或短语都行，不要写成段落，不要串成流畅文章；
- 重点是**具体名词**：人名、身份、品牌、型号、菜品、地名、数字、时间、价位，见到就抄；
- 逐件事记：发生了什么、结果是什么、谁主张了什么；
- 绝不写"介绍了若干物品""讲了各种罐头"这类概括——那正是这一步要避开的东西；
- 遇到明显的语音识别错误（同音错字、乱码、读不通的句子），把原句照抄，并标出疑似应为什么；
- 只依据原文，不补充原文没有的信息。篇幅 300 字以内。"""

QA_SYSTEM = """只依据给定的转写文本作答，不臆测。

规则：
- 有依据时：先给结论，再引用一句原文作为支撑；
- 没有依据时：直接回答「文中没有提到」；
- 问题有歧义时：先说明你的理解，再作答。"""

# 形如 "12. 文本" 或 "12、文本"
_NUM_RE = re.compile(r"^\s*(\d+)\s*[.、:：)]\s*(.*)$")

# 送给模型的文本上限（字符）。中文约 1 字 ≈ 1.4 token，配 32K 上下文时留足输出余量。
MAX_INPUT_CHARS = 20000

# 分段摘要的触发阈值（字符）。只给超长素材兜底——实测把一万字左右的稿子
# 切成三段做笔记再合并，细节和「转写存疑处」都会在两次压缩里掉光，
# 不如一次性喂进去。所以阈值放在一次能塞下的上限附近。
MAP_CHARS = 12000

# 校对时每段的字符数。校对必须看到全文——切成小段后失去上下文，7B 只会
# 靠同音瞎猜（把「我们」猜成「你们」这种），误报比真错还多。
PROOF_CHARS = 20000


def _chunk(segments: list, max_chars: int = 1200, max_items: int = 20) -> list[list]:
    """把字幕切成若干批，每批不超过字符数与条数上限。"""
    batches: list[list] = []
    cur: list = []
    size = 0
    for seg in segments:
        n = len(seg.text)
        if cur and (size + n > max_chars or len(cur) >= max_items):
            batches.append(cur)
            cur, size = [], 0
        cur.append(seg)
        size += n
    if cur:
        batches.append(cur)
    return batches


def _parse_numbered(reply: str) -> dict[int, str]:
    """把「1. 文本」这样的编号行解析成 {编号: 文本}（续行并入上一条）。"""
    mapping: dict[int, str] = {}
    for line in reply.splitlines():
        m = _NUM_RE.match(line)
        if m:
            mapping[int(m.group(1))] = m.group(2).strip()
        elif line.strip() and mapping:
            last = max(mapping)
            mapping[last] = (mapping[last] + " " + line.strip()).strip()
    return mapping


def translate_segments(
    client: ChatClient,
    segments: list,
    *,
    target: str = "简体中文",
    max_chars: int = 1200,
    on_batch=None,
) -> list:
    """逐批翻译，返回新的 Segment 列表（时间轴不变）。"""
    out = []
    batches = _chunk(segments, max_chars=max_chars)

    for idx, batch in enumerate(batches, 1):
        lines = "\n".join(f"{i}. {seg.text}" for i, seg in enumerate(batch, 1))
        prompt = f"把下面每条字幕翻译成{target}，编号必须一一对应：\n\n{lines}"

        mapping: dict[int, str] = {}
        for attempt in range(2):
            extra = ""
            if attempt > 0:
                extra = (
                    f"\n\n注意：上一次返回的条数不对。这一次必须严格输出 {len(batch)} 条，"
                    f"每条单独一行，编号从 1 到 {len(batch)}，不要合并、不要省略、不要多加。"
                )
            reply = client.chat(
                [
                    {"role": "system", "content": TRANSLATE_SYSTEM},
                    {"role": "user", "content": prompt + extra},
                ],
                temperature=0.2 if attempt == 0 else 0.1,
            )
            mapping = _parse_numbered(reply)
            if len(mapping) == len(batch):
                break

        # 条数对不上时逐条退回原文：宁可保留原文，也不让时间轴错位
        for i, seg in enumerate(batch, 1):
            text = mapping.get(i) or seg.text
            out.append(type(seg)(start=seg.start, end=seg.end, text=text))

        if on_batch:
            on_batch(idx, len(batches), len(mapping), len(batch))

    return out


def _split_text(text: str, size: int) -> list[str]:
    """按字符数把长文本切段，尽量在换行处断开（转写稿一行一句）。"""
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    cur: list[str] = []
    n = 0
    for line in text.splitlines(keepends=True):
        if n + len(line) > size and cur:
            parts.append("".join(cur))
            cur, n = [], 0
        cur.append(line)
        n += len(line)
    if cur:
        parts.append("".join(cur))
    return parts


def summarize(client: ChatClient, text: str, *, on_part=None) -> str:
    """对转写全文做摘要（一句话结论 + 内容大意 + 转写存疑处）。"""
    return _summarize_body(client, text, on_part=on_part).rstrip()


def _summarize_body(client: ChatClient, text: str, *, on_part=None) -> str:
    """摘要正文：一句话结论 + 内容大意。

    超长文本先分段做「笔记」再合并；一万字左右的稿子不分段——实测切成三段
    做笔记再合并，细节会在两次压缩里掉光。
    """
    parts = _split_text(text, MAP_CHARS)

    if len(parts) == 1:
        prompt = f"下面是这段音视频的自动转写原始文本：\n\n{text}"
    else:
        notes: list[str] = []
        for i, part in enumerate(parts, 1):
            note = client.chat(
                [
                    {"role": "system", "content": MAP_SYSTEM},
                    {
                        "role": "user",
                        "content": f"这是同一份转写稿的第 {i}/{len(parts)} 段：\n\n{part}",
                    },
                ],
                temperature=0.0,
                max_tokens=800,
            )
            notes.append(note.strip())
            if on_part:
                on_part(i, len(parts))

        merged = "\n\n".join(
            f"—— 第 {i} 段笔记 ——\n{n}" for i, n in enumerate(notes, 1)
        )
        if len(merged) > MAX_INPUT_CHARS:
            merged = merged[:MAX_INPUT_CHARS] + "\n\n（后面的笔记从略）"
        prompt = (
            f"下面是这段音视频按时间顺序分段记录的笔记（共 {len(parts)} 段）。"
            "请据此写出整体摘要，不要逐段复述，要合成一篇：\n\n" + merged
        )

    return client.chat(
        [
            {"role": "system", "content": SUMMARIZE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=1500,
    )


def proofread(client: ChatClient, text: str, *, on_part=None) -> str:
    """逐段校对转写稿，返回 markdown 列表；一处可疑都没有时返回「无」。"""
    parts = _split_text(text, PROOF_CHARS)
    found: list[str] = []
    for i, part in enumerate(parts, 1):
        reply = client.chat(
            [
                {"role": "system", "content": PROOF_SYSTEM},
                {
                    "role": "user",
                    "content": f"转写稿第 {i}/{len(parts)} 段：\n\n{part}",
                },
            ],
            temperature=0.0,
            max_tokens=1000,
        ).strip()
        if on_part:
            on_part(i, len(parts))
        if not _no_issue(reply):
            found.append(reply)
    return "\n\n".join(found) if found else "无。"


def _no_issue(reply: str) -> bool:
    """模型表示「没发现问题」的几种写法。"""
    t = reply.strip().strip("-*· \n").rstrip("。.．")
    return t in {"无", "没有", "未发现", "未发现错误", "未发现明显错误"}


def ask(client: ChatClient, text: str, question: str) -> str:
    """针对转写内容提问。"""
    prompt = (
        f"转写文本：\n{text[:MAX_INPUT_CHARS]}\n\n"
        f"问题：{question}"
    )
    return client.chat(
        [
            {"role": "system", "content": QA_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=1024,
    )

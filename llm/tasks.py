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

SUMMARIZE_SYSTEM = """你是一名通用内容摘要编辑，输入是任意领域音视频的自动转写稿——访谈、讲座、会议、庭审、教程、播客都可能。

只依据原文，不补充文本里没有的信息。

分三部分写，用【】小标题。除「转写存疑处」要求对着原文核验外，其余两部分写成连贯的话就行，不必凑条目、不必编号：

【一句话结论】
一句话，说清最关键的人和结果。有判决、决定、具体数字就写进去。

【内容大意】
把这段内容讲清楚，抓主线和关键细节，不必面面俱到。
- 原文里的具体数字、时间、人名、机构名，务必保留。
- 有两个以上说话人时，转述观点必须写明是谁说的（作者、投稿者、受访者、提问者……）；判断不出来就写"有人提出"，不要硬派给某个人。
- 拿不准的用"疑似""据称"，不要写成定论。

【转写存疑处】
只在确实发现语音识别出错时才写：专有名词或数字明显是同音错字、句子里混进乱码、一句话读不通。
每条附上对应原文；有几处写几处，一处都没有就只写「无」。
不要为了凑数把正常句子抄一遍，这里也不放内容本身的疑问。"""

QA_SYSTEM = """只依据给定的转写文本作答，不臆测。

规则：
- 有依据时：先给结论，再引用一句原文作为支撑；
- 没有依据时：直接回答「文中没有提到」；
- 问题有歧义时：先说明你的理解，再作答。"""

# 形如 "12. 文本" 或 "12、文本"
_NUM_RE = re.compile(r"^\s*(\d+)\s*[.、:：)]\s*(.*)$")

# 送给模型的文本上限（字符）。中文约 1 字 ≈ 1.4 token，配 32K 上下文时留足输出余量。
MAX_INPUT_CHARS = 20000


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


def summarize(client: ChatClient, text: str) -> str:
    """对转写全文做摘要（通用三段式）。"""
    body = text[:MAX_INPUT_CHARS]
    truncated = len(text) > MAX_INPUT_CHARS
    prompt = "下面是这段音视频的自动转写原始文本"
    prompt += "（内容过长，只给了前面一部分）" if truncated else ""
    prompt += f"：\n\n{body}"
    return client.chat(
        [
            {"role": "system", "content": SUMMARIZE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=1500,
    )


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

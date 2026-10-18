"""LLM 任务：翻译、摘要、问答。

翻译的做法：把字幕按「条数 + 字符数」分批，让模型按编号逐条译。
回来之后按编号对回原来的时间轴，所以字幕条数不会变。
"""

from __future__ import annotations

import re

from .client import ChatClient

TRANSLATE_SYSTEM = (
    "你是专业的字幕翻译。要求：\n"
    "1. 只输出译文，不要解释、不要评论、不要加引号；\n"
    "2. 保持原有的编号和行数，一条对一条；\n"
    "3. 译文要口语化、自然，符合字幕阅读习惯；\n"
    "4. 人名、机构名、专业术语保持准确，必要时保留原文。"
)

SUMMARIZE_SYSTEM = (
    "你是会议与视频内容分析助手。要求：先给一句话结论，再给要点列表，"
    "最后标注内容里提到但未展开的疑问点。不要编造原文没有的信息。"
)

_NUM_RE = re.compile(r"^\s*(\d+)\s*[.、:：)]\s*(.*)$")


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
        prompt = f"把下面每条字幕翻译成{target}，保持编号：\n\n{lines}"

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


def summarize(client: ChatClient, text: str, *, max_points: int = 8) -> str:
    """对转写全文做摘要。"""
    prompt = (
        f"下面是视频的完整转写文本，请总结：\n"
        f"- 一句话结论\n- 不超过 {max_points} 个要点\n- 文中提到但没说清的问题\n\n"
        f"文本：\n{text[:12000]}"
    )
    return client.chat(
        [
            {"role": "system", "content": SUMMARIZE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )


def ask(client: ChatClient, text: str, question: str) -> str:
    """针对转写内容提问。"""
    prompt = f"根据以下视频转写内容回答问题。若文中没有依据，直接说「文中没有提到」。\n\n转写：\n{text[:12000]}\n\n问题：{question}"
    return client.chat(
        [
            {"role": "system", "content": "你只依据给定文本作答，不臆测。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )

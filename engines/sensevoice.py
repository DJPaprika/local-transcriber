"""SenseVoice 引擎：通过 Node 桥调用 sherpa-onnx。

Bridge 只负责「WAV 进去，字符级时间戳出来」；断句合并成字幕放在 Python 这边，
这样两个引擎的产物形态才能统一。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .base import EngineError, Segment, Transcript, run_streaming

# 断句规则（与旧版 JS 保持一致）
SENTENCE_END = set("。！？!?…")
CLAUSE_END = set("，,；;、")
MAX_CHARS = 24
MAX_SECONDS = 8.0
MIN_SECONDS = 1.0
BREAK_AFTER = 0.45


def build_segments(chars: list[list], *, max_chars: int = MAX_CHARS) -> list[Segment]:
    """把字符级时间戳 [(字, 秒), ...] 合并成句级字幕。"""
    cues: list[Segment] = []
    cur: list[tuple[str, float]] = []

    def flush(end: float) -> None:
        if not cur:
            return
        start = cur[0][1]
        text = "".join(c for c, _ in cur).strip()
        if text:
            cues.append(Segment(start=start, end=max(end, start + 0.2), text=text))
        cur.clear()

    for i, (ch, t) in enumerate(chars):
        cur.append((ch, t))
        nxt = chars[i + 1][1] if i + 1 < len(chars) else None
        span = t - cur[0][1]
        gap = (nxt - t) if nxt is not None else 99.0

        if ch in SENTENCE_END:
            flush(t + 0.35)
        elif gap >= BREAK_AFTER:
            flush(t + 0.25)
        elif span >= MAX_SECONDS:
            flush(t + 0.2)
        elif len(cur) >= max_chars and (ch in CLAUSE_END or len(cur) >= max_chars + 6):
            flush(t + 0.2)

    flush(chars[-1][1] + 0.5 if chars else 0.0)

    # 保证最短显示时长、且不与下一条重叠
    for i, cue in enumerate(cues):
        if cue.end - cue.start < MIN_SECONDS:
            cue.end = cue.start + MIN_SECONDS
        if i + 1 < len(cues) and cue.end > cues[i + 1].start:
            cue.end = max(cue.start + 0.2, cues[i + 1].start - 0.05)
    return cues


class SenseVoiceEngine:
    """基于 sherpa-onnx + SenseVoice-small。中文极快，但只支持 5 种语言。"""

    name = "sensevoice"

    def __init__(self, node_exe: str, bridge: Path):
        self.node_exe = node_exe
        self.bridge = Path(bridge)

    def transcribe(
        self, wav: Path, *, language: str = "auto", quiet: bool = False
    ) -> Transcript:
        wav = Path(wav)
        json_path = Path(f"{wav}.sv.json")
        log_path = Path(f"{wav}.bridge.log")

        cmd = [self.node_exe, str(self.bridge), str(wav), str(json_path), language]
        code, elapsed = run_streaming(cmd, log_path, quiet=quiet)
        if code != 0:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
            raise EngineError(f"SenseVoice 桥退出码 {code}\n{tail}")

        if not json_path.exists():
            raise EngineError(f"桥没有生成 {json_path}")

        data = json.loads(json_path.read_text(encoding="utf-8"))
        segments = build_segments(data.get("chars", []))
        stats = dict(data.get("stats", {}))
        stats["elapsed_seconds"] = round(elapsed, 2)

        return Transcript(
            segments=segments,
            text="\n".join(s.text for s in segments),
            lang=data.get("lang", language),
            engine=self.name,
            stats=stats,
        )

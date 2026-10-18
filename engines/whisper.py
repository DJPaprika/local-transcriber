"""Whisper 引擎：调用 whisper.cpp 的 whisper-cli。"""

from __future__ import annotations

import re
from pathlib import Path

from .base import EngineError, Segment, Transcript, run_streaming

# whisper.cpp 的耗时行：whisper_print_timings:     load time =   79547.09 ms
TIMING_RE = re.compile(r"whisper_print_timings:\s*([a-z_ ]+?)\s*=\s*([\d.]+)\s*ms")

# SRT 条目：时间行 + 文本（到空行为止）
CUE_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n(.*?)(?:\n\s*\n|\Z)",
    re.S,
)


def parse_timings(log_text: str) -> dict[str, float]:
    """从 whisper 输出里抽取各阶段耗时（秒）。"""
    out: dict[str, float] = {}
    for name, ms in TIMING_RE.findall(log_text):
        out[name.strip().replace(" ", "_")] = round(float(ms) / 1000.0, 2)
    return out


def parse_srt(srt_text: str) -> list[Segment]:
    """解析 SRT 为 Segment 列表。"""
    segs: list[Segment] = []
    for m in CUE_RE.finditer(srt_text):
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(m.group(i)) for i in range(1, 9))
        segs.append(
            Segment(
                start=h1 * 3600 + m1 * 60 + s1 + ms1 / 1000,
                end=h2 * 3600 + m2 * 60 + s2 + ms2 / 1000,
                text=m.group(9).strip(),
            )
        )
    return segs


class WhisperEngine:
    """基于 whisper.cpp。多语种、原生时间戳，但慢。"""

    name = "whisper"

    def __init__(self, exe: str, model: Path, threads: int = 8):
        self.exe = exe
        self.model = Path(model)
        self.threads = threads

    def transcribe(
        self, wav: Path, *, language: str = "zh", quiet: bool = False
    ) -> Transcript:
        wav = Path(wav)
        prefix = wav.with_suffix("")
        log_path = prefix.with_suffix(".whisper.log")

        cmd = [
            self.exe,
            "-m", str(self.model),
            "-f", str(wav),
            "-t", str(self.threads),
            "-osrt", "-otxt",
            "-of", str(prefix),
            "-pp",
        ]
        if language:
            cmd += ["-l", language]

        code, elapsed = run_streaming(cmd, log_path, quiet=quiet)
        if code != 0:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
            raise EngineError(f"whisper 退出码 {code}\n{tail}")

        srt_path = Path(f"{prefix}.srt")
        if not srt_path.exists():
            raise EngineError(f"whisper 没有生成 {srt_path}")

        txt_path = Path(f"{prefix}.txt")
        text = txt_path.read_text(encoding="utf-8", errors="replace") if txt_path.exists() else ""
        timings = parse_timings(log_path.read_text(encoding="utf-8", errors="replace"))

        return Transcript(
            segments=parse_srt(srt_path.read_text(encoding="utf-8", errors="replace")),
            text=text.strip(),
            lang=language or "auto",
            engine=self.name,
            stats={
                "elapsed_seconds": round(elapsed, 2),
                "whisper_timings": timings,
                "log": str(log_path),
            },
        )

"""引擎公共接口与数据结构。

设计目标：让 whisper 和 sensevoice 产出一模一样的结果形状，
上层（transcribe.py）不需要关心用的是哪个引擎。

    Engine.transcribe(wav) -> Transcript
    Transcript.segments    -> [Segment(start, end, text)]
    Transcript.to_srt()    -> SRT 文本
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol


class EngineError(RuntimeError):
    """引擎执行失败，上层捕获后给出友好提示。"""


@dataclass(slots=True)
class Segment:
    """一条字幕片段（秒）。"""

    start: float
    end: float
    text: str


@dataclass(slots=True)
class Transcript:
    """统一的转写结果。两个引擎都必须产出这个结构。"""

    segments: list[Segment]
    text: str
    lang: str
    engine: str
    stats: dict = field(default_factory=dict)

    def to_srt(self) -> str:
        blocks = [
            f"{i}\n{srt_time(seg.start)} --> {srt_time(seg.end)}\n{seg.text}\n"
            for i, seg in enumerate(self.segments, 1)
        ]
        return "\n".join(blocks)

    @property
    def duration(self) -> float:
        return self.segments[-1].end if self.segments else 0.0


def srt_time(sec: float) -> str:
    """秒 → SRT 时间格式 HH:MM:SS,mmm（注意是逗号，不是小数点）。"""
    ms = max(0, round(sec * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def run_streaming(
    cmd: list[str],
    log_path: Path,
    quiet: bool = False,
    on_line: Callable[[str], None] | None = None,
) -> tuple[int, float]:
    """跑子进程：输出写入日志文件，同时实时转发到终端。

    不用管道——子进程直接写文件，主进程轮询增量再转发。
    这样既能看进度，又能事后解析完整输出（某些受限环境下管道不可用）。
    返回 (退出码, 耗时秒)。
    """
    t0 = time.perf_counter()
    with open(log_path, "wb") as fh:
        try:
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
        except FileNotFoundError as exc:
            raise EngineError(f"无法执行：{cmd[0]}") from exc

        pos = 0
        while True:
            ret = proc.poll()
            if not quiet:
                with open(log_path, "rb") as rf:
                    rf.seek(pos)
                    chunk = rf.read()
                    pos = rf.tell()
                if chunk:
                    buf = getattr(sys.stdout, "buffer", None)
                    if buf is not None:
                        buf.write(chunk)
                        buf.flush()
                    else:
                        sys.stdout.write(chunk.decode("utf-8", "replace"))
                        sys.stdout.flush()
            if ret is not None:
                return ret, time.perf_counter() - t0
            time.sleep(0.15)


class Engine(Protocol):
    """引擎接口。实现方只需提供 name 属性与 transcribe 方法。"""

    name: str

    def transcribe(self, wav: Path, *, language: str) -> Transcript:
        ...

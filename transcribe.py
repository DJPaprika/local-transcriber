#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local-transcriber —— 本地离线语音转写流水线

一条命令把「在线视频 / 本地媒体文件」变成「纯文本 + SRT 字幕」：

    yt-dlp 取流  →  ffmpeg 抽 16kHz 单声道 PCM  →  whisper.cpp 转写  →  统计耗时

设计要点
--------
* 统一转成 16kHz / 单声道 / 16-bit PCM —— 这是 whisper 系列模型的输入规格，
  采样率或声道数不匹配时 whisper.cpp 会自行重采样，白白多花一遍 CPU。
* 音频时长不调用 ffprobe，直接由 WAV 字节数推算（16000 Hz × 2 字节 = 32000 B/s）。
* 解析 whisper.cpp 的耗时明细，把「模型加载」这项一次性固定成本单独拆出来 ——
  否则短音频的实测速度会被严重低估（本机加载约 80 秒）。
* 全程只依赖 Python 标准库，无需 pip 安装任何东西。

用法
----
    python transcribe.py video.mp4
    python transcribe.py "https://youtu.be/xxxx" -l zh -t 12
    python transcribe.py audio.m4a -m models/ggml-large-v3-turbo.bin -o out
    python transcribe.py video.mp4 --verbose          # 实时显示 whisper 输出
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# 16kHz 单声道 16-bit PCM：每秒固定 32000 字节
BYTES_PER_SECOND = 16000 * 2

# whisper.cpp 的耗时行形如：
#   whisper_print_timings:     load time =   79547.09 ms
TIMING_RE = re.compile(r"whisper_print_timings:\s*([a-z_ ]+?)\s*=\s*([\d.]+)\s*ms")


# --------------------------------------------------------------------------- 输出


if hasattr(sys.stdout, "reconfigure"):
    # Windows 控制台默认可能是 GBK，显式统一为 UTF-8，避免中文乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def hms(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s, re.I))


def safe_stem(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_") or "transcript"


def resolve_tool(label: str, value: str, flag: str) -> str:
    """在 PATH 里找工具；找不到就给出可操作的报错。"""
    found = shutil.which(value)
    if not found and Path(value).exists():
        found = str(Path(value).resolve())
    if not found:
        sys.exit(
            f"[x] 找不到 {label}：{value}\n"
            f"    请先安装，然后用 {flag} 指定完整路径，或把它加入 PATH。"
        )
    return found


def parse_timings(text: str) -> dict[str, float]:
    """从 whisper.cpp 输出里抽取耗时明细（单位：秒）。"""
    out: dict[str, float] = {}
    for name, ms in TIMING_RE.findall(text):
        out[name.strip().replace(" ", "_")] = round(float(ms) / 1000.0, 2)
    return out


def run(cmd: list[str], stage: str) -> float:
    """执行外部命令并计时，输出直接继承到终端。"""
    log(f"\n[>>] {stage}")
    log("     " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    t0 = time.perf_counter()
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        sys.exit(f"[x] 无法执行：{cmd[0]}")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"[x] {stage} 失败（退出码 {exc.returncode}）")
    return time.perf_counter() - t0


def run_logged(cmd: list[str], stage: str, log_path: Path) -> tuple[float, str]:
    """执行命令并把输出写入日志文件（不用管道，受限环境下也安全）。"""
    log(f"\n[>>] {stage}")
    log("     " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    t0 = time.perf_counter()
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
            subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, check=True)
    except FileNotFoundError:
        sys.exit(f"[x] 无法执行：{cmd[0]}")
    except subprocess.CalledProcessError as exc:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        log(tail)
        sys.exit(f"[x] {stage} 失败（退出码 {exc.returncode}）")
    elapsed = time.perf_counter() - t0
    return elapsed, log_path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- 参数


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="transcribe",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="本地离线语音转写：在线视频 / 本地媒体 → 文本 + SRT 字幕",
        epilog=textwrap.dedent(
            """\
            示例：
              python transcribe.py video.mp4
              python transcribe.py "https://youtu.be/xxxx" -l zh -t 12
              python transcribe.py audio.m4a -m models/ggml-large-v3-turbo.bin -o out
            """
        ),
    )
    p.add_argument("source", help="URL，或本地媒体文件路径")
    p.add_argument("-o", "--outdir", default="output", help="输出目录（默认 output）")
    p.add_argument(
        "-m",
        "--model",
        default=os.environ.get("WHISPER_MODEL", ""),
        help="whisper.cpp 的 ggml 模型路径（默认读环境变量 WHISPER_MODEL）",
    )
    p.add_argument("-n", "--name", default="", help="输出文件名前缀（默认由源文件名推导）")
    p.add_argument("-l", "--language", default="zh", help="语言代码，auto 表示自动识别（默认 zh）")
    p.add_argument(
        "-t",
        "--threads",
        type=int,
        default=max(4, (os.cpu_count() or 8) - 4),
        help="转写线程数（默认 CPU 核数 - 4）",
    )
    p.add_argument("--whisper", default="whisper-cli", help="whisper-cli 可执行文件")
    p.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件")
    p.add_argument("--yt-dlp", default="yt-dlp", help="yt-dlp 可执行文件")
    p.add_argument("--prompt", default="", help="初始提示词，用于纠正专有名词")
    p.add_argument("--keep-audio", action="store_true", help="保留抽取出的 16k wav")
    p.add_argument("--verbose", action="store_true", help="实时显示 whisper 输出（默认写入日志）")
    return p.parse_args(argv)


# --------------------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if not args.model:
        sys.exit("[x] 未指定模型：用 -m/--model 指定 ggml 模型路径，或设置环境变量 WHISPER_MODEL")
    model = Path(args.model).expanduser()
    if not model.exists():
        sys.exit(f"[x] 模型文件不存在：{model}")

    whisper = resolve_tool("whisper-cli", args.whisper, "--whisper")
    ffmpeg = resolve_tool("ffmpeg", args.ffmpeg, "--ffmpeg")

    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    work = outdir / ".work"
    work.mkdir(exist_ok=True)

    stats: dict[str, object] = {
        "source": args.source,
        "model": model.name,
        "threads": args.threads,
        "language": args.language,
    }

    # ---- 1. 取流 -----------------------------------------------------------
    if is_url(args.source):
        ytdlp = resolve_tool("yt-dlp", args.yt_dlp, "--yt-dlp")
        acquire = run(
            [
                ytdlp,
                "-f", "bestaudio/best",
                "--no-playlist",
                "-o", str(work / "source.%(ext)s"),
                args.source,
            ],
            "下载媒体（yt-dlp）",
        )
        candidates = [f for f in work.glob("source.*") if f.is_file()]
        if not candidates:
            sys.exit("[x] 下载完成但没找到媒体文件")
        media = max(candidates, key=lambda f: f.stat().st_size)
        default_stem = "transcript"
    else:
        media = Path(args.source).expanduser()
        if not media.exists():
            sys.exit(f"[x] 文件不存在：{media}")
        acquire = 0.0
        default_stem = safe_stem(media.stem)

    stats["media"] = media.name
    stats["media_bytes"] = media.stat().st_size
    stats["acquire_seconds"] = round(acquire, 2)

    # ---- 2. 抽音频 ---------------------------------------------------------
    extract = run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
            "-i", str(media),
            "-vn",                 # 丢掉视频流
            "-ac", "1",            # 单声道
            "-ar", "16000",        # 16kHz
            "-c:a", "pcm_s16le",   # 16-bit PCM
            str(work / "audio-16k.wav"),
        ],
        "抽取音频（16kHz / 单声道 / PCM）",
    )

    wav = work / "audio-16k.wav"
    duration = wav.stat().st_size / BYTES_PER_SECOND
    stats["extract_seconds"] = round(extract, 2)
    stats["wav_bytes"] = wav.stat().st_size
    stats["duration_seconds"] = round(duration, 2)

    # ---- 3. 转写 -----------------------------------------------------------
    stem = safe_stem(args.name or default_stem)
    prefix = outdir / stem
    whisper_log = outdir / f"{stem}.whisper.log"

    cmd = [
        whisper,
        "-m", str(model),
        "-f", str(wav),
        "-t", str(args.threads),
        "-otxt", "-osrt",
        "-of", str(prefix),
        "-pp",
    ]
    if args.language:
        cmd += ["-l", args.language]
    if args.prompt:
        cmd += ["--prompt", args.prompt]

    log(f"\n     音频时长 {hms(duration)}，开始转写……")
    if args.verbose:
        transcribe = run(cmd, "转写（whisper.cpp）")
        timings: dict[str, float] = {}
    else:
        transcribe, output = run_logged(cmd, "转写（whisper.cpp）", whisper_log)
        timings = parse_timings(output)

    stats["transcribe_seconds"] = round(transcribe, 2)
    if timings:
        stats["whisper_timings_seconds"] = timings

    # ---- 4. 报告 -----------------------------------------------------------
    if not args.keep_audio:
        wav.unlink(missing_ok=True)

    load = timings.get("load_time", 0.0)
    net = transcribe - load if load else transcribe
    speed_all = duration / transcribe if transcribe > 0 else 0.0
    speed_net = duration / net if net > 0 else 0.0

    stats["realtime_factor_total"] = round(speed_all, 2)
    stats["realtime_factor_net"] = round(speed_net, 2)

    outputs = [Path(f"{prefix}.{ext}") for ext in ("txt", "srt")]
    stats["outputs"] = [str(p) for p in outputs if p.exists()]

    log()
    log("=" * 52)
    log("  转写完成")
    log("=" * 52)
    log(f"  媒体来源    : {stats['media']}")
    log(f"  音频时长    : {hms(duration)}  ({duration:.1f} s)")
    log(f"  取流耗时    : {acquire:.1f} s")
    log(f"  抽音频耗时  : {extract:.1f} s")
    log("-" * 52)
    log(f"  转写总耗时  : {transcribe:.1f} s")
    if load:
        log(f"    ├─ 模型加载: {load:.1f} s   ← 一次性固定成本")
        log(f"    └─ 净转写  : {net:.1f} s")
    log(f"  处理速度    : {speed_all:.2f}× 实时（含加载）", )
    if load:
        log(f"                {speed_net:.2f}× 实时（不含加载）")
    log(f"  转写线程    : {args.threads}")
    log("-" * 52)
    for path in outputs:
        log(f"  输出        : {path}{'' if path.exists() else '  (缺失)'}")
    log("=" * 52)

    stats_path = outdir / f"{stem}.stats.json"
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  统计        : {stats_path}")
    if not args.verbose and whisper_log.exists():
        log(f"  运行日志    : {whisper_log}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

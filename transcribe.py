#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""local-transcriber —— 本地离线语音转写流水线

一条命令把「在线视频 / 本地媒体」变成「文本 + SRT 字幕」，支持两个引擎：

    sensevoice（默认）  快，中文最优，支持 zh/en/ja/ko/yue
    whisper             慢，但多语种、时间戳更精确

    yt-dlp 取流 → ffmpeg 抽 16kHz 单声道 PCM → 引擎转写 → txt + srt + stats

用法
----
    python transcribe.py video.mp4
    python transcribe.py "https://youtu.be/xxxx" -e whisper -l en
    python transcribe.py audio.m4a -e whisper -m models/ggml-large-v3-turbo.bin
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

from engines import EngineError, SenseVoiceEngine, WhisperEngine

BYTES_PER_SECOND = 16000 * 2  # 16kHz 单声道 16-bit：每秒固定字节数

if hasattr(sys.stdout, "reconfigure"):
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
    found = shutil.which(value)
    if not found and Path(value).exists():
        found = str(Path(value).resolve())
    if not found:
        sys.exit(
            f"[x] 找不到 {label}：{value}\n"
            f"    请先安装，然后用 {flag} 指定完整路径，或把它加入 PATH。"
        )
    return found


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="transcribe",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="本地离线语音转写：视频/音频 → 文本 + SRT 字幕",
        epilog=textwrap.dedent(
            """\
            引擎：
              sensevoice（默认）  中文快准，支持 zh/en/ja/ko/yue
              whisper            多语种，时间戳更精确

            示例：
              python transcribe.py video.mp4
              python transcribe.py "https://youtu.be/xxxx" -e whisper -l en
            """
        ),
    )
    p.add_argument("source", help="URL，或本地媒体文件路径")
    p.add_argument("-e", "--engine", default="sensevoice", choices=["sensevoice", "whisper"],
                   help="转写引擎（默认 sensevoice）")
    p.add_argument("-o", "--outdir", default="output", help="输出目录（默认 output）")
    p.add_argument("-n", "--name", default="", help="输出文件名前缀")
    p.add_argument("-l", "--language", default="auto",
                   help="语言代码；sensevoice 支持 auto/zh/en/ja/ko/yue，whisper 支持更多（默认 auto）")
    p.add_argument("-t", "--threads", type=int, default=max(4, (os.cpu_count() or 8) - 4),
                   help="whisper 转写线程数（默认 CPU 核数 - 4）")
    p.add_argument("-m", "--model", default=os.environ.get("WHISPER_MODEL", ""),
                   help="whisper 的 ggml 模型路径（-e whisper 时必需，默认读 WHISPER_MODEL）")
    p.add_argument("--whisper", default="whisper-cli", help="whisper-cli 可执行文件")
    p.add_argument("--node", default="node", help="node 可执行文件（sensevoice 需要）")
    p.add_argument("--bridge", default=str(Path(__file__).parent / "bridge" / "sv-bridge.js"),
                   help="SenseVoice 桥脚本路径")
    p.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件")
    p.add_argument("--yt-dlp", default="yt-dlp", help="yt-dlp 可执行文件")
    p.add_argument("--prompt", default="", help="whisper 初始提示词，用于纠正专有名词")
    p.add_argument("--keep-audio", action="store_true", help="保留中间 wav")
    p.add_argument("--quiet", action="store_true", help="不显示实时输出，只写日志")
    return p.parse_args(argv)


def build_engine(args: argparse.Namespace):
    if args.engine == "whisper":
        if not args.model:
            sys.exit("[x] 用 whisper 引擎必须指定模型：-m <ggml 模型路径>，或设置 WHISPER_MODEL")
        model = Path(args.model).expanduser()
        if not model.exists():
            sys.exit(f"[x] 模型文件不存在：{model}")
        return WhisperEngine(
            exe=resolve_tool("whisper-cli", args.whisper, "--whisper"),
            model=model,
            threads=args.threads,
        )
    return SenseVoiceEngine(
        node_exe=resolve_tool("node", args.node, "--node"),
        bridge=Path(args.bridge),
    )


def acquire_source(args: argparse.Namespace, work: Path) -> tuple[Path, float, str]:
    """取流：URL 下载 / 本地文件直接用。返回 (媒体路径, 耗时, 默认名前缀)。"""
    if not is_url(args.source):
        media = Path(args.source).expanduser()
        if not media.exists():
            sys.exit(f"[x] 文件不存在：{media}")
        return media, 0.0, safe_stem(media.stem)

    ytdlp = resolve_tool("yt-dlp", args.yt_dlp, "--yt-dlp")
    for old in work.glob("source.*"):  # 清掉上次残留，避免 glob 选到旧文件
        old.unlink(missing_ok=True)

    log("\n[>>] 下载媒体（yt-dlp）")
    cmd = [ytdlp, "-f", "bestaudio/best", "--no-playlist",
           "-o", str(work / "source.%(ext)s"), args.source]
    log("     " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd)  # 继承终端输出，实时可见
    except FileNotFoundError:
        sys.exit(f"[x] 无法执行 yt-dlp：{ytdlp}")
    if proc.returncode != 0:
        sys.exit(f"[x] 下载失败（退出码 {proc.returncode}）")

    candidates = [f for f in work.glob("source.*") if f.is_file()]
    if not candidates:
        sys.exit("[x] 下载完成但没找到媒体文件")
    media = max(candidates, key=lambda f: f.stat().st_size)
    return media, time.perf_counter() - t0, "transcript"


def extract_audio(media: Path, work: Path, ffmpeg: str) -> tuple[Path, float, float]:
    """抽成 16kHz / 单声道 / 16-bit PCM。返回 (wav, 耗时, 时长秒)。"""
    wav = work / "audio-16k.wav"
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
           "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", str(wav)]

    log("\n[>>] 抽取音频（16kHz / 单声道 / PCM）")
    log("     " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd)
    except FileNotFoundError:
        sys.exit(f"[x] 无法执行 ffmpeg：{ffmpeg}")
    if proc.returncode != 0:
        sys.exit(f"[x] ffmpeg 失败（退出码 {proc.returncode}）")
    elapsed = time.perf_counter() - t0

    if not wav.exists():
        sys.exit("[x] ffmpeg 没有生成 wav")
    # 时长 = 字节数 ÷ 每秒字节数（不调用 ffprobe）
    return wav, elapsed, wav.stat().st_size / BYTES_PER_SECOND


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    engine = build_engine(args)
    ffmpeg = resolve_tool("ffmpeg", args.ffmpeg, "--ffmpeg")

    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    work = outdir / ".work"
    work.mkdir(exist_ok=True)

    media, acquire_sec, default_stem = acquire_source(args, work)
    wav, extract_sec, duration = extract_audio(media, work, ffmpeg)

    log(f"\n     音频时长 {hms(duration)}，引擎 {engine.name}，开始转写……")
    try:
        transcript = engine.transcribe(wav, language=args.language, quiet=args.quiet)
    except EngineError as exc:
        sys.exit(f"[x] {exc}")

    stem = safe_stem(args.name or default_stem)
    (outdir / f"{stem}.srt").write_text(transcript.to_srt(), encoding="utf-8")
    (outdir / f"{stem}.txt").write_text(transcript.text + "\n", encoding="utf-8")

    if not args.keep_audio:
        wav.unlink(missing_ok=True)

    timings = transcript.stats.get("whisper_timings", {}) or {}
    load = timings.get("load_time") or transcript.stats.get("load_seconds") or 0.0
    elapsed = transcript.stats.get("elapsed_seconds", 0.0)
    net = elapsed - load if load and elapsed else elapsed
    speed_all = duration / elapsed if elapsed else 0.0
    speed_net = duration / net if net else 0.0

    stats = {
        "source": args.source,
        "engine": engine.name,
        "language": transcript.lang,
        "media": media.name,
        "media_bytes": media.stat().st_size,
        "duration_seconds": round(duration, 2),
        "acquire_seconds": round(acquire_sec, 2),
        "extract_seconds": round(extract_sec, 2),
        "transcribe_seconds": round(elapsed, 2),
        "realtime_factor_total": round(speed_all, 2),
        "realtime_factor_net": round(speed_net, 2),
        "segments": len(transcript.segments),
        "engine_stats": transcript.stats,
    }
    (outdir / f"{stem}.stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    log()
    log("=" * 52)
    log("  转写完成")
    log("=" * 52)
    log(f"  媒体来源    : {media.name}")
    log(f"  引擎        : {engine.name}（识别语言 {transcript.lang}）")
    log(f"  音频时长    : {hms(duration)}  ({duration:.1f} s)")
    log(f"  取流 / 抽音频: {acquire_sec:.1f} s / {extract_sec:.1f} s")
    log("-" * 52)
    log(f"  转写耗时    : {elapsed:.1f} s")
    if load:
        log(f"    ├─ 加载   : {load:.1f} s")
        log(f"    └─ 净转写 : {net:.1f} s")
    log(f"  处理速度    : {speed_all:.2f}× 实时" + (f"（净 {speed_net:.2f}×）" if load else ""))
    log(f"  字幕条数    : {len(transcript.segments)}")
    log("-" * 52)
    log(f"  输出        : {outdir / f'{stem}.srt'}")
    log(f"              : {outdir / f'{stem}.txt'}")
    log(f"  统计        : {outdir / f'{stem}.stats.json'}")
    log("=" * 52)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

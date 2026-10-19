#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对转写结果做翻译 / 摘要 / 问答（本地 llama-server + Qwen）。

用法
----
    # 把英文字幕翻成中文，输出 video.zh.srt
    python translate.py output/video.srt

    # 对全文做摘要
    python translate.py output/video.txt --task summary

    # 针对内容提问
    python translate.py output/video.txt --task ask -q "作者的核心论点是什么"

服务器行为
----------
默认「惰性启停」：没有活着的 llama-server 就自己拉一个，跑完关掉；
已经有一个在跑就直接复用（不接管），退出时不动它。
用 --keep-server 可以让本次拉起的服务继续留着自己用。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from engines.whisper import parse_srt
from llm import ChatClient, LlamaServer, ServerStartError, ask, summarize, translate_segments

DEFAULT_SERVER = os.environ.get("LLAMA_SERVER_EXE", r"D:\toolchains\llama.cpp\llama-server.exe")
DEFAULT_MODEL = os.environ.get(
    "LLM_MODEL",
    r"D:\workspace\tools\llm\models\qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf",
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="translate",
        description="用本地 Qwen 对转写结果做翻译 / 摘要 / 问答",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("input", help="SRT（翻译用）或 TXT（摘要/问答用）")
    p.add_argument("--task", default="translate", choices=["translate", "summary", "ask"])
    p.add_argument("--target", default="简体中文", help="目标语言（默认 简体中文）")
    p.add_argument("-q", "--question", default="", help="--task ask 的问题")
    p.add_argument("-o", "--out", default="", help="输出路径")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--server", default=DEFAULT_SERVER, help="llama-server 可执行文件")
    p.add_argument("--model", default=DEFAULT_MODEL, help="GGUF 模型路径")
    p.add_argument("--ngl", type=int, default=24, help="卸载到 GPU 的层数（留余量用 24）")
    p.add_argument("--ctx", type=int, default=32768, help="上下文长度（长文本摘要建议 ≥32768）")
    p.add_argument("--keep-server", action="store_true", help="跑完不关闭本次启动的服务")
    p.add_argument("--no-start", action="store_true", help="假定服务已在运行，不尝试启动")
    p.add_argument(
        "--pid-file",
        default=str(Path(__file__).resolve().parent / "output" / ".jobs" / "llama-server.pid"),
        help="记录 llama-server 的 pid（Web UI 用来停常驻服务）；传空串则不写",
    )
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 条字幕（调试用，0=全部）")
    args = p.parse_args(argv)

    src = Path(args.input).expanduser()
    if not src.exists():
        sys.exit(f"[x] 文件不存在：{src}")

    is_srt = src.suffix.lower() == ".srt"
    if args.task == "translate" and not is_srt:
        sys.exit("[x] 翻译需要 SRT 文件（要保留时间轴）；摘要/问答用 TXT")
    if args.task == "ask" and not args.question:
        sys.exit("[x] --task ask 必须用 -q 提供问题")

    server = LlamaServer(
        exe=Path(args.server), model=Path(args.model),
        port=args.port, ngl=args.ngl, ctx=args.ctx,
        pid_file=Path(args.pid_file) if args.pid_file else None,
    )

    if args.no_start:
        if not server.is_up():
            sys.exit(f"[x] 端口 {args.port} 上没有运行中的 llama-server")
        entered = None
    else:
        entered = server

    try:
        if entered is not None:
            print(f"[>>] 启动 llama-server（ngl={args.ngl}, ctx={args.ctx}）……", flush=True)
            entered.__enter__()
            print("[>>] 服务就绪", flush=True)
        client = ChatClient(port=args.port)

        if args.task == "translate":
            segments = parse_srt(src.read_text(encoding="utf-8", errors="replace"))
            if not segments:
                sys.exit("[x] 没有解析到字幕条目")
            if args.limit > 0:
                segments = segments[: args.limit]
            print(f"[>>] 共 {len(segments)} 条字幕，开始翻译……", flush=True)

            def progress(i: int, total: int, got: int, expected: int) -> None:
                warn = "" if got == expected else f"  ⚠️ 本批只回 {got}/{expected} 条"
                print(f"     批次 {i}/{total} 完成{warn}", flush=True)

            new_segs = translate_segments(
                client, segments, target=args.target, on_batch=progress
            )
            srt = "\n".join(
                f"{i}\n{_srt_time(s.start)} --> {_srt_time(s.end)}\n{s.text}\n"
                for i, s in enumerate(new_segs, 1)
            )
            out = Path(args.out) if args.out else src.with_suffix(".zh.srt")
            out.write_text(srt, encoding="utf-8")
            print(f"[ok] 翻译完成 → {out}", flush=True)

        elif args.task == "summary":
            text = src.read_text(encoding="utf-8", errors="replace")
            out = Path(args.out) if args.out else src.with_suffix(".summary.md")
            result = summarize(
                client, text,
                on_part=lambda i, n: print(f"     分段笔记 {i}/{n}", flush=True),
            )
            out.write_text(_summary_header(src, len(text)) + result + "\n", encoding="utf-8")
            print(result)
            print(f"\n[ok] 摘要 → {out}", flush=True)

        else:  # ask
            text = src.read_text(encoding="utf-8", errors="replace")
            print(ask(client, text, args.question), flush=True)

    except ServerStartError as exc:
        sys.exit(f"[x] {exc}")
    finally:
        if entered is not None:
            if args.keep_server:
                print("[>>] --keep-server：保留服务", flush=True)
            else:
                entered.stop()
                print("[>>] 已关闭本次启动的服务", flush=True)

    return 0


def _hms(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _summary_header(src: Path, chars: int) -> str:
    """摘要文件头部：来源、时长、引擎、生成时间。

    没有它的话，过几天翻回来看一份 summary.md，完全不知道对应哪个视频。
    stats 在同目录的 xxx.stats.json 里，顺手读一下。
    """
    lines = [f"> 字幕：`{src.name}` ／ {chars} 字"]
    try:
        st = json.loads(src.with_suffix(".stats.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = None
    if st:
        lines.append(
            f"> 素材：{st.get('source', '?')}"
            f"（{_hms(st.get('duration_seconds') or 0)}，"
            f"{st.get('segments', '?')} 条字幕）"
        )
        lines.append(
            f"> 引擎：{st.get('engine', '?')}，转写 "
            f"{st.get('transcribe_seconds', 0):.1f} 秒"
            f"（{st.get('realtime_factor_total', 0):.2f}× 实时）"
        )
    lines.append(f"> 生成：{datetime.now():%Y-%m-%d %H:%M} · Qwen2.5-7B-Instruct 本地推理")
    return "\n".join(lines) + "\n\n"


def _srt_time(sec: float) -> str:
    ms = max(0, round(sec * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


if __name__ == "__main__":
    raise SystemExit(main())

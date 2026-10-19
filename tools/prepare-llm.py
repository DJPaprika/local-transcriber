#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 / 一键准备本地 LLM（llama.cpp + Qwen GGUF）。

    python tools/prepare-llm.py            # 只检查，缺什么说清楚
    python tools/prepare-llm.py --download # 下载 llama.cpp（Vulkan 构建）和模型

默认路径和 translate.py 共用同一套环境变量：
    LLAMA_SERVER_EXE   默认 D:/toolchains/llama.cpp/llama-server.exe
    LLM_MODEL          默认 D:/workspace/tools/llm/models/qwen2.5-7b-...-00001-of-00002.gguf

下载走 HTTPS。GitHub / Hugging Face 连不上时加 --proxy：

    python tools/prepare-llm.py --download --proxy http://127.0.0.1:7890
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from translate import DEFAULT_MODEL, DEFAULT_SERVER  # noqa: E402

LLAMA_REPO = "ggml-org/llama.cpp"
MODEL_REPO = "Qwen/Qwen2.5-7B-Instruct-GGUF"
MODEL_PATTERN = re.compile(r"qwen2\.5-7b-instruct-q4_k_m-(\d+)-of-(\d+)\.gguf$", re.I)
CHUNK = 1024 * 1024


# ---------------------------------------------------------------- 网络

def opener(proxy: str | None):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def fetch_json(url: str, proxy: str | None) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "local-transcriber/1.1"})
    with opener(proxy).open(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(url: str, dest: Path, proxy: str | None, label: str) -> None:
    """流式下载，打印百分比；失败自动重试两次。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    total = 0
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "local-transcriber/1.1"}
            )
            with opener(proxy).open(req, timeout=60) as resp, part.open("wb") as f:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                t0 = time.time()
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 / total
                        speed = done / 1024 / 1024 / max(time.time() - t0, 0.1)
                        print(
                            f"\r      {label}: {pct:5.1f}%  "
                            f"{done / 1024 / 1024:.0f}/{total / 1024 / 1024:.0f} MB  "
                            f"{speed:.1f} MB/s",
                            end="", flush=True,
                        )
            if total and part.stat().st_size != total:
                raise OSError(f"大小不符：{part.stat().st_size} != {total}")
            part.replace(dest)
            print(f"\r      {label}: 完成（{dest.stat().st_size / 1024 / 1024:.0f} MB）      ")
            return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            print(f"\n      [!] 第 {attempt} 次下载失败：{exc}")
            part.unlink(missing_ok=True)
            if attempt == 3:
                raise


# ---------------------------------------------------------------- 检查

def _size(n: int) -> str:
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.0f} MB"


def model_files(model: Path) -> list[Path]:
    """模型可能是分片的，把同前缀的分片都列出来。"""
    m = MODEL_PATTERN.search(model.name)
    if not m:
        return [model]
    prefix = model.name[: m.start()]
    return sorted(model.parent.glob(prefix + "*-of-*.gguf"))


def check(server: Path, model: Path) -> tuple[bool, list[str]]:
    problems: list[str] = []
    if not server.is_file():
        problems.append(f"llama-server 不存在：{server}")
    parts = model_files(model)
    missing = [p for p in parts if not p.is_file()]
    if not model.is_file():
        problems.append(f"模型不存在：{model}")
    elif missing:
        problems.append("模型分片缺失：" + "、".join(p.name for p in missing))
    return (not problems), problems


def describe(server: Path, model: Path) -> None:
    print("=" * 60)
    print("  本地 LLM 依赖检查")
    print("=" * 60)

    if server.is_file():
        print(f"  llama-server : {server}（{_size(server.stat().st_size)}）")
    else:
        print(f"  llama-server : [x] 缺失 {server}")

    parts = [p for p in model_files(model) if p.is_file()]
    if parts:
        total = sum(p.stat().st_size for p in parts)
        print(f"  模型         : {model.parent}（{len(parts)} 个分片，{_size(total)}）")
    else:
        print(f"  模型         : [x] 缺失 {model}")

    print("=" * 60)
    print("  翻译 / 摘要 / 问答都用到这两样；只做转写可以不管。")


# ---------------------------------------------------------------- 下载

def install_llama(server_dir: Path, proxy: str | None) -> None:
    api = f"https://api.github.com/repos/{LLAMA_REPO}/releases/latest"
    print(f"[>>] 查询最新 llama.cpp 版本：{api}")
    data = fetch_json(api, proxy)
    asset = next(
        (
            a for a in data.get("assets", [])
            if "win-vulkan-x64" in a["name"] and a["name"].endswith(".zip")
        ),
        None,
    )
    if asset is None:
        raise RuntimeError("最新 release 里没有 win-vulkan-x64.zip，请手动下载")
    print(f"     {data.get('tag_name', '?')} · {asset['name']}")
    print(f"     {asset['size'] / 1024 / 1024:.0f} MB")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        zip_path = tmpdir / asset["name"]
        download(asset["browser_download_url"], zip_path, proxy, "llama.cpp")
        extract = tmpdir / "extract"
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract)

        exe = next(extract.rglob("llama-server.exe"), None)
        if exe is None:
            raise RuntimeError("压缩包里没找到 llama-server.exe")
        src_dir = exe.parent
        server_dir.mkdir(parents=True, exist_ok=True)
        for f in src_dir.iterdir():
            if f.is_file():
                shutil.copy2(f, server_dir / f.name)
    print(f"[ok] llama.cpp 已解压到 {server_dir}")


def install_model(model: Path, proxy: str | None) -> None:
    m = re.search(r"-(\d+)-of-(\d+)\.gguf$", model.name, re.I)
    if not m:
        raise RuntimeError(f"看不懂模型文件名（需要 -00001-of-00002 这种分片名）：{model.name}")
    total = int(m.group(2))
    prefix = model.name[: m.start() + 1]
    for i in range(1, total + 1):
        name = f"{prefix}{i:05d}-of-{total:05d}.gguf"
        dest = model.parent / name
        if dest.is_file():
            print(f"[ok] 已存在，跳过：{name}")
            continue
        url = f"https://huggingface.co/{MODEL_REPO}/resolve/main/{name}?download=true"
        print(f"[>>] 下载 {name}")
        download(url, dest, proxy, name)
    print(f"[ok] 模型已就绪：{model}")


# ---------------------------------------------------------------- 入口

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="prepare-llm", description="准备本地 LLM 运行环境")
    ap.add_argument("--server", default=DEFAULT_SERVER, help="llama-server 路径")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="GGUF 模型路径（第一个分片）")
    ap.add_argument("--download", action="store_true", help="缺什么下载什么")
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY", ""), help="HTTP 代理")
    args = ap.parse_args(argv)

    server = Path(args.server).expanduser()
    model = Path(args.model).expanduser()
    proxy = args.proxy or None

    describe(server, model)
    ok, problems = check(server, model)

    if ok:
        print("\n[ok] 都齐了。试试：")
        print("     python translate.py output\\example.txt --task summary")
        return 0

    for line in problems:
        print(f"  [x] {line}")

    if not args.download:
        print("\n  一键下载：python tools/prepare-llm.py --download")
        return 1

    print("\n[>>] 开始下载（llama.cpp 约 100 MB，模型约 4.4 GB）")
    if not server.is_file():
        install_llama(server.parent, proxy)
    if not model.is_file():
        install_model(model, proxy)

    ok, problems = check(server, model)
    if ok:
        print("\n[ok] 准备完成")
        return 0
    for line in problems:
        print(f"  [x] {line}")
    return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

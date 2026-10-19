#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 / 设置 SenseVoice 引擎的运行依赖。

    python tools/setup-sherpa.py            # 只检查，打印结论和修复建议
    python tools/setup-sherpa.py --persist  # 把 SHERPA_ONNX_NODE 写进用户环境变量

检查三样东西：
    1. node 在不在 PATH 里
    2. sherpa-onnx-node（npm 包）在哪
    3. SenseVoice 模型文件齐不齐

探测顺序和 transcribe.py / bridge/sv-bridge.js 保持一致，
所以这里找到什么，转写时就能用什么。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_NAME = "SHERPA_ONNX_NODE"


def candidates() -> list[Path]:
    out: list[Path] = []
    explicit = os.environ.get(ENV_NAME, "").strip()
    if explicit:
        out.append(Path(explicit))

    out += [
        ROOT / "node_modules" / "sherpa-onnx-node",
        ROOT / "bridge" / "node_modules" / "sherpa-onnx-node",
    ]

    appdata = os.environ.get("APPDATA")
    if appdata:
        npm_root = Path(appdata) / "npm" / "node_modules"
        out += [
            npm_root / "@deepseek-ai" / "dsh" / "node_modules" / "sherpa-onnx-node",
            npm_root / "sherpa-onnx-node",
        ]

    dsh_home = os.environ.get("DSH_HOME")
    if dsh_home:
        out.append(Path(dsh_home) / "node_modules" / "sherpa-onnx-node")
    return out


def find_sherpa() -> Path | None:
    for cand in candidates():
        try:
            if (cand / "package.json").is_file() or (cand / "index.js").is_file():
                return cand
        except OSError:
            continue
    return None


def model_dirs() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("SENSEVOICE_MODEL_DIR", "").strip()
    if env:
        out.append(Path(env))
    home = Path.home()
    out += [
        home / ".dsh" / "speech-to-text" / "sensevoice" / "models",
        ROOT / "models",
    ]
    return out


def find_models() -> Path | None:
    for d in model_dirs():
        files = [
            d / "sensevoice-onnx" / "model.int8.onnx",
            d / "sensevoice-onnx" / "tokens.txt",
            d / "silero" / "silero_vad.onnx",
        ]
        if all(f.is_file() for f in files):
            return d
    return None


def persist(value: str) -> bool:
    if sys.platform == "win32":
        # setx 写的是用户级变量（HKCU），不需要管理员权限；新开的终端才生效。
        proc = subprocess.run(
            ["setx", ENV_NAME, value],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        print(proc.stdout.strip())
        return proc.returncode == 0
    print(f'把下面这行加进 shell 配置（~/.bashrc 或 ~/.zshrc）：')
    print(f'  export {ENV_NAME}="{value}"')
    return True


def main() -> int:
    argv = sys.argv[1:]

    # run.cmd 用这个只取路径：成功时只打印一行，别的一概不输出。
    if "--print-path" in argv:
        found = find_sherpa()
        if found:
            print(found)
        return 0 if found else 1

    persist_flag = "--persist" in argv

    ok = True
    print("=" * 56)
    print("  SenseVoice 依赖检查")
    print("=" * 56)

    node = shutil.which("node")
    if node:
        # 不抓子进程输出：受限环境可能不允许建管道，拿版本不值这个麻烦。
        print(f"  node            : {node}")
    else:
        ok = False
        print("  node            : [x] 不在 PATH 里，去 https://nodejs.org 装一个")

    sherpa = find_sherpa()
    if sherpa:
        print(f"  {ENV_NAME:<16}: {sherpa}")
    else:
        ok = False
        print(f"  {ENV_NAME:<16}: [x] 没找到")
        print("                    装一个：npm i sherpa-onnx-node")
        print(f"                    或用 {ENV_NAME} 指定目录")

    models = find_models()
    if models:
        print(f"  SenseVoice 模型 : {models}")
    else:
        ok = False
        print("  SenseVoice 模型 : [x] 没找到完整的模型目录")
        print("                    期望结构：<MODEL_DIR>/sensevoice-onnx/model.int8.onnx")
        print("                              <MODEL_DIR>/sensevoice-onnx/tokens.txt")
        print("                              <MODEL_DIR>/silero/silero_vad.onnx")
        print("                    用 SENSEVOICE_MODEL_DIR 指定")

    print("=" * 56)

    if not sherpa:
        return 1

    print(f"  当前进程里 {'已设置' if os.environ.get(ENV_NAME) else '未设置'} {ENV_NAME}")
    if persist_flag:
        print(f"\n[>>] 写入用户环境变量 {ENV_NAME} = {sherpa}")
        if persist(str(sherpa)):
            print("[ok] 已写入。新开的终端里生效；当前窗口可临时执行：")
            print(f'     set {ENV_NAME}={sherpa}')
        else:
            print("[x] 写入失败，可手动执行：")
            print(f'     setx {ENV_NAME} "{sherpa}"')
    elif not os.environ.get(ENV_NAME):
        print("\n  想一劳永逸：python tools/setup-sherpa.py --persist")

    print("\n  下一步：python transcribe.py <视频或音频>")
    return 0 if ok else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

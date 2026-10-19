"""llama-server 的惰性启停管理。

行为：
  - 启动前先探测端口，已经有服务在跑就直接用（不接管它）
  - 没有就自己拉起一个，等 /health 就绪
  - 退出时：只关自己拉起的那个，别人的不动

这样「偶尔翻一次」能自动清理显存，「批量翻 20 个」也不会重复加载模型。
"""

from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


class ServerStartError(RuntimeError):
    """服务无法启动。"""


class LlamaServer:
    def __init__(
        self,
        exe: Path,
        model: Path,
        *,
        port: int = 8080,
        ngl: int = 24,
        ctx: int = 8192,
        ready_timeout: float = 180.0,
        pid_file: Path | None = None,
    ):
        self.exe = Path(exe)
        self.model = Path(model)
        self.port = port
        self.ngl = ngl
        self.ctx = ctx
        self.ready_timeout = ready_timeout
        self.pid_file = Path(pid_file) if pid_file else None
        self.proc: subprocess.Popen | None = None
        self._owned = False

    # ---------------------------------------------------------------- 状态

    def is_up(self, timeout: float = 1.0) -> bool:
        """探测服务是否已经就绪。"""
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/health", timeout=timeout
            ) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # ---------------------------------------------------------------- 启停

    def start(self) -> None:
        if self.is_up():
            return  # 复用已有服务，不接管

        if not self.exe.exists():
            raise ServerStartError(f"找不到 llama-server：{self.exe}")
        if not self.model.exists():
            raise ServerStartError(f"找不到模型：{self.model}")

        cmd = [
            str(self.exe),
            "-m", str(self.model),
            "-ngl", str(self.ngl),
            "--ctx-size", str(self.ctx),
            "--host", "127.0.0.1",
            "--port", str(self.port),
        ]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._owned = True
        self._write_pid()

        t0 = time.time()
        while time.time() - t0 < self.ready_timeout:
            if self.proc.poll() is not None:
                self._owned = False
                raise ServerStartError(
                    f"llama-server 启动后立刻退出（退出码 {self.proc.returncode}），"
                    "请检查 Vulkan 驱动和模型路径"
                )
            if self.is_up():
                return
            time.sleep(0.5)

        self.stop()
        raise ServerStartError(f"llama-server 在 {self.ready_timeout:.0f} 秒内没有就绪")

    def stop(self) -> None:
        """只关自己拉起的进程。"""
        if not (self.proc and self._owned):
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None
        self._owned = False
        self._clear_pid()

    # ---------------------------------------------------------------- pid 文件
    # 常驻模式下进程要活过 translate.py，得把 pid 留下来，Web UI 才知道怎么停。

    def _write_pid(self) -> None:
        if self.pid_file is None or self.proc is None:
            return
        try:
            self.pid_file.parent.mkdir(parents=True, exist_ok=True)
            self.pid_file.write_text(str(self.proc.pid), encoding="ascii")
        except OSError:
            pass

    def _clear_pid(self) -> None:
        if self.pid_file is None:
            return
        try:
            self.pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    # ---------------------------------------------------------------- 上下文

    def __enter__(self) -> "LlamaServer":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

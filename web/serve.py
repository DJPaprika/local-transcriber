"""本地 Web UI，只用标准库。

    python web/serve.py              # 从 8081 开始找空闲端口，然后自动开浏览器
    python web/serve.py --no-browser # 不自动开浏览器
    python web/serve.py --port 9000  # 指定端口（被占则递增）

浏览器里就是一个页面：贴链接或路径 → 看进度和日志 → 下载字幕、跑摘要。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from jobs import JobManager  # noqa: E402

STATIC = HERE / "static"
OUTPUT = ROOT / "output"

# 安全名字：不允许路径穿越，也不允许空
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fa5.\- ]{1,80}$")

MANAGER: JobManager | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "local-transcriber/1.0"

    # ------------------------------------------------------------ 工具

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, status: int = 200, ctype: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, download: bool = False) -> None:
        if not path.is_file():
            self._text("not found", 404)
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in (".md", ".txt", ".srt"):
            ctype = "text/plain; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if download:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{path.name}"'
            )
        self.end_headers()
        self.wfile.write(data)

    def _body_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt, *args) -> None:  # 别把每个请求都打到控制台
        pass

    # ------------------------------------------------------------ 路由

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._file(STATIC / "index.html")
        if path.startswith("/static/"):
            target = (STATIC / path[len("/static/"):]).resolve()
            if STATIC.resolve() not in target.parents and target != STATIC.resolve():
                return self._text("forbidden", 403)
            return self._file(target)
        if path == "/favicon.ico":
            return self._text("", 204)

        if path == "/api/jobs":
            return self._json(MANAGER.list() if MANAGER else [])

        m = re.match(r"^/api/jobs/([0-9a-f]+)$", path)
        if m:
            try:
                offset = int(query.get("offset", ["0"])[0])
            except ValueError:
                offset = 0
            snap = MANAGER.snapshot(m.group(1), offset) if MANAGER else None
            if snap is None:
                return self._json({"error": "任务不存在"}, 404)
            return self._json(snap)

        if path == "/api/files":
            rel = query.get("path", [""])[0]
            return self._serve_output(rel, download=True)

        return self._text("not found", 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/jobs":
            return self._create_job()

        m = re.match(r"^/api/jobs/([0-9a-f]+)/cancel$", path)
        if m:
            ok = MANAGER.cancel(m.group(1)) if MANAGER else False
            return self._json({"ok": ok})

        return self._text("not found", 404)

    # ------------------------------------------------------------ 业务

    def _serve_output(self, rel: str, download: bool):
        if not rel:
            return self._text("missing path", 400)
        target = (OUTPUT / rel).resolve()
        if OUTPUT.resolve() not in target.parents and target != OUTPUT.resolve():
            return self._text("forbidden", 403)
        return self._file(target, download=download)

    def _create_job(self):
        body = self._body_json()
        kind = (body.get("kind") or "transcribe").strip()
        params = body.get("params") or {}

        err = self._validate(kind, params)
        if err:
            return self._json({"error": err}, 400)

        try:
            job = MANAGER.submit(kind, params)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json(job, 201)

    def _validate(self, kind: str, p: dict) -> str | None:
        if kind == "transcribe":
            src = (p.get("source") or "").strip()
            if not src:
                return "请填写视频链接或本地文件路径"
            p["source"] = src
            name = (p.get("name") or "").strip()
            if not name:
                p["name"] = _derive_name(src)
            if not _NAME_RE.match(p["name"]):
                return "输出名只能用字母、数字、中文、下划线、点和短横线"
            p.setdefault("engine", "sensevoice")
            p.setdefault("language", "auto")
            return None

        if kind in ("summary", "translate"):
            inp = (p.get("input") or "").strip()
            if not inp:
                return "请选择一个已完成的转写任务"
            p["input"] = inp
            name = (p.get("name") or "").strip()
            if not name:
                return "缺少输出名"
            if not _NAME_RE.match(name):
                return "输出名不合法"
            return None

        if kind == "ask":
            inp = (p.get("input") or "").strip()
            q = (p.get("question") or "").strip()
            if not inp:
                return "请选择一个已完成的转写任务"
            if not q:
                return "请填写问题"
            p["input"], p["question"] = inp, q
            p.setdefault("name", "ask")
            return None

        return f"未知任务类型：{kind}"


def _derive_name(src: str) -> str:
    """从 URL 或文件名推一个安全的输出名。"""
    s = src.strip().rstrip("/")
    base = s.rsplit("/", 1)[-1] or "transcript"
    if "v=" in base:  # YouTube 风格
        base = base.split("v=")[-1]
    base = base.split("?")[0]
    for suffix in (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".wav", ".flac", ".srt", ".txt"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
            break
    base = re.sub(r"[^\w\u4e00-\u9fa5.\-]+", "_", base).strip("._")
    return (base or "transcript")[:60]


def find_port(start: int) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit(f"从 {start} 开始找不到空闲端口")


def main(argv=None) -> int:
    global MANAGER
    ap = argparse.ArgumentParser(prog="serve", description="local-transcriber 的本地 Web UI")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    MANAGER = JobManager(OUTPUT)
    port = find_port(args.port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    url = f"http://127.0.0.1:{port}/"
    print("=" * 52)
    print("  local-transcriber Web UI")
    print("=" * 52)
    print(f"  地址 : {url}")
    print(f"  输出 : {OUTPUT}")
    print("  Ctrl+C 停止")
    print("=" * 52, flush=True)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[>>] 停止中……", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

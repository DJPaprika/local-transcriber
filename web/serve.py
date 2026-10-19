"""本地 Web UI，只用标准库。

    python web/serve.py              # 从 8081 开始找空闲端口，然后自动开浏览器
    python web/serve.py --no-browser # 不自动开浏览器
    python web/serve.py --port 9000  # 指定端口（被占则递增）

浏览器里就是一个页面：贴链接、拖文件或填路径 → 看进度和日志 → 下载字幕、跑摘要。

新增的接口（相对最初版本）：
    DELETE /api/jobs/{id}    删除一条已结束的任务（只删记录，不删产物）
    POST   /api/upload       拖拽上传：body 是文件原始字节，?name=文件名
    GET    /api/settings     读取 Web 层设置（目前只有 keep_server）
    POST   /api/settings     修改设置
    GET    /api/llm          本地 llama-server 是否在跑
    POST   /api/llm/stop     停掉常驻的 llama-server
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from jobs import JobManager  # noqa: E402

STATIC = HERE / "static"
OUTPUT = ROOT / "output"
UPLOAD_DIR = OUTPUT / ".uploads"
PID_FILE = OUTPUT / ".jobs" / "llama-server.pid"
LLM_PORT = 8080

# 安全名字：不允许路径穿越，也不允许空
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fa5.\- ]{1,80}$")

MAX_UPLOAD = 8 * 1024**3  # 8 GB，够放一小时以上的视频
UPLOAD_EXTS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m4v",
    ".m4a", ".mp3", ".wav", ".flac", ".ogg", ".aac", ".opus", ".wma",
}
UPLOAD_CHUNK = 1024 * 1024

MANAGER: JobManager | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "local-transcriber/1.1"

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
            # HTTP 头只能是 latin-1，中文文件名必须走 RFC 5987 的 filename*。
            fallback = path.name.encode("ascii", "ignore").decode() or "download"
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{fallback}"; '
                f"filename*=UTF-8''{quote(path.name)}",
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

        if path == "/api/settings":
            return self._json(MANAGER.get_settings() if MANAGER else {})

        if path == "/api/llm":
            return self._json(self._llm_status())

        return self._text("not found", 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path == "/api/jobs":
            return self._create_job()

        m = re.match(r"^/api/jobs/([0-9a-f]+)/cancel$", path)
        if m:
            ok = MANAGER.cancel(m.group(1)) if MANAGER else False
            return self._json({"ok": ok})

        if path == "/api/upload":
            name = (query.get("name") or [""])[0]
            return self._upload(name)

        if path == "/api/settings":
            if not MANAGER:
                return self._json({"error": "服务未就绪"}, 503)
            return self._json(MANAGER.update_settings(self._body_json()))

        if path == "/api/llm/stop":
            return self._json(self._llm_stop())

        return self._text("not found", 404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        m = re.match(r"^/api/jobs/([0-9a-f]+)$", path)
        if m:
            ok = MANAGER.delete(m.group(1)) if MANAGER else False
            if not ok:
                return self._json({"error": "任务不存在，或还没结束"}, 409)
            return self._json({"ok": True})
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
                return "请填写视频链接、本地文件路径，或拖一个文件进来"
            p["source"] = src
            # 名字留空 = 让 transcribe.py 用视频标题（和 CLI 一致）。
            name = (p.get("name") or "").strip()
            p["name"] = name
            if name and not _NAME_RE.match(name):
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
            p["name"] = name
            if name and not _NAME_RE.match(name):
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

    # ------------------------------------------------------------ 上传

    def _upload(self, raw_name: str) -> None:
        if MANAGER is None:
            return self._json({"error": "服务未就绪"}, 503)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return self._json({"error": "空文件"}, 400)
        if n > MAX_UPLOAD:
            return self._json({"error": f"文件太大（上限 {MAX_UPLOAD // 1024**3} GB）"}, 413)

        safe = _safe_upload_name(raw_name)
        if safe is None:
            return self._json({"error": "不支持的文件类型"}, 400)

        # 每次上传单独一个目录，文件名保持原样——转写时的输出名就是文件本身的名字。
        folder = UPLOAD_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._json({"error": f"无法创建上传目录：{exc}"}, 500)
        dest = folder / safe

        remaining = n
        try:
            with dest.open("wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(UPLOAD_CHUNK, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except OSError as exc:
            shutil.rmtree(folder, ignore_errors=True)
            return self._json({"error": f"写入失败：{exc}"}, 500)

        if remaining > 0:
            shutil.rmtree(folder, ignore_errors=True)
            return self._json({"error": "上传中断，文件不完整"}, 400)

        return self._json(
            {"path": str(dest.resolve()), "name": safe, "size": n}, 201
        )

    # ------------------------------------------------------------ LLM 状态

    def _llm_status(self) -> dict:
        keep = MANAGER.get_settings().get("keep_server") if MANAGER else False
        pid = None
        try:
            pid = int(PID_FILE.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            pid = None
        running = _port_open(LLM_PORT)
        return {
            "running": running,
            "pid": pid,
            "stale": bool(pid and not running),
            "keep_server": bool(keep),
            "port": LLM_PORT,
        }

    def _llm_stop(self) -> dict:
        try:
            pid = int(PID_FILE.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return {"ok": False, "error": "没有记录到 llama-server 的 pid（可能是外部启动的服务）"}

        # 端口已经关了：进程早没了，清掉残留的 pid 文件就算完成。
        if not _port_open(LLM_PORT):
            _unlink_pid()
            return {"ok": True, "pid": pid, "already_stopped": True}

        if not _kill_pid(pid):
            return {
                "ok": False,
                "error": f"停不掉 pid {pid}：taskkill 和终止进程都被拒绝（受限沙箱？）",
            }

        for _ in range(25):  # 最多等 5 秒，端口释放才算真的停了
            if not _port_open(LLM_PORT):
                break
            time.sleep(0.2)
        _unlink_pid()
        return {"ok": True, "pid": pid}


def _port_open(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _kill_pid(pid: int) -> bool:
    """taskkill 杀整棵树；被沙箱拒绝时退回 os.kill。"""
    if sys.platform == "win32":
        try:
            done = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15,
            )
            if done.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _unlink_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _safe_upload_name(raw: str) -> str | None:
    """把浏览器给的文件名收敛成一个安全的落盘名，扩展名必须是音视频。"""
    name = Path(raw.replace("\\", "/")).name.strip()
    name = re.sub(r'[\/:*?"<>|\x00-\x1f]+', "_", name).strip("._ ")
    if not name:
        name = "upload"
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext not in UPLOAD_EXTS:
        return None
    return (stem[:60] or "upload") + ext


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

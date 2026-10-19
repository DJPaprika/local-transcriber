"""Web UI 的任务队列。

设计要点（和 docs/webui-plan.md 对应）：

- **串行**：一次只跑一个任务。SenseVoice 会吃满 CPU，显存也只够一个 llama-server。
- **不用管道**：子进程的 stdout/stderr 直接写日志文件，父进程按 offset 增量读；
  进度从 .progress.json 读。这样父子之间没有 pipe，Windows 上也不会踩到手柄问题。
- **能杀干净**：取消时在 Windows 上用 `taskkill /T /F` 杀整棵进程树，
  否则 node bridge、ffmpeg 会变成孤儿继续占 CPU。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

KIND_LABEL = {
    "transcribe": "转写",
    "summary": "摘要",
    "translate": "翻译",
    "ask": "问答",
}

TERMINAL = {"done", "failed", "cancelled"}


class JobManager:
    def __init__(self, out_root: Path):
        self.out_root = out_root
        self.jobs_dir = out_root / ".jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._order: list[str] = []
        self._queue: deque[str] = deque()
        self._current: str | None = None
        self._proc: subprocess.Popen | None = None
        self._cancelled: set[str] = set()

        self._wake = threading.Event()
        self._load_history()
        threading.Thread(target=self._run_loop, daemon=True).start()

    # ------------------------------------------------------------ 持久化

    def _load_history(self) -> None:
        for path in self.jobs_dir.glob("*/job.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            jid = data.get("id")
            if not jid:
                continue
            # 上次退出时还在跑的任务，标记成中断
            if data.get("status") in ("queued", "running"):
                data["status"] = "failed"
                data["error"] = "服务重启，任务被中断"
            self._jobs[jid] = data
            self._order.append(jid)
        self._order.sort(key=lambda j: self._jobs[j].get("created_at", 0))

    def _job_dir(self, jid: str) -> Path:
        d = self.jobs_dir / jid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save(self, job: dict) -> None:
        try:
            path = self._job_dir(job["id"]) / "job.json"
            path.write_text(
                json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ------------------------------------------------------------ 对外

    def submit(self, kind: str, params: dict) -> dict:
        if kind not in KIND_LABEL:
            raise ValueError(f"未知任务类型：{kind}")
        jid = uuid.uuid4().hex[:12]
        job = {
            "id": jid,
            "kind": kind,
            "label": KIND_LABEL[kind],
            "params": params,
            "status": "queued",
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "returncode": None,
            "progress": None,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._jobs[jid] = job
            self._order.append(jid)
            self._queue.append(jid)
        self._save(job)
        self._wake.set()
        return job

    def get(self, jid: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(jid)
            return dict(job) if job else None

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(self._jobs[j]) for j in reversed(self._order)]

    def cancel(self, jid: str) -> bool:
        with self._lock:
            job = self._jobs.get(jid)
            if not job or job["status"] in TERMINAL:
                return False
            if job["status"] == "queued":
                self._cancelled.add(jid)
                job["status"] = "cancelled"
                job["finished_at"] = time.time()
                self._save(job)
                return True
            proc = self._proc
            self._cancelled.add(jid)
        self._kill_tree(proc)
        return True

    def snapshot(self, jid: str, log_offset: int = 0) -> dict | None:
        """返回任务状态 + 增量日志 + 当前进度。"""
        job = self.get(jid)
        if job is None:
            return None
        log_text, offset = self.read_log(jid, log_offset)
        job["log"] = log_text
        job["log_offset"] = offset
        job["progress"] = self.read_progress(jid)
        return job

    def read_progress(self, jid: str) -> dict | None:
        path = self.jobs_dir / jid / "progress.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def read_log(self, jid: str, offset: int = 0) -> tuple[str, int]:
        path = self.jobs_dir / jid / "job.log"
        try:
            size = path.stat().st_size
            if size <= offset:
                return "", offset
            with path.open("rb") as f:
                f.seek(offset)
                chunk = f.read()
            return chunk.decode("utf-8", errors="replace"), offset + len(chunk)
        except OSError:
            return "", offset

    # ------------------------------------------------------------ 执行

    def _run_loop(self) -> None:
        while True:
            self._wake.wait()
            while True:
                with self._lock:
                    if not self._queue:
                        self._wake.clear()
                        break
                    jid = self._queue.popleft()
                    job = self._jobs.get(jid)
                    if job is None or job["status"] == "cancelled":
                        continue
                    job["status"] = "running"
                    job["started_at"] = time.time()
                    self._current = jid
                self._save(job)
                try:
                    self._execute(job)
                except Exception as exc:  # 兜底，别让 worker 线程死掉
                    with self._lock:
                        job["status"] = "failed"
                        job["error"] = f"内部错误：{exc}"
                        job["finished_at"] = time.time()
                    self._save(job)

    def _build_cmd(self, job: dict) -> list[str]:
        p = job["params"]
        kind = job["kind"]
        outdir = self.out_root / p["name"]

        if kind == "transcribe":
            cmd = [
                PY, str(ROOT / "transcribe.py"), p["source"],
                "-o", str(outdir),
                "-n", p["name"],
                "-e", p.get("engine", "sensevoice"),
                "-l", p.get("language", "auto"),
                "--progress", str(self.jobs_dir / job["id"] / "progress.json"),
            ]
            if p.get("keep_audio"):
                cmd.append("--keep-audio")
            return cmd

        # 摘要 / 翻译 / 问答的输入：前端给的是相对 output/ 的路径（如 clip40/clip40.txt），
        # 直接交给 translate.py 会按当前工作目录解析，落到项目根下就找不到了。
        inp = Path(p["input"]).expanduser()
        if not inp.is_absolute():
            inp = self.out_root / inp
        src = str(inp)

        if kind == "translate":
            return [
                PY, str(ROOT / "translate.py"), src,
                "--task", "translate",
                "--target", p.get("target", "简体中文"),
                "-o", str(outdir / f"{p['name']}.zh.srt"),
            ]

        if kind == "summary":
            return [
                PY, str(ROOT / "translate.py"), src,
                "--task", "summary",
                "-o", str(outdir / f"{p['name']}.summary.md"),
            ]

        return [
            PY, str(ROOT / "translate.py"), src,
            "--task", "ask", "-q", p["question"],
        ]

    def _execute(self, job: dict) -> None:
        jid = job["id"]
        jdir = self._job_dir(jid)
        log_path = jdir / "job.log"
        cmd = self._build_cmd(job)

        if job["kind"] != "transcribe":
            # 让 translate.py 的输出目录存在
            (self.out_root / job["params"]["name"]).mkdir(parents=True, exist_ok=True)

        with log_path.open("wb") as logf:
            logf.write(("$ " + " ".join(cmd) + "\n\n").encode("utf-8"))
            logf.flush()
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
            except OSError as exc:
                job["status"] = "failed"
                job["error"] = f"无法启动子进程：{exc}"
                job["finished_at"] = time.time()
                self._save(job)
                return
            with self._lock:
                self._proc = proc
            rc = proc.wait()
            with self._lock:
                self._proc = None

        with self._lock:
            cancelled = jid in self._cancelled
            self._cancelled.discard(jid)
            self._current = None
            job["returncode"] = rc
            job["finished_at"] = time.time()
            if cancelled:
                job["status"] = "cancelled"
                job["error"] = "已取消"
            elif rc == 0:
                job["status"] = "done"
                job["result"] = self._collect_result(job)
            else:
                job["status"] = "failed"
                job["error"] = self._tail_error(log_path)
        self._save(job)

    def _collect_result(self, job: dict) -> dict:
        p = job["params"]
        outdir = self.out_root / p["name"]
        result: dict = {"files": []}
        if outdir.is_dir():
            for f in sorted(outdir.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    result["files"].append(
                        {"name": f.name, "size": f.stat().st_size}
                    )
        prog = self.read_progress(job["id"])
        if prog:
            result.update(
                {k: prog[k] for k in ("srt", "txt", "stats", "segments", "duration")
                 if k in prog}
            )
        return result

    @staticmethod
    def _tail_error(log_path: Path, n: int = 12) -> str:
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "任务失败"
        tail = [ln for ln in lines[-n:] if ln.strip()]
        return "\n".join(tail) or "任务失败"

    @staticmethod
    def _kill_tree(proc: subprocess.Popen | None) -> None:
        """尽量连孙进程一起杀，杀不动就退回只杀子进程。

        Windows 上必须用 taskkill /T，否则 node bridge、ffmpeg 会变成孤儿继续
        占 CPU。但 taskkill 需要打开进程句柄，某些受限环境（容器、低完整性沙箱）
        会被拒绝，这时至少要保证子进程本身退出，任务状态才能落定。
        """
        if proc is None or proc.poll() is not None:
            return
        if sys.platform == "win32":
            try:
                done = subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=15,
                )
                if done.returncode == 0:
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            proc.kill()
        except OSError:
            pass

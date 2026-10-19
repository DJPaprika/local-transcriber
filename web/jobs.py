"""Web UI 的任务队列。

设计要点（和 docs/webui-plan.md 对应）：

- **串行**：一次只跑一个任务。SenseVoice 会吃满 CPU，显存也只够一个 llama-server。
- **不用管道**：子进程的 stdout/stderr 直接写日志文件，父进程按 offset 增量读；
  进度从 .progress.json 读。这样父子之间没有 pipe，Windows 上也不会踩到手柄问题。
- **能杀干净**：取消时在 Windows 上用 `taskkill /T /F` 杀整棵进程树，
  否则 node bridge、ffmpeg 会变成孤儿继续占 CPU。
- **输出命名交给 transcribe.py**：不再由 Web 层猜名字，所以 UI 和 CLI 产出的
  都是「视频标题」，落在同一个 output/ 目录里。
"""

from __future__ import annotations

import json
import shutil
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

SETTINGS_DEFAULT = {"keep_server": False}


class JobManager:
    def __init__(self, out_root: Path):
        self.out_root = out_root
        self.jobs_dir = out_root / ".jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.jobs_dir / "settings.json"

        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._order: list[str] = []
        self._queue: deque[str] = deque()
        self._current: str | None = None
        self._proc: subprocess.Popen | None = None
        self._cancelled: set[str] = set()
        self._settings = self._load_settings()

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

    def _load_settings(self) -> dict:
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return dict(SETTINGS_DEFAULT)
        return {**SETTINGS_DEFAULT, **{k: data[k] for k in SETTINGS_DEFAULT if k in data}}

    def get_settings(self) -> dict:
        with self._lock:
            return dict(self._settings)

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            for key in SETTINGS_DEFAULT:
                if key in patch:
                    self._settings[key] = bool(patch[key])
            data = dict(self._settings)
        try:
            self.settings_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
        return data

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
        self._job_dir(jid)
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

    def delete(self, jid: str) -> bool:
        """从列表里删掉一个已结束的任务（连同它的日志目录）。

        正在跑的不让删——要删先取消，避免状态和真实进程脱节。
        产物文件不动：用户可能还想留着字幕，删任务不该顺手删内容。
        """
        with self._lock:
            job = self._jobs.get(jid)
            if job is None or job["status"] not in TERMINAL:
                return False
            self._jobs.pop(jid, None)
            if jid in self._order:
                self._order.remove(jid)
            self._queue = deque(x for x in self._queue if x != jid)
        shutil.rmtree(self.jobs_dir / jid, ignore_errors=True)
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

    def _resolve_input(self, rel: str) -> Path:
        """前端给的是相对 output/ 的路径（如 clip40/clip40.txt），
        直接交给 translate.py 会按当前工作目录解析，落到项目根下就找不到了。"""
        p = Path(rel).expanduser()
        return p if p.is_absolute() else self.out_root / p

    def _build_cmd(self, job: dict) -> list[str]:
        p = job["params"]
        kind = job["kind"]

        if kind == "transcribe":
            # 不给 -n 时由 transcribe.py 自己推导（URL 用视频标题，本地文件用文件名），
            # 这样 Web UI 和 CLI 的产物命名一致。
            cmd = [
                PY, str(ROOT / "transcribe.py"), p["source"],
                "-o", str(self.out_root),
                "-e", p.get("engine", "sensevoice"),
                "-l", p.get("language", "auto"),
                "--progress", str(self.jobs_dir / job["id"] / "progress.json"),
            ]
            if (p.get("name") or "").strip():
                cmd += ["-n", p["name"].strip()]
            if p.get("keep_audio"):
                cmd.append("--keep-audio")
            return cmd

        cmd = [PY, str(ROOT / "translate.py"), str(self._resolve_input(p["input"]))]
        if kind == "translate":
            cmd += ["--task", "translate", "--target", p.get("target", "简体中文")]
        elif kind == "summary":
            cmd += ["--task", "summary"]
        else:  # ask
            cmd += ["--task", "ask", "-q", p["question"]]

        # 常驻开关：批量跑摘要/翻译时不要每个任务都重新加载模型。
        if self.get_settings().get("keep_server"):
            cmd.append("--keep-server")
        return cmd

    def _execute(self, job: dict) -> None:
        jid = job["id"]
        jdir = self._job_dir(jid)
        log_path = jdir / "job.log"
        cmd = self._build_cmd(job)

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

    # ------------------------------------------------------------ 产物

    def _rel_dir(self, d: Path) -> str:
        """把产物目录转成相对 output/ 的 POSIX 路径（空串表示 output 根）。"""
        try:
            rel = d.resolve().relative_to(self.out_root.resolve())
        except (ValueError, OSError):
            return d.as_posix()
        return "" if str(rel) == "." else rel.as_posix()

    @staticmethod
    def _file_entry(f: Path) -> dict:
        return {"name": f.name, "size": f.stat().st_size}

    def _transcribe_outputs(self, job: dict) -> tuple[list[dict], Path]:
        """转写产物按 progress.json 里记录的真实路径找，名字可能是视频标题。"""
        prog = self.read_progress(job["id"]) or {}
        srt = prog.get("srt")
        if srt:
            srt_path = Path(srt)
            d, stem = srt_path.parent, srt_path.stem
        else:
            # 理论上不会走到；兜底用提交时的名字。
            name = (job["params"].get("name") or "").strip() or job["id"]
            d, stem = self.out_root, name

        files = []
        for suffix in (".srt", ".txt", ".stats.json"):
            f = d / f"{stem}{suffix}"
            if f.is_file():
                files.append(self._file_entry(f))
        return files, d

    def _llm_outputs(self, job: dict) -> tuple[list[dict], Path]:
        """摘要 / 翻译沿用 translate.py 的默认命名：和源文件同目录同前缀。"""
        src = self._resolve_input(job["params"]["input"])
        suffix = ".summary.md" if job["kind"] == "summary" else ".zh.srt"
        out = src.with_suffix(suffix)
        files = [self._file_entry(out)] if out.is_file() else []
        return files, out.parent

    def _collect_result(self, job: dict) -> dict:
        if job["kind"] == "transcribe":
            files, d = self._transcribe_outputs(job)
        elif job["kind"] in ("summary", "translate"):
            files, d = self._llm_outputs(job)
        else:
            files, d = [], self.out_root

        result: dict = {"files": files, "dir": self._rel_dir(d)}
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

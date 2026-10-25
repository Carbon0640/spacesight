"""后台任务（扫描 / 体检）注册表：带进度、可取消、线程安全。"""
from __future__ import annotations

import threading
import time
import uuid


class Job:
    def __init__(self, kind: str, **extra):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.status = "running"          # running | done | error | cancelled
        self.started = time.time()
        self.finished: float | None = None
        self.files = 0
        self.dirs = 0
        self.bytes = 0
        self.current = ""
        self.message = ""
        self.error = ""
        self.warning = ""
        self.result: dict = {}
        self.extra = extra
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 状态更新
    def progress(self, files: int | None = None, nbytes: int | None = None,
                 dirs: int | None = None, current: str | None = None) -> None:
        with self._lock:
            if files is not None:
                self.files = files
            if nbytes is not None:
                self.bytes = nbytes
            if dirs is not None:
                self.dirs = dirs
            if current is not None:
                self.current = current

    def note(self, message: str) -> None:
        with self._lock:
            self.message = message

    def warn(self, message: str) -> None:
        with self._lock:
            self.warning = message

    def finish(self, status: str = "done", result: dict | None = None) -> None:
        with self._lock:
            self.status = status
            self.finished = time.time()
            if result is not None:
                self.result = result
            self.current = ""

    def fail(self, err: str) -> None:
        with self._lock:
            self.error = err
        self.finish("error")

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = (self.finished or time.time()) - self.started
            return {
                "id": self.id, "kind": self.kind, "status": self.status,
                "started": self.started, "elapsed": round(elapsed, 2),
                "files": self.files, "dirs": self.dirs, "bytes": self.bytes,
                "current": self.current, "message": self.message,
                "error": self.error, "warning": self.warning,
                "result": self.result, "extra": self.extra,
                "done": self.status != "running",
            }


class JobRegistry:
    def __init__(self, keep: int = 40):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._keep = keep
        self._lock = threading.Lock()

    def new(self, kind: str, **extra) -> Job:
        job = Job(kind, **extra)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self._keep:
                old = self._order.pop(0)
                self._jobs.pop(old, None)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, only_running: bool = False) -> list[dict]:
        with self._lock:
            jobs = [self._jobs[i] for i in self._order if i in self._jobs]
        out = [j.snapshot() for j in jobs]
        if only_running:
            out = [j for j in out if j["status"] == "running"]
        return out[::-1]

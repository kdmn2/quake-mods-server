"""Background jobs with progress, persisted in SQLite.

Why: converting or publishing a large package can take minutes, longer than a reverse proxy keeps an idle request open. The web
request now only records a job and returns; a single worker thread does the work while the browser polls a tiny status endpoint
(each poll is a short request, so no proxy timeout applies).

* One worker thread: conversions are CPU/disk heavy and two at once would just compete.
* Progress is written to the database (throttled), so any request thread can read it and a restart can mark what was interrupted.
* Cancellation is cooperative: Progress raises JobCancelled the next time the job reports progress.
* "inline" mode runs a job in the calling thread (used by tests and by anyone who prefers synchronous behaviour).
"""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

PROGRESS_WRITE_INTERVAL = 0.4
KEEP_FINISHED_JOBS = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL,                       -- queued | running | done | failed | cancelled
    stage TEXT NOT NULL DEFAULT '',
    done INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    percent REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT 'info',        -- success | warning | error | info
    next_endpoint TEXT,
    next_values TEXT,
    next_auto INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    updated_at REAL NOT NULL,
    finished_at REAL
)
"""


class JobCancelled(Exception):
    """Raised inside a job when the administrator cancelled it."""


@dataclass
class JobOutcome:
    message: str
    level: str = "success"
    next_endpoint: str | None = None
    next_values: dict[str, Any] = field(default_factory=dict)
    auto_redirect: bool = False


class JobFailed(Exception):
    """A failure with a user-facing message; may carry the page to send the administrator to next."""

    def __init__(self, message: str, next_endpoint: str | None = None, next_values: dict[str, Any] | None = None):
        super().__init__(message)
        self.next_endpoint = next_endpoint
        self.next_values = next_values or {}


class Progress:
    """Callable progress sink: progress(stage, done, total). Doubles as the cancellation point."""

    def __init__(self, manager: "JobManager", job_id: str):
        self._m = manager
        self.job_id = job_id
        self._plan: list[tuple[str, float]] = []
        self._last_write = 0.0
        self._last_stage = ""
        self._percent = 0.0

    def plan(self, stages: list[tuple[str, float]]) -> None:
        """Declare the stages this job will report, with relative weights, so percent is meaningful across them."""
        self._plan = list(stages)

    def __call__(self, stage: str, done: int, total: int) -> None:
        if self._m._cancel_flags.get(self.job_id) and self._m._cancel_flags[self.job_id].is_set():
            raise JobCancelled()
        now = time.monotonic()
        finished_stage = total > 0 and done >= total
        if stage == self._last_stage and not finished_stage and now - self._last_write < PROGRESS_WRITE_INTERVAL:
            return
        self._last_stage, self._last_write = stage, now
        percent = self._percent_for(stage, done, total)
        self._percent = max(self._percent, percent)
        self._m._update(self.job_id, stage=stage, done=int(done), total=int(total), percent=round(self._percent, 1))

    def _percent_for(self, stage: str, done: int, total: int) -> float:
        names = [n for n, _ in self._plan]
        if stage not in names:
            return self._percent
        weights = dict(self._plan)
        whole = sum(weights.values()) or 1.0
        before = sum(w for n, w in self._plan[:names.index(stage)])
        frac = min(1.0, done / total) if total > 0 else 0.0
        return 100.0 * (before + weights[stage] * frac) / whole


class JobManager:
    def __init__(self, connect: Callable[[], sqlite3.Connection], inline: Callable[[], bool], log: Callable[[str], None] = print):
        self._connect = connect
        self._inline = inline
        self._log = log
        self._queue: "queue.Queue[tuple[str, Callable[[Progress], JobOutcome]]]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cancel_flags: dict[str, threading.Event] = {}

    # ---- schema / recovery ---------------------------------------------------------------------
    def init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(SCHEMA)

    def recover_interrupted(self) -> int:
        """At startup nothing can still be running: mark leftovers as failed instead of leaving them 'running' forever."""
        now = time.time()
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE jobs SET state='failed', level='error', message='Interrupted by a restart of the application. Nothing was published or changed.', "
                "finished_at=?, updated_at=? WHERE state IN ('queued','running')", (now, now))
            return cur.rowcount or 0
        finally:
            conn.close()

    # ---- submitting ----------------------------------------------------------------------------
    def submit(self, kind: str, title: str, fn: Callable[[Progress], JobOutcome]) -> str:
        job_id = uuid.uuid4().hex
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("INSERT INTO jobs (id, kind, title, state, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                         (job_id, kind, title, "queued", now, now))
            conn.execute("DELETE FROM jobs WHERE state IN ('done','failed','cancelled') AND id NOT IN "
                         "(SELECT id FROM jobs WHERE state IN ('done','failed','cancelled') ORDER BY finished_at DESC LIMIT ?)", (KEEP_FINISHED_JOBS,))
        finally:
            conn.close()
        self._cancel_flags[job_id] = threading.Event()
        if self._inline():
            self._run(job_id, fn)
        else:
            self._ensure_worker()
            self._queue.put((job_id, fn))
        return job_id

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="job-worker", daemon=True)
                self._worker.start()

    def _loop(self) -> None:
        while True:
            job_id, fn = self._queue.get()
            try:
                self._run(job_id, fn)
            finally:
                self._queue.task_done()

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Test helper: wait until the queue is drained."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    # ---- running -------------------------------------------------------------------------------
    def _run(self, job_id: str, fn: Callable[[Progress], JobOutcome]) -> None:
        flag = self._cancel_flags.setdefault(job_id, threading.Event())
        if flag.is_set() or self._cancel_requested_in_db(job_id):
            self._finish(job_id, "cancelled", "Cancelled before it started.", "info")
            return
        self._update(job_id, state="running", started_at=time.time())
        progress = Progress(self, job_id)
        try:
            outcome = fn(progress)
        except JobCancelled:
            self._finish(job_id, "cancelled", "Cancelled. Nothing was changed.", "info")
        except JobFailed as exc:
            self._finish(job_id, "failed", str(exc), "error", exc.next_endpoint, exc.next_values)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._finish(job_id, "failed", str(exc) or exc.__class__.__name__, "error")
        except Exception as exc:                               # defensive: a job must never leave the UI spinning forever
            self._log(f"[job {job_id[:8]}] unexpected error:\n{traceback.format_exc()}")
            self._finish(job_id, "failed", f"Unexpected error: {exc.__class__.__name__}: {exc}", "error")
        else:
            self._finish(job_id, "done", outcome.message, outcome.level, outcome.next_endpoint, outcome.next_values, outcome.auto_redirect)
        finally:
            self._cancel_flags.pop(job_id, None)

    def _cancel_requested_in_db(self, job_id: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
            return bool(row and row[0])
        finally:
            conn.close()

    def _update(self, job_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        conn = self._connect()
        try:
            conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
        except sqlite3.OperationalError as exc:               # progress is advisory; a locked database must not kill the job
            self._log(f"[job {job_id[:8]}] progress not recorded: {exc}")
        finally:
            conn.close()

    def _finish(self, job_id: str, state: str, message: str, level: str, next_endpoint: str | None = None,
                next_values: dict[str, Any] | None = None, auto: bool = False) -> None:
        now = time.time()
        fields: dict[str, Any] = dict(state=state, message=message, level=level, finished_at=now,
                                      next_endpoint=next_endpoint, next_values=json.dumps(next_values or {}), next_auto=int(auto))
        if state == "done":
            fields["percent"] = 100.0
        self._update(job_id, **fields)

    # ---- reading / cancelling ------------------------------------------------------------------
    def get(self, job_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        job = dict(row)
        job["next_values"] = json.loads(job["next_values"] or "{}")
        return job

    def active(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM jobs WHERE state IN ('queued','running') ORDER BY created_at").fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def request_cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job["state"] not in ("queued", "running"):
            return False
        flag = self._cancel_flags.get(job_id)
        if flag:
            flag.set()
        self._update(job_id, cancel_requested=1)
        return True

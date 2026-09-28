"""The portal's jobs: each action that changes something, kept for the
Activity page.

A job is either an `sbx` command in a child process, or a short Python
function. The child is the same `bin/sbx` that a terminal runs, so the portal
cannot drift from the CLI: the checks, the order of the steps and the error
texts are the CLI's own. Its stdin is closed, or carries one secret for a
`--stdin` option, so a command that wants a terminal stops with its own error
and does not hang.

A secret never goes into a job's record: the command line of a job has none
(every secret goes over stdin), and a function job logs only what it writes.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import REPO_ROOT

SBX = REPO_ROOT / "bin" / "sbx"
KEEP = 300                 # the number of finished jobs kept on disk
MAX_LINES = 20000          # a longer log keeps its last lines
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


@dataclass
class Job:
    id: str
    title: str
    command: str              # what the Activity page shows; never a secret
    target: str = ""          # "sandbox:sbx-lab", "template:rails", ...: one running job per target
    started: float = 0.0
    ended: float | None = None
    status: str = "running"   # running | ok | failed | cancelled | interrupted
    code: int | None = None
    lines: list[str] = field(default_factory=list)
    dropped: int = 0          # lines dropped from the start of a long log

    def summary(self) -> dict:
        out = asdict(self)
        del out["lines"]
        out["line_count"] = self.dropped + len(self.lines)
        return out

    def detail(self, since: int = 0) -> dict:
        """The summary and the lines from number `since` on (numbers count
        the dropped lines too, so a poller's offset stays valid)."""
        out = self.summary()
        start = max(0, since - self.dropped)
        out["since"] = self.dropped + start
        out["lines"] = self.lines[start:]
        return out


class Busy(RuntimeError):
    pass


class Jobs:
    def __init__(self, directory: Path, sbx_argv: list[str] | None = None):
        self.dir = directory
        self.sbx_argv = sbx_argv or [sys.executable, str(SBX)]
        self._jobs: dict[str, Job] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._load()

    # --- records ---
    def _load(self) -> None:
        if not self.dir.is_dir():
            return
        for path in sorted(self.dir.glob("*.json"))[-KEEP:]:
            try:
                job = Job(**json.loads(path.read_text()))
            except (OSError, ValueError, TypeError):
                continue
            if job.status == "running":
                # The portal stopped while the job ran; its end is unknown.
                job.status, job.ended = "interrupted", job.ended or job.started
                self._save(job)
            self._jobs[job.id] = job

    def _save(self, job: Job) -> None:
        with self._save_lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, 0o700)
            path = self.dir / f"{job.id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(job)))
            os.replace(tmp, path)
            for old in sorted(self.dir.glob("*.json"))[:-KEEP]:
                old.unlink(missing_ok=True)
                self._jobs.pop(old.stem, None)

    def _new(self, title: str, command: str, target: str) -> Job:
        with self._lock:
            if target and any(j.target == target and j.status == "running" for j in self._jobs.values()):
                raise Busy(f"another job is running on {target.split(':', 1)[-1]}; wait for it to end")
            # Sortable by time: the Activity page and the prune both rely on it.
            job = Job(f"{time.time_ns() // 1_000_000:012x}-{secrets.token_hex(3)}", title, command, target,
                      started=time.time())
            self._jobs[job.id] = job
        self._save(job)
        return job

    def _append(self, job: Job, text: str) -> None:
        for line in _ANSI.sub("", text).splitlines() or [""]:
            job.lines.append(line.rstrip())
        if len(job.lines) > MAX_LINES:
            cut = len(job.lines) - MAX_LINES
            del job.lines[:cut]
            job.dropped += cut

    def _finish(self, job: Job, code: int, status: str | None = None) -> None:
        job.code = code
        job.ended = time.time()
        if job.status == "running":
            job.status = status or ("ok" if code == 0 else "failed")
        self._save(job)

    def record(self, title: str, command: str, target: str = "", lines: list[str] | None = None,
               ok: bool = True) -> Job:
        """A finished entry for a direct action (a saved setting, a written
        file), so the Activity page shows every change in one place."""
        job = self._new(title, command, "")
        job.target = target
        for line in lines or []:
            self._append(job, line)
        self._finish(job, 0 if ok else 1)
        return job

    # --- runs ---
    def run_sbx(self, args: list[str], title: str, target: str = "", stdin: bytes | None = None) -> Job:
        import shlex
        job = self._new(title, "sbx " + shlex.join(args), target)
        env = dict(os.environ, PYTHONUNBUFFERED="1", NO_COLOR="1")
        try:
            proc = subprocess.Popen(self.sbx_argv + args, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                    # Its own process group: a cancel stops the ssh and
                                    # the scp that it started too.
                                    start_new_session=True)
        except OSError as exc:
            self._append(job, f"sbx: cannot start: {exc}")
            self._finish(job, 127)
            return job
        self._procs[job.id] = proc

        def pump():
            if stdin is not None:
                try:
                    proc.stdin.write(stdin)
                    proc.stdin.close()
                except OSError:
                    pass
            buf = b""
            while chunk := proc.stdout.read1(4096):
                buf += chunk
                # A progress bar rewrites its line with \r; keep each state.
                *whole, buf = re.split(rb"\r\n|\n|\r", buf)
                for line in whole:
                    self._append(job, line.decode(errors="replace"))
            if buf:
                self._append(job, buf.decode(errors="replace"))
            proc.stdout.close()
            code = proc.wait()
            self._procs.pop(job.id, None)
            self._finish(job, code)

        threading.Thread(target=pump, name=f"job-{job.id}", daemon=True).start()
        return job

    def run_fn(self, fn, title: str, command: str, target: str = "") -> Job:
        """`fn(log)` does the work; `log(text)` adds lines. An exception is
        the failure, and its text is the last line."""
        job = self._new(title, command, target)

        def work():
            try:
                fn(lambda text: self._append(job, str(text)))
            except Exception as exc:  # noqa: BLE001 - every failure ends the job the same way
                self._append(job, f"error: {exc}")
                self._finish(job, 1)
                return
            self._finish(job, 0)

        threading.Thread(target=work, name=f"job-{job.id}", daemon=True).start()
        return job

    def cancel(self, job_id: str) -> bool:
        proc = self._procs.get(job_id)
        job = self._jobs.get(job_id)
        if proc is None or job is None:
            return False
        job.status = "cancelled"
        self._append(job, "== cancelled from the portal")
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        return True

    def stop_all(self) -> list[str]:
        """At the portal's end: stop each child, and return their titles."""
        titles = []
        for job_id in list(self._procs):
            if self.cancel(job_id):
                titles.append(self._jobs[job_id].title)
        return titles

    # --- queries ---
    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.id, reverse=True)

    def running(self) -> list[Job]:
        return [j for j in self.all() if j.status == "running"]

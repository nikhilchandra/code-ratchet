"""Lets one graph at a time run on a project, with a lock file in .ratchet/state.

The refactor and the audit are graphs, and a second run of either is refused while the lock is
held. The lock is the file graph.lock, whose name does not end in .json, so neither graph's list
of jobs picks it up. It holds JSON with these fields:
    graph: "refactor" or "audit".
    job: the job's slug.
    job_file: the job's .json file, relative to .ratchet/state, written with forward slashes.
    pid: the process id of the run that took the lock.
    state: "running", or "waiting" when an audit waits at a gate with no process.
    gate: "" while running, otherwise "questions" or "draft".

A lock that says running whose process is gone belongs to a run that stopped without a word, such
as after a power cut. take_lock() rolls that job back and starts from a clean slate, and resumes
nothing. A lock that says waiting is not crashed: it keeps the lock with no process by design.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from ratchet.git import Repo
from ratchet.project import Project

REFACTOR = "refactor"
AUDIT = "audit"

# The states of a lock, and the gates an audit waits at.
RUNNING = "running"
WAITING = "waiting"
QUESTIONS_GATE = "questions"
DRAFT_GATE = "draft"

# What a gate waits for, as it reads in the refusal.
GATE_WAITS = {
    QUESTIONS_GATE: "your answers",
    DRAFT_GATE: "your decision on its draft",
}

# The Windows access right to ask whether a process has exited, the exit code of a process that
# has not, and the error that says a process exists but may not be opened.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5


class LockError(RuntimeError):
    """Signals that a graph may not run now, because another holds the lock or the lock cannot be
    read."""


@dataclass(frozen=True)
class Lock:
    """Holds the fields of the lock file."""

    graph: str
    job: str
    job_file: str
    pid: int
    state: str
    gate: str


def lock_path(project: Project) -> Path:
    """Returns the file that holds the graph lock of a project."""
    return project.state_dir / "graph.lock"


def read_lock(project: Project) -> Lock | None:
    """Reads the graph lock of a project.

    Args:
        project: The project.

    Returns:
        The lock, or None when no graph holds it.

    Raises:
        LockError: The lock file is empty or damaged. The message names the file.
    """
    path = lock_path(project)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return Lock(**json.loads(text))
    except (ValueError, TypeError) as error:
        raise LockError(f"{path} cannot be read ({error}). Delete it if no run is using it.") from error


def process_is_alive(pid: int) -> bool:
    """Answers whether a process is still running.

    Args:
        pid: The process id. 0 or less never names a process.

    Returns:
        True when the process runs. A process that the user may not open counts as running, and a
        process that has exited counts as gone even while some handle to it is open.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_process_is_alive(pid: int) -> bool:
    """Answers process_is_alive() on Windows, where a process that has exited can still be opened
    while another process holds a handle to it, so its exit code decides."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def free_job_slug(folder: Path, base: str) -> str:
    """Returns the name that rooster gives a job started with the name 'base'.

    Args:
        folder: The folder that holds the graph's job files.
        base: The name asked for. A counter is added when a job of that name exists.
    """
    if not (folder / f"{base}.json").exists():
        return base
    n = 2
    while (folder / f"{base}-{n}.json").exists():
        n += 1
    return f"{base}-{n}"


def halt_job_file(path: Path) -> None:
    """Marks a job that is running or waiting as halted, in its saved file.

    Args:
        path: The job's .json file. A job that is done or halted, or has no file, is left alone.
    """
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["status"] not in (RUNNING, WAITING):
        return
    data["status"] = "halted"
    data["next_step"] = None
    data["halt_reason"] = "the run stopped before the job finished"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def take_lock(
    project: Project,
    *,
    graph: str,
    job: str,
    job_file: Path,
    resuming: bool = False,
    say: Callable[[str], None] = print,
) -> None:
    """Takes the graph lock for a job, rolling back a job that stopped without finishing.

    Args:
        project: The project.
        graph: The graph that takes the lock, "refactor" or "audit".
        job: The job's slug.
        job_file: The job's .json file, under the project's state folder.
        resuming: True when an audit takes its own lock back to resume at a gate.
        say: The function that shows the line saying that an earlier job was rolled back.

    Raises:
        LockError: A run holds the lock, or an audit waits at a gate and is not the one being
            resumed, or the lock cannot be read.
        GitError: Undoing the changes of an earlier refactor failed.
    """
    mine = Lock(graph, job, job_file.relative_to(project.state_dir).as_posix(), os.getpid(),
                RUNNING, "")
    while True:
        held = read_lock(project)
        if held is None:
            if _create_lock(project, mine):
                return
            continue
        held_status = _job_status(project.state_dir / held.job_file)
        if held.state == WAITING and held_status == WAITING:
            if resuming and (held.graph, held.job) == (graph, job):
                _write_lock(project, mine)
                return
            raise LockError(f"Audit {held.job} is waiting for {GATE_WAITS[held.gate]}. "
                            "Finish or discard it first.")
        if held.state != WAITING or held_status == RUNNING:
            if process_is_alive(held.pid):
                article = "A refactor" if held.graph == REFACTOR else "An audit"
                raise LockError(f"{article} ({held.job}) is running. Wait for it to finish.")
            _roll_back(project, held, say)
        lock_path(project).unlink(missing_ok=True)


def _roll_back(project: Project, held: Lock, say: Callable[[str], None]) -> None:
    """Undoes a job that stopped without finishing, and says so.

    A refactor's uncommitted changes are undone. An audit makes none, so the owner's own changes
    to the tree are kept.

    Args:
        project: The project.
        held: The lock that the stopped job left.
        say: The function that shows the line.
    """
    if held.graph == REFACTOR:
        repo = Repo(project.root)
        if repo.changed_paths():
            repo.undo()
        outcome = "Its changes were undone."
    else:
        outcome = "It was halted."
    halt_job_file(project.state_dir / held.job_file)
    say(f"The last {held.graph} ({held.job}) stopped without finishing. {outcome}")


def _job_status(path: Path) -> str | None:
    """Returns the status saved in a job's file, or None when the file is not there."""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["status"]


def _create_lock(project: Project, lock: Lock) -> bool:
    """Makes the lock file, which fails when another run made it first.

    Returns:
        True when the lock was made, and False when a lock file already exists.
    """
    path = lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(asdict(lock), file)
    return True


def _write_lock(project: Project, lock: Lock) -> None:
    """Replaces the lock file in one step, so a reader never sees half of it."""
    path = lock_path(project)
    scratch = path.with_suffix(".lock.tmp")
    scratch.write_text(json.dumps(asdict(lock)), encoding="utf-8")
    os.replace(scratch, path)


def set_lock_job(project: Project, job: str, job_file: Path) -> None:
    """Points the lock at the next job of the run that holds it.

    Args:
        project: The project.
        job: The job's slug.
        job_file: The job's .json file, under the project's state folder.
    """
    held = read_lock(project)
    _write_lock(project, Lock(held.graph, job, job_file.relative_to(project.state_dir).as_posix(),
                              held.pid, held.state, held.gate))


def hold_at_gate(project: Project, gate: str) -> None:
    """Marks the lock as waiting at a gate, with no process running.

    Args:
        project: The project.
        gate: QUESTIONS_GATE or DRAFT_GATE.
    """
    held = read_lock(project)
    _write_lock(project, Lock(held.graph, held.job, held.job_file, held.pid, WAITING, gate))


def release_lock(project: Project) -> None:
    """Removes the graph lock, which is not an error when it is already gone."""
    with contextlib.suppress(FileNotFoundError):
        lock_path(project).unlink()

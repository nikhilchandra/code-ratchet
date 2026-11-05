"""Checks the lock that lets one graph at a time run on a project: the refusals while another
graph runs or an audit waits at a gate, the roll back of a job whose process is gone, and the
helpers that halt a job file, name a job and tell whether a process is alive."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from ratchet.git import Repo
from ratchet.graph_lock import (
    LockError,
    free_job_slug,
    halt_job_file,
    process_is_alive,
    read_lock,
    release_lock,
    take_lock,
)
from ratchet.project import Project
from lock_files import FAKE_PID, LIST, lock_path, read_job_file, write_job_file, write_lock

# The lines that refuse a second graph, and the lines that a roll back shows.
REFACTOR_RUNS = "A refactor (job-012) is running. Wait for it to finish."
AUDIT_RUNS = "An audit (audit-x) is running. Wait for it to finish."
ANSWERS_WAIT = "Audit audit-x is waiting for your answers. Finish or discard it first."
DRAFT_WAITS = "Audit audit-x is waiting for your decision on its draft. Finish or discard it first."
REFACTOR_UNDONE = "The last refactor (job-012) stopped without finishing. Its changes were undone."
AUDIT_HALTED = "The last audit (audit-x) stopped without finishing. It was halted."


def refactor_job(project: Project, slug: str):
    """Returns the job file of the refactor job 'slug' on the scratch project's problem list."""
    return project.state_dir / LIST / "refactor" / f"{slug}.json"


def audit_job(project: Project, slug: str):
    """Returns the job file of the audit job 'slug'."""
    return project.state_dir / "audits" / f"{slug}.json"


def write_audit_lock(project: Project, status: str, **fields: object) -> None:
    """Writes the job file of audit-x with the status 'status', and a lock that names it. The
    lock waits at the questions gate unless 'fields' change it."""
    next_step = None if status in ("done", "halted") else "check"
    write_job_file(audit_job(project, "audit-x"), status, next_step)
    write_lock(project, **({"graph": "audit", "job": "audit-x", "job_file": "audits/audit-x.json",
                            "state": "waiting", "gate": "questions"} | fields))


def test_a_second_graph_is_refused_while_a_refactor_runs(scratch) -> None:
    """Proves a running refactor whose process lives refuses an audit, naming the refactor's
    job."""
    project, _ = scratch
    write_job_file(refactor_job(project, "job-012"), "running", "implement")
    write_lock(project)

    with pytest.raises(LockError) as refused:
        take_lock(project, graph="audit", job="audit-y", job_file=audit_job(project, "audit-y"),
                  say=lambda line: None)

    assert str(refused.value) == REFACTOR_RUNS
    assert read_lock(project).job == "job-012"


def test_a_running_audit_refuses_a_refactor(scratch) -> None:
    """Proves a running audit whose process lives refuses a refactor, naming the audit's job."""
    project, _ = scratch
    write_audit_lock(project, "running", state="running", gate="")

    with pytest.raises(LockError) as refused:
        take_lock(project, graph="refactor", job="job-001",
                  job_file=refactor_job(project, "job-001"), say=lambda line: None)

    assert str(refused.value) == AUDIT_RUNS


def test_an_audit_waiting_for_answers_is_refused_and_not_treated_as_crashed(scratch) -> None:
    """Proves an audit waiting at the questions gate, whose process is gone by design, refuses
    another graph and is left exactly as it was."""
    project, _ = scratch
    write_audit_lock(project, "waiting", pid=FAKE_PID)
    lock_before = lock_path(project).read_bytes()
    job_before = audit_job(project, "audit-x").read_bytes()
    lines: list[str] = []

    with pytest.raises(LockError) as refused:
        take_lock(project, graph="refactor", job="job-001",
                  job_file=refactor_job(project, "job-001"), say=lines.append)

    assert str(refused.value) == ANSWERS_WAIT
    assert lines == []
    assert lock_path(project).read_bytes() == lock_before
    assert audit_job(project, "audit-x").read_bytes() == job_before


def test_an_audit_waiting_at_its_draft_names_the_draft(scratch) -> None:
    """Proves an audit waiting at the draft gate is refused with the draft's wording."""
    project, _ = scratch
    write_audit_lock(project, "waiting", pid=FAKE_PID, gate="draft")

    with pytest.raises(LockError) as refused:
        take_lock(project, graph="audit", job="audit-y", job_file=audit_job(project, "audit-y"),
                  say=lambda line: None)

    assert str(refused.value) == DRAFT_WAITS


def test_a_stale_refactor_lock_is_rolled_back(scratch) -> None:
    """Proves a running refactor whose process is gone has its changes undone and its job halted,
    says so, and hands the lock to the new job."""
    project, repo = scratch
    app = project.root / "src" / "app.py"
    app.write_text(app.read_text(encoding="utf-8") + "\nX = 1\n", encoding="utf-8")
    write_job_file(refactor_job(project, "job-012"), "running", "review")
    write_lock(project, pid=FAKE_PID)
    lines: list[str] = []

    take_lock(project, graph="refactor", job="job-013", job_file=refactor_job(project, "job-013"),
              say=lines.append)

    assert lines == [REFACTOR_UNDONE]
    assert repo.changed_paths() == []
    old = read_job_file(refactor_job(project, "job-012"))
    assert old["status"] == "halted" and old["next_step"] is None
    lock = read_lock(project)
    assert (lock.graph, lock.job, lock.pid, lock.state) == ("refactor", "job-013", os.getpid(),
                                                            "running")
    assert lock.job_file == f"{LIST}/refactor/job-013.json"


def test_a_stale_audit_lock_is_rolled_back(scratch) -> None:
    """Proves a running audit whose process is gone is halted and the lock goes to the new job,
    while the owner's own changes to the tree are kept."""
    project, repo = scratch
    app = project.root / "src" / "app.py"
    app.write_text(app.read_text(encoding="utf-8") + "\nX = 1\n", encoding="utf-8")
    write_audit_lock(project, "running", pid=FAKE_PID, state="running", gate="")
    lines: list[str] = []

    take_lock(project, graph="audit", job="audit-y", job_file=audit_job(project, "audit-y"),
              say=lines.append)

    assert lines == [AUDIT_HALTED]
    assert read_job_file(audit_job(project, "audit-x"))["status"] == "halted"
    assert repo.changed_paths() == ["src/app.py"]
    assert read_lock(project).job == "audit-y"


def test_resuming_rewrites_a_waiting_lock_as_running(scratch) -> None:
    """Proves the waiting audit itself may take its lock back, which then says running."""
    project, _ = scratch
    write_audit_lock(project, "waiting", pid=FAKE_PID)
    lines: list[str] = []

    take_lock(project, graph="audit", job="audit-x", job_file=audit_job(project, "audit-x"),
              resuming=True, say=lines.append)

    lock = read_lock(project)
    assert (lock.job, lock.state, lock.gate, lock.pid) == ("audit-x", "running", "", os.getpid())
    assert lines == []


def test_resuming_a_different_job_is_refused(scratch) -> None:
    """Proves resuming is allowed only for the job that the waiting lock names."""
    project, _ = scratch
    write_audit_lock(project, "waiting", pid=FAKE_PID)
    lock_before = lock_path(project).read_bytes()

    with pytest.raises(LockError) as refused:
        take_lock(project, graph="audit", job="audit-y", job_file=audit_job(project, "audit-y"),
                  resuming=True, say=lambda line: None)

    assert str(refused.value) == ANSWERS_WAIT
    assert lock_path(project).read_bytes() == lock_before


def test_a_waiting_lock_whose_job_reads_running_is_refused_while_its_process_lives(
        scratch) -> None:
    """Proves a lock that says waiting, for a job that is running in a live process, refuses
    another graph as a running one."""
    project, _ = scratch
    write_audit_lock(project, "running", gate="draft")

    with pytest.raises(LockError) as refused:
        take_lock(project, graph="refactor", job="job-001",
                  job_file=refactor_job(project, "job-001"), say=lambda line: None)

    assert str(refused.value) == AUDIT_RUNS


def test_a_waiting_lock_whose_job_reads_running_is_rolled_back_when_its_process_is_gone(
        scratch) -> None:
    """Proves an audit killed while it was settled, which left the lock waiting and the job
    running, does not lock the project for good."""
    project, _ = scratch
    write_audit_lock(project, "running", pid=FAKE_PID, gate="draft")
    lines: list[str] = []

    take_lock(project, graph="refactor", job="job-001", job_file=refactor_job(project, "job-001"),
              say=lines.append)

    assert lines == [AUDIT_HALTED]
    assert read_job_file(audit_job(project, "audit-x"))["status"] == "halted"
    lock = read_lock(project)
    assert (lock.graph, lock.job, lock.state) == ("refactor", "job-001", "running")


@pytest.mark.parametrize("status", ["done", "halted", None])
def test_a_left_over_waiting_lock_whose_job_finished_is_removed(scratch, status) -> None:
    """Proves a waiting lock whose job is done, halted or missing is dropped without a word."""
    project, _ = scratch
    write_audit_lock(project, status or "done", pid=FAKE_PID)
    if status is None:
        audit_job(project, "audit-x").unlink()
    lines: list[str] = []

    take_lock(project, graph="refactor", job="job-001", job_file=refactor_job(project, "job-001"),
              say=lines.append)

    assert lines == []
    assert read_lock(project).job == "job-001"


def test_two_runs_started_together_cannot_both_take_the_lock(
        scratch, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves a run that found no lock, while another run made one in the meantime, reads the
    lock again and is refused rather than writing over it."""
    project, _ = scratch
    write_job_file(refactor_job(project, "job-012"), "running", "implement")
    write_lock(project)
    lock_before = lock_path(project).read_bytes()
    real_read = read_lock
    calls: list[int] = []

    def late_read(*args, **kwargs):
        # the first read finds no lock, as if the other run had not made it yet
        calls.append(1)
        if len(calls) == 1:
            return None
        return real_read(*args, **kwargs)

    monkeypatch.setattr("ratchet.graph_lock.read_lock", late_read)

    with pytest.raises(LockError) as refused:
        take_lock(project, graph="audit", job="audit-y", job_file=audit_job(project, "audit-y"),
                  say=lambda line: None)

    assert str(refused.value) == REFACTOR_RUNS
    assert lock_path(project).read_bytes() == lock_before


def test_an_unreadable_lock_is_refused_and_named(scratch) -> None:
    """Proves an empty lock file is refused with a message that names the file."""
    project, _ = scratch
    lock_path(project).parent.mkdir(parents=True, exist_ok=True)
    lock_path(project).write_text("", encoding="utf-8")

    with pytest.raises(LockError, match="graph.lock"):
        take_lock(project, graph="refactor", job="job-001",
                  job_file=refactor_job(project, "job-001"), say=lambda line: None)


def test_release_lock_with_no_lock_does_nothing(scratch) -> None:
    """Proves releasing a lock that is not there raises nothing."""
    project, _ = scratch

    release_lock(project)

    assert not lock_path(project).exists()


def test_release_lock_removes_the_lock(scratch) -> None:
    """Proves a lock that was taken is gone once released."""
    project, _ = scratch
    take_lock(project, graph="refactor", job="job-001", job_file=refactor_job(project, "job-001"),
              say=lambda line: None)

    release_lock(project)

    assert read_lock(project) is None
    assert not lock_path(project).exists()


def test_halt_job_file_marks_running_and_waiting_jobs_halted_and_leaves_ended_ones(
        tmp_path) -> None:
    """Proves halt_job_file halts a running or waiting job, and leaves a done or halted job's
    file as it was."""
    files = {status: write_job_file(tmp_path / f"{status}.json", status,
                                    None if status in ("done", "halted") else "review")
             for status in ("running", "waiting", "done", "halted")}
    ended = {status: files[status].read_bytes() for status in ("done", "halted")}

    for path in files.values():
        halt_job_file(path)

    for status in ("running", "waiting"):
        job = read_job_file(files[status])
        assert job["status"] == "halted" and job["next_step"] is None
    for status, before in ended.items():
        assert files[status].read_bytes() == before


def test_free_job_slug_counts_past_every_name_taken(tmp_path) -> None:
    """Proves free_job_slug gives the name that rooster gives a job, counting past each saved job
    of the same name."""
    folder = tmp_path / "refactor"
    assert free_job_slug(folder, "job-001") == "job-001"
    folder.mkdir()
    (folder / "job-001.json").write_text("{}", encoding="utf-8")
    assert free_job_slug(folder, "job-001") == "job-001-2"
    (folder / "job-001-2.json").write_text("{}", encoding="utf-8")

    assert free_job_slug(folder, "job-001") == "job-001-3"


def test_process_is_alive_for_this_process() -> None:
    """Proves the running test process counts as alive."""
    assert process_is_alive(os.getpid())


def test_process_is_alive_is_false_for_a_fake_pid() -> None:
    """Proves a process id that no process holds counts as dead."""
    assert not process_is_alive(FAKE_PID)


@pytest.mark.parametrize("pid", [0, -1])
def test_a_pid_of_zero_or_less_is_not_alive(pid) -> None:
    """Proves a process id of 0 or less counts as dead."""
    assert not process_is_alive(pid)


def test_a_finished_process_whose_handle_is_still_open_is_not_alive() -> None:
    """Proves a process that has exited counts as dead, even while a handle to it is open."""
    # keep the Popen object, so that on Windows its handle stays open after the exit
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()

    assert not process_is_alive(child.pid)
    assert child.returncode == 0


def test_halt_job_file_leaves_a_job_with_no_file_alone(tmp_path) -> None:
    """Proves halt_job_file does nothing for a job whose file was never saved."""
    path = tmp_path / "never-saved.json"

    halt_job_file(path)

    assert not path.exists()


def test_process_is_alive_on_posix_reads_the_signal_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves a process that the signal check cannot find is dead, one the user may not signal is
    alive, and one it reaches is alive."""
    monkeypatch.setattr("ratchet.graph_lock.sys.platform", "linux")
    outcomes = {1: ProcessLookupError(), 2: PermissionError(), 3: None}

    def fake_kill(pid: int, signal: int) -> None:
        assert signal == 0
        if outcomes[pid] is not None:
            raise outcomes[pid]

    monkeypatch.setattr("ratchet.graph_lock.os.kill", fake_kill)

    assert not process_is_alive(1)
    assert process_is_alive(2)
    assert process_is_alive(3)

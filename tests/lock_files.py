"""Writes the files that the tests of the graph lock start from: the lock in .ratchet/state, and
the job files that the lock names, as rooster saves them."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from rooster import Job, JobStatus

from ratchet.project import Project

# A process id that no process holds, for a lock left by a process that is gone.
FAKE_PID = 2_147_483_644

# The scratch project's problem list, whose folder holds the refactor jobs.
LIST = "problems-20261026-09h00m"


def lock_path(project: Project) -> Path:
    """Returns the file that holds the graph lock of a project."""
    return project.state_dir / "graph.lock"


def write_lock(project: Project, **fields: object) -> Path:
    """Writes the graph lock of a project and returns its path.

    The lock names a running refactor job, job-012, held by this process. Each of 'fields'
    replaces one of graph, job, job_file, pid, state and gate. The job_file is the job's .json
    path relative to project.state_dir, written with forward slashes.
    """
    lock = {"graph": "refactor", "job": "job-012", "job_file": f"{LIST}/refactor/job-012.json",
            "pid": os.getpid(), "state": "running", "gate": ""} | fields
    path = lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock), encoding="utf-8")
    return path


def write_job_file(path: Path, status: str, next_step: str | None) -> Path:
    """Writes a job file at 'path' as rooster saves a job, with the job named after the file, the
    status 'status' and the next step 'next_step', and returns the path."""
    job = Job(slug=path.stem, next_step=next_step, prompt="", status=JobStatus(status))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(job), indent=2), encoding="utf-8")
    return path


def read_job_file(path: Path) -> dict:
    """Returns the saved job at 'path' as a dict."""
    return json.loads(path.read_text(encoding="utf-8"))

"""Run state, resource journal, events and the single-active-run lock.

State lives under $SNAP_COMMON. runs/<run-id>/ is readable by local operators so
`run status` needs no worker or cloud access; private/ holds run credentials and
keys and is root-only. state.json is replaced atomically; resources.jsonl and
events.jsonl are append-only. Only the worker writes here; `check` never does.
"""

import fcntl
import json
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from campaign.model.profiles import RunConfig, Schedule
from campaign.model.target import Target
from probe.models import RunPlan

STATE_ROOT = Path(os.environ.get("SNAP_COMMON", ".scratch/state"))
RUNS = STATE_ROOT / "runs"
PRIVATE = STATE_ROOT / "private"
LOCK = PRIVATE / "run.lock"
SOCKET = STATE_ROOT / "control.sock"

Phase = Literal["accepted", "preparing", "running", "finalizing", "finished"]
Execution = Literal["completed", "cancelled", "failed", "interrupted"]
Finalization = Literal["complete", "incomplete", "retained"]
Action = Literal["intent", "created", "deleted", "rejected", "failed", "requires-login"]


class RunState(BaseModel):
    """Lifecycle and outcome of one run."""

    model_config = ConfigDict(extra="forbid")

    id: str
    duration_s: int
    model: str
    cloud: str
    config: RunConfig
    plan: RunPlan
    schedule: Schedule
    targets: list[Target] = Field(default_factory=list)
    owner_uid: int
    phase: Phase = "accepted"
    detail: str | None = None
    cancel_requested: bool = False
    retain: bool = False
    warmup_start: datetime | None = None
    measured_start: datetime | None = None
    execution: Execution | None = None
    finalization: Finalization | None = None
    code: int | None = None
    causes: list[str] = Field(default_factory=list)
    finalize_attempts: int = 0
    created_at: datetime
    updated_at: datetime
    deadline: datetime


class ResourceEntry(BaseModel):
    """One journal line: intent is written before the cloud or host call."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    name: str
    action: Action
    id: str | None = None
    where: str | None = None
    detail: str | None = None
    at: datetime


class RunActiveError(Exception):
    """Another run is active or was interrupted and is not finalized."""


def now() -> datetime:
    """Current UTC time."""
    return datetime.now(UTC)


def run_dir(run_id: str) -> Path:
    """Public directory of a run."""
    return RUNS / run_id


def private_dir(run_id: str) -> Path:
    """Root-only directory with a run's credentials and keys."""
    return PRIVATE / "runs" / run_id


def write_file(path: Path, text: str) -> None:
    """Replace a file atomically."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def append_line(path: Path, line: str) -> None:
    """Append one line and flush it before returning."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    with os.fdopen(fd, "a", encoding="utf-8") as file:
        file.write(line + "\n")
        file.flush()
        os.fsync(file.fileno())


def load(run_id: str) -> RunState:
    """Read one run's state; unknown or malformed IDs raise FileNotFoundError."""
    try:
        run_id = str(uuid.UUID(run_id))
    except ValueError:
        raise FileNotFoundError(run_id) from None
    return RunState.model_validate_json((run_dir(run_id) / "state.json").read_text())


def runs() -> list[RunState]:
    """All runs, newest first."""
    if not RUNS.is_dir():
        return []
    found = [
        load(path.name) for path in RUNS.iterdir() if (path / "state.json").is_file()
    ]
    return sorted(found, key=lambda state: state.created_at, reverse=True)


def retained() -> RunState | None:
    """The newest finished run that kept its resources, if any."""
    return next((s for s in runs() if s.finalization == "retained"), None)


def unfinished() -> RunState | None:
    """The run that is active or interrupted, if any."""
    return next((s for s in runs() if s.phase != "finished"), None)


_saving = threading.Lock()


def save(state: RunState) -> None:
    """Persist a run's state, stamping the update time; safe across threads."""
    with _saving:
        state.updated_at = now()
        text = state.model_dump_json(indent=2)
        write_file(run_dir(state.id) / "state.json", text)


def record(
    run_id: str,
    kind: str,
    name: str,
    action: Action,
    id: str | None = None,
    where: str | None = None,
    detail: str | None = None,
) -> None:
    """Append a resource journal line."""
    entry = ResourceEntry(
        kind=kind,
        name=name,
        action=action,
        id=id,
        where=where,
        detail=detail,
        at=now(),
    )
    append_line(run_dir(run_id) / "resources.jsonl", entry.model_dump_json())


def resources(run_id: str) -> list[ResourceEntry]:
    """Read a run's resource journal in order."""
    path = run_dir(run_id) / "resources.jsonl"
    if not path.is_file():
        return []
    return [
        ResourceEntry.model_validate(json.loads(line))
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def transition(
    run_id: str, entry: ResourceEntry, action: Action, detail: str | None = None
) -> None:
    """Record a new action for an existing journal entry, keeping its identity."""
    record(run_id, entry.kind, entry.name, action, entry.id, entry.where, detail)


def outstanding(run_id: str) -> list[ResourceEntry]:
    """Resources still owned by the run, oldest first.

    The latest entry per (where, kind, name) wins. "deleted" and "rejected"
    (a create the API refused, so nothing exists) take a resource off the list.
    """
    last: dict[tuple[str | None, str, str], ResourceEntry] = {}
    for entry in resources(run_id):
        key = (entry.where, entry.kind, entry.name)
        if entry.id is None and key in last:
            entry = entry.model_copy(update={"id": last[key].id})
        last[key] = entry
    return [e for e in last.values() if e.action not in ("deleted", "rejected")]


def event(run_id: str, event_name: str, **fields: object) -> None:
    """Append a run event, such as a phase start or a workload step."""
    line = json.dumps({"at": now().isoformat(), "event": event_name, **fields})
    append_line(run_dir(run_id) / "events.jsonl", line)


def events(run_id: str) -> list[dict[str, object]]:
    """Read a run's events in order."""
    path = run_dir(run_id) / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@contextmanager
def lock() -> Iterator[None]:
    """Hold the installation run lock; fail at once if another process holds it."""
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            active = unfinished()
            raise RunActiveError(
                f"Run {active.id} is active." if active else "A run is active."
            ) from None
        yield
    finally:
        os.close(fd)


def create(
    *,
    model: str,
    cloud: str,
    config: RunConfig,
    plan: RunPlan,
    schedule: Schedule,
    owner_uid: int,
    retain: bool = False,
) -> RunState:
    """Accept a new run. Call while holding lock()."""
    if previous := unfinished():
        raise RunActiveError(
            f'Run {previous.id} did not finish; run "cloud-stress run stop" first.'
        )
    created = now()
    state = RunState(
        id=str(uuid.uuid4()),
        duration_s=schedule.duration_s,
        model=model,
        cloud=cloud,
        config=config,
        plan=plan,
        schedule=schedule,
        owner_uid=owner_uid,
        retain=retain,
        created_at=created,
        updated_at=created,
        deadline=datetime.fromtimestamp(
            created.timestamp() + schedule.max_elapsed_s, UTC
        ),
    )
    RUNS.mkdir(mode=0o755, parents=True, exist_ok=True)
    run_dir(state.id).mkdir(mode=0o755)
    for sub in ("metrics", "raw"):
        (run_dir(state.id) / sub).mkdir(mode=0o755)
    save(state)
    return state

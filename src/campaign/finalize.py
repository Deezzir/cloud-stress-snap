"""Finalize a run from its resource journal: stop, retrieve, delete, report.

The same path serves normal completion, cancellation, recovery after a crash and
a later `run stop` retry. Directories, the guest and cloud resources are removed
only after the evidence they hold is retrieved, unless the operator abandons it.
"""

import base64
import io
import shlex
import shutil
import tarfile
from pathlib import Path

import jubilant
from openstack.connection import Connection

from campaign import provision, remote, report
from campaign import state as run_state
from campaign.guest import Guest
from campaign.state import ResourceEntry, RunState


def _short(error: BaseException) -> str:
    return " ".join(str(getattr(error, "stderr", "") or error).split())[:300]


def _needs_login(error: BaseException) -> bool:
    text = _short(error).lower()
    return any(word in text for word in ("login", "macaroon", "discharge", "401"))


def _failed(state: RunState, entry: ResourceEntry, error: BaseException) -> None:
    action: run_state.Action = "requires-login" if _needs_login(error) else "failed"
    run_state.transition(state.id, entry, action, _short(error))


def finalize(
    state: RunState,
    conn: Connection | None,
    juju: jubilant.Juju,
    abandon_evidence: bool = False,
) -> None:
    """Stop run-owned work, retrieve evidence, clean up and write the result."""
    state.phase, state.code = "finalizing", None
    state.finalize_attempts += 1
    _progress(state, "stopping workloads")
    run_state.event(state.id, "phase", phase="finalizing")
    raw = run_state.run_dir(state.id) / "raw"
    private = run_state.private_dir(state.id)
    guest = _guest(state)

    # 1. Stop: floods first so no load outlives the run.
    entries = run_state.outstanding(state.id)
    for entry in sorted(entries, key=lambda e: "flood" not in e.name):
        try:
            if entry.kind == "unit":
                remote.stop_unit(juju, entry.where or "", entry.name)
            elif entry.kind == "guest-unit" and guest:
                guest.stop_unit(entry.name)
        except Exception as error:  # noqa: BLE001 - keep finalizing the rest
            _failed(state, entry, error)

    # 2. Retrieve evidence; a unit is done once its output is in the bundle.
    _progress(state, "retrieving evidence")
    pending_where: set[str] = set()
    guest_pending = False
    for entry in run_state.outstanding(state.id):
        if entry.kind not in ("unit", "guest-unit"):
            continue
        try:
            if entry.kind == "unit":
                if entry.id:
                    text = remote.fetch(juju, entry.where or "", entry.id)
                    if text is not None:
                        (raw / entry.id.rsplit("/", 1)[-1]).write_text(text)
            elif guest and entry.id:
                _fetch_guest(guest, entry.id, raw / entry.id.rsplit("/", 1)[-1])
            elif entry.id:
                raise RuntimeError("the guest is not reachable")
            run_state.transition(state.id, entry, "deleted")
        except Exception as error:  # noqa: BLE001
            if abandon_evidence:
                run_state.event(
                    state.id, "evidence-lost", name=entry.name, message=_short(error)
                )
                run_state.transition(state.id, entry, "deleted", "evidence abandoned")
                continue
            _failed(state, entry, error)
            if entry.kind == "unit":
                pending_where.add(entry.where or "")
            else:
                guest_pending = True

    # 3. Delete what no longer holds pending evidence.
    _progress(state, "deleting run-owned resources")
    for entry in run_state.outstanding(state.id):
        if entry.kind != "dir" or entry.where in pending_where:
            continue
        try:
            remote.remove_dir(juju, entry.where or "", entry.name)
            run_state.transition(state.id, entry, "deleted")
        except Exception as error:  # noqa: BLE001
            _failed(state, entry, error)
    if not guest_pending:
        if conn is None:
            for entry in run_state.outstanding(state.id):
                if entry.where == "openstack" and entry.action != "requires-login":
                    run_state.transition(
                        state.id, entry, "requires-login", "OpenStack login expired"
                    )
        else:
            try:
                provision.cleanup(conn, state)
            except Exception as error:  # noqa: BLE001 - e.g. expired token
                run_state.event(state.id, "cleanup-error", message=_short(error))

    # 4. Result.
    _progress(state, "writing the result")
    left = run_state.outstanding(state.id)
    state.finalization = "incomplete" if left else "complete"
    state.causes = [c for c in state.causes if not c.startswith("cleanup:")]
    state.causes += [
        f"cleanup: {e.kind} {e.name} on {e.where}: {e.action}" for e in left
    ]
    try:
        result = report.build(state)
        qualification = result["qualification"]
        assert isinstance(qualification, dict)
        state.qualification = qualification["summary"]
        state.causes += [c for c in qualification["causes"] if c not in state.causes]
        state.code = report.exit_code(state)
        report.write(state, result)
    except Exception as error:  # noqa: BLE001 - a missing result is incomplete
        state.finalization = "incomplete"
        state.causes.append(f"result: {_short(error)}")
        state.code = report.exit_code(state)
    # A run with resources left stays unfinished, so `run stop` can retry it.
    state.phase = "finalizing" if left else "finished"
    state.detail = "cleanup pending" if left else None
    if not left:
        shutil.rmtree(private, ignore_errors=True)
    run_state.save(state)
    run_state.event(
        state.id, "finalized", finalization=state.finalization, code=state.code
    )


def _progress(state: RunState, detail: str) -> None:
    state.detail = detail
    run_state.save(state)


def _guest(state: RunState) -> Guest | None:
    """The run's guest, if it was provisioned and its keys are still present."""
    target = run_state.run_dir(state.id) / "target.json"
    private = run_state.private_dir(state.id)
    if not target.is_file() or not (private / "id_ed25519").is_file():
        return None
    address = provision.Target.model_validate_json(target.read_text()).address
    return Guest(address, private / "id_ed25519", private / "known_hosts")


def _fetch_guest(guest: Guest, path: str, target: Path) -> None:
    """Copy a run-owned guest directory into the bundle."""
    data = guest.run(f"tar -C {shlex.quote(path)} -czf - . | base64 -w0", timeout=300)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(data)), mode="r:gz") as tar:
        tar.extractall(target, filter="data")

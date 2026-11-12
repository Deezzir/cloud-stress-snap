"""Finalize a run from its resource journal: stop, retrieve, delete, report.

The same path serves normal completion, cancellation, recovery after a crash and
a later `run stop` retry. Directories, guests and cloud resources are removed
only after the evidence they hold is retrieved, unless the operator abandons it.
A run started with --retain retains its resources after stopping its work and
retrieving evidence, until `run stop --clean` clears state.retain.
"""

import base64
import io
import shlex
import shutil
import tarfile
from pathlib import Path

import jubilant
from openstack.connection import Connection

from campaign.lifecycle import provision
from campaign.model import state as run_state
from campaign.model.state import ResourceEntry, RunState
from campaign.report import build as report
from campaign.transport import remote
from campaign.transport.guest import Guest, guest_for


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
    guests = {t.name: guest_for(juju, state, t) for t in state.targets}
    prefix = f"cloud-stress-{state.id[:8]}-"

    # 1. Stop: floods first so no load outlives the run.
    not_stopped: set[tuple[str | None, str]] = set()
    entries = run_state.outstanding(state.id)
    for entry in sorted(entries, key=lambda e: "flood" not in e.name):
        try:
            if entry.kind == "unit":
                run_dir = entry.id.rsplit("/", 1)[0] if entry.id else None
                remote.stop_unit(juju, entry.where or "", entry.name, run_dir)
            elif entry.kind == "guest-unit":
                if entry.where not in guests:
                    raise RuntimeError(f"the guest {entry.where} is not known")
                guests[entry.where].stop_unit(entry.name)
        except Exception as error:  # noqa: BLE001 - keep finalizing the rest
            if entry.kind in ("unit", "guest-unit"):
                not_stopped.add((entry.where, entry.name))
            _failed(state, entry, error)

    # 2. Retrieve evidence. A unit is done once it is stopped and its output is
    # in the bundle or abandoned. A guest unit that did not stop is stopped by
    # deleting its server, which needs abandon_evidence.
    _progress(state, "retrieving evidence")
    pending_where: set[str] = set()
    guest_pending = False
    for entry in run_state.outstanding(state.id):
        if entry.kind not in ("unit", "guest-unit"):
            continue
        stopped = (entry.where, entry.name) not in not_stopped
        try:
            if entry.kind == "unit":
                if entry.id:
                    text = remote.fetch(juju, entry.where or "", entry.id)
                    if text is not None:
                        (raw / entry.id.rsplit("/", 1)[-1]).write_text(text)
            elif entry.id and entry.where in guests:
                name = entry.name.removeprefix(prefix)
                _fetch_guest(guests[entry.where], entry.id, raw / entry.where / name)
            elif entry.id:
                raise RuntimeError(f"the guest {entry.where} is not known")
            evidence = True
        except Exception as error:  # noqa: BLE001
            evidence = False
            if abandon_evidence:
                run_state.event(
                    state.id, "evidence-lost", name=entry.name, message=_short(error)
                )
            elif stopped:
                _failed(state, entry, error)
        if stopped and (evidence or abandon_evidence):
            detail = None if evidence else "evidence abandoned"
            run_state.transition(state.id, entry, "deleted", detail)
        elif entry.kind == "unit":
            pending_where.add(entry.where or "")
        elif not abandon_evidence:
            guest_pending = True
            if entry.where in guests:
                pending_where.add(guests[entry.where].machine)

    # 3. Delete what no longer holds pending evidence. A host's directory holds
    # the guest key, so it stays while evidence in its guests is pending.
    # A retaining run keeps everything left; `run stop --clean` finalizes it again.
    retain = state.retain
    if retain:
        _progress(state, "retaining run-owned resources")
    else:
        _progress(state, "deleting run-owned resources")
        for entry in run_state.outstanding(state.id):
            if entry.kind not in ("dir", "snap") or entry.where in pending_where:
                continue
            try:
                if entry.kind == "snap":
                    remote.remove_snap(juju, entry.where or "", entry.name)
                else:
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
                _settle_guest_units(state)

    _progress(state, "writing the result")
    left = run_state.outstanding(state.id)
    retained = retain and bool(left)
    state.finalization = (
        "retained" if retained else "incomplete" if left else "complete"
    )
    state.causes = [c for c in state.causes if not c.startswith("cleanup:")]
    if not retained:
        state.causes += [
            f"cleanup: {e.kind} {e.name} on {e.where}: {e.action}" for e in left
        ]
    try:
        result, metrics = report.build(state)
        state.code = report.exit_code(state)
        report.write(state, result, metrics)
    except Exception as error:  # noqa: BLE001 - a missing result is incomplete
        state.finalization = "incomplete"
        state.causes.append(f"result: {_short(error)}")
        state.code = report.exit_code(state)
    # A run with resources left stays unfinished, so `run stop` can retry it.
    # A retained run is finished; its keys stay for inspection and `--clean`.
    pending = bool(left) and not retained
    state.phase = "finalizing" if pending else "finished"
    state.detail = "cleanup pending" if pending else None
    if not left:
        shutil.rmtree(private, ignore_errors=True)
    run_state.save(state)
    run_state.event(
        state.id, "finalized", finalization=state.finalization, code=state.code
    )


def _settle_guest_units(state: RunState) -> None:
    """Close guest units whose server is deleted: deleting it stopped them."""
    left = run_state.outstanding(state.id)
    servers = {e.name for e in left if e.where == "openstack" and e.kind == "server"}
    for entry in left:
        server = provision.name(state, "server", entry.where)
        if entry.kind == "guest-unit" and server not in servers:
            run_state.transition(
                state.id, entry, "deleted", "stopped by deleting its server"
            )


def _progress(state: RunState, detail: str) -> None:
    state.detail = detail
    run_state.save(state)


def _fetch_guest(guest: Guest, path: str, target: Path) -> None:
    """Copy a run-owned guest directory into the bundle."""
    data = guest.run(f"tar -C {shlex.quote(path)} -czf - . | base64 -w0", timeout=300)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(data)), mode="r:gz") as tar:
        tar.extractall(target, filter="data")

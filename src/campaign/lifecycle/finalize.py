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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import jubilant
from openstack.connection import Connection

from campaign.lifecycle import provision
from campaign.model import state as run_state
from campaign.model.state import ResourceEntry, RunState
from campaign.report import build as report
from campaign.transport import remote
from campaign.transport.guest import guest_for
from constants import PARALLEL


def _short(error: BaseException) -> str:
    return " ".join(str(getattr(error, "stderr", "") or error).split())[:300]


def _failed(state: RunState, entry: ResourceEntry, error: BaseException) -> None:
    detail = _short(error)
    login = any(
        word in detail.lower() for word in ("login", "macaroon", "discharge", "401")
    )
    run_state.transition(
        state.id, entry, "requires-login" if login else "failed", detail
    )


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
    senders = {t.name: t for t in state.host_senders}
    prefix = f"cloud-stress-{state.id[:8]}-"

    # 1. Stop: floods first so no load outlives the run.
    not_stopped: set[tuple[str | None, str]] = set()
    units = [
        e
        for e in run_state.outstanding(state.id)
        if e.kind in ("unit", "guest-unit", "host-workload")
    ]

    def place(entry: ResourceEntry) -> str:
        """The machine or guest an entry runs on; one place is handled serially."""
        if entry.kind == "guest-unit":
            return f"guest:{entry.where}"
        if entry.kind == "host-workload" and entry.where in senders:
            return senders[entry.where].machine
        return entry.where or ""

    def stop(entry: ResourceEntry) -> None:
        try:
            if entry.kind == "unit":
                run_dir = entry.id.rsplit("/", 1)[0] if entry.id else None
                remote.stop_unit(juju, entry.where or "", entry.name, run_dir)
            elif entry.kind == "guest-unit":
                if entry.where not in guests:
                    raise RuntimeError(f"the guest {entry.where} is not known")
                guests[entry.where].stop_unit(entry.name)
            elif entry.kind == "host-workload":
                sender = senders.get(entry.where or "")
                if sender is None:
                    raise RuntimeError(f"the host sender {entry.where} is not known")
                root = f"/var/snap/trex-generator/common/cloud-stress-{state.id}"
                name = entry.name.removeprefix(prefix)
                if name in ("flood", "receipt"):
                    path = f"{root}/flood.py"
                elif name == "trex" or name.startswith("trex-"):
                    index = 0 if name == "trex" else int(name.removeprefix("trex-"))
                    path = f"{root}/process-{index}/trex_cfg.yaml"
                else:
                    raise RuntimeError(f"unknown host workload {entry.name}")
                remote.stop_unit(
                    juju, sender.machine, entry.name, None, process_path=path
                )
        except Exception as error:  # noqa: BLE001 - keep finalizing the rest
            not_stopped.add((entry.where, entry.name))
            _failed(state, entry, error)

    floods = [e for e in units if "flood" in e.name]
    _each_place(floods, place, stop)
    _each_place([e for e in units if "flood" not in e.name], place, stop)

    # 2. Retrieve evidence. A unit is done once it is stopped and its output is
    # in the bundle or abandoned. A guest unit that did not stop is stopped by
    # deleting its server, which needs abandon_evidence.
    _progress(state, "retrieving evidence")
    pending_where: set[str] = set()
    keep_cloud = False
    retrieved: set[tuple[str | None, str]] = set()

    def retrieve(entry: ResourceEntry) -> None:
        stopped = (entry.where, entry.name) not in not_stopped
        try:
            if entry.kind == "unit":
                if entry.id:
                    text = remote.fetch(juju, entry.where or "", entry.id)
                    if text is not None:
                        (raw / entry.id.rsplit("/", 1)[-1]).write_text(text)
            elif entry.kind == "host-workload" and any(
                w == entry.where for w, _ in not_stopped
            ):
                raise RuntimeError(f"{entry.where}: workloads still hold evidence")
            elif entry.id:
                where = entry.where or ""
                archive = f"tar -C {shlex.quote(entry.id)} -czf - ."
                cmd = f"set -o pipefail; {archive} | base64 -w0"
                data = (
                    remote.sh(juju, senders[where].machine, cmd, wait=300)
                    if entry.kind == "host-workload"
                    else guests[where].run(cmd, timeout=300)
                )
                target = raw / where / entry.name.removeprefix(prefix)
                if not target.resolve().is_relative_to(raw.resolve()):
                    raise ValueError("evidence destination is outside the run")
                _extract(data, target)
            retrieved.add((entry.where, entry.name))
        except Exception as error:  # noqa: BLE001
            if abandon_evidence:
                run_state.event(
                    state.id, "evidence-lost", name=entry.name, message=_short(error)
                )
            elif stopped:
                _failed(state, entry, error)

    _each_place(units, place, retrieve)
    for entry in units:
        stopped = (entry.where, entry.name) not in not_stopped
        evidence = (entry.where, entry.name) in retrieved
        if stopped and (evidence or abandon_evidence):
            detail = None if evidence else "evidence abandoned"
            run_state.transition(state.id, entry, "deleted", detail)
        elif entry.kind == "unit":
            pending_where.add(entry.where or "")
        elif entry.kind == "host-workload":
            # A source that is still running also holds its Neutron identity.
            keep_cloud = True
            sender = senders.get(entry.where or "")
            if sender:
                pending_where.add(sender.machine)
            else:
                pending_where.update(h.machine for h in state.plan.hosts)
        elif not abandon_evidence:
            keep_cloud = True
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
            if (
                entry.kind not in ("dir", "snap", "rpc-guard")
                or entry.where in pending_where
            ):
                continue
            try:
                if entry.kind == "snap":
                    _cleanup_snap(state, juju, entry)
                elif entry.kind == "rpc-guard":
                    remote.remove_rpc_guard(juju, entry.where or "", entry.name)
                else:
                    remote.remove_dir(juju, entry.where or "", entry.name, state.id)
                run_state.transition(state.id, entry, "deleted")
            except Exception as error:  # noqa: BLE001
                _failed(state, entry, error)
        if not keep_cloud:
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
    retained = (
        retain and bool(left) and not (not_stopped or pending_where or keep_cloud)
    )
    state.finalization = (
        "retained" if retained else "incomplete" if left else "complete"
    )
    state.causes = [
        c for c in state.causes if not c.startswith(("cleanup:", "result:"))
    ]
    if not retained:
        state.causes += [
            f"cleanup: {e.kind} {e.name} on {e.where}: {e.action}"
            + (f" ({e.detail})" if e.kind == "snap" and e.detail else "")
            for e in left
        ]
    try:
        result, metrics = report.build(state)
        state.code = report.exit_code(state)
        report.write(state, result, metrics)
    except Exception as error:  # noqa: BLE001 - report errors do not change ownership
        state.causes.append(f"result: {_short(error)}")
        state.code = 20
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


def _cleanup_snap(state: RunState, juju: jubilant.Juju, entry: ResourceEntry) -> None:
    if not entry.where or not entry.where.isdecimal():
        raise ValueError("invalid snap machine")
    if state.plan.hosts and entry.where not in {h.machine for h in state.plan.hosts}:
        raise RuntimeError("snap journal target is not a planned host")
    # A retry's latest action is "failed" or "requires-login". Recover the
    # original install window, never borrowing ownership from a previous intent.
    intent = created = None
    identity = None
    key = ("snap", entry.where, entry.name)
    for record in run_state.resources(state.id):
        if (record.kind, record.where, record.name) != key:
            continue
        if record.action in ("intent", "deleted", "rejected"):
            intent = record.at if record.action == "intent" else None
            created = None
            identity = None
        elif record.action == "created":
            created = record.at
            identity = record.id
    remote.remove_owned_snap(juju, entry.where, entry.name, intent, created, identity)


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


def _extract(data: str, target: Path) -> None:
    """Extract workload directory evidence into the bundle."""
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(data)), mode="r:gz") as tar:
        tar.extractall(target, filter="data")


def _each_place(
    entries: list[ResourceEntry],
    place: Callable[[ResourceEntry], str],
    work: Callable[[ResourceEntry], None],
) -> None:
    """Apply work in parallel across places, in journal order within each place."""
    groups: dict[str, list[ResourceEntry]] = {}
    for entry in entries:
        groups.setdefault(place(entry), []).append(entry)

    def serial(group: list[ResourceEntry]) -> None:
        for entry in group:
            work(entry)

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        list(pool.map(serial, groups.values()))

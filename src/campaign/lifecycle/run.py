"""Execute one run: prepare the fleet, then run the workloads on a shared timeline.

finalize.py stops and cleans up afterwards.
"""

import json
import re
import shlex
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TypeVar

import jubilant
from openstack.connection import Connection

from campaign.lifecycle import provision
from campaign.lifecycle.finalize import finalize
from campaign.lifecycle.provision import PreparationError
from campaign.model import state as run_state
from campaign.model.state import RunState
from campaign.model.target import HostTarget, Target
from campaign.transport import remote
from campaign.transport.guest import Guest, guest_for, guest_keygen, keygen
from campaign.workloads import collect, fio, trex
from constants import (
    BOOT_TIMEOUT_S,
    DRAIN_S,
    LEAD_S,
    MONITOR_S,
    PARALLEL,
    PREFILL_S_PER_GIB,
    RECEIPT_MIN,
    RECEIPT_PPS,
    RECEIPT_S,
    SAMPLE_INTERVAL_S,
    TREX_CHANNEL,
    TREX_PLUGS,
    TREX_RPC_PORT,
    TREX_SNAP,
)

T = TypeVar("T")

# One `cloud-init status --long` error: ('module', SomeError("message"))
CLOUD_INIT_ERROR = re.compile(r"""\('([^']+)', \w+\((['"])(.*)\2\)\)""")


def cloud_init_errors(status: str) -> list[str]:
    """Readable entries from the errors section of `cloud-init status --long`."""
    errors, section = [], ""
    for line in status.splitlines():
        text = line.strip()
        if text.startswith("- "):
            if section == "errors":
                match = CLOUD_INIT_ERROR.fullmatch(text[2:])
                errors.append(f"{match[1]}: {match[3]}" if match else text[2:])
        elif text.endswith(":"):
            section = text[:-1]
    return errors


class Cancelled(Exception):
    """The operator stopped the run."""


class Run:
    """One accepted run in the worker."""

    def __init__(self, state: RunState, conn: Connection, juju: jubilant.Juju) -> None:
        """Bind the run to its access and a cancellation flag."""
        self.state = state
        self.conn = conn
        self.juju = juju
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.done = False
        self.dir = run_state.run_dir(state.id)
        self.private = run_state.private_dir(state.id)
        self.short = state.id[:8]
        self.host_dir = f"/var/lib/cloud-stress/{state.id}"
        self.guest_dir = f"/var/lib/cloud-stress/{state.id}"
        self.trex_dir = f"/var/snap/{TREX_SNAP}/common/cloud-stress-{self.short}"
        self.prepare_deadline = time.monotonic() + state.schedule.prepare_budget_s
        self.guests: dict[str, Guest] = {}
        self.links: dict[str, str] = {}
        self.devices: dict[str, str] = {}

    def execute(self) -> None:
        """Run the configuration end to end; finalization always follows."""
        error: str | None = None
        execution: run_state.Execution = "completed"
        try:
            self.prepare()
            self.measure()
        except Cancelled:
            execution = "cancelled"
        except PreparationError as failure:
            execution, error = "failed", f"preparation: {failure}"
        except Exception as failure:  # noqa: BLE001 - recorded, then finalized
            execution = "failed"
            name, text = type(failure).__name__, " ".join(str(failure).split())
            error = (text if text.startswith(name) else f"{name}: {text}")[:300]
        if error:
            run_state.event(self.state.id, "error", message=error)
            self.state.causes.append(error)
        self.state.execution = execution
        finalize(self.state, self.conn, self.juju)
        # `run stop --clean` may clear retain after deletion was skipped.
        while True:
            with self.lock:
                if self.state.retain or self.state.finalization != "retained":
                    self.done = True
                    return
            finalize(self.state, self.conn, self.juju)

    def phase(self, phase: run_state.Phase, detail: str | None = None) -> None:
        """Record a lifecycle phase change."""
        self.state.phase, self.state.detail = phase, detail
        run_state.save(self.state)
        run_state.event(self.state.id, "phase", phase=phase, detail=detail)

    def note(self, detail: str) -> None:
        """Update the progress line shown by `run status`."""
        self.state.detail = detail
        run_state.save(self.state)

    def wait(self, seconds: float) -> None:
        """Sleep, but wake up at once on cancellation."""
        if self.cancel.wait(max(seconds, 0)):
            raise Cancelled

    def wait_for(self, check: Callable[[], bool], timeout: float, what: str) -> None:
        """Poll check until it holds, within timeout and the preparation budget."""
        if self.state.phase == "preparing":
            timeout = min(timeout, self.prepare_deadline - time.monotonic())
        deadline = time.monotonic() + timeout
        while not check():
            if time.monotonic() > deadline:
                raise PreparationError(f"{what} timed out after {int(timeout)}s")
            self.wait(5)

    def each(self, items: Iterable[T], work: Callable[[T], object]) -> None:
        """Apply work to every item in parallel; raise the first failure."""
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            list(pool.map(work, items))

    def targets(self, role: str | None = None) -> list[Target]:
        """The run's VMs, optionally those taking one network role; peers take both."""
        return [
            t for t in self.state.targets if role in (None, t.role) or t.role == "peer"
        ]

    def senders(self) -> list[Target | HostTarget]:
        """VM and physical-host sources; host sources never enter guest workflows."""
        return [*self.targets("sender"), *self.state.host_senders]

    def sender_dir(self, target: Target | HostTarget) -> str:
        """Each sender's run-owned snap directory."""
        if isinstance(target, HostTarget):
            return f"/var/snap/{TREX_SNAP}/common/cloud-stress-{self.state.id}"
        return self.trex_dir

    def command(
        self,
        target: Target | HostTarget,
        command: str,
        timeout: int = 120,
    ) -> str:
        """Execute against the explicitly identified VM or physical host."""
        if self.cancel.is_set():
            raise Cancelled
        if self.state.phase == "preparing":
            remaining = int(self.prepare_deadline - time.monotonic())
            if remaining <= 0:
                raise PreparationError("preparation deadline exceeded")
            timeout = min(timeout, remaining)
        if isinstance(target, HostTarget):
            try:
                return remote.sh(self.juju, target.machine, command, wait=timeout)
            except (jubilant.CLIError, jubilant.TaskError, TimeoutError) as error:
                detail = str(getattr(error, "stderr", "") or error).rsplit(
                    "Stderr:", 1
                )[-1]
                raise RuntimeError(
                    f"host command in {target.name} failed: "
                    + " ".join(detail.split())[-300:]
                ) from None
        return self.guests[target.name].run(command, timeout=timeout)

    def prepare(self) -> None:
        """Create the fleet and verify every VM, device and traffic path."""
        config = self.state.config
        self.phase("preparing", "checking quota")
        self.check_quota()
        self.note(f"creating {len(self.state.plan.vms)} VMs")
        self.create_fleet()
        self.note("waiting for the guests")
        self.each(self.targets(), self.ready_guest)
        if config.workloads.trex:
            self.note("adding receive counters")
            self.each(self.targets("receiver"), self.add_counters)
        self.note("starting collectors")
        self.start_collectors()
        if config.workloads.trex:
            self.note("preparing TRex senders")
            self.each(self.senders(), self.start_trex)
            self.note("checking receipt between every sender and receiver")
            self.verify_receipt()
        if config.workloads.fio:
            self.note("prefilling the data volumes")
            self.prefill()

    def check_quota(self) -> None:
        """Quota for the whole fleet, before any run-owned resource exists."""
        plan, fio_config = self.state.plan, self.state.config.workloads.fio
        flavor = self.conn.compute.get_flavor(plan.flavor_id or "")
        problems = provision.check_quota(
            self.conn,
            len(plan.vms),
            flavor.vcpus,
            flavor.ram,
            fio_config.volume_size_gib if fio_config else 0,
        )
        if problems:
            raise PreparationError("; ".join(problems))

    def create_fleet(self) -> None:
        """Create the VMs with the SSH key of their own jump host."""
        public_keys = {}
        for host in self.state.plan.hosts:
            self.own_dir(host.machine, self.host_dir)
            if host.vms:
                public_keys[host.name] = guest_keygen(
                    self.juju, host.machine, self.host_dir
                )
        host_public = keygen(self.private / "host_ed25519")
        host_private = (self.private / "host_ed25519").read_text()
        try:
            self.state.targets = provision.provision(
                self.conn,
                self.state,
                public_keys,
                (host_private, host_public),
                self.wait,
            )
        finally:
            run_state.save(self.state)
        for target in self.state.targets:
            run_state.event(self.state.id, "target", **target.model_dump())
            self.guests[target.name] = guest_for(self.juju, self.state, target)
        for sender in self.state.host_senders:
            run_state.event(self.state.id, "host-sender", **sender.model_dump())
        self.each(self.guests.values(), lambda g: g.pin_host_key(host_public))

    def ready_guest(self, target: Target) -> None:
        """Wait for SSH and cloud-init; find the data interface and volume."""
        guest = self.guests[target.name]
        self.wait_for(
            guest.reachable,
            BOOT_TIMEOUT_S,
            f"SSH to {target.name} via {guest.namespace}",
        )
        status = guest.run("cloud-init status --wait >/dev/null; echo $?", timeout=900)
        if status.strip() not in ("0", "2"):
            detail = guest.run("cloud-init status --long 2>&1; true")
            run_state.event(
                self.state.id, "cloud-init", vm=target.name, status=detail.strip()
            )
            errors = cloud_init_errors(detail)
            raise PreparationError(
                f"cloud-init failed in {target.name}"
                + (f": {'; '.join(errors)}" if errors else f" ({status.strip()})")
            )
        before = time.time()
        remote_now = float(guest.run("date +%s.%N"))
        after = time.time()
        run_state.event(
            self.state.id,
            "clock",
            vm=target.name,
            offset_s=round(remote_now - (before + after) / 2, 3),
            uncertainty_s=round((after - before) / 2, 3),
        )
        if target.mac:
            link = guest.run(
                "for d in /sys/class/net/*; do "
                f'[ "$(cat $d/address)" = {target.mac} ] && basename $d; done; true'
            ).strip()
            if not link:
                raise PreparationError(f"{target.name} has no interface {target.mac}")
            self.links[target.name] = link
            mtu = int(
                guest.run(f"ip link set {link} up && cat /sys/class/net/{link}/mtu")
            )
            trex_config = self.state.config.workloads.trex
            assert trex_config
            size = trex.ip_packet_bytes(trex_config, self.state.plan.ip_version)
            if size > mtu:
                raise PreparationError(
                    f"{size}-byte TRex packets exceed the "
                    f"{mtu}-byte MTU of {link} in {target.name}"
                )
        if target.role in ("receiver", "peer"):
            self.tool(target.name, "nftables", guest.run("nft --version").strip())
        fio_config = self.state.config.workloads.fio
        if fio_config:
            version = guest.run("fio --version").strip()
            if not version.startswith("fio-"):
                raise PreparationError(f"fio is not available in {target.name}")
            self.tool(target.name, "fio", version)
            device, reason = fio.find_device(guest, target.volume_id or "", fio_config)
            if not device:
                raise PreparationError(f"{target.name}: {reason}")
            self.devices[target.name] = device

    def tool(self, vm: str, name: str, version: str, *, host: bool = False) -> None:
        """Record a tool version at its VM or host source."""
        run_state.event(
            self.state.id,
            "tool",
            **{"source" if host else "vm": vm},
            name=name,
            version=version,
        )

    def prepare_host_snap(self, target: HostTarget) -> None:
        """Reuse an existing installation; journal only our own install attempt."""
        inspect = f"snap debug api /v2/snaps/{TREX_SNAP}"
        if remote.trex_install_date(self.command(target, inspect)) is not None:
            return
        args = (self.state.id, "snap", TREX_SNAP)
        run_state.record(*args, "intent", where=target.machine)
        before = run_state.now()
        output = self.command(
            target,
            f"LC_ALL=C snap install {TREX_SNAP} --channel={TREX_CHANNEL} 2>&1",
            timeout=900,
        )
        if f'snap "{TREX_SNAP}" is already installed' in output:
            run_state.record(*args, "rejected", where=target.machine)
            return
        if not re.search(rf"^{TREX_SNAP} .+ installed$", output, re.MULTILINE):
            raise PreparationError(f"{target.name}: no confirmed {TREX_SNAP} install")
        # Finish documenting a successful install even if cancellation arrived
        # while snapd was working; the next preparation command honors it.
        installed = remote.trex_install_date(
            remote.sh(self.juju, target.machine, inspect)
        )
        if installed is None or not before <= installed <= run_state.now():
            raise PreparationError(f"{target.name}: cannot verify {TREX_SNAP} install")
        run_state.record(
            *args, "created", id=installed.isoformat(), where=target.machine
        )
        self.command(
            target, " && ".join(f"snap connect {TREX_SNAP}:{p}" for p in TREX_PLUGS)
        )

    def start_trex(self, target: Target | HostTarget) -> None:
        """Prepare a sender and start its servers without changing host devices."""
        trex_config = self.state.config.workloads.trex
        assert trex_config
        sockets = [0] * trex_config.processes
        rpc_port = TREX_RPC_PORT
        root = self.sender_dir(target)
        if isinstance(target, HostTarget):
            self.prepare_host_snap(target)
            rpc_port = trex.host_rpc_port(self.state.id)
            preflight = trex.host_preflight_command(
                target, self.state.plan, rpc_port, trex_config.processes
            )
            try:
                out = self.command(target, preflight, timeout=180)
            except RuntimeError as error:
                raise PreparationError(str(error)) from None
            details = json.loads(out)
            run_state.event(
                self.state.id,
                "host-package",
                where=target.machine,
                **details["package"],
            )
            target.cpus = details["cpus"]
            sockets, version = details["sockets"], details["version"]
            table = trex.rpc_table(self.state.id)
            tables = self.command(target, "nft list tables").splitlines()
            if f"table inet {table}" in tables:
                raise PreparationError(f"{target.name}: run RPC table already exists")
            run_state.record(
                self.state.id, "rpc-guard", table, "intent", where=target.machine
            )
            self.command(
                target, trex.rpc_guard_command(table, rpc_port, trex_config.processes)
            )
            run_state.record(
                self.state.id, "rpc-guard", table, "created", where=target.machine
            )
            self.own_dir(target.machine, root)
        else:
            install = trex.install_command()
            run_state.event(
                self.state.id, "command", where=target.name, command=install
            )
            try:
                version = self.command(target, install, timeout=900).strip()
            except RuntimeError as error:
                raise PreparationError(
                    f"installing the {TREX_SNAP} snap in {target.name} failed: {error}"
                ) from None
        self.tool(
            target.name,
            "trex",
            version.splitlines()[-1] if version else "",
            host=isinstance(target, HostTarget),
        )
        self.command(
            target,
            f"mkdir -p {root} && "
            f"printf %s {shlex.quote(trex.flood_script())} > {root}/flood.py",
        )
        for index in range(trex_config.processes):
            directory = f"{root}/process-{index}"
            text = (
                trex.host_config(
                    target,
                    self.state.plan.mtu or 1500,
                    rpc_port,
                    f"cs-{self.state.id}",
                    index,
                    sockets[index],
                )
                if isinstance(target, HostTarget)
                else trex.config(
                    self.links[target.name], target.mac or "", index, f"cs-{self.short}"
                )
            )
            run_state.event(
                self.state.id,
                "file",
                where=target.name,
                path=f"{directory}/trex_cfg.yaml",
                text=text,
            )
            self.command(
                target,
                f"mkdir -p {directory} && printf %s {shlex.quote(text)} "
                f"> {directory}/trex_cfg.yaml",
            )
            self.start_workload(
                target,
                "trex" if index == 0 else f"trex-{index}",
                self.state.schedule.max_elapsed_s,
                trex.server_command(
                    directory,
                    target.backend if isinstance(target, HostTarget) else "af_packet",
                ),
                # Fetch the whole tree once, including the aggregate controller output.
                evidence=root if index == 0 else None,
            )

            def ready(index: int = index, directory: str = directory) -> bool:
                if isinstance(target, HostTarget):
                    listening = self.command(
                        target, f"ss -Hltnp 'sport = :{rpc_port + 2 * index}'"
                    ).strip()
                    if listening:
                        pids = set(re.findall(r"pid=(\d+)", listening))
                        if not pids:
                            raise PreparationError(
                                "cannot identify the host TRex RPC owner"
                            )
                        for pid in pids:
                            argv = self.command(
                                target, f"cat /proc/{pid}/cmdline"
                            ).split("\0")
                            if f"{directory}/trex_cfg.yaml" not in argv:
                                raise PreparationError(
                                    "host TRex RPC port belongs to another process"
                                )
                        return True
                elif self.command(
                    target, trex.server_ready_command(index, rpc_port)
                ).strip():
                    return True
                name = "trex" if index == 0 else f"trex-{index}"
                rc_dir = root if isinstance(target, HostTarget) else self.guest_dir
                code = self.command(
                    target, f"cat {rc_dir}/{name}.rc 2>/dev/null || true"
                ).strip()
                if code:
                    log = self.command(
                        target, f"tail -c 2000 {directory}/trex.log"
                    ).strip()
                    raise PreparationError(
                        f"{target.name}: TRex exited {code}: {log[-300:]}"
                    )
                return False

            self.wait_for(
                ready,
                120,
                f"TRex server {index} start in {target.name}",
            )

    def add_counters(self, target: Target) -> None:
        """Count benchmark packets from each assigned sender in a receiver."""
        trex_config = self.state.config.workloads.trex
        assert trex_config
        sources: list[Target | HostTarget] = [
            *self.state.targets,
            *self.state.host_senders,
        ]
        by_name = {t.name: t for t in sources}
        senders = {
            c.sender: by_name[c.sender].address or ""
            for c in self.state.plan.connections
            if c.receiver == target.name
        }
        command = trex.counters_command(
            trex.counter_table(self.state.id),
            target.address or "",
            trex_config.udp_dst_port,
            senders,
        )
        run_state.event(self.state.id, "command", where=target.name, command=command)
        self.guests[target.name].run(command)

    def flood(
        self,
        target: Target | HostTarget,
        name: str,
        steps: list[tuple[int, float]],
        end: float,
    ) -> None:
        """Start a scheduled flood from one sender to its receivers."""
        trex_config = self.state.config.workloads.trex
        assert trex_config
        by_name = {t.name: t for t in self.state.targets}
        peers: list[dict[str, object]] = [
            {
                "name": c.receiver,
                "mac": by_name[c.receiver].mac,
                "dst": by_name[c.receiver].address,
            }
            for c in self.state.plan.connections
            if c.sender == target.name
        ]
        command = trex.flood_command(
            self.sender_dir(target),
            trex_config,
            {"address": target.address or "", "mac": target.mac or ""},
            peers,
            steps,
            end,
            name,
            vlan=self.state.plan.vlan if isinstance(target, HostTarget) else None,
            rpc_port=trex.host_rpc_port(self.state.id)
            if isinstance(target, HostTarget)
            else TREX_RPC_PORT,
        )
        lifetime = int(end - time.time()) + 120
        self.start_workload(target, name, lifetime, command, evidence=None)

    def counters(self, target: Target) -> dict[str, int]:
        """A receiver's current packet count from each sender."""
        out = self.guests[target.name].run(
            f"nft -j list counters table inet {trex.counter_table(self.state.id)}"
        )
        items = json.loads(out).get("nftables", [])
        return {
            item["counter"]["name"].removeprefix("from_"): item["counter"]["packets"]
            for item in items
            if "counter" in item
        }

    def verify_receipt(self) -> None:
        """Send a short low-rate burst on every connection; each must arrive."""
        start = time.time() + 30
        end = start + RECEIPT_S
        self.each(
            self.senders(),
            lambda t: self.flood(t, "receipt", [(RECEIPT_PPS, start)], end),
        )
        self.wait(end + 5 - time.time())
        self.each(self.senders(), lambda t: self.wait_workload(t, "receipt"))
        received: dict[str, dict[str, int]] = {}

        def read(target: Target) -> None:
            received[target.name] = self.counters(target)

        self.each(self.targets("receiver"), read)
        short = []
        for c in self.state.plan.connections:
            expected = RECEIPT_PPS * RECEIPT_S * c.share
            got = received.get(c.receiver, {}).get(c.sender, 0)
            if got < RECEIPT_MIN * expected:
                short.append(f"{c.sender} to {c.receiver} ({got} of {expected:.0f})")
        run_state.event(
            self.state.id,
            "receipt",
            pps_per_sender=RECEIPT_PPS,
            seconds=RECEIPT_S,
            received=received,
        )
        if short:
            raise PreparationError(
                "low-rate packets did not arrive: " + ", ".join(short[:10])
            )

    def prefill(self) -> None:
        """Write every volume once before the warm-up and verify it."""
        fio_config = self.state.config.workloads.fio
        assert fio_config
        size = fio_config.volume_size_gib
        budget = size * PREFILL_S_PER_GIB

        def start(target: Target) -> None:
            job = fio.prefill_job(self.devices[target.name], size)
            command = fio.prefill_command(job, f"{self.guest_dir}/prefill")
            self.start_workload(target, "prefill", budget, command)

        def verify(target: Target) -> None:
            self.check_exit(target, "prefill")
            text = self.command(target, f"cat {self.guest_dir}/prefill/prefill.json")
            if not fio.prefill_complete(text, size):
                raise PreparationError(f"the {target.name} prefill did not complete")

        self.each(self.targets(), start)
        deadline = time.monotonic() + budget + 60
        pending = list(self.targets())
        while pending:
            if time.monotonic() > deadline:
                raise PreparationError(f"the prefill took longer than {budget}s")
            self.wait(30)
            running = self.running([(t, "prefill") for t in pending])
            self.each(
                (t for t in pending if (t.name, "prefill") not in running), verify
            )
            pending = [t for t in pending if (t.name, "prefill") in running]
            self.note(f"prefilling the data volumes, {len(pending)} running")

    def measure(self) -> None:
        """Start every workload on the shared timeline and watch it to the end."""
        state, schedule, config = self.state, self.state.schedule, self.state.config
        warm = time.time() + LEAD_S
        start = warm + schedule.warmup_s
        end = start + schedule.duration_s
        state.warmup_start = datetime.fromtimestamp(warm, UTC)
        state.measured_start = datetime.fromtimestamp(start, UTC)
        self.phase("running", "starting workloads")
        run_state.event(
            state.id,
            "timeline",
            warmup_start=warm,
            measured_start=start,
            measured_end=end,
        )
        units: list[tuple[Target | HostTarget, str]] = []
        trex_config, fio_config = config.workloads.trex, config.workloads.fio
        if trex_config:
            pps = [p.pps_per_sender for p in trex_config.profiles]
            steps = [(pps[0], warm)] + [
                (pps[w.profile], start + w.start_s) for w in schedule.trex[1:]
            ]
            self.each(
                self.senders(),
                lambda t: self.flood(t, "flood", steps, end),
            )
            units += [(t, "flood") for t in self.senders()]
        if fio_config:
            first = fio_config.profiles[0]

            def start_fio(target: Target) -> None:
                device = self.devices[target.name]
                jobs: list[dict[str, object]] = [
                    {
                        "name": "warmup",
                        "file": fio.job_file(first, fio_config, device),
                        "numjobs": fio_config.numjobs,
                        "start": warm,
                        "end": start,
                    }
                ]
                for window in schedule.fio:
                    profile = fio_config.profiles[window.profile]
                    jobs.append(
                        {
                            "name": f"{window.profile + 1:02d}-{profile.name}",
                            "file": fio.job_file(profile, fio_config, device),
                            "numjobs": fio_config.numjobs,
                            "start": start + window.start_s,
                            "end": start + window.start_s + window.length_s,
                        }
                    )
                command = fio.run_command(jobs, f"{self.guest_dir}/fio")
                lifetime = int(end - time.time()) + 300
                self.start_workload(target, "fio", lifetime, command)

            self.each(self.targets(), start_fio)
            units += [(t, "fio") for t in self.targets()]
        run_state.event(state.id, "dispatched", late_s=round(time.time() - warm, 1))
        self.note("warm-up")
        self.wait(start - time.time())
        self.check_receivers()
        while time.time() < end:
            self.note(f"measuring, {int(end - time.time())}s left")
            self.wait(min(MONITOR_S, end - time.time()))
            if time.time() < end - 5:
                self.check_running(units, deadline=end)
            self.check_receivers()
        self.note("waiting for the workloads to finish")
        self.each(units, lambda unit: self.wait_workload(*unit))
        settle = max(DRAIN_S, *SAMPLE_INTERVAL_S.values())
        self.wait(max(end + settle - time.time(), 0))
        self.check_receivers()

    def check_receivers(self) -> None:
        """Require every receiver to be counting; its sampler stops on a failed read."""
        if self.state.config.workloads.trex:
            self.each(self.targets("receiver"), self.check_counters)

    def running(
        self, units: list[tuple[Target | HostTarget, str]]
    ) -> set[tuple[str, str]]:
        """The (target, unit) pairs whose unit is still running."""
        found: set[tuple[str, str]] = set()

        def check(unit: tuple[Target | HostTarget, str]) -> None:
            target, name = unit
            unit_name = f"cloud-stress-{self.short}-{name}"
            out = self.command(
                target, f"systemctl is-active {unit_name}.service || true"
            )
            if out.strip() in ("active", "activating", "deactivating"):
                found.add((target.name, name))

        self.each(units, check)
        return found

    def check_running(
        self,
        units: list[tuple[Target | HostTarget, str]],
        *,
        deadline: float,
    ) -> None:
        """Fail for stopped units unless their scheduled deadline has passed."""
        running = self.running(units)
        # Remote status queries may finish after the workload's scheduled end.
        if time.time() >= deadline:
            return
        stopped = [f"{n} in {t.name}" for t, n in units if (t.name, n) not in running]
        if stopped:
            raise RuntimeError(
                "workloads stopped before the end: " + ", ".join(stopped[:10])
            )

    def check_counters(self, target: Target) -> None:
        """Fail the run when a receiver's latest sample lacks fresh counters."""
        out = self.guests[target.name].run(
            f"systemctl is-active --quiet cloud-stress-{self.short}-sampler.service && "
            f"date +%s.%N && tail -n 1 {self.guest_dir}/sampler/samples.jsonl"
        )
        now, _, line = out.strip().partition("\n")
        try:
            sample = json.loads(line)
            age = float(now) - float(sample["t"])
        except (ValueError, KeyError, TypeError):
            raise RuntimeError(f"no receive counter samples in {target.name}") from None
        senders = [
            c.sender for c in self.state.plan.connections if c.receiver == target.name
        ]
        missing = [s for s in senders if f"nft.from_{s}.packets" not in sample]
        if age > 3 * SAMPLE_INTERVAL_S["guest"] + 5 or missing or "nft.error" in sample:
            raise RuntimeError(
                f"receive counters in {target.name} are not being read: "
                + str(sample.get("nft.error") or f"latest sample {age:.0f}s old")
            )

    def own_dir(self, where: str, path: str) -> None:
        """Claim a new directory or reuse one already marked by this run."""
        run_id = self.state.id
        run_state.record(run_id, "dir", path, "intent", where=where)
        outcome = remote.sh(
            self.juju,
            where,
            f"mkdir -p {path.rsplit('/', 1)[0]} && "
            f"if mkdir -m 0700 {path} 2>/dev/null; then echo {run_id} > {path}/.owner; "
            f"elif ! test -d {path}; then exit 1; "
            f'elif test "$(cat {path}/.owner 2>/dev/null)" != {run_id}; '
            "then echo occupied; fi",
        )
        if outcome.strip() == "occupied":
            run_state.record(run_id, "dir", path, "rejected", where=where)
            raise PreparationError(f"{path} on {where} is not owned by this run")
        run_state.record(run_id, "dir", path, "created", where=where)

    def start_host_unit(
        self, where: str, unit: str, lifetime: int, command: str, output: str
    ) -> None:
        """Start a host transient unit, journaled before and after."""
        run_state.record(self.state.id, "unit", unit, "intent", id=output, where=where)
        run_state.event(
            self.state.id, "command", unit=unit, where=where, command=command
        )
        remote.sh(self.juju, where, remote.unit_command(unit, lifetime, command))
        run_state.record(self.state.id, "unit", unit, "created", id=output, where=where)

    def start_workload(
        self,
        target: Target | HostTarget,
        name: str,
        lifetime: int,
        command: str,
        evidence: str | None = "",
    ) -> None:
        """Start a unit and journal where to retrieve its evidence."""
        if isinstance(target, HostTarget):
            kind, root = "host-workload", self.sender_dir(target)
        else:
            kind, root = "guest-unit", self.guest_dir
            out = f"{root}/{name}"
            command = f"mkdir -p {shlex.quote(out)} && {command}"
            if evidence == "":
                evidence = out
        unit = f"cloud-stress-{self.short}-{name}"
        record = {"id": evidence, "where": target.name}
        run_state.record(self.state.id, kind, unit, "intent", **record)
        run_state.event(
            self.state.id, "command", unit=unit, where=target.name, command=command
        )
        rc_path = f"{root}/{name}.rc"
        self.command(target, remote.unit_command(unit, lifetime, command, rc_path))
        run_state.record(self.state.id, kind, unit, "created", **record)

    def wait_workload(
        self, target: Target | HostTarget, name: str, timeout: int = 120
    ) -> None:
        """Wait for a workload to finish and require a recorded zero exit code."""
        self.wait_for(
            lambda: (target.name, name) not in self.running([(target, name)]),
            timeout,
            f"{name} in {target.name}",
        )
        self.check_exit(target, name)

    def check_exit(self, target: Target | HostTarget, name: str) -> None:
        """Read the exit status recorded by the stopped unit."""
        root = (
            self.sender_dir(target)
            if isinstance(target, HostTarget)
            else self.guest_dir
        )
        code = self.command(target, f"cat {root}/{name}.rc").strip()
        if code != "0":
            raise RuntimeError(f"{name} in {target.name} exited with {code}")

    def start_collectors(self) -> None:
        """Start host, guest and Ceph samplers covering the whole run."""
        plan, lifetime = self.state.plan, self.state.schedule.max_elapsed_s
        sources = []
        for host in plan.hosts:
            taps = [
                # Nova names a guest's tap device after its port.
                f"tap{t.port_id[:11]}"
                for t in self.state.targets
                if t.host == host.name and t.port_id
            ]
            args = collect.start_args(host.link, host.hw_offload, taps)
            sources.append((f"host-{host.name}", "host", host.machine, args))
        if self.state.config.workloads.fio and plan.ceph_mon:
            sources.append(("ceph", "ceph", plan.ceph_mon, collect.start_args(None)))
        for name, mode, where, args in sources:
            output = f"{self.host_dir}/{name}.jsonl"
            try:
                if mode == "ceph":
                    self.own_dir(where, self.host_dir)
                remote.push(
                    self.juju, where, f"{self.host_dir}/sampler.py", collect.SAMPLER
                )
                before = time.time()
                remote_now = float(remote.sh(self.juju, where, "date +%s.%N"))
                after = time.time()
                self.start_host_unit(
                    where,
                    f"cloud-stress-{self.short}-{name}",
                    lifetime,
                    f"exec python3 {self.host_dir}/sampler.py {mode} {output} "
                    f"{SAMPLE_INTERVAL_S[mode]} {shlex.quote(args)}",
                    output,
                )
                run_state.event(
                    self.state.id,
                    "collector",
                    name=name,
                    where=where,
                    clock_offset_s=round(remote_now - (before + after) / 2, 3),
                    uncertainty_s=round((after - before) / 2, 3),
                )
            except (
                jubilant.CLIError,
                jubilant.TaskError,
                TimeoutError,
                ValueError,
            ) as error:
                message = " ".join(str(getattr(error, "stderr", "") or error).split())
                run_state.event(
                    self.state.id,
                    "collector-failed",
                    name=name,
                    where=where,
                    message=message[:300],
                )
        table = trex.counter_table(self.state.id)

        def guest_sampler(target: Target) -> None:
            guest = self.guests[target.name]
            path = f"{self.guest_dir}/sampler.py"
            guest.run(
                f"mkdir -p {self.guest_dir} && printf %s "
                f"{shlex.quote(collect.SAMPLER)} > {path}"
            )
            args = collect.start_args(
                self.links.get(target.name),
                nft=table if target.role in ("receiver", "peer") else None,
            )
            self.start_workload(
                target,
                "sampler",
                lifetime,
                f"exec python3 {path} guest {self.guest_dir}/sampler/samples.jsonl "
                f"{SAMPLE_INTERVAL_S['guest']} {shlex.quote(args)}",
            )

        self.each(self.targets(), guest_sampler)

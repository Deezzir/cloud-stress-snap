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
from campaign.model.target import Target
from campaign.transport import remote
from campaign.transport.guest import Guest, guest_for, keygen
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
            futures = [pool.submit(work, item) for item in items]
        for future in futures:
            future.result()

    def targets(self, role: str | None = None) -> list[Target]:
        """The run's VMs, optionally those taking one network role; peers take both."""
        return [
            t for t in self.state.targets if role in (None, t.role) or t.role == "peer"
        ]

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
            self.note("installing TRex in the senders")
            self.each(self.targets("sender"), self.start_trex)
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
        """Create the VMs, then place the access keys on every host."""
        public_key = keygen(self.private / "id_ed25519")
        host_public = keygen(self.private / "host_ed25519")
        host_private = (self.private / "host_ed25519").read_text()
        try:
            self.state.targets = provision.provision(
                self.conn,
                self.state,
                public_key,
                (host_private, host_public),
                self.wait,
            )
        finally:
            run_state.save(self.state)
        for target in self.state.targets:
            run_state.event(self.state.id, "target", **target.model_dump())
            self.guests[target.name] = guest_for(self.juju, self.state, target)
        private = (self.private / "id_ed25519").read_text()
        for host in self.state.plan.hosts:
            self.own_host_dir(host.machine)
        first: dict[str, Guest] = {}
        for guest in self.guests.values():
            first.setdefault(guest.machine, guest)
        self.each(first.values(), lambda g: g.install_keys(private, host_public))
        others = [g for g in self.guests.values() if g not in first.values()]
        self.each(others, lambda g: g.install_keys(None, host_public))

    def ready_guest(self, target: Target) -> None:
        """Wait for SSH and cloud-init; find the data interface and volume."""
        guest = self.guests[target.name]
        self.wait_for(
            guest.namespace_ready,
            BOOT_TIMEOUT_S,
            f"the {guest.namespace} namespace on {target.host}",
        )
        self.wait_for(guest.reachable, BOOT_TIMEOUT_S, f"SSH to {target.name}")
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
            guest.run(f"ip link set {link} up")
            mtu = int(guest.run(f"cat /sys/class/net/{link}/mtu"))
            trex_config = self.state.config.workloads.trex
            assert trex_config
            size = trex.ip_packet_bytes(trex_config, self.state.plan.ip_version)
            if size > mtu:
                raise PreparationError(
                    f"{size}-byte TRex packets exceed the "
                    f"{mtu}-byte MTU of {link} in {target.name}"
                )
        if target.role in ("receiver", "peer"):
            version = guest.run("nft --version || true").strip()
            if not version.startswith("nftables"):
                raise PreparationError(f"nftables is not available in {target.name}")
            self.tool(target.name, "nftables", version)
        fio_config = self.state.config.workloads.fio
        if fio_config:
            version = guest.run("fio --version || true").strip()
            if not version.startswith("fio-"):
                raise PreparationError(f"fio is not available in {target.name}")
            self.tool(target.name, "fio", version)
            device, reason = fio.find_device(guest, target.volume_id or "", fio_config)
            if not device:
                raise PreparationError(f"{target.name}: {reason}")
            self.devices[target.name] = device

    def tool(self, vm: str, name: str, version: str) -> None:
        """Record a tool version used in a guest."""
        run_state.event(self.state.id, "tool", vm=vm, name=name, version=version)

    def start_trex(self, target: Target) -> None:
        """Install TRex in a sender and start its server on the data interface."""
        guest = self.guests[target.name]
        install = trex.install_command()
        run_state.event(self.state.id, "command", where=target.name, command=install)
        try:
            version = guest.run(install, timeout=900).strip()
        except RuntimeError as error:
            raise PreparationError(
                f"installing the {TREX_SNAP} snap in {target.name} failed: {error}"
            ) from None
        self.tool(target.name, "trex", version.splitlines()[-1] if version else "")
        directory = self.trex_dir
        for path, text in (
            (
                "trex_cfg.yaml",
                trex.config(self.links[target.name], target.mac or ""),
            ),
            ("flood.py", trex.flood_script()),
        ):
            if path.endswith(".yaml"):
                run_state.event(
                    self.state.id, "file", where=target.name, path=path, text=text
                )
            guest.run(
                f"mkdir -p {directory} && printf %s {shlex.quote(text)} "
                f"> {directory}/{path}"
            )
        self.start_guest_unit(
            guest,
            "trex",
            self.state.schedule.max_elapsed_s,
            trex.server_command(directory),
            evidence=directory,
        )
        self.wait_for(
            lambda: bool(guest.run(trex.server_ready_command()).strip()),
            120,
            f"TRex server start in {target.name}",
        )

    def add_counters(self, target: Target) -> None:
        """Count benchmark packets from each assigned sender in a receiver."""
        trex_config = self.state.config.workloads.trex
        assert trex_config
        by_name = {t.name: t for t in self.state.targets}
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
        self, target: Target, name: str, steps: list[tuple[int, float]], end: float
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
            self.trex_dir,
            trex_config,
            {"address": target.address or "", "mac": target.mac or ""},
            peers,
            steps,
            end,
            name,
        )
        lifetime = int(end - time.time()) + 120
        self.start_guest_unit(
            self.guests[target.name], name, lifetime, command, evidence=None
        )

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
            self.targets("sender"),
            lambda t: self.flood(t, "receipt", [(RECEIPT_PPS, start)], end),
        )
        self.wait(end + 5 - time.time())
        self.each(self.targets("sender"), lambda t: self.wait_guest_unit(t, "receipt"))
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
            self.start_guest_unit(self.guests[target.name], "prefill", budget, command)

        self.each(self.targets(), start)
        deadline = time.monotonic() + budget + 60
        pending = list(self.targets())
        while pending:
            if time.monotonic() > deadline:
                raise PreparationError(f"the prefill took longer than {budget}s")
            self.wait(30)
            running = self.running([(t, "prefill") for t in pending])
            pending = [t for t in pending if (t.name, "prefill") in running]
            self.note(f"prefilling the data volumes, {len(pending)} running")

        def verify(target: Target) -> None:
            self.wait_guest_unit(target, "prefill")
            text = self.guests[target.name].run(
                f"cat {self.guest_dir}/prefill/prefill.json 2>/dev/null || true"
            )
            if not text.strip() or not fio.prefill_complete(text, size):
                raise PreparationError(f"the {target.name} prefill did not complete")

        self.each(self.targets(), verify)

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
        units: list[tuple[Target, str]] = []
        trex_config, fio_config = config.workloads.trex, config.workloads.fio
        if trex_config:
            pps = [p.pps_per_sender for p in trex_config.profiles]
            steps = [(pps[0], warm)] + [
                (pps[w.profile], start + w.start_s) for w in schedule.trex[1:]
            ]
            self.each(
                self.targets("sender"),
                lambda t: self.flood(t, "flood", steps, end),
            )
            units += [(t, "flood") for t in self.targets("sender")]
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
                self.start_guest_unit(
                    self.guests[target.name], "fio", lifetime, command
                )

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
                self.check_running(units)
            self.check_receivers()
        self.note("waiting for the workloads to finish")
        self.each(units, lambda unit: self.wait_guest_unit(*unit))
        settle = max(DRAIN_S, *SAMPLE_INTERVAL_S.values())
        self.wait(max(end + settle - time.time(), 0))
        self.check_receivers()

    def check_receivers(self) -> None:
        """Require every receiver to be counting; its sampler stops on a failed read."""
        if self.state.config.workloads.trex:
            receivers = self.targets("receiver")
            self.check_running([(t, "sampler") for t in receivers])
            self.each(receivers, self.check_counters)

    def running(self, units: list[tuple[Target, str]]) -> set[tuple[str, str]]:
        """The (VM, unit) pairs whose guest unit is still running."""
        found: set[tuple[str, str]] = set()

        def check(unit: tuple[Target, str]) -> None:
            target, name = unit
            guest = self.guests[target.name]
            if guest.unit_active(f"cloud-stress-{self.short}-{name}"):
                found.add((target.name, name))

        self.each(units, check)
        return found

    def check_running(self, units: list[tuple[Target, str]]) -> None:
        """Fail the run as soon as a required workload unit has stopped."""
        running = self.running(units)
        stopped = [f"{n} in {t.name}" for t, n in units if (t.name, n) not in running]
        if stopped:
            raise RuntimeError(
                "workloads stopped before the end: " + ", ".join(stopped[:10])
            )

    def check_counters(self, target: Target) -> None:
        """Fail the run when a receiver's latest sample lacks fresh counters."""
        out = self.guests[target.name].run(
            f"date +%s.%N; tail -n 1 {self.guest_dir}/sampler/samples.jsonl"
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

    def own_host_dir(self, where: str) -> None:
        """Journal the run's directory on a machine or unit before writing it."""
        run_state.record(self.state.id, "dir", self.host_dir, "created", where=where)

    def start_host_unit(
        self, where: str, unit: str, lifetime: int, command: str, output: str
    ) -> None:
        """Start a host transient unit, journaled before and after."""
        run_state.record(self.state.id, "unit", unit, "intent", id=output, where=where)
        run_state.event(
            self.state.id, "command", unit=unit, where=where, command=command
        )
        remote.start_unit(self.juju, where, unit, lifetime, command)
        run_state.record(self.state.id, "unit", unit, "created", id=output, where=where)

    def start_guest_unit(
        self,
        guest: Guest,
        name: str,
        lifetime: int,
        command: str,
        evidence: str | None = "",
    ) -> None:
        """Start a guest transient unit, journaled with the directory it writes.

        By default the unit writes its own directory under the run's guest
        directory; evidence names another directory, or None for none.
        """
        unit = f"cloud-stress-{self.short}-{name}"
        out = f"{self.guest_dir}/{name}"
        evidence = out if evidence == "" else evidence
        record = {"id": evidence, "where": guest.name}
        run_state.record(self.state.id, "guest-unit", unit, "intent", **record)
        run_state.event(
            self.state.id, "command", unit=unit, where=guest.name, command=command
        )
        guest.start_unit(
            unit,
            lifetime,
            f"mkdir -p {shlex.quote(out)} && {command}",
            f"{out}.rc",
        )
        run_state.record(self.state.id, "guest-unit", unit, "created", **record)

    def wait_guest_unit(self, target: Target, name: str, timeout: int = 120) -> None:
        """Wait for a guest unit to finish and require a zero exit code."""
        guest, unit = self.guests[target.name], f"cloud-stress-{self.short}-{name}"
        self.wait_for(
            lambda: not guest.unit_active(unit), timeout, f"{name} in {target.name}"
        )
        code = guest.exit_code(f"{self.guest_dir}/{name}.rc")
        if code != 0:
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
                self.own_host_dir(where)
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
            self.start_guest_unit(
                guest,
                "sampler",
                lifetime,
                f"exec python3 {path} guest {self.guest_dir}/sampler/samples.jsonl "
                f"{SAMPLE_INTERVAL_S['guest']} {shlex.quote(args)}",
            )

        self.each(self.targets(), guest_sampler)

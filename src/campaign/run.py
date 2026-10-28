"""Execute one run: prepare, calibrate and measure; finalize.py cleans up."""

import shlex
import threading
import time
from collections.abc import Callable

import jubilant
from openstack.connection import Connection

from campaign import collect, fio, provision, remote
from campaign import state as run_state
from campaign.finalize import finalize
from campaign.guest import Guest, keygen
from campaign.profiles import FioCell, uses_fio, uses_trex
from campaign.provision import PreparationError, Target
from campaign.state import RunState
from campaign.trex import Sender

SAMPLE_INTERVAL_S = {"host": 5, "ceph": 10}
RECEIPT_PPS, RECEIPT_S, RECEIPT_MIN = 1000, 15, 0.8
CALIBRATE_S = 60
BOOT_TIMEOUT_S = 600


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
        self.dir = run_state.run_dir(state.id)
        self.private = run_state.private_dir(state.id)
        self.short = state.id[:8]
        self.host_dir = f"/var/lib/cloud-stress/{state.id}"
        self.guest_dir = f"/var/lib/cloud-stress/{state.id}"
        schedule = state.schedule
        self.prepare_deadline = (
            time.monotonic() + schedule.prepare_budget_s + schedule.calibrate_budget_s
        )
        self.sender: Sender | None = None
        self.guest: Guest | None = None
        self.target: Target | None = None
        self.device = ""
        self.guest_link = ""
        self.offered_iops = 0

    def execute(self) -> None:
        """Run the profile end to end; finalization always follows."""
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
            error = f"{type(failure).__name__}: {' '.join(str(failure).split())[:300]}"
        if error:
            run_state.event(self.state.id, "error", message=error)
            self.state.causes.append(error)
        self.state.execution = execution
        finalize(self.state, self.conn, self.juju)

    # Progress and waiting

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
        if self.state.phase in ("preparing", "calibrating"):
            timeout = min(timeout, self.prepare_deadline - time.monotonic())
        deadline = time.monotonic() + timeout
        while not check():
            if time.monotonic() > deadline:
                raise PreparationError(f"{what} timed out after {int(timeout)}s")
            self.wait(5)

    # Preparation

    def prepare(self) -> None:
        """Create owned resources and verify every path before load."""
        state = self.state
        self.phase("preparing", "checking quota and prerequisites")
        self.check_prerequisites()
        self.note("creating the target guest")
        self.target = self.create_target()
        self.note(f"waiting for SSH to {self.target.address}")
        self.guest = self.ready_guest(self.target)
        self.note("starting collectors")
        self.start_collectors()
        self.phase("calibrating")
        if uses_fio(state.profile):
            self.note("prefilling the data volume")
            self.prefill()
        if state.profile == "network-ceph":
            self.note("measuring attainable IOPS")
            self.calibrate_fio()
        if self.sender:
            self.note("starting TRex and checking target receipt")
            self.start_trex()
            self.verify_receipt()

    def check_prerequisites(self) -> None:
        """Quota and sender checks that need no run-owned resources."""
        plan, state = self.state.plan, self.state
        flavor = self.conn.compute.get_flavor(plan.flavor_id or "")
        problems = provision.check_quota(
            self.conn, flavor.vcpus, flavor.ram, state.schedule.volume_gb
        )
        if uses_trex(state.profile):
            self.sender = Sender(self.juju, plan.sender_machine or "", state.id)
            problems += self.sender.preflight()
            run_state.event(state.id, "tool", name="trex", version=self.sender.version)
        if problems:
            raise PreparationError("; ".join(problems))

    def create_target(self) -> Target:
        """Create the run's guest, volume and access keys."""
        public_key = keygen(self.private / "id_ed25519")
        host_public = keygen(self.private / "host_ed25519")
        host_private = (self.private / "host_ed25519").read_text()
        (self.private / "known_hosts").write_text("")
        target = provision.provision(
            self.conn,
            self.state,
            public_key,
            (host_private, host_public),
            uses_trex(self.state.profile),
            self.wait,
        )
        (self.dir / "target.json").write_text(target.model_dump_json(indent=2))
        run_state.event(self.state.id, "target", **target.model_dump())
        guest = Guest(
            target.address, self.private / "id_ed25519", self.private / "known_hosts"
        )
        guest.pin(host_public)
        return target

    def ready_guest(self, target: Target) -> Guest:
        """Wait for SSH and cloud-init; find the data interface and volume."""
        guest = Guest(
            target.address, self.private / "id_ed25519", self.private / "known_hosts"
        )
        self.wait_for(guest.reachable, BOOT_TIMEOUT_S, "SSH to the target guest")
        status = guest.run("cloud-init status --wait >/dev/null; echo $?", timeout=900)
        if status.strip() not in ("0", "2"):
            raise PreparationError(f"cloud-init failed in the guest ({status.strip()})")
        self.guest_link = guest.run(
            "for d in /sys/class/net/*; do "
            f'[ "$(cat $d/address)" = {target.mac} ] && basename $d; done; true'
        ).strip()
        if not self.guest_link:
            raise PreparationError(f"no guest interface has MAC {target.mac}")
        if uses_fio(self.state.profile):
            version = guest.run("fio --version || true").strip()
            if not version.startswith("fio-"):
                raise PreparationError("fio is not available in the guest")
            run_state.event(self.state.id, "tool", name="fio", version=version)
            device, reason = fio.find_device(
                guest, target.volume_id or "", self.state.schedule.volume_gb
            )
            if not device:
                raise PreparationError(reason)
            self.device = device
        counter = f"/sys/class/net/{self.guest_link}/statistics/rx_packets"
        self.start_guest_unit(
            guest,
            "rx",
            self.state.schedule.max_elapsed_s,
            f"while true; do echo $(date +%s.%N) $(cat {counter}); sleep 1; done "
            f"> {self.guest_dir}/rx/rx.log",
        )
        return guest

    def prefill(self) -> None:
        """Write the whole volume once and verify it before measured reads."""
        assert self.guest
        out = f"{self.guest_dir}/prefill"
        self.start_guest_unit(
            self.guest,
            "prefill",
            self.state.schedule.calibrate_budget_s,
            fio.prefill_command(self.device, out),
        )
        self.wait_guest_unit("prefill", self.state.schedule.calibrate_budget_s)
        text = self.guest.run(f"cat {out}/prefill.json 2>/dev/null || true")
        if not text.strip() or not fio.prefill_complete(
            text, self.state.schedule.volume_gb
        ):
            raise PreparationError("the volume prefill did not complete")

    def calibrate_fio(self) -> None:
        """Measure attainable IOPS and fix the offered rate for network-ceph."""
        assert self.guest
        schedule = self.state.schedule
        cell = schedule.fio_cells[0].model_copy(update={"length_s": CALIBRATE_S})
        self.run_fio(cell, None, "calibration")
        text = self.guest.run(f"cat {self.guest_dir}/calibration/{cell.label}.json")
        result = fio.summary(text)
        if not result["complete"]:
            raise PreparationError(f"fio calibration did not complete: {result}")
        measured = float(result["iops"])  # type: ignore[arg-type]
        self.offered_iops = int(measured * (schedule.fio_rate_fraction or 1))
        if self.offered_iops <= 0:
            raise PreparationError("fio calibration completed no I/O")
        run_state.event(
            self.state.id,
            "calibration",
            measured_iops=measured,
            offered_iops=self.offered_iops,
        )

    def start_trex(self) -> None:
        """Start the run's TRex server on the sender's provider link."""
        assert self.sender
        plan = self.state.plan
        machine = plan.sender_machine or ""
        speed = remote.sh(
            self.juju, machine, f"cat /sys/class/net/{plan.sender_link}/speed"
        ).strip()
        if not speed.isdigit() or int(speed) <= 0:
            raise PreparationError(f"cannot read the speed of {plan.sender_link}")
        self.own_host_dir(machine)
        self.sender.stage(plan.sender_link or "", int(speed))
        self.start_host_unit(
            machine,
            self.sender.server_unit,
            self.state.schedule.max_elapsed_s,
            self.sender.server_command(),
            self.sender.log,
        )
        self.wait_for(self.sender.server_ready, 120, "TRex server start")

    def verify_receipt(self) -> None:
        """Send a short low-rate burst and check the guest received it."""
        assert self.guest
        counter = f"/sys/class/net/{self.guest_link}/statistics/rx_packets"
        before = int(self.guest.run(f"cat {counter}"))
        self.flood("receipt", [(RECEIPT_PPS, RECEIPT_S)])
        self.wait(RECEIPT_S + 5)
        received = int(self.guest.run(f"cat {counter}")) - before
        ratio = received / (RECEIPT_PPS * RECEIPT_S)
        run_state.event(
            self.state.id,
            "receipt",
            sent_pps=RECEIPT_PPS,
            seconds=RECEIPT_S,
            received=received,
            ratio=round(ratio, 3),
        )
        if ratio < RECEIPT_MIN:
            raise PreparationError(
                f"the target received {received} of {RECEIPT_PPS * RECEIPT_S} "
                "low-rate packets; check the VLAN, MAC, address and security group"
            )

    # Measurement

    def measure(self) -> None:
        """Run the measured phases of the profile."""
        state, schedule = self.state, self.state.schedule
        self.phase("running")
        state.measured_start = run_state.now()
        run_state.save(state)
        start = time.time()
        if state.profile == "fio-sweep":
            run_state.event(state.id, "measured-phase", phase="sweep")
            for cell in schedule.fio_cells:
                self.note(f"fio {cell.label}")
                run_state.event(state.id, "fio-cell", cell=cell.label)
                self.run_fio(cell, None, cell.label)
            return
        if state.profile == "network-ceph":
            self.start_fio(schedule.fio_cells[0], self.offered_iops, "measured")
        for item in schedule.phases:
            self.wait(start + item.start_s - time.time())
            run_state.event(state.id, "measured-phase", phase=item.name)
            self.note(f"{item.name} phase")
            if item.name == "stress" and self.sender:
                count = len(schedule.trex_steps_pps)
                step_s = item.length_s // count
                steps = [(pps, step_s) for pps in schedule.trex_steps_pps]
                steps[-1] = (steps[-1][0], item.length_s - step_s * (count - 1))
                self.flood("stress", steps)
        self.wait(start + schedule.duration_s - time.time())
        if state.profile == "network-ceph":
            self.wait_guest_unit("measured", 300)

    # Owned units and files, journaled before they start

    def own_host_dir(self, where: str) -> None:
        """Journal the run's directory on a machine or unit before writing it."""
        run_state.record(self.state.id, "dir", self.host_dir, "created", where=where)

    def start_host_unit(
        self, where: str, unit: str, lifetime: int, command: str, output: str
    ) -> None:
        """Start a host transient unit, journaled before and after."""
        run_state.record(self.state.id, "unit", unit, "intent", id=output, where=where)
        remote.start_unit(self.juju, where, unit, lifetime, command)
        run_state.record(self.state.id, "unit", unit, "created", id=output, where=where)

    def start_guest_unit(
        self, guest: Guest, name: str, lifetime: int, command: str
    ) -> None:
        """Start a guest transient unit with its own output directory."""
        unit = f"cloud-stress-{self.short}-{name}"
        out = f"{self.guest_dir}/{name}"
        record = {"id": out, "where": "guest"}
        run_state.record(self.state.id, "guest-unit", unit, "intent", **record)
        guest.start_unit(
            unit, lifetime, f"mkdir -p {shlex.quote(out)} && {command}", f"{out}.rc"
        )
        run_state.record(self.state.id, "guest-unit", unit, "created", **record)

    def wait_guest_unit(self, name: str, timeout: int) -> None:
        """Wait for a guest unit to finish and require a zero exit code."""
        assert self.guest
        guest, unit = self.guest, f"cloud-stress-{self.short}-{name}"
        self.wait_for(lambda: not guest.unit_active(unit), timeout + 60, name)
        code = guest.exit_code(f"{self.guest_dir}/{name}.rc")
        if code != 0:
            raise RuntimeError(f"guest job {name} exited with {code}")

    def start_fio(self, cell: FioCell, rate: int | None, name: str) -> None:
        """Start one fio job in the guest without waiting."""
        assert self.guest
        job = fio.job_file(cell, self.device, self.state.schedule.volume_gb, rate)
        (self.dir / "raw" / f"fio-{name}.fio").write_text(job)
        command = fio.run_command(cell, job, f"{self.guest_dir}/{name}")
        self.start_guest_unit(self.guest, name, cell.length_s + 120, command)

    def run_fio(self, cell: FioCell, rate: int | None, name: str) -> None:
        """Run one fio job in the guest and wait for it."""
        self.start_fio(cell, rate, name)
        self.wait_guest_unit(name, cell.length_s + 120)

    def flood(self, name: str, steps: list[tuple[int, int]]) -> None:
        """Start a TRex flood at the target."""
        assert self.sender and self.target
        unit, command, lifetime, out = self.sender.flood_command(
            name, self.target, self.state.plan.vlan or 0, steps
        )
        self.start_host_unit(self.sender.machine, unit, lifetime, command, out)
        run_state.event(self.state.id, "flood", name=name, steps=steps)

    def start_collectors(self) -> None:
        """Start host and Ceph samplers with a lifetime covering the run."""
        plan = self.state.plan
        sources = [
            (
                "target-host",
                "host",
                plan.target_machine,
                plan.target_link,
                plan.target_hw_offload,
            ),
        ]
        if uses_trex(self.state.profile):
            sources.append(
                ("sender-host", "host", plan.sender_machine, plan.sender_link, False)
            )
        if uses_fio(self.state.profile) and plan.ceph_mon:
            sources.append(("ceph", "ceph", plan.ceph_mon, None, False))
        for name, mode, where, link, offload in sources:
            if not where:
                continue
            output = f"{self.host_dir}/{name}.jsonl"
            try:
                self.own_host_dir(where)
                remote.push(
                    self.juju, where, f"{self.host_dir}/sampler.py", collect.SAMPLER
                )
                before = time.time()
                remote_now = float(remote.sh(self.juju, where, "date +%s.%N"))
                offset = remote_now - (before + time.time()) / 2
                self.start_host_unit(
                    where,
                    f"cloud-stress-{self.short}-{name}",
                    self.state.schedule.max_elapsed_s,
                    f"exec python3 {self.host_dir}/sampler.py {mode} {output} "
                    f"{SAMPLE_INTERVAL_S[mode]} "
                    f"{shlex.quote(collect.start_args(link, offload))}",
                    output,
                )
                run_state.event(
                    self.state.id,
                    "collector",
                    name=name,
                    where=where,
                    clock_offset_s=round(offset, 3),
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

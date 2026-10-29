"""TRex changing-tuple UDP flood on the sender hypervisor's provider bond."""

import json
import shlex
from importlib import resources

import jubilant

from campaign.model.profiles import FLOOD_PORT, FLOOD_SOURCE
from campaign.model.target import Target
from campaign.transport import remote

SNAP = "trex-generator"
CHANNEL = "latest/edge"
PLUGS = ("network-control", "hardware-observe", "process-control", "system-observe")
RPC_PORT = 4501

FILES = resources.files("campaign.workloads") / "files"


class Sender:
    """The run-owned TRex snap, server and flood on one sender machine."""

    def __init__(self, juju: jubilant.Juju, machine: str, run_id: str) -> None:
        """Remember where the run's TRex units and files live."""
        self.juju = juju
        self.machine = machine
        self.dir = f"/var/snap/{SNAP}/common/{run_id}"
        self.server_unit = f"cloud-stress-{run_id[:8]}-trex"
        self.flood_unit = f"cloud-stress-{run_id[:8]}-flood"
        self.log = f"{self.dir}/trex.log"
        self.version = ""

    def preflight(self) -> list[str]:
        """Refuse a TRex snap the run would not own, or a busy TRex server or port."""
        out = remote.sh(
            self.juju,
            self.machine,
            f"snap list {SNAP} >/dev/null 2>&1 && echo installed; "
            f"ss -Hltn 'sport = :{RPC_PORT}' | grep -q . && echo port; "
            "pgrep -x _t-rex-64 >/dev/null && echo process; "
            f"snap info {SNAP} >/dev/null 2>&1 || echo unreachable; true",
        ).split()
        problems = []
        if "installed" in out:
            problems.append(f"the {SNAP} snap is already installed on the sender")
        if "process" in out:
            problems.append("a _t-rex-64 process is already running on the sender")
        if "port" in out:
            problems.append(f"TCP port {RPC_PORT} is already in use on the sender")
        if "unreachable" in out:
            problems.append(f"the sender cannot reach the Snap Store for {SNAP}")
        return problems

    def install(self) -> None:
        """Install the snap, hold refreshes for the run and connect its plugs."""
        connect = "; ".join(f"snap connect {SNAP}:{plug}" for plug in PLUGS)
        remote.sh(
            self.juju,
            self.machine,
            f"set -e; snap install {SNAP} --channel={CHANNEL}; "
            f"snap refresh --hold {SNAP}; {connect}",
            wait=600,
        )
        self.version = remote.sh(
            self.juju,
            self.machine,
            f"snap list {SNAP} | awk 'NR==2 {{print $2 \" rev \" $3}}'",
        ).strip()

    def stage(self, link: str) -> None:
        """Write the TRex config and flood script into the run's directory."""
        config = (FILES / "trex_cfg.yaml").read_text().format(link=link)
        remote.push(self.juju, self.machine, f"{self.dir}/trex_cfg.yaml", config)
        remote.push(
            self.juju,
            self.machine,
            f"{self.dir}/flood.py",
            (FILES / "trex_flood.py").read_text(),
        )

    def server_command(self) -> str:
        """Command for the TRex server on the provider link with a raw socket."""
        return (
            f"exec /snap/bin/{SNAP}.t-rex-64 -i -c 1 --no-scapy-server "
            f"--no-ofed-check --cfg {self.dir}/trex_cfg.yaml >> {self.log} 2>&1"
        )

    def server_ready(self) -> bool:
        """Whether the run's TRex server accepts RPC connections."""
        out = remote.sh(self.juju, self.machine, f"ss -Hltn 'sport = :{RPC_PORT}'")
        return bool(out.strip())

    def flood_command(
        self, name: str, target: Target, vlan: int, steps: list[tuple[int, int]]
    ) -> tuple[str, str, int, str]:
        """Unit, command, lifetime and stats file of a stepped flood."""
        ipv6 = ":" in target.address
        out = f"{self.dir}/{name}.jsonl"
        total = sum(seconds for _, seconds in steps)
        args = {
            "dst": target.address,
            "mac": target.mac,
            "vlan": vlan,
            "ipv6": ipv6,
            "src": FLOOD_SOURCE[6 if ipv6 else 4],
            "dport": FLOOD_PORT,
            "steps": steps,
            "max_s": total + 5,
            "out": out,
        }
        command = (
            f"exec /snap/bin/{SNAP}.python {self.dir}/flood.py "
            f"{shlex.quote(json.dumps(args))} >> {self.log} 2>&1"
        )
        return f"{self.flood_unit}-{name}", command, total + 60, out

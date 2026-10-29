"""TRex changing-tuple UDP flood on the sender hypervisor's provider bond."""

import json
import shlex

import jubilant

from campaign.model.profiles import FLOOD_PORT, FLOOD_SOURCE
from campaign.model.target import Target
from campaign.transport import remote

SNAP = "trex-generator"
CHANNEL = "latest/edge"
PLUGS = ("network-control", "hardware-observe", "process-control", "system-observe")
RPC_PORT = 4501

CONFIG = """\
- version: 2
  interfaces: ["{link}", "dummy"]
  port_info:
    - dest_mac: [0x00,0x00,0x00,0x00,0x00,0x01]
      src_mac:  [0x00,0x00,0x00,0x00,0x00,0x02]
    - dest_mac: [0x00,0x00,0x00,0x00,0x00,0x02]
      src_mac:  [0x00,0x00,0x00,0x00,0x00,0x01]
  port_bandwidth_gb: 10
  port_mtu: 1500
"""

# Runs on the sender with the snap's Python and TRex client library.
# Every packet carries a new source address and UDP source port, so megaflow
# wildcarding cannot fold packets together. Ports are acquired without force:
# a port owned by another session makes the run fail instead of taking it over.
FLOOD = """\
import json, signal, sys, time

from trex.stl.api import *

args = json.loads(sys.argv[1])
out = open(args["out"], "a", buffering=1)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

l3 = IPv6(src=args["src"], dst=args["dst"]) if args["ipv6"] else IP(
    src=args["src"], dst=args["dst"])
packet = Ether(dst=args["mac"]) / Dot1Q(vlan=args["vlan"]) / l3 / UDP(
    sport=1024, dport=args["dport"]) / Raw(load=b"\\x00" * 18)
src_size = 8 if args["ipv6"] else 2
vm = STLScVmRaw([
    STLVmFlowVar(name="src", min_value=1, max_value=256 ** src_size - 1,
                 size=src_size, op="inc"),
    STLVmWrFlowVar(fv_name="src", pkt_offset="IPv6.src" if args["ipv6"] else "IP.src",
                   offset_fixup=8 if args["ipv6"] else 2),
    STLVmFlowVar(name="sport", min_value=1024, max_value=65535, size=2, op="inc"),
    STLVmWrFlowVar(fv_name="sport", pkt_offset="UDP.sport"),
    STLVmFixChecksumHw(l3_offset="IPv6" if args["ipv6"] else "IP", l4_offset="UDP",
                       l4_type=CTRexVmInsFixHwCs.L4_TYPE_UDP),
])
client = STLClient()
client.connect()
try:
    client.acquire(ports=[0])
    client.reset(ports=[0])
    client.add_streams(STLStream(packet=STLPktBuilder(pkt=packet, vm=vm),
                                 mode=STLTXCont()), ports=[0])
    client.clear_stats()
    started = False
    for step, (pps, seconds) in enumerate(args["steps"]):
        if started:
            client.update(ports=[0], mult=f"{pps}pps")
        else:
            client.start(ports=[0], mult=f"{pps}pps", duration=args["max_s"])
            started = True
        end = time.time() + seconds
        while time.time() < end:
            time.sleep(1)
            stats = client.get_stats()[0]
            out.write(json.dumps({
                "t": time.time(), "step": step, "requested_pps": pps,
                "opackets": stats["opackets"], "tx_pps": stats["tx_pps"],
                "oerrors": stats["oerrors"]}) + "\\n")
finally:
    try:
        client.stop(ports=[0])
        client.release(ports=[0])
    finally:
        client.disconnect()
"""


class Sender:
    """The run-owned TRex snap, server and flood on one sender machine."""

    def __init__(self, juju: jubilant.Juju, machine: str, run_id: str) -> None:
        """Remember where the run's TRex units and files live."""
        self.juju = juju
        self.machine = machine
        # A strictly confined snap only reads and writes under its own directories.
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
            f"ss -Hltn 'sport = :{RPC_PORT}' | grep -q . && echo busy; "
            "pgrep -x _t-rex-64 >/dev/null && echo busy; "
            f"snap info {SNAP} >/dev/null 2>&1 || echo unreachable; true",
        ).split()
        problems = []
        if "installed" in out:
            problems.append(f"the {SNAP} snap is already installed on the sender")
        if "busy" in out:
            problems.append("another TRex server is running on the sender")
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
        config = CONFIG.format(link=link)
        remote.push(self.juju, self.machine, f"{self.dir}/trex_cfg.yaml", config)
        remote.push(self.juju, self.machine, f"{self.dir}/flood.py", FLOOD)

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

"""TRex sender configuration, preflight and scheduled traffic."""

import json
import shlex
from importlib import resources

import yaml

from campaign.model.profiles import TrexConfig
from campaign.model.target import HostTarget
from constants import TREX_CHANNEL, TREX_PLUGS, TREX_RPC_PORT, TREX_SNAP
from probe.models import RunPlan

FILES = resources.files("campaign.workloads") / "files"


def install_command() -> str:
    """Install the snap, hold refreshes for the run and connect its plugs."""
    connect = "; ".join(f"snap connect {TREX_SNAP}:{plug}" for plug in TREX_PLUGS)
    return (
        f"set -e; snap install {TREX_SNAP} --channel={TREX_CHANNEL}; "
        f"snap refresh --hold {TREX_SNAP}; {connect}; "
        f"snap list {TREX_SNAP} | awk 'NR==2 {{print $2 \" rev \" $3}}'"
    )


def ip_packet_bytes(trex: TrexConfig, ip_version: int | None) -> int:
    """IP packet size of the TRex traffic: IP and UDP headers and the payload."""
    return (40 if ip_version == 6 else 20) + 8 + trex.udp_payload_bytes


def config(link: str, mac: str, index: int = 0, prefix: str = "cloudstress") -> str:
    """Use the guest's data interface with the AF_PACKET driver's default MTU."""
    octets = ",".join(f"0x{octet}" for octet in mac.split(":"))
    return (
        (FILES / "trex_cfg.yaml")
        .read_text()
        .format(
            link=link,
            mac=octets,
            prefix=f"{prefix}-{index}",
            rpc=TREX_RPC_PORT + 2 * index,
            pub=TREX_RPC_PORT - 1 + 2 * index,
            master=3 * index,
            rx=3 * index + 1,
            traffic=3 * index + 2,
        )
    )


def host_config(
    sender: HostTarget,
    mtu: int,
    rpc_port: int,
    prefix: str,
    index: int = 0,
    socket: int = 0,
) -> str:
    """A host server on explicit CPUs, with isolated RPC and DPDK identities."""
    master, rx, traffic = sender.cpus[3 * index : 3 * index + 3]
    mac = [int(octet, 16) for octet in sender.mac.split(":")]
    item: dict[str, object] = {
        "version": 2,
        "interfaces": [sender.interface, "dummy"],
        "prefix": f"{prefix}-{index}",
        "zmq_pub_port": rpc_port - 1 + 2 * index,
        "zmq_rpc_port": rpc_port + 2 * index,
        "port_info": [
            {"src_mac": mac, "dest_mac": mac.copy()},
            {"src_mac": [0, 0, 0, 0, 0, 1], "dest_mac": [0, 0, 0, 0, 0, 2]},
        ],
        "platform": {
            "master_thread_id": master,
            "latency_thread_id": rx,
            "dual_if": [{"socket": socket, "threads": [traffic]}],
        },
    }
    if sender.backend == "dpdk":
        item.update(
            low_end=True,
            limit_memory=512,
            port_mtu=min(mtu, 1500),
            ext_dpdk_opt=["--huge-dir", "/dev/hugepages", "--huge-unlink=always"],
        )
    return str(yaml.safe_dump([item], sort_keys=False))


def host_rpc_ports(run_id: str, count: int = 8) -> list[int]:
    """Run-specific TCP range first, then the next slots; the host picks a free one."""
    slot = int(run_id.replace("-", "")[:8], 16)
    return [20001 + 128 * ((slot + offset) % 300) for offset in range(count)]


def host_preflight_command(
    sender: HostTarget,
    plan: RunPlan,
    rpc_ports: list[int],
    processes: int,
) -> str:
    """Read-only checks of operator preparation; never bind devices or take ports."""
    args = {
        **sender.model_dump(),
        "mtu": plan.mtu,
        "physnet": plan.physical_network,
        "rpc_ports": rpc_ports,
        "processes": processes,
    }
    script = (FILES / "host_preflight.py").read_text()
    return f"python3 -c {shlex.quote(script)} {shlex.quote(json.dumps(args))}"


def rpc_table(run_id: str) -> str:
    """Exact run-owned nftables table protecting host TRex control ports."""
    return f"cloud_stress_{run_id.replace('-', '')}_rpc"


def rpc_guard_command(table: str, rpc_port: int, processes: int) -> str:
    """Drop non-loopback access to TRex's otherwise unauthenticated TCP servers."""
    ports = ", ".join(str(p) for p in range(rpc_port - 1, rpc_port + 2 * processes - 1))
    rules = (
        f"table inet {table} {{\n"
        " chain input {\n  type filter hook input priority -300; policy accept;\n"
        f'  iifname != "lo" tcp dport {{ {ports} }} drop\n'
        " }\n}\n"
    )
    return f"printf %s {shlex.quote(rules)} | nft -f -"


def server_command(directory: str, backend: str = "af_packet") -> str:
    """Command for the TRex server with one traffic core."""
    app = "native" if backend == "dpdk" else "t-rex-64"
    return (
        f"exec /snap/bin/{TREX_SNAP}.{app} -i -c 1 --no-scapy-server "
        f"--no-ofed-check --cfg {shlex.quote(directory + '/trex_cfg.yaml')} "
        f">> {shlex.quote(directory + '/trex.log')} 2>&1"
    )


def server_ready_command(index: int = 0, rpc_port: int = TREX_RPC_PORT) -> str:
    """Prints a line once the TRex server accepts RPC connections."""
    return f"ss -Hltn 'sport = :{rpc_port + 2 * index}'"


def flood_command(
    directory: str,
    trex: TrexConfig,
    sender: dict[str, str],
    peers: list[dict[str, object]],
    steps: list[tuple[int, float]],
    end: float,
    name: str,
    *,
    vlan: int | None = None,
    rpc_port: int = TREX_RPC_PORT,
) -> str:
    """Flood from the sender to its peers, changing rate at each step's start time.

    steps holds (pps_per_sender, epoch seconds); the flood stops at end.
    """
    args = {
        "src": sender["address"],
        "src_mac": sender["mac"],
        "ipv6": ":" in sender["address"],
        "peers": peers,
        "dport": trex.udp_dst_port,
        "sports": list(trex.udp_src_ports),
        "payload": trex.udp_payload_bytes,
        "steps": steps,
        "end": end,
        "out": f"{directory}/{name}.jsonl",
        "processes": trex.processes,
        "rpc_port": rpc_port,
        "vlan": vlan,
    }
    return (
        f"/snap/bin/{TREX_SNAP}.python {shlex.quote(directory + '/flood.py')} "
        f"{shlex.quote(json.dumps(args))} "
        f">> {shlex.quote(directory + '/trex.log')} 2>&1"
    )


def flood_script() -> str:
    """The flood script copied into the sender guest."""
    return (FILES / "trex_flood.py").read_text()


def counter_table(run_id: str) -> str:
    """Name of the run's nftables table in receiver guests."""
    return f"cloud_stress_{run_id[:8]}"


def counters_command(
    table: str, address: str, dport: int, senders: dict[str, str]
) -> str:
    """Count benchmark packets from each sender, then drop them.

    Rules match the sender and receiver addresses, UDP and the destination port.
    They run at the raw prerouting hook, before connection tracking, so the guest
    neither tracks the flows nor answers with port-unreachable messages.
    senders maps a sender name to its address.
    """
    family = "ip6" if ":" in address else "ip"
    lines = [f"table inet {table} {{"]
    lines += [f"  counter from_{name} {{ }}" for name in senders]
    lines += [
        "  chain prerouting {",
        "    type filter hook prerouting priority raw; policy accept;",
    ]
    lines += [
        f"    {family} saddr {source} {family} daddr {address} udp dport {dport} "
        f'counter name "from_{name}" drop'
        for name, source in senders.items()
    ]
    lines += ["  }", "}", ""]
    return f"printf %s {shlex.quote(chr(10).join(lines))} | nft -f -"

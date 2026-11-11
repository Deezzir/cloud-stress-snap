"""TRex in sender guests: the trex-generator snap, its server and a scheduled flood."""

import json
import shlex
from importlib import resources

from campaign.model.profiles import TrexConfig
from constants import TREX_CHANNEL, TREX_PLUGS, TREX_RPC_PORT, TREX_SNAP

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


def config(link: str, mac: str, mtu: int) -> str:
    """TRex platform config: the guest's data interface through a Linux socket."""
    octets = ",".join(f"0x{octet}" for octet in mac.split(":"))
    return (FILES / "trex_cfg.yaml").read_text().format(link=link, mac=octets, mtu=mtu)


def server_command(directory: str) -> str:
    """Command for the TRex server with one traffic core."""
    return (
        f"exec /snap/bin/{TREX_SNAP}.t-rex-64 -i -c 1 --no-scapy-server "
        f"--no-ofed-check --cfg {directory}/trex_cfg.yaml >> {directory}/trex.log 2>&1"
    )


def server_ready_command() -> str:
    """Prints a line once the TRex server accepts RPC connections."""
    return f"ss -Hltn 'sport = :{TREX_RPC_PORT}'"


def flood_command(
    directory: str,
    trex: TrexConfig,
    sender: dict[str, str],
    peers: list[dict[str, object]],
    steps: list[tuple[int, float]],
    end: float,
    name: str,
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
    }
    return (
        f"/snap/bin/{TREX_SNAP}.python {directory}/flood.py "
        f"{shlex.quote(json.dumps(args))} >> {directory}/trex.log 2>&1"
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

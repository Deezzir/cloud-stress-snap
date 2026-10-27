"""Bounded, read-only host observations through `juju exec` on Juju machines."""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

import jubilant

from probe.models import (
    Disk,
    Host,
    HostProbe,
    InfraResult,
    JujuResult,
    NetLink,
    ProbeNotice,
)

EXEC_TIMEOUT = 180
COMMANDS = {
    "cpu": "env LC_ALL=C lscpu -J",
    "kernel": "uname -r",
    "memory": "cat /proc/meminfo",
    "network": "ip -j -d link",
    "nic-speed": "sh -c 'grep -Hs . /sys/class/net/*/speed || true'",
    "storage": "lsblk -J -b -o NAME,TYPE,SIZE,MODEL,TRAN",
    "pci": "lspci -Dn",
}

log = logging.getLogger(__name__)


def _link(item: dict[str, Any]) -> NetLink | None:
    """Keep physical NICs, bonds, VLANs and Linux bridges; skip virtual ports."""
    info = item.get("linkinfo") or {}
    data = info.get("info_data") or {}
    kind = info.get("info_kind")
    if kind in ("bond", "vlan", "bridge"):
        link_kind = kind
    elif kind is None and item.get("link_type") == "ether":
        link_kind = "physical"
    else:
        return None
    return NetLink(
        name=str(item["ifname"]),
        kind=link_kind,
        mtu=item.get("mtu"),
        state=item.get("operstate"),
        master=item.get("master"),
        bond_mode=data.get("mode") if kind == "bond" else None,
        vlan_id=data.get("id") if kind == "vlan" else None,
        parent=item.get("link") if kind == "vlan" else None,
    )


def _apply(probe: HostProbe, source: str, stdout: str) -> None:
    """Keep selected hardware fields, never unrestricted native command output."""
    if len(stdout) > 1024 * 1024:
        raise ValueError("Host probe result is too large")
    if source == "cpu":
        values = {
            str(field["field"]).rstrip(":"): field.get("data")
            for field in json.loads(stdout)["lscpu"]
        }
        probe.architecture = values.get("Architecture")
        probe.cpu_count = int(values["CPU(s)"]) if values.get("CPU(s)") else None
        probe.numa_nodes = (
            int(values["NUMA node(s)"]) if values.get("NUMA node(s)") else None
        )
    elif source == "kernel":
        probe.kernel = stdout.strip()[:128]
    elif source == "memory":
        value = re.search(r"^MemTotal:\s+(\d+) kB$", stdout, re.MULTILINE)
        if not value:
            raise ValueError("Unsupported memory output")
        probe.memory_bytes = int(value[1]) * 1024
    elif source == "network":
        probe.links = [
            link for item in json.loads(stdout)[:1024] if (link := _link(item))
        ][:256]
    elif source == "nic-speed":
        speeds = {
            match[1]: int(match[2])
            for line in stdout.splitlines()[:1024]
            if (match := re.fullmatch(r"/sys/class/net/([^/]+)/speed:(\d+)", line))
        }
        for link in probe.links:
            link.speed_mbps = speeds.get(link.name) or None
    elif source == "storage":
        devices = json.loads(stdout)["blockdevices"]
        probe.disks = [
            Disk(
                name=str(device["name"]),
                size_bytes=int(device["size"]) if device.get("size") else None,
                model=device.get("model"),
                transport=device.get("tran"),
            )
            for device in devices[:256]
            if device.get("type") == "disk"
        ]
    elif source == "pci":
        probe.pci_devices = [
            f"{fields[0]} {fields[2]}"
            for line in stdout.splitlines()[:512]
            if (fields := line.split())
            and len(fields) >= 3
            and re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", fields[0])
            and re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{4}", fields[2])
        ]


MARKER = "@@cloud-stress"
SCRIPT = "\n".join(
    f"echo '{MARKER} {source}'; {command} 2>/dev/null; echo \"{MARKER} rc $?\""
    for source, command in COMMANDS.items()
)


def _sections(stdout: str) -> dict[str, tuple[int, str]]:
    """Split the batched script output into (exit code, stdout) per command."""
    return {
        source: (int(code), output)
        for source, output, code in re.findall(
            rf"^{MARKER} (\S+)\n(.*?)^{MARKER} rc (\d+)$",
            stdout,
            re.MULTILINE | re.DOTALL,
        )
        if source in COMMANDS
    }


def probe_infra(
    model: str | None, juju: JujuResult, hosts: list[Host]
) -> tuple[InfraResult, list[ProbeNotice]]:
    """Probe Nova-listed hosts through their verified Juju machines."""
    if model is None or not hosts:
        return InfraResult(host_probes=[]), [
            ProbeNotice(
                source="host-probes",
                reason="not-queried",
                message="Host hardware probes were skipped; add --juju-model "
                "to probe discovered hosts."
                if hosts
                else "No compute hosts were discovered to probe.",
            )
        ]
    machines = {
        machine.hypervisor_id: machine.id
        for machine in juju.machines
        if machine.hypervisor_id and machine.parent_id is None
    }
    labels = {host.id: (host.hostname or host.id).split(".")[0] for host in hosts}
    notices = [
        ProbeNotice(
            source=f"host-probe:{host.id}",
            reason="unresolved-identity",
            message=f"{labels[host.id]}: no matching Juju machine; not probed.",
        )
        for host in hosts
        if host.id not in machines
    ]
    selected = [host.id for host in hosts if host.id in machines]
    if not selected:
        return InfraResult(host_probes=[]), notices
    client = jubilant.Juju(model=model)

    def probe(host_id: str) -> tuple[HostProbe, list[ProbeNotice]]:
        label, machine = labels[host_id], machines[host_id]
        result = HostProbe(host_id=host_id, status="unreachable")
        found: list[ProbeNotice] = []
        log.debug("Juju: exec on machine %s (%s): host probe script", machine, label)
        try:
            stdout = client.exec(SCRIPT, machine=machine, wait=EXEC_TIMEOUT).stdout
        except (jubilant.CLIError, jubilant.TaskError, TimeoutError) as error:
            detail = " ".join(str(getattr(error, "stderr", "") or error).split())
            log.debug("%s: host probe failed: %s", label, detail[:300])
            timed_out = isinstance(error, TimeoutError)
            return result, [
                ProbeNotice(
                    source=f"host-probe:{host_id}",
                    reason="transport-unreachable",
                    message=f"{label}: juju exec timed out after {EXEC_TIMEOUT}s; "
                    "the machine agent may be busy or down."
                    if timed_out
                    else f"{label}: juju exec failed: {detail[:200] or 'no details'}.",
                )
            ]
        sections = _sections(stdout)
        for source, command in COMMANDS.items():
            reason: Literal["unsupported-version", "missing-tool", "unavailable"]
            code, output = sections.get(source, (-1, ""))
            if code == 0:
                try:
                    _apply(result, source, output)
                    continue
                except (KeyError, ValueError, TypeError, IndexError):
                    reason, message = (
                        "unsupported-version",
                        f"{label}: could not parse the output of '{command}'.",
                    )
            elif code == 127:
                reason, message = (
                    "missing-tool",
                    f"{label}: '{command}' is not installed.",
                )
            elif code < 0:
                reason, message = "unavailable", f"{label}: no result for '{command}'."
            else:
                reason, message = (
                    "unavailable",
                    f"{label}: '{command}' failed (exit {code}).",
                )
            found.append(
                ProbeNotice(
                    source=f"{source}:{host_id}", reason=reason, message=message
                )
            )
        result.status = "partial" if found else "queried"
        return result, found

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(probe, selected))
    for _, found in outcomes:
        notices.extend(found)
    return InfraResult(host_probes=[result for result, _ in outcomes]), notices

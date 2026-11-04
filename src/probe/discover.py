"""Cloud discovery and correlation of OpenStack, Juju and MAAS records."""

import logging
import os

import click

from probe.models import (
    CheckResult,
    Host,
    HostProbe,
    JujuResult,
    MaasInterface,
    MaasResult,
    OpenstackResult,
    ProbeNotice,
)
from probe.openstack_probe import (
    CACERT_UNREADABLE,
    configured_clouds,
    probe_openstack,
    resolve_cloud,
)

# Outer IPv4 header (20) plus OVN's 38-byte Geneve header allowance.
GENEVE_OVERHEAD = 58

log = logging.getLogger(__name__)


def get_cloud_name() -> str:
    """The single cloud visible from OS_* variables or clouds.yaml."""
    if os.environ.get("OS_CLOUD"):
        return os.environ["OS_CLOUD"]
    if os.environ.get("OS_AUTH_URL"):
        return "envvars"
    names = configured_clouds()
    if len(names) == 1:
        return names[0]
    if not names:
        raise click.UsageError("No OS_* credentials or clouds.yaml found")
    raise click.UsageError(
        f"Multiple clouds in clouds.yaml ({', '.join(names)}); set OS_CLOUD"
    )


def discover_cloud() -> tuple[str, OpenstackResult, list[ProbeNotice]]:
    """Resolve the cloud and collect inventory without exposing access details."""
    try:
        cloud = get_cloud_name()
        selected = resolve_cloud(cloud)
        log.debug(
            "OpenStack: cloud %r, auth URL %s, region %s",
            cloud,
            selected.config.get("auth", {}).get("auth_url"),
            selected.region_name or "default",
        )
        openstack, notices = probe_openstack(selected)
        return cloud, openstack, notices
    except click.ClickException:
        raise
    except ValueError as error:
        log.debug("OpenStack configuration rejected: %s", error)
        if str(error) in (
            "TLS certificate verification must remain enabled",
            "Password authentication requires an HTTPS auth URL",
            CACERT_UNREADABLE,
        ):
            raise click.UsageError(str(error)) from None
        raise click.UsageError("Invalid OpenStack access configuration") from None
    except Exception as error:
        log.debug("OpenStack discovery failed: %s: %s", type(error).__name__, error)
        raise click.UsageError("OpenStack access or discovery failed") from None


def short_name(name: str | None) -> str:
    """Return the first DNS label of a hostname."""
    return name.split(".")[0] if name else "-"


def link_juju_to_hypervisors(
    juju: JujuResult, maas: MaasResult, hosts: list[Host]
) -> list[ProbeNotice]:
    """Match Juju machines to Nova hypervisors by address or hostname.

    MAAS records are preferred; without MAAS, Juju's own machine facts are used.
    """
    by_ip: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for host in hosts:
        if host.host_ip:
            by_ip.setdefault(host.host_ip, []).append(host.id)
        if host.hostname:
            by_name.setdefault(short_name(host.hostname), []).append(host.id)
    records = {record.system_id: record for record in maas.machines}
    for machine in juju.machines:
        if machine.parent_id:
            continue
        record = records.get(machine.instance_id or "")
        addresses = record.ip_addresses if record else machine.ip_addresses
        hostname = short_name(record.hostname if record else machine.hostname or "")
        matches = {
            host_id
            for ip in addresses
            if len(by_ip.get(ip, [])) == 1
            for host_id in by_ip[ip]
        }
        if not matches and hostname and len(by_name.get(hostname, [])) == 1:
            matches = set(by_name[hostname])
        if len(matches) == 1:
            machine.hypervisor_id = matches.pop()
    compute_machines = {
        unit.parent_machine_id
        for unit in juju.units
        if unit.name.startswith("nova-compute/")
    }
    return [
        ProbeNotice(
            source=f"juju.machine:{machine.id}",
            reason="unresolved-identity",
            message=f"Juju machine {machine.id} runs nova-compute but matches "
            "no Nova hypervisor.",
        )
        for machine in juju.machines
        if machine.id in compute_machines and machine.hypervisor_id is None
    ]


def traffic_spaces(juju: JujuResult) -> dict[str, str]:
    """Map Ceph and overlay traffic roles to the Juju spaces bound to them."""
    roles = {
        ("ceph-mon", "public"): "ceph-public",
        ("ceph-osd", "public"): "ceph-public",
        ("ceph-osd", "cluster"): "ceph-cluster",
        ("ovn-chassis", "data"): "overlay",
        ("ovn-dedicated-chassis", "data"): "overlay",
    }
    return {
        roles[(app.charm, endpoint)]: space
        for app in juju.apps
        for endpoint, space in app.bindings.items()
        if (app.charm, endpoint) in roles and space
    }


def root_link(name: str, interfaces: dict[str, MaasInterface]) -> str:
    """Follow VLAN and bridge parents down to the carrying bond or NIC."""
    seen = set()
    while (
        (iface := interfaces.get(name))
        and iface.type in ("vlan", "bridge")
        and iface.parents
        and name not in seen
    ):
        seen.add(name)
        name = iface.parents[0]
    return name


def machine_probes(result: CheckResult) -> dict[str, HostProbe]:
    """Map MAAS system IDs to host probes through verified Juju machines."""
    probes = {probe.host_id: probe for probe in result.infra.host_probes}
    return {
        str(machine.instance_id): probes[machine.hypervisor_id]
        for machine in result.juju.machines
        if machine.instance_id and machine.hypervisor_id in probes
    }


def datapath_notices(result: CheckResult) -> list[ProbeNotice]:
    """Flag MTU and bond problems on the links that carry tenant and Ceph traffic."""
    notices = []
    if not any(machine.interfaces for machine in result.maas.machines):
        notices.append(
            ProbeNotice(
                source="datapath",
                reason="not-queried",
                message="Traffic was not mapped to physical links; add "
                "--juju-model and --maas-url.",
            )
        )
    mtus: dict[str, set[int]] = {}
    for machine in result.maas.machines:
        for iface in machine.interfaces:
            for space in iface.spaces:
                if iface.mtu:
                    mtus.setdefault(space, set()).add(iface.mtu)
    notices.extend(
        ProbeNotice(
            source=f"datapath.mtu:{space}",
            reason="unavailable",
            message=f"Space {space} uses different MTUs across machines "
            f"({', '.join(map(str, sorted(values)))}); large packets will be "
            "dropped or fragmented.",
        )
        for space, values in sorted(mtus.items())
        if len(values) > 1
    )
    overlay = traffic_spaces(result.juju).get("overlay")
    networks = result.openstack.networks
    if overlay and overlay in mtus and networks:
        if all(network.network_type is None for network in networks):
            notices.append(
                ProbeNotice(
                    source="datapath.geneve",
                    reason="permission-denied",
                    message="Neutron network types are hidden from this identity; "
                    "Geneve MTU headroom was not checked.",
                )
            )
        else:
            tenant = max(
                (n.mtu for n in networks if n.network_type == "geneve" and n.mtu),
                default=None,
            )
            physical = min(mtus[overlay])
            if tenant and tenant + GENEVE_OVERHEAD > physical:
                notices.append(
                    ProbeNotice(
                        source="datapath.geneve",
                        reason="unavailable",
                        message=f"Geneve networks use MTU {tenant}, but the overlay "
                        f"space {overlay} has MTU {physical}; it needs at least "
                        f"{tenant + GENEVE_OVERHEAD}.",
                    )
                )
    hostnames = {host.id: short_name(host.hostname) for host in result.openstack.hosts}
    for probe in result.infra.host_probes:
        for bond in (link for link in probe.links if link.kind == "bond"):
            members = [link for link in probe.links if link.master == bond.name]
            label = f"{hostnames.get(probe.host_id, probe.host_id)} {bond.name}"
            down = [link.name for link in members if link.state != "UP"]
            if down:
                notices.append(
                    ProbeNotice(
                        source=f"datapath.bond:{probe.host_id}:{bond.name}",
                        reason="unavailable",
                        message=f"{label}: down member(s) {', '.join(down)}; the bond "
                        "runs with reduced capacity.",
                    )
                )
            speeds = {link.speed_mbps for link in members if link.speed_mbps}
            if len(speeds) > 1:
                notices.append(
                    ProbeNotice(
                        source=f"datapath.bond:{probe.host_id}:{bond.name}",
                        reason="unavailable",
                        message=f"{label}: members run at different speeds "
                        f"({', '.join(format_speed(s) for s in sorted(speeds))}).",
                    )
                )
    return notices


def format_speed(mbps: int | None) -> str:
    """Format a link speed in Mb/s as G or M, or "?" when unknown."""
    if not mbps:
        return "?"
    return f"{mbps // 1000}G" if mbps >= 1000 else f"{mbps}M"


def nova_state_notices(result: CheckResult) -> list[ProbeNotice]:
    """Flag hosts that answer probes while Nova cannot schedule on them."""
    probed = {
        probe.host_id
        for probe in result.infra.host_probes
        if probe.status in ("queried", "partial")
    }
    return [
        ProbeNotice(
            source=f"nova.hypervisor:{host.id}",
            reason="unavailable",
            message=f"{short_name(host.hostname)}: host is up, but Nova reports it "
            f"{'down' if host.state == 'down' else 'disabled'}; no instances will "
            "be scheduled there. Check nova-compute on the host.",
        )
        for host in result.openstack.hosts
        if host.id in probed and (host.state == "down" or host.status == "disabled")
    ]

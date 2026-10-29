"""Discovery, correlation, and output helpers for the cloud-stress commands."""

import logging
import os
import sys

import click

from probe.models import (
    CephOsd,
    CephResult,
    CheckResult,
    Host,
    HostProbe,
    JujuResult,
    MaasInterface,
    MaasResult,
    OpenstackResult,
    ProbeNotice,
    Profile,
    ProfileReadiness,
    RunPlan,
)
from probe.openstack_probe import (
    configured_clouds,
    probe_openstack,
    resolve_cloud,
)

# Outer IPv4 header (20) plus OVN's 38-byte Geneve header allowance.
GENEVE_OVERHEAD = 58

log = logging.getLogger(__name__)


def get_cloud_name() -> str:
    """Determine the cloud name from environment variables or user input."""
    if os.environ.get("OS_CLOUD"):
        return os.environ["OS_CLOUD"]
    if os.environ.get("OS_AUTH_URL"):
        return "envvars"
    names = configured_clouds()
    if len(names) == 1:
        return names[0]
    if not names:
        raise click.UsageError("No OS_* credentials or clouds.yaml found")
    if not sys.stdin.isatty():
        raise click.UsageError("Multiple clouds found; specify --cloud")
    return str(click.prompt("Cloud", type=click.Choice(names)))


def discover_cloud(
    cloud: str | None,
) -> tuple[str, OpenstackResult, list[ProbeNotice]]:
    """Resolve the cloud and collect inventory without exposing access details."""
    try:
        cloud = cloud or get_cloud_name()
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


def _root_link(name: str, interfaces: dict[str, MaasInterface]) -> str:
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


def _machine_probes(result: CheckResult) -> dict[str, HostProbe]:
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
                        f"({', '.join(_speed(s) for s in sorted(speeds))}).",
                    )
                )
    return notices


def _speed(mbps: int | None) -> str:
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


def _bond_health(probe: HostProbe, name: str) -> tuple[bool, int] | None:
    """Return (all members up, total member speed) for a host link, if present."""
    links = {link.name: link for link in probe.links}
    link = links.get(name)
    if link is None:
        return None
    members = [item for item in probe.links if item.master == name] or [link]
    return (
        all(item.state == "UP" for item in members),
        sum(item.speed_mbps or 0 for item in members),
    )


PLAN_CHOICES = (
    "sender",
    "target",
    "network",
    "ip_version",
    "mgmt_network",
    "image",
    "flavor",
    "volume_type",
)


def resolve_plan(
    result: CheckResult, overrides: dict[str, str] | None = None
) -> tuple[RunPlan, dict[str, str]]:
    """Choose run placement from live facts; return the plan and missing choices.

    An override names the sender or target host, the network, IP version or
    management network, or the image, flavor or volume type; it must still pass the
    same checks as an automatic choice.
    """
    pick = overrides or {}
    plan, missing = RunPlan(), {}
    probes = {
        probe.host_id: probe
        for probe in result.infra.host_probes
        if probe.status in ("queried", "partial")
    }
    usable = {
        host.id: short_name(host.hostname)
        for host in result.openstack.hosts
        if host.id in probes
        and host.state == "up"
        and host.status != "disabled"
        and probes[host.id].architecture == "x86_64"
    }
    charms = {app.name: app.charm for app in result.juju.apps}
    hostnames = {host.id: host.hostname or host.id for host in result.openstack.hosts}
    machines = {
        machine.hypervisor_id: machine.id
        for machine in result.juju.machines
        if machine.hypervisor_id and machine.parent_id is None
    }
    mon = next((name for name, charm in charms.items() if charm == "ceph-mon"), None)
    plan.ceph_mon = f"{mon}/leader" if mon else None
    osd_hosts = {short_name(osd.host) for osd in result.ceph.osds if osd.status == "up"}
    physnets = {ovs.host_id: ovs.physnets for ovs in result.ovs}

    networks = [
        network
        for network in result.openstack.networks
        if network.network_type == "vlan"
        and any(network.physical_network in p for p in physnets.values())
        and pick.get("network") in (None, network.name, network.id)
    ]
    version = int(pick["ip_version"]) if "ip_version" in pick else None
    network = min(
        networks,
        key=lambda n: (
            (version or 6) not in n.ip_versions,
            n.name or n.id,
        ),
        default=None,
    )
    if network is None:
        missing["network"] = (
            f"{pick['network']} is not a VLAN network on the OVN bridge mappings"
            if "network" in pick
            else "no VLAN provider network on the OVN bridge mappings"
        )
    else:
        plan.network, plan.vlan = network.name or network.id, network.segmentation_id
        plan.network_id = network.id
        plan.ip_version = 6 if 6 in network.ip_versions else 4
        if not network.ip_versions:
            plan.ip_version = None
            missing["network"] = f"{plan.network} has no subnet"
        elif version:
            plan.ip_version = 4 if version == 4 else 6
            if version not in network.ip_versions:
                missing["network"] = f"{plan.network} has no IPv{version} subnet"

    # Guests are managed over an overlay network, through its OVN metadata
    # namespace on the target host. Octavia's lb-mgmt-net is never used.
    mgmt = min(
        (
            n
            for n in result.openstack.networks
            if n.network_type in ("geneve", "vxlan")
            and 4 in n.ip_versions
            and n.name != "lb-mgmt-net"
            and pick.get("mgmt_network") in (None, n.name, n.id)
        ),
        key=lambda n: n.name or n.id,
        default=None,
    )
    if mgmt:
        plan.mgmt_network, plan.mgmt_network_id = mgmt.name or mgmt.id, mgmt.id
    else:
        missing["mgmt_network"] = (
            f"{pick['mgmt_network']} is not an overlay network with an IPv4 subnet"
            if "mgmt_network" in pick
            else "no overlay network with an IPv4 subnet for guest management"
        )

    # Healthiest provider link first: all bond members up, then total speed.
    ranked = []
    if network:
        for host_id, name in usable.items():
            iface = physnets.get(host_id, {}).get(network.physical_network or "")
            health = _bond_health(probes[host_id], iface) if iface else None
            if iface and health:
                ranked.append((not health[0], -health[1], name, host_id, iface))
    ranked.sort()
    targets = [
        item
        for item in ranked
        if item[2] in osd_hosts and pick.get("target") in (None, item[2])
    ]
    if targets:
        plan.target, plan.target_link = targets[0][2], targets[0][4]
        plan.target_hypervisor = hostnames[targets[0][3]]
        plan.target_machine = machines.get(targets[0][3])
        ovs = next((o for o in result.ovs if o.host_id == targets[0][3]), None)
        if ovs:
            plan.target_ovs_unit, plan.target_hw_offload = ovs.unit, ovs.hw_offload
            plan.target_datapath = ovs.bridges.get("br-int")
    elif "target" in pick:
        missing["target"] = (
            f"{pick['target']} is not a usable x86_64 hypervisor with Ceph OSDs up "
            "on the provider network"
        )
    else:
        missing["target"] = "no usable x86_64 hypervisor with Ceph OSDs up"
    senders = [
        item
        for item in ranked
        if item[2] != plan.target and pick.get("sender") in (None, item[2])
    ]
    if senders:
        plan.sender, plan.sender_link = senders[0][2], senders[0][4]
        plan.sender_machine = machines.get(senders[0][3])
    elif "sender" in pick:
        missing["sender"] = (
            f"{pick['sender']} is not a usable hypervisor carrying the provider "
            "network, or it is the target"
        )
    elif network:
        missing["sender"] = (
            f"no second usable hypervisor carrying {network.physical_network}"
        )

    images = [
        image
        for image in result.openstack.images
        if "ubuntu" in f"{image.os_distro} {image.name}".lower()
        and "amphora" not in (image.name or "").lower()
        # Nova accepts only its own architecture names, e.g. not "amd64v3".
        and image.architecture in (None, "x86_64")
        and pick.get("image") in (None, image.name, image.id)
    ]
    image = max(images, key=lambda i: i.created_at or "", default=None)
    if image:
        plan.image, plan.image_id = image.name or image.id, image.id
    else:
        missing["image"] = (
            f"{pick['image']} is not an x86_64 Ubuntu image"
            if "image" in pick
            else "no x86_64 Ubuntu image"
        )
    flavor = min(
        (
            f
            for f in result.openstack.flavors
            if not f.restricted
            and f.vcpus >= 2
            and f.ram_mb >= 4096
            and f.disk_gb >= 20
            and pick.get("flavor") in (None, f.name, f.id)
        ),
        key=lambda f: (f.vcpus, f.ram_mb, f.disk_gb),
        default=None,
    )
    if flavor:
        plan.flavor, plan.flavor_id = flavor.name, flavor.id
    else:
        missing["flavor"] = (
            f"{pick['flavor']} is restricted or smaller than 2 vCPUs, 4 GiB RAM "
            "and 20 GB disk"
            if "flavor" in pick
            else "no flavor with 2 vCPUs, 4 GiB RAM and 20 GB disk"
        )
    ceph_backends = {name for name, charm in charms.items() if charm == "cinder-ceph"}
    volume_type = next(
        (t for t in result.openstack.volume_types if t.backend in ceph_backends),
        None,
    ) or next(
        (t for t in result.openstack.volume_types if "ceph" in t.name.lower()), None
    )
    backends = {charm for charm in charms.values() if charm.startswith("cinder-")} - {
        "cinder-backup"
    }
    if "volume_type" in pick:
        named = next(
            (
                t
                for t in result.openstack.volume_types
                if pick["volume_type"] in (t.name, t.id)
            ),
            None,
        )
        if named and (named.backend in ceph_backends or "ceph" in named.name.lower()):
            plan.volume_type = named.name
        else:
            missing["volume_type"] = (
                f"{pick['volume_type']} is not a Ceph-backed volume type"
            )
    elif volume_type:
        plan.volume_type = volume_type.name
    elif backends == {"cinder-ceph"}:
        plan.volume_type = "default"
    else:
        missing["volume_type"] = "no Ceph volume type"
    return plan, missing


PROFILE_NEEDS: dict[Profile, tuple[str, ...]] = {
    "flow-churn": ("network", "mgmt_network", "target", "sender", "image", "flavor"),
    "fio-sweep": (
        "network",
        "mgmt_network",
        "target",
        "image",
        "flavor",
        "volume_type",
    ),
    "network-ceph": (
        "network",
        "mgmt_network",
        "target",
        "sender",
        "image",
        "flavor",
        "volume_type",
    ),
}


def profile_readiness(
    result: CheckResult, missing: dict[str, str]
) -> list[ProfileReadiness]:
    """Gate each profile on its placement choices and on Ceph health."""
    ceph = result.ceph
    ceph_blockers = []
    checks = {check.split(":")[0] for check in ceph.health_checks}
    if ceph.health is None:
        ceph_blockers.append("Ceph health is unknown")
    elif ceph.health == "HEALTH_ERR":
        ceph_blockers.append("Ceph is HEALTH_ERR")
    down = sum(osd.status not in (None, "up") for osd in ceph.osds)
    if down:
        ceph_blockers.append(f"{down} OSDs down")
    if "SLOW_OPS" in checks:
        ceph_blockers.append("Ceph has slow operations")
    unclean = sum(
        count for state, count in ceph.pg_states.items() if state != "active+clean"
    )
    if unclean:
        ceph_blockers.append(f"{unclean} PGs not active+clean")
    readiness = []
    for profile, needs in PROFILE_NEEDS.items():
        found = [missing[need] for need in needs if need in missing]
        if profile != "flow-churn":
            found += ceph_blockers
        readiness.append(
            ProfileReadiness(profile=profile, ready=not found, blockers=found)
        )
    return readiness


def print_ovs(result: CheckResult) -> None:
    """Print OVS bridge datapath types and hw-offload per hypervisor."""
    read = [ovs for ovs in result.ovs if ovs.bridges]
    if not read:
        return
    hostnames = {host.id: short_name(host.hostname) for host in result.openstack.hosts}
    click.echo("\nOVS:")
    rows = [["HOST", "HW OFFLOAD", "BRIDGES"]]
    for ovs in sorted(read, key=lambda o: hostnames.get(o.host_id, o.host_id)):
        rows.append(
            [
                hostnames.get(ovs.host_id, ovs.host_id),
                {True: "on", False: "off", None: "?"}[ovs.hw_offload],
                ", ".join(
                    name + (f" ({kind})" if kind != "system" else "")
                    for name, kind in sorted(ovs.bridges.items())
                ),
            ]
        )
    print_table(rows)


def print_readiness(readiness: list[ProfileReadiness], plan: RunPlan) -> None:
    """Print each profile's status and the placement a run would use."""
    if not readiness:
        return
    click.echo("\nProfiles:")
    rows = [["PROFILE", "STATUS"]]
    for item in readiness:
        status = "ready" if item.ready else "blocked: " + ", ".join(item.blockers)
        rows.append([item.profile, status])
    print_table(rows)
    if any(item.ready for item in readiness):
        click.echo("\nRun plan:")
        print_plan(plan)


def print_plan(plan: RunPlan) -> None:
    """Print the placement of a run."""
    network = plan.network
    if network:
        network += f", VLAN {plan.vlan}" if plan.vlan else ""
        network += f", IPv{plan.ip_version}" if plan.ip_version else ""
    print_table(
        [
            ["Sender", f"{plan.sender} ({plan.sender_link})" if plan.sender else "-"],
            ["Target", plan.target or "-"],
            ["Network", network or "-"],
            ["Management", plan.mgmt_network or "-"],
            ["Image", plan.image or "-"],
            ["Flavor", plan.flavor or "-"],
            ["Volume type", plan.volume_type or "-"],
        ]
    )


def print_table(rows: list[list[str]]) -> None:
    """Print rows as left-aligned columns."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        cells = (c.ljust(w) for c, w in zip(row, widths, strict=True))
        click.echo("  " + "  ".join(cells).rstrip())


def print_hypervisors(result: CheckResult) -> None:
    """Print one row per Nova hypervisor with its state and probe results."""
    probes = {probe.host_id: probe for probe in result.infra.host_probes}
    juju_ids = {
        machine.hypervisor_id: machine.id
        for machine in result.juju.machines
        if machine.hypervisor_id
    }
    probed = sum(probe.status in ("queried", "partial") for probe in probes.values())
    hosts = result.openstack.hosts
    click.echo(f"\nHypervisors ({probed} of {len(hosts)} probed):")
    rows = [
        ["HOST", "ADDRESS", "NOVA", "JUJU", "VCPUS", "RAM", "NUMA", "KERNEL", "PROBE"]
    ]
    for host in hosts:
        probe = probes.get(host.id)
        nova = host.state or "?"
        if host.status == "disabled":
            nova += ", disabled"
        probe_status = {"queried": "ok", "partial": "partial", "unreachable": "failed"}
        rows.append(
            [
                short_name(host.hostname),
                host.host_ip or "-",
                nova,
                juju_ids.get(host.id, "-"),
                str(host.vcpus) if host.vcpus is not None else "?",
                f"{host.memory_mb / 1024:.0f} GiB" if host.memory_mb else "?",
                str(probe.numa_nodes) if probe and probe.numa_nodes else "-",
                probe.kernel if probe and probe.kernel else "-",
                probe_status[probe.status] if probe else "skipped",
            ]
        )
    print_table(rows)


def print_juju_result(result: JujuResult, maas: MaasResult) -> None:
    """Print Juju machines with their MAAS hostname and deployed applications."""
    if result.model is None:
        return
    physical = [m for m in result.machines if m.parent_id is None]
    click.echo(
        f"\nJuju model {result.model}: {len(physical)} machines, "
        f"{len(result.machines) - len(physical)} containers, "
        f"{sum(unit.principal is None for unit in result.units)} units"
    )
    names = {record.system_id: record.hostname for record in maas.machines}
    rows = [["MACHINE", "HOST", "APPLICATIONS"]]
    for machine in physical:
        apps = sorted(
            {
                unit.name.split("/")[0]
                for unit in result.units
                if unit.parent_machine_id == machine.id and unit.principal is None
            }
        )
        rows.append(
            [
                machine.id,
                short_name(names.get(machine.instance_id or "")),
                ", ".join(apps) or "-",
            ]
        )
    print_table(rows)


def print_network(result: CheckResult) -> None:
    """Print the links of each machine and the spaces each one carries."""
    machines = [machine for machine in result.maas.machines if machine.interfaces]
    if not machines:
        return
    roles: dict[str, list[str]] = {}
    for role, space in traffic_spaces(result.juju).items():
        roles.setdefault(space, []).append(role)
    probes = _machine_probes(result)
    click.echo("\nNetwork links:")
    rows = [["HOST", "LINK", "TYPE", "SPEED", "MTU", "SPACES"]]
    for machine in sorted(machines, key=lambda m: m.hostname or m.system_id):
        interfaces = {iface.name: iface for iface in machine.interfaces}
        carried: dict[str, dict[str, int | None]] = {}
        for item in machine.interfaces:
            root = _root_link(item.name, interfaces)
            for space in item.spaces:
                carried.setdefault(root, {})[space] = item.mtu
        observed = {
            link.name: link
            for link in (
                probes[machine.system_id].links if machine.system_id in probes else []
            )
        }
        for name, spaces in sorted(carried.items()):
            iface = interfaces.get(name)
            link = observed.get(name)
            kind = iface.type if iface else "?"
            mode = (link.bond_mode if link else None) or (
                iface.bond_mode if iface else None
            )
            if mode:
                kind += f" {mode}"
            members = len(iface.parents) if iface and iface.type == "bond" else 0
            if members:
                kind += f" x{members}"
            speed = link.speed_mbps if link else None
            mtu = (link.mtu if link else None) or (iface.mtu if iface else None)
            rows.append(
                [
                    short_name(machine.hostname),
                    name,
                    kind,
                    _speed(speed or (iface.link_speed_mbps if iface else None)),
                    str(mtu or "?"),
                    ", ".join(
                        space
                        + (f" [{'/'.join(roles[space])}]" if space in roles else "")
                        + (f" mtu {spaces[space]}" if spaces[space] != mtu else "")
                        for space in sorted(spaces)
                    ),
                ]
            )
    print_table(rows)
    geneve = sorted(
        {
            network.mtu
            for network in result.openstack.networks
            if network.network_type == "geneve" and network.mtu
        }
    )
    if geneve:
        click.echo(f"  Geneve tenant network MTU: {', '.join(map(str, geneve))}")


def print_ceph(ceph: CephResult) -> None:
    """Print Ceph health, OSD placement per host and pool redundancy."""
    if ceph.health is None and not ceph.osds and not ceph.pools:
        return
    pgs = sum(ceph.pg_states.values())
    clean = ceph.pg_states.get("active+clean", 0)
    click.echo(
        f"\nCeph: {ceph.health or '?'}, {clean}/{pgs} PGs active+clean, "
        f"quorum {', '.join(ceph.quorum) or '?'}"
    )
    if ceph.versions:
        click.echo(
            "  Versions: "
            + "; ".join(f"{d} {'/'.join(v)}" for d, v in ceph.versions.items())
        )
    click.echo(
        f"  Networks: public space {ceph.public_space or '?'}, "
        f"cluster space {ceph.cluster_space or 'same as public'}"
    )
    if ceph.osds:
        hosts: dict[str, list[CephOsd]] = {}
        for osd in ceph.osds:
            hosts.setdefault(osd.host or "?", []).append(osd)
        rows = [["HOST", "OSDS", "CLASS", "SIZE", "MAX FILL", "DOWN"]]
        for host, osds in sorted(hosts.items()):
            size = sum(osd.size_bytes or 0 for osd in osds)
            fill = max((osd.used_percent or 0 for osd in osds), default=0)
            rows.append(
                [
                    short_name(host),
                    str(len(osds)),
                    ",".join(sorted({osd.device_class or "?" for osd in osds})),
                    f"{size / 1024**4:.1f} TiB",
                    f"{fill:.0f}%",
                    str(sum(osd.status not in (None, "up") for osd in osds)),
                ]
            )
        print_table(rows)
    if ceph.pools:
        rules = {rule.id: rule for rule in ceph.crush_rules}
        rows = [["POOL", "TYPE", "SIZE", "PGS", "FAILURE DOMAIN", "USED BY"]]
        for pool in ceph.pools:
            rule = rules.get(pool.crush_rule)
            domain = rule.failure_domain if rule and rule.failure_domain else "?"
            if rule and rule.device_class:
                domain += f" ({rule.device_class})"
            rows.append(
                [
                    pool.name,
                    pool.type
                    + (f" {pool.erasure_profile}" if pool.erasure_profile else ""),
                    f"{pool.size} (min {pool.min_size})",
                    str(pool.pg_num),
                    domain,
                    ", ".join(pool.used_by or pool.applications) or "-",
                ]
            )
        print_table(rows)


def print_notices(notices: list[ProbeNotice]) -> None:
    """Print probe-owned messages, separating issues from skipped checks."""
    issues = [notice for notice in notices if notice.reason != "not-queried"]
    skipped = [notice for notice in notices if notice.reason == "not-queried"]
    click.echo()
    if not issues:
        click.echo("No issues found.")
    for title, entries in (("Issues", issues), ("Not checked", skipped)):
        if not entries:
            continue
        click.echo(f"{title}:")
        for notice in entries:
            click.echo(f"  - {notice.message}")


def print_check_result(result: CheckResult, as_json: bool) -> None:
    """Print structured JSON or a compact human-readable summary."""
    if as_json:
        click.echo(result.model_dump_json(indent=2))
        return
    openstack = result.openstack
    click.echo(f"Cloud {result.cloud}, project {openstack.project_id}")
    click.echo(
        f"OpenStack: {len(openstack.hosts)} hypervisors, "
        f"{len(openstack.instances)} instances, {len(openstack.volumes)} volumes, "
        f"{len(openstack.networks)} networks, {len(openstack.ports)} ports"
    )
    if result.maas.machines:
        click.echo(f"MAAS: {len(result.maas.machines)} machines matched to Juju")
    print_hypervisors(result)
    print_juju_result(result.juju, result.maas)
    print_network(result)
    print_ovs(result)
    print_ceph(result.ceph)
    print_readiness(result.readiness, result.plan)
    print_notices(result.notices)

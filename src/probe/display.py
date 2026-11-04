"""Terminal and JSON output of check results and run plans."""

import click

from probe.discover import (
    format_speed,
    machine_probes,
    root_link,
    short_name,
    traffic_spaces,
)
from probe.models import (
    CephOsd,
    CephResult,
    CheckResult,
    JujuResult,
    MaasResult,
    ProbeNotice,
    Profile,
    ProfileReadiness,
    RunPlan,
)
from probe.plan import PROFILE_NEEDS


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


def print_plan(plan: RunPlan, profile: Profile | None = None) -> None:
    """Print the placement of a run; with a profile, only what it uses."""
    needs = PROFILE_NEEDS[profile] if profile else None
    network = plan.network
    if network:
        network += f", VLAN {plan.vlan}" if plan.vlan else ""
        network += f", IPv{plan.ip_version}" if plan.ip_version else ""
    guest = ", ".join(x for x in (plan.flavor, plan.image) if x)
    rows = [
        (
            "sender",
            "Sender",
            f"{plan.sender} ({plan.sender_link})" if plan.sender else None,
        ),
        ("target", "Target", plan.target),
        ("network", "Network", network),
        ("mgmt_network", "Management", plan.mgmt_network),
        ("image", "Guest", guest),
        ("volume_type", "Volume type", plan.volume_type),
    ]
    print_table(
        [
            [label, value or "-"]
            for need, label, value in rows
            if needs is None or need in needs
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
    probes = machine_probes(result)
    click.echo("\nNetwork links:")
    rows = [["HOST", "LINK", "TYPE", "SPEED", "MTU", "SPACES"]]
    for machine in sorted(machines, key=lambda m: m.hostname or m.system_id):
        interfaces = {iface.name: iface for iface in machine.interfaces}
        carried: dict[str, dict[str, int | None]] = {}
        for item in machine.interfaces:
            root = root_link(item.name, interfaces)
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
                    format_speed(speed or (iface.link_speed_mbps if iface else None)),
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
        click.echo(f"MAAS: {len(result.maas.machines)} machines")
    print_hypervisors(result)
    print_juju_result(result.juju, result.maas)
    print_network(result)
    print_ovs(result)
    print_ceph(result.ceph)
    print_readiness(result.readiness, result.plan)
    print_notices(result.notices)

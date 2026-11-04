"""Run placement and per-profile readiness from discovery results."""

from probe.discover import short_name
from probe.models import (
    CheckResult,
    HostProbe,
    Network,
    Profile,
    ProfileReadiness,
    RunPlan,
)


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


def resolve_plan(
    result: CheckResult, overrides: dict[str, str] | None
) -> tuple[RunPlan, dict[str, str]]:
    """Choose run placement from live facts; return the plan and missing choices.

    An override names the sender or target host, the network, IP version or
    management network, or the image, flavor or volume type; it must still pass the
    same checks as an automatic choice.
    """
    pick = overrides or {}
    plan, missing = RunPlan(), dict[str, str]()
    charms = {app.name: app.charm for app in result.juju.apps}
    mon = next((name for name, charm in charms.items() if charm == "ceph-mon"), None)
    plan.ceph_mon = f"{mon}/leader" if mon else None
    physnets = {ovs.host_id: ovs.physnets for ovs in result.ovs}
    network = _pick_networks(result, pick, physnets, plan, missing)
    _pick_hosts(result, pick, physnets, network, plan, missing)
    _pick_guest(result, pick, plan, missing)
    _pick_volume_type(result, pick, charms, plan, missing)
    return plan, missing


def _pick_networks(
    result: CheckResult,
    pick: dict[str, str],
    physnets: dict[str, dict[str, str]],
    plan: RunPlan,
    missing: dict[str, str],
) -> Network | None:
    """Choose the VLAN provider network and the overlay management network."""
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
    # namespace on the target host.
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
    return network


def _pick_hosts(
    result: CheckResult,
    pick: dict[str, str],
    physnets: dict[str, dict[str, str]],
    network: Network | None,
    plan: RunPlan,
    missing: dict[str, str],
) -> None:
    """Choose the target and sender hypervisors on the provider network."""
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
    hostnames = {host.id: host.hostname or host.id for host in result.openstack.hosts}
    machines = {
        machine.hypervisor_id: machine.id
        for machine in result.juju.machines
        if machine.hypervisor_id and machine.parent_id is None
    }
    osd_hosts = {short_name(osd.host) for osd in result.ceph.osds if osd.status == "up"}

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


def _pick_guest(
    result: CheckResult, pick: dict[str, str], plan: RunPlan, missing: dict[str, str]
) -> None:
    """Choose the guest image and flavor."""
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


def _pick_volume_type(
    result: CheckResult,
    pick: dict[str, str],
    charms: dict[str, str],
    plan: RunPlan,
    missing: dict[str, str],
) -> None:
    """Choose a Ceph-backed volume type."""
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


PROFILE_NEEDS: dict[Profile, tuple[str, ...]] = {
    "host-network": ("network", "mgmt_network", "target", "sender", "image", "flavor"),
    "ceph-block": (
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
    result: CheckResult, missing: dict[str, str], allow_recovering: bool
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
    allowed_states = {"active", "clean", "scrubbing", "deep"}
    if allow_recovering:
        allowed_states.update(
            {"remapped", "backfill_wait", "backfilling", "recovery_wait", "recovering"}
        )
    unclean = sum(
        count
        for state, count in ceph.pg_states.items()
        if not (
            state == "active+clean"
            or (
                allow_recovering
                and "active" in state.split("+")
                and set(state.split("+")) <= allowed_states
            )
        )
    )
    if unclean:
        ceph_blockers.append(f"{unclean} PGs not active+clean")
    readiness = []
    for profile, needs in PROFILE_NEEDS.items():
        found = [missing[need] for need in needs if need in missing]
        if profile != "host-network":
            found += ceph_blockers
        readiness.append(
            ProfileReadiness(profile=profile, ready=not found, blockers=found)
        )
    return readiness

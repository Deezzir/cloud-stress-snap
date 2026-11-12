"""Run resources, fleet placement and workload readiness from discovery results."""

from math import ceil
from typing import Literal, NamedTuple

from campaign.model.profiles import Placement, RunConfig, normalize_arch
from campaign.workloads.trex import ip_packet_bytes
from constants import TREX_ARCHITECTURES, TREX_MIN_VCPUS
from probe.discover import short_name
from probe.models import (
    CheckResult,
    Connection,
    HostPlan,
    HostProbe,
    Network,
    RunPlan,
    VmPlan,
    Workload,
    WorkloadReadiness,
)

NEEDS: dict[Workload, tuple[str, ...]] = {
    "trex": ("network", "mgmt_network", "image", "flavor", "placement"),
    "fio": ("mgmt_network", "image", "flavor", "volume_type", "placement"),
}


class Candidate(NamedTuple):
    """A usable hypervisor, its provider link health and its VM capacity."""

    host: HostPlan
    health: tuple[bool, int] | None
    capacity: int | None


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
    result: CheckResult,
    overrides: dict[str, str] | None,
    workloads: set[Workload],
    config: RunConfig | None = None,
) -> tuple[RunPlan, dict[str, str]]:
    """Choose resources and, with a configuration, the fleet; return what is missing.

    An override names the network, IP version, management network, image, flavor
    or volume type; it must still pass the same checks as an automatic choice.
    """
    pick = overrides or {}
    plan, missing = RunPlan(), dict[str, str]()
    charms = {app.name: app.charm for app in result.juju.apps}
    mon = next((name for name, charm in charms.items() if charm == "ceph-mon"), None)
    plan.ceph_mon = f"{mon}/leader" if mon else None
    physnets = {ovs.host_id: ovs.physnets for ovs in result.ovs}
    network = _pick_networks(result, pick, physnets, plan, missing)
    _pick_images(result, pick, plan, missing)
    _pick_flavor(result, pick, "trex" in workloads, plan, missing)
    _pick_volume_type(result, pick, charms, plan, missing)
    candidates = _candidates(result, physnets, network, plan)
    trex_config = config.workloads.trex if config else None
    if trex_config and plan.mtu:
        size = ip_packet_bytes(trex_config, plan.ip_version)
        if size > plan.mtu:
            missing["network"] = (
                f"{size}-byte TRex packets exceed the {plan.mtu}-byte MTU of "
                f"{plan.network}"
            )
    if config is not None:
        if not missing.keys() & {"mgmt_network", "image", "flavor"}:
            problem = _place(config, candidates, plan)
            if problem:
                missing["placement"] = problem
    else:
        trex = "trex" in workloads
        usable = [c for c in candidates if not _exclusion(c, plan, trex)]
        if trex and len(usable) < 2:
            missing["placement"] = f"fewer than two amd64 hosts carry {plan.network}"
        elif not usable:
            missing["placement"] = "no usable amd64 hypervisor"
    return plan, missing


def _pick_images(
    result: CheckResult, pick: dict[str, str], plan: RunPlan, missing: dict[str, str]
) -> None:
    """Choose the newest Ubuntu guest image for each architecture.

    An image without an architecture property is taken as amd64.
    """
    for image in sorted(result.openstack.images, key=lambda i: i.created_at or ""):
        if "image" in pick:
            if pick["image"] not in (image.name, image.id):
                continue
        elif "ubuntu" not in f"{image.os_distro} {image.name}".lower() or (
            "amphora" in (image.name or "").lower()
        ):
            continue
        arch = normalize_arch(image.architecture or "x86_64")
        if arch:
            plan.images[arch] = image.name or image.id
            plan.image_ids[arch] = image.id
    if not plan.images:
        missing["image"] = (
            f"{pick['image']} is not an image with a known architecture"
            if "image" in pick
            else "no Ubuntu image"
        )


def _pick_flavor(
    result: CheckResult,
    pick: dict[str, str],
    trex: bool,
    plan: RunPlan,
    missing: dict[str, str],
) -> None:
    """Choose the smallest flavor large enough for the guests."""
    vcpus = TREX_MIN_VCPUS if trex else 2
    flavor = min(
        (
            f
            for f in result.openstack.flavors
            if not f.restricted
            and f.vcpus >= vcpus
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
            f"{pick['flavor']} is restricted or smaller than {vcpus} vCPUs, 4 GiB "
            "RAM and 20 GB disk"
            if "flavor" in pick
            else f"no flavor with {vcpus} vCPUs, 4 GiB RAM and 20 GB disk"
        )


def _candidates(
    result: CheckResult,
    physnets: dict[str, dict[str, str]],
    network: Network | None,
    plan: RunPlan,
) -> list[Candidate]:
    """Usable hypervisors in name order.

    Capacity is what Placement reports as left for the flavor, or None when
    unknown; Nova makes the final placement, which the run verifies.
    """
    probes = {
        probe.host_id: probe
        for probe in result.infra.host_probes
        if probe.status in ("queried", "partial")
    }
    machines = {
        machine.hypervisor_id: machine.id
        for machine in result.juju.machines
        if machine.hypervisor_id and machine.parent_id is None
    }
    flavor = next((f for f in result.openstack.flavors if f.id == plan.flavor_id), None)
    found = []
    for host in result.openstack.hosts:
        probe = probes.get(host.id)
        arch = normalize_arch(probe.architecture) if probe else None
        machine = machines.get(host.id)
        if not (probe and machine) or host.state != "up" or host.status == "disabled":
            continue
        link = None
        if network:
            link = physnets.get(host.id, {}).get(network.physical_network or "")
        health = _bond_health(probe, link) if link else None
        ovs = next((o for o in result.ovs if o.host_id == host.id), None)
        capacity = None
        if flavor and host.vcpus_free is not None and host.memory_mb_free is not None:
            fits = flavor.vcpus <= (host.vcpus_max_unit or flavor.vcpus) and (
                flavor.ram_mb <= (host.memory_mb_max_unit or flavor.ram_mb)
            )
            capacity = (
                max(
                    min(
                        host.vcpus_free // flavor.vcpus,
                        host.memory_mb_free // flavor.ram_mb,
                    ),
                    0,
                )
                if fits
                else 0
            )
        found.append(
            Candidate(
                HostPlan(
                    name=short_name(host.hostname or host.id),
                    hypervisor=host.hostname or host.id,
                    machine=machine,
                    architecture=arch or "unknown",
                    vms=0,
                    link=link if health else None,
                    ovs_unit=ovs.unit if ovs else None,
                    datapath=ovs.bridges.get("br-int") if ovs else None,
                    hw_offload=ovs.hw_offload if ovs else None,
                ),
                health,
                capacity,
            )
        )
    return sorted(found, key=lambda c: c.host.name)


def _exclusion(
    candidate: Candidate,
    plan: RunPlan,
    trex: bool,
    placement: Placement | None = None,
) -> str | None:
    """Why a host cannot take the run's VMs, or None when it can."""
    host = candidate.host
    if placement is not None and (
        host.name in placement.exclude_hosts
        or host.hypervisor in placement.exclude_hosts
    ):
        return "it is excluded by placement.exclude_hosts"
    architectures = placement.architectures if placement is not None else ["amd64"]
    if host.architecture == "unknown":
        return "its architecture is unknown"
    if architectures and host.architecture not in architectures:
        return f"its architecture {host.architecture} is not selected"
    if host.architecture not in plan.image_ids:
        return f"there is no guest image for {host.architecture}"
    if trex and host.architecture not in TREX_ARCHITECTURES:
        return f"TRex does not support {host.architecture}"
    if trex and not host.link:
        return f"it has no healthy link on {plan.network}"
    return None


def _spread(count: int, hosts: list[Candidate]) -> dict[str, int] | None:
    """Spread count VMs round-robin over hosts within their capacity."""
    counts = {c.host.name: 0 for c in hosts}
    while count:
        placed = False
        for c in hosts:
            if count and (c.capacity is None or counts[c.host.name] < c.capacity):
                counts[c.host.name] += 1
                count -= 1
                placed = True
        if not placed:
            return None
    return {name: n for name, n in counts.items() if n}


def _interleave(vms: list[VmPlan]) -> list[VmPlan]:
    """Order VMs by their index on their host, so neighbours are on other hosts."""
    seen: dict[str, int] = {}
    keyed = []
    for vm in vms:
        seen[vm.host] = seen.get(vm.host, 0) + 1
        keyed.append((seen[vm.host], vm.host, vm.name, vm))
    return [item[3] for item in sorted(keyed, key=lambda k: k[:3])]


def connect(senders: list[VmPlan], receivers: list[VmPlan]) -> list[Connection]:
    """Pair senders with receivers so that every VM on each side takes part.

    With more senders, each sender has one receiver; otherwise receivers are
    dealt to senders in turn. A sender's rate is shared among its receivers.
    """
    senders, receivers = _interleave(senders), _interleave(receivers)
    if len(senders) >= len(receivers):
        pairs = [(s, receivers[i % len(receivers)]) for i, s in enumerate(senders)]
    else:
        pairs = [(senders[i % len(senders)], r) for i, r in enumerate(receivers)]
    fanout = {s.name: sum(p[0] is s for p in pairs) for s in senders}
    return [
        Connection(sender=s.name, receiver=r.name, share=1 / fanout[s.name])
        for s, r in pairs
    ]


def _place(config: RunConfig, candidates: list[Candidate], plan: RunPlan) -> str | None:
    """Resolve the fleet into plan; return why it cannot be placed, if so."""
    placement, trex = config.placement, config.workloads.trex
    total = placement.total
    reasons = {
        c.host.name: _exclusion(c, plan, trex is not None, placement)
        for c in candidates
    }
    by_name = {c.host.name: c for c in candidates}
    by_name |= {c.host.hypervisor: c for c in candidates}
    eligible = [c for c in candidates if reasons[c.host.name] is None]
    focus: Candidate | None = None

    if placement.hosts is not None:
        counts: dict[str, int] = {}
        for name, count in placement.hosts.items():
            found = by_name.get(name)
            if found is None:
                return f"{name} is not a usable hypervisor"
            if reasons[found.host.name]:
                return f"{name} cannot be used: {reasons[found.host.name]}"
            if found.capacity is not None and count > found.capacity:
                return f"{name} has room for at most {found.capacity} VMs"
            counts[found.host.name] = count
        if trex:
            if len(counts) < 2:
                return "trex needs VMs on at least two hypervisors"
            chosen = [by_name[name] for name in counts]
            if trex.focus_host:
                focus = by_name.get(trex.focus_host)
                if focus is None or focus.host.name not in counts:
                    return f"focus host {trex.focus_host} is not in placement.hosts"
            else:
                focus = _rank(chosen)[0]
    elif trex:
        if total < 2:
            return "trex needs at least two VMs"
        if len(eligible) < 2:
            return "trex needs two eligible hypervisors" + _excluded(reasons)
        on_focus = ceil(total / 2)
        if trex.focus_host:
            focus = by_name.get(trex.focus_host)
            if focus is None:
                return f"focus host {trex.focus_host} is not a usable hypervisor"
            if reasons[focus.host.name]:
                return f"{trex.focus_host} cannot be used: {reasons[focus.host.name]}"
            options = [focus]
        else:
            options = _rank(eligible)
        counts = {}
        for option in options:
            if option.capacity is not None and option.capacity < on_focus:
                continue
            others = [c for c in eligible if c is not option]
            remote = _spread(total - on_focus, others) if others else None
            if remote:
                focus, counts = option, {option.host.name: on_focus, **remote}
                break
        if not counts:
            return (
                f"no focus host can take {on_focus} VMs with {total - on_focus} "
                "on other hosts"
            )
    else:
        if not eligible:
            return "no hypervisor can take the run's VMs" + _excluded(reasons)
        counts = _spread(total, eligible) or {}
        if not counts:
            return f"the eligible hypervisors have no room for {total} VMs"

    names = sorted(counts, key=lambda n: (focus is None or n != focus.host.name, n))
    vms: list[VmPlan] = []
    for name in names:
        is_focus = focus is not None and name == focus.host.name
        host = by_name[name].host.model_copy(
            update={"vms": counts[name], "focus": is_focus}
        )
        plan.hosts.append(host)
        for _ in range(counts[name]):
            role: Literal["sender", "receiver"] | None = None
            if trex:
                sends = host.focus == (trex.mode == "fan-out")
                role = "sender" if sends else "receiver"
            vms.append(
                VmPlan(
                    name=f"vm{len(vms) + 1:03d}",
                    host=name,
                    image=plan.images[host.architecture],
                    image_id=plan.image_ids[host.architecture],
                    role=role,
                )
            )
    plan.vms = vms
    if trex and focus:
        plan.focus = focus.host.name
        plan.connections = connect(
            [v for v in vms if v.role == "sender"],
            [v for v in vms if v.role == "receiver"],
        )
    return None


def _excluded(reasons: dict[str, str | None]) -> str:
    """Excluded hosts and why, for an error message."""
    found = [f"{name}: {reason}" for name, reason in reasons.items() if reason]
    return f" ({'; '.join(found[:5])})" if found else ""


def _rank(candidates: list[Candidate]) -> list[Candidate]:
    """Healthiest provider link first: all bond members up, then speed, then name."""
    return sorted(
        candidates,
        key=lambda c: (
            not (c.health and c.health[0]),
            -(c.health[1] if c.health else 0),
            c.host.name,
        ),
    )


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
        plan.mtu = network.mtu
        plan.ip_version = 6 if 6 in network.ip_versions else 4
        if not network.ip_versions:
            plan.ip_version = None
            missing["network"] = f"{plan.network} has no subnet"
        elif version:
            plan.ip_version = 4 if version == 4 else 6
            if version not in network.ip_versions:
                missing["network"] = f"{plan.network} has no IPv{version} subnet"

    # Guests are managed over an overlay network, through its OVN metadata
    # namespace on each VM's host.
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


def workload_readiness(
    result: CheckResult,
    missing: dict[Workload, dict[str, str]],
    allow_recovering: bool,
) -> list[WorkloadReadiness]:
    """Gate each workload on its resource choices; fio also on Ceph health."""
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
    for workload, found in missing.items():
        blockers = [found[need] for need in NEEDS[workload] if need in found]
        if workload == "fio":
            blockers += ceph_blockers
        readiness.append(
            WorkloadReadiness(workload=workload, ready=not blockers, blockers=blockers)
        )
    return readiness

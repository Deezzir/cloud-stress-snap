"""Run-owned OpenStack resources, created after a journaled intent."""

import base64
import ipaddress
import re
import time
from collections.abc import Callable
from typing import Any

import yaml
from openstack import exceptions
from openstack.connection import Connection

from campaign.model import state as run_state
from campaign.model.state import RunState
from campaign.model.target import HostTarget, Target
from constants import BUILD_TIMEOUT_S


class PreparationError(Exception):
    """A required capability is missing; no load was applied."""


def name(state: RunState, kind: str, vm: str | None = None) -> str:
    """Deterministic, run-owned resource name."""
    return (
        f"cloud-stress-{state.id}-{vm}-{kind}"
        if vm
        else f"cloud-stress-{state.id}-{kind}"
    )


def _create(
    state: RunState, kind: str, create: Callable[[str], Any], vm: str | None = None
) -> Any:
    resource_name = name(state, kind, vm)
    run_state.record(state.id, kind, resource_name, "intent", where="openstack")
    try:
        resource = create(resource_name)
    except exceptions.HttpException as error:
        # A 4xx answer means nothing was created; anything else is ambiguous and
        # stays outstanding so cleanup can look it up by its run-owned name.
        status = error.status_code or 0
        action: run_state.Action = "rejected" if 400 <= status < 500 else "failed"
        run_state.record(
            state.id,
            kind,
            resource_name,
            action,
            where="openstack",
            detail=" ".join(str(error).split())[:300],
        )
        raise
    run_state.record(
        state.id, kind, resource_name, "created", id=resource.id, where="openstack"
    )
    return resource


def metadata_address(conn: Connection, network_id: str) -> str:
    """IPv4 address of the network's OVN metadata port, the guests' SSH source."""
    for port in conn.network.ports(
        network_id=network_id, device_owner="network:distributed"
    ):
        for item in port.fixed_ips:
            if ":" not in item["ip_address"]:
                return str(item["ip_address"])
    raise PreparationError(
        "the management network has no OVN metadata port with an IPv4 address"
    )


def check_quota(
    conn: Connection, count: int, vcpus: int, ram_mb: int, volume_gb: int
) -> list[str]:
    """Report quota that would block the run's servers or volumes."""
    problems = []
    compute = conn.compute.get_limits().absolute
    if compute.instances_used + count > compute.instances >= 0:
        problems.append(
            f"Instance quota: {count:,} needed, "
            f"{compute.instances - compute.instances_used:,} available"
        )
    if compute.total_cores_used + count * vcpus > compute.total_cores >= 0:
        problems.append(
            f"vCPU quota: {count * vcpus:,} needed, "
            f"{compute.total_cores - compute.total_cores_used:,} available"
        )
    if compute.total_ram_used + count * ram_mb > compute.total_ram >= 0:
        problems.append(
            f"RAM quota: {count * ram_mb:,} MiB needed, "
            f"{compute.total_ram - compute.total_ram_used:,} MiB available"
        )
    if volume_gb:
        volume = conn.block_storage.get_limits().absolute
        used, limit = volume.total_gigabytes_used, volume.max_total_volume_gigabytes
        if used + count * volume_gb > limit >= 0:
            problems.append(
                f"Volume quota: {count * volume_gb:,} GiB needed, "
                f"{limit - used:,} GiB available"
            )
        used, limit = volume.total_volumes_used, volume.max_total_volumes
        if used + count > limit >= 0:
            problems.append(
                f"Volume count quota: {count:,} needed, {limit - used:,} available"
            )
    return problems


def _port_identity(port: Any, version: int | None) -> tuple[str, str]:
    """Require the reserved MAC and an address of the planned IP family."""
    mac = str(port.mac_address or "").lower()
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        raise PreparationError(f"port {port.id} has no valid MAC address")
    for item in port.fixed_ips:
        address = ipaddress.ip_address(item["ip_address"])
        if address.version == version:
            return mac, str(address)
    raise PreparationError(f"port {port.id} has no IPv{version} address")


def provision(
    conn: Connection,
    state: RunState,
    public_keys: dict[str, str],
    host_key: tuple[str, str],
    sleep: Callable[[float], None],
) -> list[Target]:
    """Create the run's security group and VMs with their ports and volumes.

    Calls run one after another; servers and volumes build in parallel in the
    cloud and are awaited together.
    """
    plan, config = state.plan, state.config
    trex, fio = config.workloads.trex, config.workloads.fio
    source = metadata_address(conn, plan.mgmt_network_id or "")
    volume_type = None if plan.volume_type_implicit else plan.volume_type
    volume_args = {"volume_type": volume_type} if volume_type else {}
    group = _create(
        state,
        "security-group",
        lambda n: conn.network.create_security_group(
            name=n, description="cloud-stress run-owned"
        ),
    )
    conn.network.create_security_group_rule(
        security_group_id=group.id,
        direction="ingress",
        ethertype="IPv4",
        protocol="tcp",
        port_range_min=22,
        port_range_max=22,
        remote_ip_prefix=f"{source}/32",
    )
    if trex:
        conn.network.create_security_group_rule(
            security_group_id=group.id,
            direction="ingress",
            ethertype=f"IPv{plan.ip_version or 6}",
            protocol="udp",
            port_range_min=trex.udp_dst_port,
            port_range_max=trex.udp_dst_port,
            remote_group_id=group.id,
        )

    def data_port(participant: str) -> Any:
        return _create(
            state,
            "port",
            lambda n: conn.network.create_port(
                name=n,
                network_id=plan.network_id,
                security_group_ids=[group.id],
                is_port_security_enabled=True,
            ),
            participant,
        )

    for sender in plan.host_senders:
        port = data_port(sender.name)
        mac, address = _port_identity(port, plan.ip_version)
        host = next(h for h in plan.hosts if h.name == sender.host)
        source_target = HostTarget(
            **sender.model_dump(),
            machine=host.machine,
            port_id=port.id,
            mac=mac,
            address=address,
        )
        state.host_senders.append(source_target)
        run_state.save(state)
        if port.device_id or port.binding_host_id:
            raise PreparationError(f"source port {port.id} is unexpectedly attached")
        assert trex
        # An unbound source port need not appear in Neutron's remote-group set.
        conn.network.create_security_group_rule(
            security_group_id=group.id,
            direction="ingress",
            ethertype=f"IPv{plan.ip_version}",
            protocol="udp",
            port_range_min=trex.udp_dst_port,
            port_range_max=trex.udp_dst_port,
            remote_ip_prefix=f"{address}/{32 if plan.ip_version == 4 else 128}",
        )
    private, public = host_key
    guest_config = {
        "ssh_keys": {"ed25519_private": private, "ed25519_public": public},
        "package_update": True,
        "packages": (["fio"] if fio else []) + (["nftables"] if trex else []),
    }
    hypervisors = {host.name: host.hypervisor for host in plan.hosts}
    targets, servers, volumes = state.targets, {}, {}
    for vm in plan.vms:
        user_data = "#cloud-config\n" + yaml.safe_dump(
            {**guest_config, "ssh_authorized_keys": [public_keys[vm.host]]}
        )
        mgmt_port = _create(
            state,
            "mgmt-port",
            lambda n: conn.network.create_port(
                name=n, network_id=plan.mgmt_network_id, security_group_ids=[group.id]
            ),
            vm.name,
        )
        networks = [{"port": mgmt_port.id}]
        target = Target(
            name=vm.name,
            host=vm.host,
            role=vm.role,
            server_id="",
            mgmt_port_id=mgmt_port.id,
            mgmt_address=next(
                a["ip_address"]
                for a in mgmt_port.fixed_ips
                if ":" not in a["ip_address"]
            ),
            security_groups=[group.id],
        )
        targets.append(target)
        run_state.save(state)
        if trex:
            port = data_port(vm.name)
            target.port_id = port.id
            target.mac, target.address = _port_identity(port, plan.ip_version)
            run_state.save(state)
            networks.append({"port": port.id})
        if fio:
            volumes[vm.name] = _create(
                state,
                "volume",
                lambda n: conn.block_storage.create_volume(
                    name=n, size=fio.volume_size_gib, **volume_args
                ),
                vm.name,
            ).id
        servers[vm.name] = _create(
            state,
            "server",
            # _create calls this at once, inside this iteration.
            lambda n: conn.compute.create_server(
                name=n,
                image_id=vm.image_id,  # noqa: B023
                flavor_id=plan.flavor_id,
                networks=networks,  # noqa: B023
                config_drive=True,
                user_data=base64.b64encode(user_data.encode()).decode(),  # noqa: B023
                hypervisor_hostname=hypervisors[vm.host],  # noqa: B023
            ),
            vm.name,
        ).id
        target.server_id = servers[vm.name]
        run_state.save(state)

    built = _wait_all(
        servers,
        conn.compute.get_server,
        "ACTIVE",
        "guest",
        sleep,
    )
    for target in targets:
        landed = built[target.name].hypervisor_hostname
        if landed != hypervisors[target.host]:
            raise PreparationError(
                f"{target.name} landed on {landed}, not {hypervisors[target.host]}"
            )
        target.hypervisor_hostname = landed
    if volumes:
        ready = _wait_all(
            volumes,
            conn.block_storage.get_volume,
            "available",
            "data volume",
            sleep,
        )
        for vm_name, volume in ready.items():
            if volume_type and volume.volume_type != volume_type:
                raise PreparationError(
                    f"the {vm_name} volume was created as {volume.volume_type}, "
                    f"not {volume_type}"
                )
            _create(
                state,
                "attachment",
                lambda _: conn.compute.create_volume_attachment(
                    servers[vm_name],  # noqa: B023
                    volume_id=volumes[vm_name],  # noqa: B023
                ),
                vm_name,
            )
        attached = _wait_all(
            volumes,
            conn.block_storage.get_volume,
            "in-use",
            "volume attachment",
            sleep,
        )
        for target in targets:
            volume = attached[target.name]
            target.volume_id = volume.id
            target.volume_type = volume.volume_type
            target.volume_size_gb = volume.size
            target.volume_backend = volume.host
    for target in targets:
        if not target.port_id:
            continue
        observed = conn.network.get_port(target.port_id)
        target.binding_host = observed.binding_host_id
        target.security_groups = list(observed.security_group_ids)
        bound = observed.binding_host_id or ""
        # Neutron may record the short name, which identifies only a unique host.
        if bound != hypervisors[target.host] and not (
            "." not in bound
            and bound == target.host == _short(hypervisors[target.host])
        ):
            raise PreparationError(
                f"the {target.name} port is bound to {observed.binding_host_id}"
            )
        if target.security_groups != [group.id]:
            raise PreparationError(
                f"the {target.name} port has security groups the run does not own"
            )
        if observed.is_port_security_enabled is False:
            raise PreparationError(f"the {target.name} port has port security disabled")
    return targets


def _wait_all(
    ids: dict[str, str],
    read: Callable[[str], Any],
    status: str,
    what: str,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """Poll every resource until all are done, within one shared timeout."""
    deadline = time.monotonic() + BUILD_TIMEOUT_S
    finished: dict[str, Any] = {}
    while time.monotonic() < deadline:
        for key, resource_id in ids.items():
            if key in finished:
                continue
            value = read(resource_id)
            if value.status == status:
                finished[key] = value
            elif value.status.lower() == "error":
                raise PreparationError(f"the {key} {what} went into ERROR")
        if len(finished) == len(ids):
            return finished
        sleep(5)
    waiting = sorted(set(ids) - set(finished))
    raise PreparationError(
        f"{len(waiting)} {what}s not ready after {BUILD_TIMEOUT_S}s: "
        + ", ".join(waiting)
    )


def _short(host: str | None) -> str:
    return (host or "").split(".")[0]


DELETE: dict[str, Callable[[Connection, str], None]] = {
    "keypair": lambda conn, id: conn.compute.delete_keypair(id, ignore_missing=True),
    "security-group": lambda conn, id: conn.network.delete_security_group(
        id, ignore_missing=True
    ),
    "port": lambda conn, id: conn.network.delete_port(id, ignore_missing=True),
    "mgmt-port": lambda conn, id: conn.network.delete_port(id, ignore_missing=True),
    "volume": lambda conn, id: conn.block_storage.delete_volume(
        id, ignore_missing=True
    ),
    "server": lambda conn, id: conn.compute.delete_server(id, ignore_missing=True),
}

FIND: dict[str, Callable[[Connection, str], Any]] = {
    "keypair": lambda conn, n: conn.compute.find_keypair(n),
    "security-group": lambda conn, n: conn.network.find_security_group(n),
    "port": lambda conn, n: conn.network.find_port(n),
    "mgmt-port": lambda conn, n: conn.network.find_port(n),
    "volume": lambda conn, n: conn.block_storage.find_volume(n),
    "server": lambda conn, n: conn.compute.find_server(n),
}


def cleanup(conn: Connection, state: RunState) -> None:
    """Delete outstanding run-owned cloud resources; failures stay in the journal.

    Deleting a server detaches its volume, so attachments are settled after the
    servers instead of through a separate detach call.
    """
    entries = [
        e for e in reversed(run_state.outstanding(state.id)) if e.where == "openstack"
    ]
    order = {"server": 0, "attachment": 1}
    for entry in sorted(entries, key=lambda e: order.get(e.kind, 2)):
        try:
            if entry.kind == "attachment":
                prefix = entry.name.removesuffix("attachment")
                server = FIND["server"](conn, f"{prefix}server")
                volume = FIND["volume"](conn, f"{prefix}volume")
                if server and volume:
                    conn.compute.delete_volume_attachment(
                        server.id, volume.id, ignore_missing=True
                    )
                if volume:
                    conn.block_storage.wait_for_status(
                        volume, "available", wait=BUILD_TIMEOUT_S
                    )
            else:
                # An intent without an ID may still have created the resource;
                # the full run ID in its name makes the lookup run-owned.
                found = entry.id or getattr(
                    FIND[entry.kind](conn, entry.name), "id", None
                )
                if found:
                    if entry.kind == "volume":
                        volume = conn.block_storage.get_volume(found)
                        # Cinder deletes an unattached error volume without force.
                        if volume.attachments or not volume.status.startswith("error"):
                            volume = conn.block_storage.wait_for_status(
                                volume, "available", wait=BUILD_TIMEOUT_S
                            )
                    DELETE[entry.kind](conn, found)
                    if entry.kind == "server":
                        conn.compute.wait_for_delete(
                            conn.compute.get_server(found), wait=BUILD_TIMEOUT_S
                        )
                    elif entry.kind == "volume":
                        conn.block_storage.wait_for_delete(volume, wait=BUILD_TIMEOUT_S)
            run_state.transition(state.id, entry, "deleted")
        except exceptions.NotFoundException:
            run_state.transition(state.id, entry, "deleted")
        except Exception as error:  # noqa: BLE001 - keep cleaning the rest
            detail = " ".join(str(error).split())[:300]
            login = isinstance(error, exceptions.HttpException) and (
                error.status_code == 401
            )
            action: run_state.Action = "requires-login" if login else "failed"
            run_state.transition(state.id, entry, action, detail)

"""Run-owned OpenStack resources, created after a journaled intent."""

import base64
import time
from collections.abc import Callable
from typing import Any

import yaml
from openstack import exceptions
from openstack.connection import Connection

from campaign.model import state as run_state
from campaign.model.profiles import FLOOD_PORT, FLOOD_SOURCE_CIDR
from campaign.model.state import RunState
from campaign.model.target import Target

BUILD_TIMEOUT = 600


class PreparationError(Exception):
    """A required capability is missing; no load was applied."""


def name(state: RunState, kind: str) -> str:
    """Deterministic, run-owned resource name."""
    return f"cloud-stress-{state.id}-{kind}"


def _create(state: RunState, kind: str, create: Callable[[str], Any]) -> Any:
    resource_name = name(state, kind)
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


def _wait(
    read: Callable[[], Any],
    done: Callable[[Any], bool],
    what: str,
    sleep: Callable[[float], None],
) -> Any:
    deadline = time.monotonic() + BUILD_TIMEOUT
    while time.monotonic() < deadline:
        value = read()
        if done(value):
            return value
        if str(getattr(value, "status", "")).lower() == "error":
            raise PreparationError(f"{what} went into ERROR")
        sleep(5)
    raise PreparationError(f"{what} not ready after {BUILD_TIMEOUT}s")


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


def check_quota(conn: Connection, vcpus: int, ram_mb: int, volume_gb: int) -> list[str]:
    """Report quota that would block the run's server or volume."""
    problems = []
    compute = conn.compute.get_limits().absolute
    if compute.instances_used + 1 > compute.instances >= 0:
        problems.append("the instance quota is used up")
    if compute.total_cores_used + vcpus > compute.total_cores >= 0:
        problems.append(f"the core quota has no room for {vcpus} vCPUs")
    if compute.total_ram_used + ram_mb > compute.total_ram >= 0:
        problems.append(f"the RAM quota has no room for {ram_mb} MiB")
    if volume_gb:
        volume = conn.block_storage.get_limits().absolute
        used, limit = volume.total_gigabytes_used, volume.max_total_volume_gigabytes
        if used + volume_gb > limit >= 0:
            problems.append(f"the volume quota has no room for {volume_gb} GB")
    return problems


def provision(
    conn: Connection,
    state: RunState,
    public_key: str,
    host_key: tuple[str, str],
    trex: bool,
    sleep: Callable[[float], None],
) -> Target:
    """Create the run's key pair, security group, ports, volume and target guest."""
    plan, schedule = state.plan, state.schedule
    source = metadata_address(conn, plan.mgmt_network_id or "")
    volume_type = None
    if schedule.volume_gb:
        types = {t.name for t in conn.block_storage.types()}
        if plan.volume_type in types:
            volume_type = plan.volume_type
        elif plan.volume_type != "default":
            raise PreparationError(f"volume type {plan.volume_type} no longer exists")
    keypair = _create(
        state,
        "keypair",
        lambda n: conn.compute.create_keypair(name=n, public_key=public_key),
    )
    group = _create(
        state,
        "security-group",
        lambda n: conn.network.create_security_group(
            name=n, description="cloud-stress run-owned"
        ),
    )
    port = _create(
        state,
        "port",
        lambda n: conn.network.create_port(
            name=n, network_id=plan.network_id, security_group_ids=[group.id]
        ),
    )
    mgmt_port = _create(
        state,
        "mgmt-port",
        lambda n: conn.network.create_port(
            name=n, network_id=plan.mgmt_network_id, security_group_ids=[group.id]
        ),
    )
    addresses = [item["ip_address"] for item in port.fixed_ips]
    address = next(
        (a for a in addresses if (":" in a) == (plan.ip_version == 6)), addresses[0]
    )
    mgmt_address = next(
        a["ip_address"] for a in mgmt_port.fixed_ips if ":" not in a["ip_address"]
    )
    rules = [("tcp", 22, f"{source}/32")]
    if trex:
        rules.append(("udp", FLOOD_PORT, FLOOD_SOURCE_CIDR[plan.ip_version or 6]))
    for protocol, port_number, cidr in rules:
        conn.network.create_security_group_rule(
            security_group_id=group.id,
            direction="ingress",
            ethertype="IPv6" if ":" in cidr else "IPv4",
            protocol=protocol,
            port_range_min=port_number,
            port_range_max=port_number,
            remote_ip_prefix=cidr,
        )
    volume = None
    if schedule.volume_gb:
        volume = _create(
            state,
            "volume",
            lambda n: conn.block_storage.create_volume(
                name=n, size=schedule.volume_gb, volume_type=volume_type
            ),
        )
    private, public = host_key
    user_data = "#cloud-config\n" + yaml.safe_dump(
        {
            "ssh_keys": {"ed25519_private": private, "ed25519_public": public},
            "package_update": bool(schedule.fio_cells),
            "packages": ["fio"] if schedule.fio_cells else [],
        }
    )
    server = _create(
        state,
        "server",
        lambda n: conn.compute.create_server(
            name=n,
            image_id=plan.image_id,
            flavor_id=plan.flavor_id,
            networks=[{"port": mgmt_port.id}, {"port": port.id}],
            config_drive=True,
            key_name=keypair.name,
            user_data=base64.b64encode(user_data.encode()).decode(),
            hypervisor_hostname=plan.target_hypervisor,
        ),
    )
    server = _wait(
        lambda: conn.compute.get_server(server.id),
        lambda s: s.status == "ACTIVE",
        "the target guest",
        sleep,
    )
    if server.hypervisor_hostname != plan.target_hypervisor:
        raise PreparationError(
            f"the guest landed on {server.hypervisor_hostname}, "
            f"not {plan.target_hypervisor}"
        )
    target = Target(
        server_id=server.id,
        port_id=port.id,
        mac=port.mac_address,
        address=address,
        mgmt_port_id=mgmt_port.id,
        mgmt_address=mgmt_address,
        binding_host=None,
        security_groups=[group.id],
    )
    if volume:
        volume_id = volume.id
        volume = _wait(
            lambda: conn.block_storage.get_volume(volume_id),
            lambda v: v.status == "available",
            "the data volume",
            sleep,
        )
        if volume_type and volume.volume_type != volume_type:
            raise PreparationError(
                f"the volume was created as {volume.volume_type}, not {volume_type}"
            )
        _create(
            state,
            "attachment",
            lambda _: conn.compute.create_volume_attachment(
                server.id, volume_id=volume_id
            ),
        )
        volume = _wait(
            lambda: conn.block_storage.get_volume(volume_id),
            lambda v: v.status == "in-use",
            "the volume attachment",
            sleep,
        )
        target.volume_id = volume.id
        target.volume_type = volume.volume_type
        target.volume_size_gb = volume.size
        target.volume_backend = volume.host
    observed = conn.network.get_port(port.id)
    target.binding_host = observed.binding_host_id
    target.security_groups = list(observed.security_group_ids)
    if _short(observed.binding_host_id) != _short(plan.target_hypervisor):
        raise PreparationError(f"the port is bound to {observed.binding_host_id}")
    if target.security_groups != [group.id]:
        raise PreparationError("the port has security groups the run does not own")
    return target


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


def cleanup(conn: Connection, state: RunState) -> list[str]:
    """Delete outstanding run-owned cloud resources; return what is left."""
    left = []
    for entry in reversed(run_state.outstanding(state.id)):
        if entry.where != "openstack":
            continue
        try:
            if entry.kind == "attachment":
                server = FIND["server"](conn, name(state, "server"))
                volume = FIND["volume"](conn, name(state, "volume"))
                if server and volume:
                    conn.compute.delete_volume_attachment(
                        server.id, volume.id, ignore_missing=True
                    )
                    conn.block_storage.wait_for_status(
                        volume, "available", wait=BUILD_TIMEOUT
                    )
            else:
                # An intent without an ID may still have created the resource;
                # the full run ID in its name makes the lookup run-owned.
                found = entry.id or getattr(
                    FIND[entry.kind](conn, entry.name), "id", None
                )
                if found:
                    DELETE[entry.kind](conn, found)
                    if entry.kind == "server":
                        conn.compute.wait_for_delete(
                            conn.compute.get_server(found), wait=BUILD_TIMEOUT
                        )
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
            left.append(f"{entry.kind} {entry.name}: {action}")
    return left

"""Read scoped OpenStack inventory without modifying the deployment."""

import logging
from collections.abc import Iterable
from itertools import islice
from typing import Any
from urllib.parse import urlsplit

import openstack
from openstack import exceptions
from openstack.config import OpenStackConfig
from openstack.config.cloud_region import CloudRegion
from openstack.connection import Connection
from openstack.placement.v1.resource_provider_inventory import ResourceProviderInventory

from probe.models import (
    ComputeService,
    Flavor,
    Host,
    Image,
    Instance,
    Network,
    OpenstackResult,
    Port,
    ProbeNotice,
    Relationship,
    Volume,
    VolumeType,
)

MAX_RECORDS = 10000
# Flavor extra specs that tie instances to particular hosts or devices.
RESTRICTING_SPECS = (
    "aggregate_instance_extra_specs:",
    "pci_passthrough:",
    "resources:",
    "trait:",
)

log = logging.getLogger(__name__)


def configured_clouds() -> list[str]:
    """List clouds visible to the invoking installation identity."""
    return [name for name in OpenStackConfig().get_cloud_names() if name != "envvars"]


def resolve_cloud(name: str) -> CloudRegion:
    """Resolve and validate the specified cloud configuration."""
    config = OpenStackConfig().get_one_cloud(name)
    if config.config.get("verify") is False:
        raise ValueError("TLS certificate verification must remain enabled")
    auth = config.config.get("auth", {})
    if any(auth.get(key) for key in ("password", "application_credential_secret")):
        if urlsplit(str(auth.get("auth_url", ""))).scheme != "https":
            raise ValueError("Password authentication requires an HTTPS auth URL")
    if not config.config.get("api_timeout"):
        config.config["api_timeout"] = 30
    return config


def _bounded(records: Iterable[Any]) -> list[Any]:
    items = list(islice(records, MAX_RECORDS + 1))
    if len(items) > MAX_RECORDS:
        raise ValueError("Discovery result exceeds 10000 records for a source")
    return items


def _collect(
    records: Iterable[Any],
    source: str,
    description: str,
    notices: list[ProbeNotice],
) -> list[Any]:
    try:
        return _bounded(records)
    except exceptions.SDKException as error:
        log.debug("%s failed: %s", description, error)
        if isinstance(error, exceptions.HttpException) and error.status_code in (
            401,
            403,
        ):
            notices.append(
                ProbeNotice(
                    source=source,
                    reason="permission-denied",
                    message=f"{description}: access denied. Check read permissions "
                    "for the selected OpenStack identity.",
                )
            )
        else:
            notices.append(
                ProbeNotice(
                    source=source,
                    reason="unavailable",
                    message=f"{description} could not be read. "
                    "Check service availability and OpenStack authentication.",
                )
            )
        return []


def _placement_inventories(
    conn: Connection, provider_id: str
) -> Iterable[ResourceProviderInventory]:
    """Defer Placement discovery so unavailable services become probe notices."""
    yield from conn.placement.resource_provider_inventories(provider_id)


def probe_openstack(cloud: CloudRegion) -> tuple[OpenstackResult, list[ProbeNotice]]:
    """Collect OpenStack infrastructure details using the provided cloud region."""
    notices: list[ProbeNotice] = []

    conn = openstack.connection.Connection(config=cloud)
    conn.authorize()
    project_id = conn.current_project_id
    if not project_id:
        raise ValueError("The cloud must identify an authenticated project")
    log.debug("OpenStack: authenticated to project %s", project_id)

    hosts = [
        Host(
            id=str(item.id),
            hostname=item.name,
            host_ip=item.host_ip,
            state=item.state,
            status=item.status,
            vcpus=None,
            memory_mb=None,
        )
        for item in _collect(
            conn.compute.hypervisors(details=True),
            "nova.hypervisors",
            "OpenStack hypervisors",
            notices,
        )
    ]
    for host in hosts:
        notice_count = len(notices)
        inventory = {
            item.resource_class: item.total
            for item in _collect(
                _placement_inventories(conn, host.id),
                f"placement.inventories:{host.id}",
                f"Placement inventory for host {host.hostname or host.id}",
                notices,
            )
        }
        host.vcpus = inventory.get("VCPU")
        host.memory_mb = inventory.get("MEMORY_MB")
        if inventory:
            host.capacity_source = "placement"
        if len(notices) == notice_count and (
            host.vcpus is None or host.memory_mb is None
        ):
            notices.append(
                ProbeNotice(
                    source=f"placement.inventories:{host.id}",
                    reason="unavailable",
                    message=f"Placement inventory for host {host.hostname or host.id} "
                    "does not expose VCPU or MEMORY_MB totals. "
                    "Missing capacity remains unknown; inspect the provider inventory.",
                )
            )
    compute_services = [
        ComputeService(
            id=str(item.id), host=item.host, binary=item.binary, state=item.state
        )
        for item in _collect(
            conn.compute.services(),
            "nova.services",
            "OpenStack compute services",
            notices,
        )
    ]
    all_project_servers = _collect(
        conn.compute.servers(all_projects=True),
        "nova.servers.all-projects",
        "OpenStack instances across all projects",
        notices,
    )
    if not all_project_servers and any(
        notice.source == "nova.servers.all-projects" for notice in notices
    ):
        all_project_servers = _collect(
            conn.compute.servers(),
            "nova.servers.project",
            "OpenStack instances in the selected project",
            notices,
        )
    instances = [
        Instance(
            id=str(item.id),
            name=item.name,
            hypervisor_hostname=item.hypervisor_hostname,
            status=item.status,
            project_id=item.project_id,
        )
        for item in all_project_servers
    ]
    ports = [
        Port(
            id=str(item.id),
            network_id=item.network_id,
            device_id=item.device_id,
            binding_host_id=item.binding_host_id,
            mac_address=item.mac_address,
        )
        for item in _collect(
            conn.network.ports(), "neutron.ports", "OpenStack network ports", notices
        )
    ]
    ip_versions: dict[str, set[int]] = {}
    for subnet in _collect(
        conn.network.subnets(), "neutron.subnets", "OpenStack subnets", notices
    ):
        ip_versions.setdefault(str(subnet.network_id), set()).add(subnet.ip_version)
    networks = [
        Network(
            id=str(item.id),
            name=item.name,
            mtu=item.mtu,
            network_type=item.provider_network_type,
            physical_network=item.provider_physical_network,
            segmentation_id=item.provider_segmentation_id,
            ip_versions=sorted(ip_versions.get(str(item.id), set())),
        )
        for item in _collect(
            conn.network.networks(), "neutron.networks", "OpenStack networks", notices
        )
    ]
    images = [
        Image(
            id=str(item.id),
            name=item.name,
            os_distro=item.os_distro,
            architecture=item.architecture,
            created_at=item.created_at,
        )
        for item in _collect(
            conn.image.images(status="active"),
            "glance.images",
            "Glance images",
            notices,
        )
    ]
    flavors = [
        Flavor(
            id=str(item.id),
            name=item.name,
            vcpus=item.vcpus,
            ram_mb=item.ram,
            disk_gb=item.disk,
            restricted=not item.is_public
            or any(key.startswith(RESTRICTING_SPECS) for key in item.extra_specs or {}),
        )
        for item in _collect(
            conn.compute.flavors(), "nova.flavors", "Nova flavors", notices
        )
    ]
    volume_types = [
        VolumeType(
            id=str(item.id),
            name=item.name,
            backend=(item.extra_specs or {}).get("volume_backend_name"),
        )
        for item in _collect(
            conn.block_storage.types(), "cinder.types", "Cinder volume types", notices
        )
    ]
    volumes = [
        Volume(
            id=str(item.id),
            name=item.name,
            server_ids=[
                str(link["server_id"])
                for link in item.attachments or []
                if link.get("server_id")
            ],
            backend_host=item.host,
        )
        for item in _collect(
            conn.block_storage.volumes(), "cinder.volumes", "OpenStack volumes", notices
        )
    ]
    if any(volume.backend_host is None for volume in volumes):
        notices.append(
            ProbeNotice(
                source="cinder.backend",
                reason="unavailable",
                message="Some volumes did not expose their storage backend. "
                "Check whether the selected identity can read backend placement.",
            )
        )
    networks_by_id = {network.id for network in networks}
    instances_by_id = {instance.id for instance in instances}
    hosts_by_name: dict[str, list[str]] = {}
    for host in hosts:
        if host.hostname:
            hosts_by_name.setdefault(host.hostname, []).append(host.id)
    if any(
        instance.hypervisor_hostname
        and len(hosts_by_name.get(instance.hypervisor_hostname, [])) != 1
        for instance in instances
    ):
        notices.append(
            ProbeNotice(
                source="nova.placement",
                reason="unresolved-identity",
                message="Some instance-to-hypervisor mappings could not be verified. "
                "Check hypervisor hostnames and inventory visibility.",
            )
        )
    relationships = [
        Relationship(
            kind="member-of",
            source_id=f"neutron:port:{port.id}",
            target_id=f"neutron:network:{port.network_id}",
            evidence_ref=f"neutron:port:{port.id}",
        )
        for port in ports
        if port.network_id in networks_by_id
    ]
    relationships.extend(
        Relationship(
            kind="attached-to",
            source_id=f"neutron:port:{port.id}",
            target_id=f"nova:server:{port.device_id}",
            evidence_ref=f"neutron:port:{port.id}",
        )
        for port in ports
        if port.device_id in instances_by_id
    )
    relationships.extend(
        Relationship(
            kind="runs-on",
            source_id=f"nova:server:{instance.id}",
            target_id=f"nova:hypervisor:{hosts_by_name[instance.hypervisor_hostname][0]}",
            evidence_ref=f"nova:server:{instance.id}",
        )
        for instance in instances
        if instance.hypervisor_hostname
        and len(hosts_by_name.get(instance.hypervisor_hostname, [])) == 1
    )
    relationships.extend(
        Relationship(
            kind="attached-to",
            source_id=f"cinder:volume:{volume.id}",
            target_id=f"nova:server:{server_id}",
            evidence_ref=f"cinder:volume:{volume.id}",
        )
        for volume in volumes
        for server_id in volume.server_ids
        if server_id in instances_by_id
    )
    return OpenstackResult(
        project_id=project_id,
        hosts=hosts,
        compute_services=compute_services,
        instances=instances,
        ports=ports,
        networks=networks,
        volumes=volumes,
        relationships=relationships,
        images=images,
        flavors=flavors,
        volume_types=volume_types,
    ), notices

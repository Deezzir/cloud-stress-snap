"""Allowlisted observations printed by the read-only check command."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Record(BaseModel):
    """Reject unexpected fields in model instances."""

    model_config = ConfigDict(extra="forbid")


class Host(Record):
    """Nova hypervisor identity and Placement scheduler inventory totals."""

    id: str
    hostname: str | None
    vcpus: int | None
    memory_mb: int | None
    host_ip: str | None = None
    state: str | None = None
    status: str | None = None
    source: Literal["nova"] = "nova"
    capacity_source: Literal["placement"] | None = None
    inspection: Literal[
        "unqueried", "queried", "partial", "unreachable", "excluded"
    ] = "unqueried"


class ComputeService(Record):
    """Nova service identity and host reported by the compute API."""

    id: str
    host: str | None
    binary: str | None
    state: str | None
    source: Literal["nova"] = "nova"


class Instance(Record):
    """Nova server and its reported placement, when visible."""

    id: str
    name: str | None
    hypervisor_hostname: str | None
    status: str | None
    project_id: str | None
    source: Literal["nova"] = "nova"


class Port(Record):
    """Neutron port with explicit device and network references."""

    id: str
    network_id: str | None
    device_id: str | None
    binding_host_id: str | None
    mac_address: str | None
    source: Literal["neutron"] = "neutron"


class Network(Record):
    """Neutron network identity, without provider configuration or secrets."""

    id: str
    name: str | None
    mtu: int | None = None
    network_type: str | None = None
    physical_network: str | None = None
    segmentation_id: int | None = None
    ip_versions: list[int] = Field(default_factory=list)
    source: Literal["neutron"] = "neutron"


class Image(Record):
    """Glance image usable for run-owned guests."""

    id: str
    name: str | None
    os_distro: str | None
    created_at: str | None


class Flavor(Record):
    """Nova flavor size."""

    id: str
    name: str
    vcpus: int
    ram_mb: int
    disk_gb: int
    restricted: bool = False


class VolumeType(Record):
    """Cinder volume type and its backend name, when visible."""

    id: str
    name: str
    backend: str | None


class Volume(Record):
    """Cinder volume and its visible attachment identities."""

    id: str
    name: str | None
    server_ids: list[str]
    backend_host: str | None
    source: Literal["cinder"] = "cinder"


class ProbeNotice(Record):
    """A probe limitation with a safe, actionable explanation for the operator."""

    source: str
    message: str = Field(min_length=1)
    reason: Literal[
        "permission-denied",
        "transport-unreachable",
        "missing-tool",
        "unsupported-version",
        "unresolved-identity",
        "unavailable",
        "not-queried",
    ]


class JujuMachine(Record):
    """Machine identity remains separate from Nova until verified."""

    id: str
    parent_id: str | None
    instance_id: str | None = None
    hostname: str | None = None
    ip_addresses: list[str] = Field(default_factory=list, max_length=64)
    hypervisor_id: str | None = None


class JujuUnit(Record):
    """Juju application unit and its declared machine placement."""

    name: str
    machine_id: str | None
    parent_machine_id: str | None
    principal: str | None = None


class JujuApp(Record):
    """Juju application charm, workload version and endpoint space bindings."""

    name: str
    charm: str
    version: str | None
    bindings: dict[str, str] = Field(default_factory=dict)


class Relationship(Record):
    """A relationship established by a particular read-only source."""

    kind: Literal["runs-on", "attached-to", "member-of"]
    source_id: str
    target_id: str
    evidence_ref: str


class Disk(Record):
    """Physical block device as reported by lsblk."""

    name: str
    size_bytes: int | None
    model: str | None
    transport: str | None


class MaasInterface(Record):
    """MAAS interface with its configured VLAN, subnets and spaces."""

    name: str
    type: str
    parents: list[str]
    mtu: int | None
    link_speed_mbps: int | None
    bond_mode: str | None
    vlan_id: int | None
    fabric: str | None
    spaces: list[str]
    subnets: list[str]


class MaasMachine(Record):
    """Existing MAAS hardware record linked by a verified machine ID."""

    system_id: str
    hostname: str | None
    architecture: str | None
    cpu_count: int | None
    memory_mb: int | None
    block_devices: list[str]
    ip_addresses: list[str] = Field(default_factory=list)
    interfaces: list[MaasInterface] = Field(default_factory=list)


class NetLink(Record):
    """Physical, bond, VLAN or bridge link observed with ip -d link."""

    name: str
    kind: Literal["physical", "bond", "vlan", "bridge"]
    mtu: int | None
    state: str | None
    speed_mbps: int | None = None
    master: str | None = None
    bond_mode: str | None = None
    vlan_id: int | None = None
    parent: str | None = None


class HostProbe(Record):
    """Allowlisted results from bounded read-only host commands."""

    host_id: str
    status: Literal["queried", "partial", "unreachable"]
    architecture: str | None = None
    kernel: str | None = None
    cpu_count: int | None = None
    numa_nodes: int | None = None
    memory_bytes: int | None = None
    links: list[NetLink] = Field(default_factory=list)
    disks: list[Disk] = Field(default_factory=list)
    pci_devices: list[str] = Field(default_factory=list)


class OpenstackResult(Record):
    """OpenStack discovery results."""

    project_id: str
    hosts: list[Host]
    compute_services: list[ComputeService]
    instances: list[Instance]
    ports: list[Port]
    networks: list[Network]
    volumes: list[Volume]
    relationships: list[Relationship]
    images: list[Image] = Field(default_factory=list)
    flavors: list[Flavor] = Field(default_factory=list)
    volume_types: list[VolumeType] = Field(default_factory=list)


class JujuResult(Record):
    """Juju discovery results."""

    model: str | None = None
    machines: list[JujuMachine]
    units: list[JujuUnit]
    apps: list[JujuApp] = Field(default_factory=list)


class MaasResult(Record):
    """MAAS discovery results."""

    machines: list[MaasMachine]


class InfraResult(Record):
    """Infrastructure discovery results."""

    host_probes: list[HostProbe]


class CephOsd(Record):
    """Ceph OSD placement, device class and fill from ceph osd df tree."""

    id: int
    host: str | None
    device_class: str | None
    size_bytes: int | None
    used_percent: float | None
    pgs: int | None
    status: str | None


class CephPool(Record):
    """Ceph pool redundancy, placement and the OpenStack services using it."""

    id: int
    name: str
    type: Literal["replicated", "erasure"]
    size: int
    min_size: int
    pg_num: int
    crush_rule: int
    erasure_profile: str | None
    applications: list[str]
    used_by: list[str] = Field(default_factory=list)


class CephCrushRule(Record):
    """CRUSH rule failure domain and optional device class."""

    id: int
    name: str
    failure_domain: str | None
    device_class: str | None


class CephResult(Record):
    """Ceph health and topology read from the ceph-mon leader."""

    health: str | None = None
    health_checks: list[str] = Field(default_factory=list)
    pg_states: dict[str, int] = Field(default_factory=dict)
    quorum: list[str] = Field(default_factory=list)
    versions: dict[str, list[str]] = Field(default_factory=dict)
    osds: list[CephOsd] = Field(default_factory=list)
    pools: list[CephPool] = Field(default_factory=list)
    crush_rules: list[CephCrushRule] = Field(default_factory=list)
    public_space: str | None = None
    cluster_space: str | None = None


class OvsHost(Record):
    """OVS bridge datapath types and offload setting read through ovn-chassis."""

    host_id: str
    unit: str
    bridges: dict[str, str | None] = Field(default_factory=dict)
    hw_offload: bool | None = None
    physnets: dict[str, str] = Field(default_factory=dict)


Profile = Literal["flow-churn", "fio-sweep", "network-ceph"]


class ProfileReadiness(Record):
    """Whether existing foundations allow a profile, and what blocks it."""

    profile: Profile
    ready: bool
    blockers: list[str] = Field(default_factory=list)


class RunPlan(Record):
    """Placement a run would use, resolved from live facts."""

    sender: str | None = None
    sender_link: str | None = None
    target: str | None = None
    network: str | None = None
    vlan: int | None = None
    ip_version: Literal[4, 6] | None = None
    image: str | None = None
    flavor: str | None = None
    volume_type: str | None = None
    sender_machine: str | None = None
    target_machine: str | None = None
    target_hypervisor: str | None = None
    target_link: str | None = None
    target_ovs_unit: str | None = None
    target_datapath: str | None = None
    target_hw_offload: bool | None = None
    network_id: str | None = None
    image_id: str | None = None
    flavor_id: str | None = None
    ceph_mon: str | None = None


class CheckResult(Record):
    """Discovery results for the cloud infrastructure."""

    cloud: str

    openstack: OpenstackResult
    juju: JujuResult
    maas: MaasResult
    infra: InfraResult
    ceph: CephResult
    ovs: list[OvsHost] = Field(default_factory=list)
    readiness: list[ProfileReadiness] = Field(default_factory=list)
    plan: RunPlan = Field(default_factory=RunPlan)
    notices: list[ProbeNotice]

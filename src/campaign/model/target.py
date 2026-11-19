"""A run-owned VM as observed after creation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from probe.models import HostSenderPlan


class Target(BaseModel):
    """A run-owned VM as observed after creation."""

    model_config = ConfigDict(extra="forbid")

    name: str
    host: str
    hypervisor_hostname: str | None = None
    role: Literal["sender", "receiver", "peer"] | None = None
    server_id: str
    mgmt_port_id: str
    mgmt_address: str
    port_id: str | None = None
    mac: str | None = None
    address: str | None = None
    binding_host: str | None = None
    security_groups: list[str]
    volume_id: str | None = None
    volume_type: str | None = None
    volume_backend: str | None = None
    volume_size_gb: int | None = None


class HostTarget(HostSenderPlan):
    """A host source using a run-owned, unattached Neutron port identity."""

    machine: str
    port_id: str
    mac: str
    address: str
    rpc_port: int | None = None

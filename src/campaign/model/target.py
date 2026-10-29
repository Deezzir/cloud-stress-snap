"""The provisioned target guest as observed after creation."""

from pydantic import BaseModel, ConfigDict


class Target(BaseModel):
    """The provisioned target guest as observed after creation."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    port_id: str
    mac: str
    address: str
    mgmt_port_id: str
    mgmt_address: str
    binding_host: str | None
    security_groups: list[str]
    volume_id: str | None = None
    volume_type: str | None = None
    volume_backend: str | None = None
    volume_size_gb: int | None = None

"""A run-owned VM as observed after creation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class Target(BaseModel):
    """A run-owned VM as observed after creation."""

    model_config = ConfigDict(extra="forbid")

    name: str
    host: str
    role: Literal["sender", "receiver"] | None = None
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

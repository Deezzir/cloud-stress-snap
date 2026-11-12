"""Run configuration: duration, placement, workload sections and the schedule."""

import re
from importlib import resources
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from constants import (
    FINALIZE_BUDGET_S,
    MAX_DURATION_S,
    MIN_WINDOW_S,
    PREFILL_S_PER_GIB,
    PREPARE_BUDGET_S,
    WARMUP_S,
)

ARCHITECTURES = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "ppc64el": "ppc64el",
    "ppc64le": "ppc64el",
    "s390x": "s390x",
}


def default_config() -> str:
    """The example configuration, packaged in the wheel or at the source root."""
    packaged = resources.files("campaign") / "config.yaml"
    if packaged.is_file():
        return packaged.read_text()
    return (Path(__file__).resolve().parents[3] / "config.yaml").read_text()


def normalize_arch(name: str | None) -> str | None:
    """Canonical architecture name, or None when unknown."""
    return ARCHITECTURES.get((name or "").strip().lower())


class Strict(BaseModel):
    """Reject unknown keys."""

    model_config = ConfigDict(extra="forbid")


class TrexProfile(Strict):
    """One TRex setting on the measured timeline."""

    pps_per_sender: int = Field(gt=0)


class TrexConfig(Strict):
    """Guest-to-guest UDP traffic between the focus host and the other hosts."""

    mode: Literal["fan-in", "fan-out"]
    focus_host: str | None = None
    udp_payload_bytes: int = Field(ge=2, le=1400)
    udp_dst_port: int = Field(ge=1, le=65535)
    udp_src_ports: tuple[int, int]
    profiles: list[TrexProfile] = Field(min_length=1)

    @model_validator(mode="after")
    def _ports(self) -> Self:
        low, high = self.udp_src_ports
        if not 1 <= low <= high <= 65535:
            raise ValueError("udp_src_ports must be [low, high] within 1-65535")
        return self


class FioProfile(Strict):
    """One fio setting on the measured timeline."""

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    rw: Literal["randread", "randwrite", "randrw", "read", "write", "rw"]
    bs: str = Field(pattern=r"^[1-9]\d*[kKmM]?$")
    iodepth: int = Field(gt=0)
    rwmixread: int | None = Field(default=None, ge=0, le=100)

    @property
    def block_bytes(self) -> int:
        """Block size in bytes; k and M are powers of 1024, as in fio."""
        units = {"k": 1024, "m": 1024**2}
        unit = units.get(self.bs[-1].lower(), 1)
        return int(self.bs.rstrip("kKmM")) * unit

    @model_validator(mode="after")
    def _mix(self) -> Self:
        if self.rwmixread is not None and self.rw not in ("randrw", "rw"):
            raise ValueError("rwmixread applies only to randrw and rw")
        return self


class FioConfig(Strict):
    """fio on one Ceph-backed Cinder volume per VM."""

    volume_size_gib: int = Field(gt=0)
    numjobs: int = Field(gt=0, le=64)
    profiles: list[FioProfile] = Field(min_length=1)

    @property
    def region_bytes(self) -> int:
        """Bytes each job covers: the whole volume, or equal 1 MiB-aligned parts."""
        total = self.volume_size_gib * 1024**3
        if self.numjobs == 1:
            return total
        return total // self.numjobs // 1024**2 * 1024**2

    @model_validator(mode="after")
    def _names(self) -> Self:
        names = [p.name for p in self.profiles]
        if len(set(names)) != len(names):
            raise ValueError("fio profile names must be unique")
        for profile in self.profiles:
            if profile.block_bytes > self.region_bytes:
                raise ValueError(
                    f"{profile.name}: bs {profile.bs} is larger than the "
                    f"{self.region_bytes} bytes each job covers"
                )
        return self


class Placement(Strict):
    """Total VM count or exact per-host counts, with host and architecture filters."""

    vms: int | None = Field(default=None, gt=0)
    hosts: dict[str, int] | None = None
    architectures: list[str] = Field(default_factory=lambda: ["amd64"])
    exclude_hosts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.vms is None) == (self.hosts is None):
            raise ValueError("use exactly one of placement.vms and placement.hosts")
        if self.hosts is not None and (
            not self.hosts or any(count <= 0 for count in self.hosts.values())
        ):
            raise ValueError("placement.hosts needs positive counts")
        normalized = []
        for name in self.architectures:
            arch = normalize_arch(name)
            if arch is None:
                raise ValueError(f"unknown architecture {name!r}")
            normalized.append(arch)
        self.architectures = list(dict.fromkeys(normalized))
        return self

    @property
    def total(self) -> int:
        """Total VM count."""
        return self.vms if self.vms is not None else sum((self.hosts or {}).values())


class Workloads(Strict):
    """Enabled workloads; an omitted section is disabled."""

    trex: TrexConfig | None = None
    fio: FioConfig | None = None

    @model_validator(mode="after")
    def _any(self) -> Self:
        if self.trex is None and self.fio is None:
            raise ValueError("enable at least one workload: trex or fio")
        return self


class RunConfig(Strict):
    """A run configuration with workload defaults filled in."""

    duration: str
    placement: Placement
    workloads: Workloads

    @model_validator(mode="after")
    def _duration(self) -> Self:
        parse_duration(self.duration)
        return self

    @property
    def duration_s(self) -> int:
        """Total measured time in seconds."""
        return parse_duration(self.duration)


class ConfigError(ValueError):
    """The configuration is not valid."""


def load_config(text: str) -> RunConfig:
    """Parse a configuration; omitted workload settings come from the defaults."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(f"invalid YAML: {error}") from None
    if not isinstance(data, dict):
        raise ConfigError("the configuration must be a YAML mapping")
    defaults = yaml.safe_load(default_config())["workloads"]
    workloads = data.get("workloads")
    if isinstance(workloads, dict):
        for name, section in workloads.items():
            if name in defaults and (section is None or isinstance(section, dict)):
                workloads[name] = {**defaults[name], **(section or {})}
    try:
        return RunConfig.model_validate(data)
    except ValidationError as error:
        problems = [
            f"{'.'.join(str(p) for p in item['loc']) or 'config'}: {item['msg']}"
            for item in error.errors()
        ]
        raise ConfigError("; ".join(problems)) from None


class Window(Strict):
    """One profile's planned window on the measured timeline."""

    profile: int
    start_s: int
    length_s: int


class Schedule(Strict):
    """Planned measured windows of each enabled workload, and time budgets."""

    duration_s: int
    warmup_s: int = WARMUP_S
    trex: list[Window] = Field(default_factory=list)
    fio: list[Window] = Field(default_factory=list)
    prepare_budget_s: int
    finalize_budget_s: int

    @property
    def max_elapsed_s(self) -> int:
        """Upper bound on wall time from acceptance to a finished run."""
        return (
            self.prepare_budget_s
            + self.warmup_s
            + self.duration_s
            + self.finalize_budget_s
        )


def split(duration_s: int, count: int) -> list[Window]:
    """Divide the measured time equally, in order; earlier windows take the rest."""
    base, extra = divmod(duration_s, count)
    windows, start = [], 0
    for index in range(count):
        length = base + (1 if index < extra else 0)
        windows.append(Window(profile=index, start_s=start, length_s=length))
        start += length
    return windows


def resolve_schedule(config: RunConfig) -> Schedule:
    """Each workload divides the duration among its own profiles."""
    duration_s = config.duration_s
    trex, fio = config.workloads.trex, config.workloads.fio
    windows = {}
    for name, section in (("trex", trex), ("fio", fio)):
        if section is None:
            continue
        count = len(section.profiles)
        if duration_s // count < MIN_WINDOW_S:
            raise ConfigError(
                f"{name}: {count} profiles need a duration of at least "
                f"{format_duration(count * MIN_WINDOW_S)}"
            )
        windows[name] = split(duration_s, count)
    prepare = PREPARE_BUDGET_S
    if fio:
        prepare += fio.volume_size_gib * PREFILL_S_PER_GIB
    return Schedule(
        duration_s=duration_s,
        trex=windows.get("trex", []),
        fio=windows.get("fio", []),
        prepare_budget_s=prepare,
        finalize_budget_s=FINALIZE_BUDGET_S,
    )


def parse_duration(text: str) -> int:
    """Parse durations such as 90s, 30m, 2h or 1h30m into seconds."""
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text.strip())
    if not text.strip() or not match or not any(match.groups()):
        raise ValueError(f"invalid duration {text!r}; use for example 30m or 1h30m")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    total = hours * 3600 + minutes * 60 + seconds
    if not 0 < total <= MAX_DURATION_S:
        raise ValueError("duration must be between 1s and 24h")
    return total


def format_duration(seconds: int) -> str:
    """Format seconds as 1h30m, 5m or 45s."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    text = (f"{hours}h" if hours else "") + (f"{minutes}m" if minutes else "")
    return text + (f"{secs}s" if secs or not text else "")

"""The three built-in profiles, their phases, budgets and measured schedule."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from probe.models import Profile

PhaseName = Literal["baseline", "stress", "recovery", "sweep"]

# Source range for flood packets: RFC 3849 documentation prefix and RFC 2544
# benchmarking range, so replies never reach a real host.
FLOOD_SOURCE = {6: "2001:db8:1::", 4: "198.18.0.0"}
FLOOD_SOURCE_CIDR = {6: "2001:db8:1::/48", 4: "198.18.0.0/15"}
FLOOD_PORT = 33434


class Phase(BaseModel):
    """One measured phase with its resolved length."""

    model_config = ConfigDict(extra="forbid")

    name: PhaseName
    start_s: int
    length_s: int


class FioCell(BaseModel):
    """One fio sweep cell: workload shape and queue depth."""

    model_config = ConfigDict(extra="forbid")

    rw: Literal["randread", "randwrite", "randrw", "read", "write"]
    bs: str
    iodepth: int
    rwmixread: int | None = None
    length_s: int = 0

    @property
    def label(self) -> str:
        """Short cell name used in file names and tables."""
        mix = f"{self.rwmixread}" if self.rwmixread is not None else ""
        return f"{self.rw}{mix}-{self.bs}-qd{self.iodepth}"


class Schedule(BaseModel):
    """The frozen, resolved execution plan of one run."""

    model_config = ConfigDict(extra="forbid")

    profile: Profile
    duration_s: int
    phases: list[Phase]
    trex_steps_pps: list[int] = Field(default_factory=list)
    fio_cells: list[FioCell] = Field(default_factory=list)
    fio_rate_fraction: float | None = None
    volume_gb: int = 0
    prepare_budget_s: int
    calibrate_budget_s: int
    finalize_budget_s: int

    @property
    def max_elapsed_s(self) -> int:
        """Upper bound on wall time from acceptance to a finished run."""
        return (
            self.prepare_budget_s
            + self.calibrate_budget_s
            + self.duration_s
            + self.finalize_budget_s
        )


class ProfileSpec(BaseModel):
    """Static definition of a built-in profile."""

    model_config = ConfigDict(extra="forbid")

    weights: dict[PhaseName, int]
    min_phase_s: int
    trex_max_pps: int = 0
    trex_steps: int = 0
    fio_cells: list[FioCell] = Field(default_factory=list)
    fio_rate_fraction: float | None = None
    volume_gb: int = 0
    prepare_budget_s: int = 900
    calibrate_budget_s: int = 0
    finalize_budget_s: int = 900


SWEEP = [
    cell.model_copy(update={"iodepth": qd})
    for cell in (
        FioCell(rw="randread", bs="4k", iodepth=1),
        FioCell(rw="randwrite", bs="4k", iodepth=1),
        FioCell(rw="read", bs="1m", iodepth=1),
        FioCell(rw="write", bs="1m", iodepth=1),
    )
    for qd in (1, 16, 64)
]

PROFILES: dict[Profile, ProfileSpec] = {
    "host-network": ProfileSpec(
        weights={"baseline": 1, "stress": 4, "recovery": 1},
        min_phase_s=60,
        trex_max_pps=1_000_000,
        trex_steps=4,
        calibrate_budget_s=120,
    ),
    "ceph-block": ProfileSpec(
        weights={"sweep": 1},
        min_phase_s=30 * len(SWEEP),
        fio_cells=SWEEP,
        volume_gb=20,
        # Prefill writes the whole volume before measured reads.
        calibrate_budget_s=900,
    ),
    "network-ceph": ProfileSpec(
        weights={"baseline": 1, "stress": 3, "recovery": 1},
        min_phase_s=120,
        trex_max_pps=1_000_000,
        trex_steps=4,
        fio_cells=[FioCell(rw="randrw", rwmixread=70, bs="4k", iodepth=16)],
        fio_rate_fraction=0.5,
        volume_gb=20,
        calibrate_budget_s=1200,
    ),
}

MAX_DURATION_S = 24 * 3600


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


def resolve(profile: Profile, duration_s: int) -> Schedule:
    """Split the measured window by phase weight; reject a too-short window."""
    if not 0 < duration_s <= MAX_DURATION_S:
        raise ValueError("duration must be between 1s and 24h")
    spec = PROFILES[profile]
    total = sum(spec.weights.values())
    phases, start = [], 0
    names = list(spec.weights)
    for index, name in enumerate(names):
        length = (
            duration_s - start
            if index == len(names) - 1
            else duration_s * spec.weights[name] // total
        )
        if length < spec.min_phase_s:
            minimum = -(-spec.min_phase_s * total // min(spec.weights.values()))
            raise ValueError(
                f"{profile} needs at least {format_duration(minimum)} so that "
                f"each phase lasts {format_duration(spec.min_phase_s)} or more"
            )
        phases.append(Phase(name=name, start_s=start, length_s=length))
        start += length
    cells = [cell.model_copy() for cell in spec.fio_cells]
    if profile == "ceph-block":
        per_cell = duration_s // len(cells)
        for cell in cells:
            cell.length_s = per_cell
        cells[-1].length_s += duration_s - per_cell * len(cells)
    else:
        for cell in cells:
            cell.length_s = duration_s
    steps = [
        spec.trex_max_pps * (step + 1) // spec.trex_steps
        for step in range(spec.trex_steps)
    ]
    return Schedule(
        profile=profile,
        duration_s=duration_s,
        phases=phases,
        trex_steps_pps=steps,
        fio_cells=cells,
        fio_rate_fraction=spec.fio_rate_fraction,
        volume_gb=spec.volume_gb,
        prepare_budget_s=spec.prepare_budget_s,
        calibrate_budget_s=spec.calibrate_budget_s,
        finalize_budget_s=spec.finalize_budget_s,
    )


def format_duration(seconds: int) -> str:
    """Format seconds as 1h30m, 5m or 45s."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    text = (f"{hours}h" if hours else "") + (f"{minutes}m" if minutes else "")
    return text + (f"{secs}s" if secs or not text else "")


def uses_trex(profile: Profile) -> bool:
    """Whether the profile runs the TRex flood."""
    return PROFILES[profile].trex_steps > 0


def uses_fio(profile: Profile) -> bool:
    """Whether the profile runs fio on a Cinder volume."""
    return bool(PROFILES[profile].fio_cells)

"""Human summary of a run result: aligned tables of what was measured."""

from datetime import UTC, datetime
from typing import cast

Row = dict[str, object]


def _fmt(value: object) -> str:
    """Format a value for a table cell; "-" for missing values."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3g}" if abs(value) < 1000 else f"{value:,.0f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _table(rows: list[list[object]]) -> list[str]:
    """Format rows, header first, as indented two-space columns."""
    cells = [[_fmt(c) for c in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(cells[0]))]
    return [
        "  " + "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()
        for row in cells
    ]


def _dict(value: object) -> Row:
    """Value when it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Row]:
    """Dict items of value when it is a list."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _window(item: Row) -> str:
    return f"{_fmt(item.get('start_s'))}-{_fmt(item.get('end_s'))}s"


def _fleet_lines(result: Row) -> list[str]:
    """Hosts, VM counts and roles."""
    fleet = _dict(result.get("fleet"))
    vms = _list(fleet.get("vms"))
    rows: list[list[object]] = [["HOST", "ARCH", "VMS", "ROLES"]]
    for host in _list(fleet.get("hosts")):
        roles = sorted(
            {
                str(v.get("role") or "-")
                for v in vms
                if v.get("host") == host.get("name")
            }
        )
        name = f"{host.get('name')}{' (focus)' if host.get('focus') else ''}"
        rows.append([name, host.get("architecture"), host.get("vms"), ", ".join(roles)])
    lines = ["", "Fleet:", *_table(rows)] if len(rows) > 1 else []
    resources = [
        ["Network", fleet.get("network")],
        ["Management", fleet.get("mgmt_network")],
        ["Flavor", fleet.get("flavor")],
        ["Volume type", fleet.get("volume_type")],
    ]
    lines += _table([row for row in resources if row[1]])
    return lines


def _timeline_lines(result: Row) -> list[str]:
    """When the measured time started and how far workers started from schedule."""
    timeline = _dict(result.get("timeline"))
    start = timeline.get("measured_start")
    if not isinstance(start, int | float):
        return []
    when = datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "",
        f"Measured from {when}, after a {_fmt(timeline.get('warmup_s'))}s warm-up.",
    ]
    steps = _dict(_dict(result.get("network")).get("step_starts"))
    for step, item in steps.items():
        if step == "0":
            continue
        first, last = _dict(item).get("first"), _dict(item).get("last")
        lines.append(
            f"  TRex profile {int(step) + 1} started at {_fmt(first)}-{_fmt(last)}s "
            f"in {_fmt(_dict(item).get('senders'))} senders"
        )
    return lines


def _rates(values: Row) -> str:
    """Reporting count and min/median/max of per-VM rates."""
    if not values.get("reporting"):
        return f"0/{_fmt(values.get('of'))}"
    spread = "/".join(_fmt(values.get(f"pps_{k}")) for k in ("min", "median", "max"))
    return f"{_fmt(values.get('reporting'))}/{_fmt(values.get('of'))}  {spread}"


def _network_lines(result: Row) -> list[str]:
    """Requested and per-VM transmitted and received rates per TRex profile."""
    network = _dict(result.get("network"))
    profiles = _list(network.get("profiles"))
    if not profiles:
        return []
    rows: list[list[object]] = [
        ["PROFILE", "WINDOW", "PPS/SENDER", "TX PPS MIN/MED/MAX", "RX PPS MIN/MED/MAX"]
    ]
    for item in profiles:
        rows.append(
            [
                int(cast(int, item.get("profile"))) + 1,
                _window(item),
                item.get("pps_per_sender"),
                _rates(_dict(item.get("tx"))),
                _rates(_dict(item.get("rx"))),
            ]
        )
    return [
        "",
        f"Network ({network.get('mode')}, focus {network.get('focus')}, "
        f"{network.get('senders')} senders, {network.get('receivers')} receivers):",
        *_table(rows),
        "  TX: TRex packets per sender. RX: packets counted per receiver. Each rate",
        "  covers that VM's samples inside the window; the count before the range",
        "  is VMs with samples out of VMs.",
    ]


def _storage_lines(result: Row) -> list[str]:
    """Per-job fio results per profile, as a range over jobs."""
    storage = _dict(result.get("storage"))
    profiles = _list(storage.get("profiles"))
    if not profiles:
        return []
    rows: list[list[object]] = [
        [
            "PROFILE",
            "DIR",
            "JOBS",
            "IOPS MIN/MED/MAX",
            "MIB/S MIN/MED/MAX",
            "P99 MS MIN/MED/MAX",
        ]
    ]

    def spread(value: object, scale: float = 1.0) -> str:
        found = _dict(value)
        return "/".join(
            _fmt(v * scale if isinstance(v := found.get(k), int | float) else None)
            for k in ("min", "median", "max")
        )

    for item in profiles:
        for d in ("read", "write"):
            values = _dict(item.get(d))
            if not values:
                continue
            rows.append(
                [
                    item.get("name"),
                    d,
                    f"{_fmt(values.get('jobs'))}/{_fmt(item.get('jobs_expected'))}",
                    spread(values.get("iops")),
                    spread(values.get("bw_kib_s"), 1 / 1024),
                    spread(values.get("p99_ms")),
                ]
            )
    return [
        "",
        f"Storage (fio, {storage.get('numjobs')} job(s) per VM, per job over its "
        "own runtime):",
        *_table(rows),
    ]


def summary_text(result: Row) -> str:
    """Human summary of a result with titled, aligned sections."""
    run = _dict(result.get("run"))
    workloads = ", ".join(
        str(w) for w in cast(list[object], run.get("workloads") or [])
    )
    lines = [
        f"Run {run.get('id')} ({workloads}, {_fmt(run.get('duration_s'))}s measured)",
        *_table(
            [
                ["Outcome", f"{run.get('execution')}, exit {run.get('code')}"],
                ["Cleanup", run.get("finalization")],
            ]
        ),
    ]
    lines += _fleet_lines(result)
    lines += _timeline_lines(result)
    lines += _network_lines(result)
    lines += _storage_lines(result)
    issues = [
        *[
            str(c)
            for c in cast(list[object], run.get("causes") or [])
            if not str(c).startswith("cleanup:")
        ],
        *[str(e) for e in cast(list[object], result.get("errors") or [])],
        *[f"gap: {g}" for g in cast(list[object], result.get("gaps") or [])],
        *[
            f"not cleaned up: {r.get('kind')} {r.get('name')} ({r.get('action')})"
            for r in _list(result.get("cleanup"))
        ],
    ]
    if issues:
        lines += ["", "Issues:", *[f"  {i}" for i in dict.fromkeys(issues)]]
    return "\n".join(lines) + "\n"

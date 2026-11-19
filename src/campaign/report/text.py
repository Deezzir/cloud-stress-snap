"""Human summary of a run result: aligned tables of what was measured."""

from datetime import UTC, datetime
from typing import cast

from campaign.model.profiles import format_duration
from constants import MAX_OUTPUT_ITEMS

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
    if not rows:
        return []
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


SOURCE_HEADERS = ["Source", "Host", "Backend", "Interface", "Source IP", "Source MAC"]


def _source_rows(fleet: Row) -> list[list[object]]:
    fields = ("name", "host", "backend", "interface", "address", "mac")
    return [[s.get(k) for k in fields] for s in _list(fleet.get("host_senders"))]


def _fleet_lines(result: Row) -> list[str]:
    """Hosts, VM counts and roles."""
    fleet = _dict(result.get("fleet"))
    vms = _list(fleet.get("vms"))
    if not vms:
        return []
    rows: list[list[object]] = [["HOST", "ARCH", "VMS", "ROLES"]]
    for host in _list(fleet.get("hosts")):
        placed = [v for v in vms if v.get("host") == host.get("name")]
        if not placed:
            continue
        roles = sorted({str(v.get("role") or "-") for v in placed})
        name = f"{host.get('name')}{' (focus)' if host.get('focus') else ''}"
        rows.append([name, host.get("architecture"), len(placed), ", ".join(roles)])
    lines = ["", "Fleet:", *_table(rows)] if len(rows) > 1 else []
    if sources := _source_rows(fleet):
        lines += ["", "Host sources:", *_table([list(SOURCE_HEADERS), *sources])]
    resources = [
        ["TRex network", fleet.get("network")],
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
        f"Scheduled measurement start: {when}",
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
        f"Network ({network.get('mode')}, "
        + (f"focus {network.get('focus')}, " if network.get("focus") else "")
        + f"{network.get('senders')} senders, {network.get('receivers')} receivers):",
        *_table(rows),
        (
            "  TX/RX: reporting/total host sources or receiver VMs, "
            "then min/median/max packets/s."
            if network.get("mode") == "host-to-vm"
            else "  TX/RX: reporting/total VMs, then min/median/max packets/s per VM."
        ),
    ]


def _spread(value: object, scale: float = 1.0) -> str | None:
    """A range of per-VM or per-job values, never a fleet total."""
    values = _dict(value)
    if not values:
        return None
    return " / ".join(
        _fmt(v * scale) if isinstance(v := values.get(k), int | float) else "—"
        for k in ("min", "median", "max")
    )


STORAGE_HEADERS = [
    "Profile",
    "Window",
    "Direction",
    "Jobs",
    "IOPS",
    "MiB/s",
    "p99 (ms)",
]


def _storage_rows(storage: Row) -> list[list[object]]:
    """Per-job fio summaries shared by the terminal and HTML reports."""
    rows: list[list[object]] = []
    for item in _list(storage.get("profiles")):
        directions = [d for d in ("read", "write") if _dict(item.get(d))]
        for direction in directions or ["unavailable"]:
            values = _dict(item.get(direction))
            rows.append(
                [
                    item.get("name"),
                    _window(item),
                    direction,
                    f"{values.get('jobs', 0)}/{_fmt(item.get('jobs_expected'))}",
                    _spread(values.get("iops")),
                    _spread(values.get("bw_kib_s"), 1 / 1024),
                    _spread(values.get("p99_ms")),
                ]
            )
    return rows


def _storage_lines(result: Row) -> list[str]:
    storage = _dict(result.get("storage"))
    rows = _storage_rows(storage)
    if not rows:
        return []
    return [
        "",
        f"fio ({storage.get('numjobs')} jobs per VM; min / median / max):",
        *_table([list(STORAGE_HEADERS), *rows]),
    ]


def summary_text(result: Row, full: bool = False) -> str:
    """Human summary of a result with titled, aligned sections."""
    run = _dict(result.get("run"))
    status: list[list[object]] = [
        ["Status", f"{run.get('execution')} (exit {run.get('code')})"],
        ["Cleanup", run.get("finalization")],
    ]
    if not result.get("timeline"):
        status.append(["Measurement", "not started"])
    lines = [f"Run {run.get('id')}", *_table(status)]
    errors = [
        *[
            str(c)
            for c in cast(list[object], run.get("causes") or [])
            if not str(c).startswith("cleanup:")
        ],
        *[str(e) for e in cast(list[object], result.get("errors") or [])],
    ]
    if errors:
        lines += ["", "Errors:", *[f"  {e}" for e in dict.fromkeys(errors)]]
    cleanup = _list(result.get("cleanup"))
    if cleanup:
        retained = run.get("finalization") == "retained"
        lines += [
            "",
            "Resources retained:" if retained else "Cleanup pending:",
            *[
                f"  {r.get('kind')} {r.get('name')} ({r.get('action')})"
                for r in cleanup
            ],
            f"Clean up: cloud-stress run stop --clean {run.get('id')}"
            if retained
            else f"Retry: cloud-stress run stop {run.get('id')}",
        ]
    if full:
        workloads = ", ".join(
            str(w) for w in cast(list[object], run.get("workloads") or [])
        )
        duration = run.get("duration_s")
        lines += ["", f"Workloads: {workloads}"]
        if isinstance(duration, int):
            lines.append(f"Planned duration: {format_duration(duration)}")
        lines += _fleet_lines(result)
        lines += _timeline_lines(result)
    lines += _network_lines(result)
    lines += _storage_lines(result)
    gaps = list(
        dict.fromkeys(str(g) for g in cast(list[object], result.get("gaps") or []))
    )
    if gaps:
        shown = gaps if full else gaps[:MAX_OUTPUT_ITEMS]
        lines += ["", f"Data gaps ({len(gaps)}):", *[f"  {g}" for g in shown]]
        if len(shown) < len(gaps):
            lines += [
                f"  {len(gaps) - len(shown)} more; "
                f"cloud-stress run status {run.get('id')} --full"
            ]
    return "\n".join(lines) + "\n"

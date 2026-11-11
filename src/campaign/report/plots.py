"""PNG graphs of a run on the common measured time axis, used by the HTML report."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402

from constants import PLOT_COLORS, PLOT_FAINT  # noqa: E402

Row = dict[str, object]
Line = tuple[list[float], list[float]]
Series = tuple[str | None, Line, dict[str, object]]

DIRS = ("read", "write")
REQUESTED: dict[str, object] = {
    "color": "0.3",
    "linestyle": ":",
    "drawstyle": "default",
}
NO_LABEL: str | None = None


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _line(rows: list[Row], key: str, scale: float = 1.0) -> Line:
    """Points of one key, drawn as steps over each value's interval.

    The line breaks at a missing value and where intervals do not meet.
    """
    xs: list[float] = []
    ys: list[float] = []
    end: float | None = None
    for row in rows:
        t = _num(row.get("t"))
        if t is None:
            continue
        if key not in row:
            continue
        value = _num(row.get(key))
        if value is None:
            end = None
            continue
        interval = _num(row.get("interval_s")) or 1.0
        start = t - interval
        if end is None or abs(start - end) > 0.25 * interval:
            if xs:
                # steps-pre extends the next value backwards to this x.
                xs.append(start)
                ys.append(float("nan"))
            xs.append(start)
            ys.append(value * scale)
        xs.append(t)
        ys.append(value * scale)
        end = t
    return xs, ys


def _points(rows: list[Row], key: str) -> Line:
    """Observations of one key at their own times, such as counter readings."""
    found = [
        (t, v)
        for r in rows
        if (t := _num(r.get("t"))) is not None and (v := _num(r.get(key))) is not None
    ]
    return [t for t, _ in found], [v for _, v in found]


def _has(line: Line) -> bool:
    return any(y == y for y in line[1])


def _markers(ax: Axes, result: Row) -> None:
    """Vertical lines where a workload changes profile."""
    timeline = result.get("timeline")
    config = result.get("config")
    if not isinstance(timeline, dict) or not isinstance(config, dict):
        return
    workloads = config.get("workloads")
    workloads = workloads if isinstance(workloads, dict) else {}
    for name, style in (("trex", "-"), ("fio", "--")):
        section = workloads.get(name)
        windows = timeline.get(name)
        if not isinstance(section, dict) or not isinstance(windows, list):
            continue
        profiles = section.get("profiles")
        profiles = profiles if isinstance(profiles, list) else []
        for window in windows[1:]:
            index = int(window["profile"])
            profile = profiles[index] if index < len(profiles) else {}
            label = (
                f"TRex {profile.get('pps_per_sender', 0):,} pps"
                if name == "trex"
                else f"fio {profile.get('name', index + 1)}"
            )
            x = float(window["start_s"])
            ax.axvline(x, color="0.4", linestyle=style, linewidth=0.8)
            ax.text(
                x,
                0.98 if name == "trex" else 0.02,
                f" {label}",
                transform=ax.get_xaxis_transform(),
                fontsize=7,
                va="top" if name == "trex" else "bottom",
                rotation=90,
                color="0.3",
            )


def _panel(
    out: Path,
    slug: str,
    title: str,
    lines: list[Series],
    result: Row,
    duration: float,
    log: bool = False,
) -> dict[str, str] | None:
    """Write one panel; skip it when no line has data."""
    lines = [item for item in lines if _has(item[1])]
    if not lines:
        return None
    labels = sum(bool(label) for label, _, _ in lines)
    legend_rows = (labels + 2) // 3
    fig, ax = plt.subplots(figsize=(11, 3.8 + 0.25 * legend_rows), dpi=150)
    for label, (xs, ys), style in lines:
        # Each value covers the interval that ends at its time.
        style = {"drawstyle": "steps-pre", **style}
        ax.plot(xs, ys, label=label, **style)  # type: ignore[arg-type]
    _markers(ax, result)
    ax.set_xlim(0, duration)
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_xlabel("measured time (s)", fontsize=9)
    ax.tick_params(labelsize=9)
    ax.grid(True, linewidth=0.3)
    if log:
        ax.set_yscale("log")
    if labels:
        ax.legend(
            fontsize=8,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.2),
            ncols=min(labels, 3),
            frameon=False,
        )
    fig.tight_layout()
    fig.savefig(out / f"{slug}.png", bbox_inches="tight")
    plt.close(fig)
    return {"title": title, "file": f"{slug}.png"}


def _per_job(jobs: dict[str, list[Row]], unit: str, scale: float = 1.0) -> list[Series]:
    """One faint line per fio job and direction."""
    return [
        series
        for d in DIRS
        for series in _labelled(
            d,
            [_line(rows, f"{d}_{unit}", scale) for rows in jobs.values()],
            {"color": PLOT_COLORS[d], **PLOT_FAINT},
        )
    ]


def _labelled(label: str, lines: list[Line], style: dict[str, object]) -> list[Series]:
    """Lines in one style; only the first carries the label."""
    found = [line for line in lines if _has(line)]
    return [
        (label if index == 0 else NO_LABEL, line, style)
        for index, line in enumerate(found)
    ]


def _requested_line(result: Row) -> Line:
    """Requested pps per sender over each TRex profile window."""
    config, timeline = result.get("config"), result.get("timeline")
    if not isinstance(config, dict) or not isinstance(timeline, dict):
        return [], []
    trex = config.get("workloads", {}).get("trex") or {}
    xs: list[float] = []
    ys: list[float] = []
    for window in timeline.get("trex") or []:
        pps = float(trex["profiles"][int(window["profile"])]["pps_per_sender"])
        begin = float(window["start_s"])
        xs += [begin, begin + float(window["length_s"])]
        ys += [pps, pps]
    return xs, ys


def render(
    result: Row, metrics: dict[str, list[Row]], out: Path
) -> list[dict[str, str]]:
    """Write the run's graphs to out and list them in order."""
    run = result.get("run")
    duration = _num(run.get("duration_s")) if isinstance(run, dict) else None
    if not duration or not result.get("timeline"):
        return []
    out.mkdir(parents=True, exist_ok=True)
    per_vm = {k: v for k, v in metrics.items() if k.startswith("network-")}
    fio_vm = {k: v for k, v in metrics.items() if k.startswith("fio-")}
    hosts = {k: v for k, v in metrics.items() if k.startswith("host-")}
    guests = {k: v for k, v in metrics.items() if k.startswith("guest-")}
    ceph = metrics.get("ceph", [])
    mib = 1 / 1024
    panels: list[tuple[str, str, list[Series], bool]] = [
        (
            "network-packets",
            "Network: packets per second per VM",
            [("requested per sender", _requested_line(result), REQUESTED)]
            + _labelled(
                "transmitted per sender (TRex)",
                [_line(rows, "tx_pps") for rows in per_vm.values()],
                {"color": "tab:blue", **PLOT_FAINT},
            )
            + _labelled(
                "received per receiver (nftables counters)",
                [_line(rows, "rx_pps") for rows in per_vm.values()],
                {"color": "tab:orange", **PLOT_FAINT},
            ),
            False,
        ),
        (
            "network-counts",
            "Network: packets per VM since its first sample in the measured time",
            _labelled(
                "transmitted (TRex)",
                [_points(rows, "tx_packets") for rows in per_vm.values()],
                {
                    "color": "tab:blue",
                    "linestyle": "none",
                    "marker": ".",
                    **PLOT_FAINT,
                },
            )
            + _labelled(
                "received (nftables counters)",
                [_points(rows, "rx_packets") for rows in per_vm.values()],
                {
                    "color": "tab:orange",
                    "linestyle": "none",
                    "marker": ".",
                    **PLOT_FAINT,
                },
            ),
            False,
        ),
        (
            "network-bytes",
            "Network: bytes per second per VM (TRex frame bytes; nftables IP bytes)",
            _labelled(
                "transmitted",
                [_line(rows, "tx_bytes_per_s") for rows in per_vm.values()],
                {"color": "tab:blue", **PLOT_FAINT},
            )
            + _labelled(
                "received",
                [_line(rows, "rx_bytes_per_s") for rows in per_vm.values()],
                {"color": "tab:orange", **PLOT_FAINT},
            ),
            False,
        ),
        (
            "storage-iops",
            "Storage: fio IOPS per job",
            _per_job(fio_vm, "iops"),
            False,
        ),
        (
            "storage-bandwidth",
            "Storage: fio bandwidth per job (MiB/s)",
            _per_job(fio_vm, "bw_kib_s", mib),
            False,
        ),
        (
            "storage-latency",
            "Storage: fio completion latency p99 per job (ms)",
            _per_job(fio_vm, "p99_ms"),
            True,
        ),
        (
            "host-cpu",
            "Hosts: CPU busy (%)",
            [
                (name.removeprefix("host-"), _line(rows, "cpu.busy_pct"), {})
                for name, rows in hosts.items()
            ],
            False,
        ),
        (
            "guest-cpu",
            "Guests: CPU busy per VM (%)",
            [
                (
                    NO_LABEL,
                    _line(rows, "cpu.busy_pct"),
                    {"color": "tab:blue", **PLOT_FAINT},
                )
                for rows in guests.values()
            ],
            False,
        ),
        (
            "drops",
            "Drops (packets/s): host links, guest taps, guest NICs, OVS lost upcalls",
            [
                series
                for label, sources, color in (
                    ("host link/tap or OVS counter", hosts, "tab:blue"),
                    ("guest NIC counter", guests, "tab:orange"),
                )
                for series in _labelled(
                    label,
                    [
                        _line(rows, key)
                        for rows in sources.values()
                        for key in sorted({k for r in rows for k in r})
                        if key.endswith(("rx_dropped_per_s", "tx_dropped_per_s"))
                        or key == "ovs.lost_per_s"
                    ],
                    {"color": color, **PLOT_FAINT},
                )
            ],
            False,
        ),
        (
            "ceph-ops",
            "Ceph: client operations per second",
            [
                ("read", _line(ceph, "ceph.read_ops"), {}),
                ("write", _line(ceph, "ceph.write_ops"), {}),
            ],
            False,
        ),
        (
            "ceph-slow",
            "Ceph: slow operations",
            [(NO_LABEL, _line(ceph, "ceph.slow_ops"), {})],
            False,
        ),
    ]
    written = []
    for slug, title, lines, log in panels:
        item = _panel(out, slug, title, lines, result, duration, log)
        if item:
            written.append(item)
    return written

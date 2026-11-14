"""Static and interactive graphs on the same measured time axis."""

import html
from pathlib import Path

import matplotlib
import plotly.graph_objects as go

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

from constants import PLOT_COLORS, PLOT_FAINT  # noqa: E402

Row = dict[str, object]
Line = tuple[list[float], list[float]]
# PNG legend label, points, PNG style, interactive series identity.
Series = tuple[str | None, Line, dict[str, object], str]
Panel = tuple[str, str, list[Series], bool]

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


def _markers(result: Row) -> list[tuple[float, str, str]]:
    """Times, labels and line styles where a workload changes profile."""
    timeline = result.get("timeline")
    config = result.get("config")
    if not isinstance(timeline, dict) or not isinstance(config, dict):
        return []
    markers = []
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
            markers.append((x, label, style))
    return markers


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
    labels = sum(bool(label) for label, _, _, _ in lines)
    legend_rows = (labels + 2) // 3
    fig, ax = plt.subplots(figsize=(11, 3.8 + 0.25 * legend_rows), dpi=150)
    for label, (xs, ys), style, _ in lines:
        # Each value covers the interval that ends at its time.
        style = {"drawstyle": "steps-pre", **style}
        ax.plot(xs, ys, label=label, **style)  # type: ignore[arg-type]
    for x, label, marker_style in _markers(result):
        ax.axvline(x, color="0.4", linestyle=marker_style, linewidth=0.8)
        ax.text(
            x,
            0.98 if marker_style == "-" else 0.02,
            f" {label}",
            transform=ax.get_xaxis_transform(),
            fontsize=7,
            va="top" if marker_style == "-" else "bottom",
            rotation=90,
            color="0.3",
        )
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
            {
                f"{name.removeprefix('fio-')} {d}": _line(rows, f"{d}_{unit}", scale)
                for name, rows in jobs.items()
            },
            {"color": PLOT_COLORS[d], **PLOT_FAINT},
        )
    ]


def _labelled(
    label: str, lines: dict[str, Line], style: dict[str, object]
) -> list[Series]:
    """Named lines in one PNG style; only the first carries the PNG label."""
    found = [(name, line) for name, line in lines.items() if _has(line)]
    return [
        (label if index == 0 else NO_LABEL, line, style, name)
        for index, (name, line) in enumerate(found)
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


def _panels(result: Row, metrics: dict[str, list[Row]]) -> list[Panel]:
    """Shared panel definitions; retain each VM, job and counter separately."""
    per_vm = {k: v for k, v in metrics.items() if k.startswith("network-")}
    fio_vm = {k: v for k, v in metrics.items() if k.startswith("fio-")}
    hosts = {k: v for k, v in metrics.items() if k.startswith("host-")}
    guests = {k: v for k, v in metrics.items() if k.startswith("guest-")}
    ceph = metrics.get("ceph", [])
    mib = 1 / 1024
    return [
        (
            "network-packets",
            "Network: packets per second per VM",
            [
                (
                    "requested per sender",
                    _requested_line(result),
                    REQUESTED,
                    "requested per sender",
                )
            ]
            + _labelled(
                "transmitted per sender (TRex)",
                {
                    f"{name.removeprefix('network-')} TX": _line(rows, "tx_pps")
                    for name, rows in per_vm.items()
                },
                {"color": "tab:blue", **PLOT_FAINT},
            )
            + _labelled(
                "received per receiver (nftables counters)",
                {
                    f"{name.removeprefix('network-')} RX": _line(rows, "rx_pps")
                    for name, rows in per_vm.items()
                },
                {"color": "tab:orange", **PLOT_FAINT},
            ),
            False,
        ),
        (
            "network-counts",
            "Network: packets per VM since its first sample in the measured time",
            _labelled(
                "transmitted (TRex)",
                {
                    f"{name.removeprefix('network-')} TX": _points(rows, "tx_packets")
                    for name, rows in per_vm.items()
                },
                {
                    "color": "tab:blue",
                    "linestyle": "none",
                    "marker": ".",
                    **PLOT_FAINT,
                },
            )
            + _labelled(
                "received (nftables counters)",
                {
                    f"{name.removeprefix('network-')} RX": _points(rows, "rx_packets")
                    for name, rows in per_vm.items()
                },
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
                {
                    f"{name.removeprefix('network-')} TX": _line(rows, "tx_bytes_per_s")
                    for name, rows in per_vm.items()
                },
                {"color": "tab:blue", **PLOT_FAINT},
            )
            + _labelled(
                "received",
                {
                    f"{name.removeprefix('network-')} RX": _line(rows, "rx_bytes_per_s")
                    for name, rows in per_vm.items()
                },
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
                (
                    name.removeprefix("host-"),
                    _line(rows, "cpu.busy_pct"),
                    {},
                    name.removeprefix("host-"),
                )
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
                    name.removeprefix("guest-"),
                )
                for name, rows in guests.items()
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
                    {
                        f"{name} {key}": _line(rows, key)
                        for name, rows in sources.items()
                        for key in sorted({k for r in rows for k in r})
                        if key.endswith(("rx_dropped_per_s", "tx_dropped_per_s"))
                        or key == "ovs.lost_per_s"
                    },
                    {"color": color, **PLOT_FAINT},
                )
            ],
            False,
        ),
        (
            "ceph-ops",
            "Ceph: client operations per second",
            [
                ("read", _line(ceph, "ceph.read_ops"), {}, "read"),
                ("write", _line(ceph, "ceph.write_ops"), {}, "write"),
            ],
            False,
        ),
        (
            "ceph-slow",
            "Ceph: slow operations",
            [(NO_LABEL, _line(ceph, "ceph.slow_ops"), {}, "slow operations")],
            False,
        ),
    ]


def render(
    result: Row, metrics: dict[str, list[Row]], out: Path
) -> list[dict[str, str]]:
    """Write the run's PNG graphs to out and list them in order."""
    run = result.get("run")
    duration = _num(run.get("duration_s")) if isinstance(run, dict) else None
    if not duration or not result.get("timeline"):
        return []
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for slug, title, lines, log in _panels(result, metrics):
        item = _panel(out, slug, title, lines, result, duration, log)
        if item:
            written.append(item)
    return written


def interactive(result: Row, metrics: dict[str, list[Row]]) -> dict[str, go.Figure]:
    """Interactive counterparts of the PNGs, with independently selectable series."""
    run = result.get("run")
    duration = _num(run.get("duration_s")) if isinstance(run, dict) else None
    if not duration or not result.get("timeline") or not metrics:
        return {}
    figures = {}
    for slug, _, lines, log in _panels(result, metrics):
        fig = go.Figure()
        for _, (xs, ys), style, name in sorted(lines, key=lambda item: item[3]):
            if not _has((xs, ys)):
                continue
            requested = style == REQUESTED
            fig.add_trace(
                go.Scatter(
                    x=xs,
                    y=ys,
                    name=html.escape(name),
                    mode="markers" if style.get("linestyle") == "none" else "lines",
                    line={
                        "shape": "linear" if requested else "vh",
                        "width": 1.5,
                        "dash": "dot" if requested else "solid",
                        **({"color": "#526473"} if requested else {}),
                    },
                    marker={"size": 4},
                    connectgaps=False,
                    hovertemplate=(
                        "%{x:.3f} s<br>%{y:,.4~g}<extra>%{fullData.name}</extra>"
                    ),
                )
            )
        if not fig.data:
            continue
        for x, label, marker_style in _markers(result):
            fig.add_vline(
                x=x,
                line_width=1,
                line_color="#8795a1",
                line_dash="solid" if marker_style == "-" else "dash",
                annotation_text=html.escape(label),
                annotation_position="top right",
                annotation_textangle=-90,
                annotation_font_size=10,
            )
        fig.update_layout(
            template="plotly_white",
            font={"family": "system-ui, sans-serif", "color": "#172b3a", "size": 12},
            margin={"l": 64, "r": 24, "t": 24, "b": 48},
            xaxis={"title": "measured time (s)", "range": [0, duration]},
            yaxis={"type": "log" if log else "linear", "rangemode": "tozero"},
            hovermode="closest",
            hoverlabel={"namelength": -1},
            legend={
                "orientation": "h",
                "y": -0.2,
                "maxheight": 120,
                "font": {"size": 11},
                "itemclick": "toggle",
                "itemdoubleclick": "toggleothers",
            },
            showlegend=True,
            uirevision=slug,
        )
        figures[f"{slug}.png"] = fig
    return figures

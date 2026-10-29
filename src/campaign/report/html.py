"""Self-contained HTML report: the summary text plus inline SVG charts."""

import html as html_lib

from campaign.model.profiles import format_duration
from campaign.report.text import summary_text

Row = dict[str, object]
Points = list[tuple[float, float]]

COLORS = ("#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b")
SHADES = {"baseline": "#e8f4e8", "stress": "#fbe9e7", "recovery": "#e8eef8"}


def _num(value: object) -> float | None:
    """Return a value as float when it is numeric, else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _label(value: float) -> str:
    """Short axis label."""
    return f"{value:.3g}" if abs(value) < 1000 else f"{value:,.0f}"


def _chart(title: str, series: dict[str, Points], phases: list[Row]) -> str:
    """Inline SVG line chart with shaded phases; empty without data.

    With phases, every chart spans the same time range so charts line up.
    """
    starts = [v for p in phases if (v := _num(p.get("start"))) is not None]
    ends = [v for p in phases if (v := _num(p.get("end"))) is not None]
    if starts and ends:
        series = {
            name: [p for p in line if min(starts) <= p[0] <= max(ends)]
            for name, line in series.items()
        }
    points = [p for line in series.values() for p in line]
    if not points:
        return ""
    width, height, pad, legend = 860, 220, 50, 140
    t0, t1 = min(p[0] for p in points), max(p[0] for p in points)
    if starts and ends:
        t0, t1 = min(starts), max(ends)
    v0, v1 = min(0.0, min(p[1] for p in points)), max(p[1] for p in points)
    t1, v1 = (t1 if t1 > t0 else t0 + 1), (v1 if v1 > v0 else v0 + 1)

    right = width - legend

    def x(t: float) -> float:
        return pad + (t - t0) / (t1 - t0) * (right - pad)

    def y(v: float) -> float:
        return height - pad + (v0 - v) / (v1 - v0) * (height - 2 * pad)

    parts = [
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
    ]
    for phase in phases:
        start = max(_num(phase.get("start")) or t0, t0)
        end = min(_num(phase.get("end")) or t1, t1)
        if end > start:
            name = str(phase.get("name"))
            parts.append(
                f'<rect x="{x(start):.1f}" y="{pad}" width="{x(end) - x(start):.1f}" '
                f'height="{height - 2 * pad}" fill="{SHADES.get(name, "#f2f2f2")}"/>'
                f'<text x="{x(start) + 3:.1f}" y="{pad + 12}" font-size="10">'
                f"{html_lib.escape(name)}</text>"
            )
    for frac in (0.0, 0.5, 1.0):
        v, t = v0 + frac * (v1 - v0), t0 + frac * (t1 - t0)
        anchor = ("start", "middle", "end")[int(frac * 2)]
        parts.append(
            f'<line x1="{pad}" y1="{y(v):.1f}" x2="{right}" y2="{y(v):.1f}" '
            f'stroke="#ddd"/>'
            f'<text x="{pad - 4}" y="{y(v) + 4:.1f}" font-size="10" '
            f'text-anchor="end">{_label(v)}</text>'
            f'<text x="{x(t):.1f}" y="{height - pad + 14}" font-size="10" '
            f'text-anchor="{anchor}">{format_duration(int(t - t0))}</text>'
        )
    parts.append(
        f'<line x1="{pad}" y1="{height - pad}" x2="{right}" y2="{height - pad}" '
        f'stroke="#333"/><line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height - pad}" '
        f'stroke="#333"/>'
    )
    for index, (name, line) in enumerate(series.items()):
        color = COLORS[index % len(COLORS)]
        coords = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in sorted(line))
        parts.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
            f'points="{coords}"/><text x="{right + 8}" y="{pad + 14 * index + 4}" '
            f'font-size="10" fill="{color}">{html_lib.escape(name)}</text>'
        )
    parts.append("</svg>")
    return f"<h3>{html_lib.escape(title)}</h3>" + "".join(parts)


def _points(rows: list[Row], key: str) -> Points:
    """(t, value) points of one numeric key."""
    return [
        (t, v)
        for r in rows
        if (t := _num(r.get("t"))) is not None and (v := _num(r.get(key))) is not None
    ]


def _summed(rows: list[Row], suffix: str) -> Points:
    """(t, sum) points over every key ending with suffix."""
    points = []
    for row in rows:
        t = _num(row.get("t"))
        values = [
            v
            for k, value in row.items()
            if k.endswith(suffix) and (v := _num(value)) is not None
        ]
        if t is not None and values:
            points.append((t, sum(values)))
    return points


def html(result: dict[str, object], metrics: dict[str, list[Row]]) -> str:
    """HTML document with inline CSS and SVG charts for a result."""
    raw_phases = result.get("phases")
    phases = (
        [p for p in raw_phases if isinstance(p, dict)]
        if isinstance(raw_phases, list)
        else []
    )
    placement = result.get("placement")
    link = placement.get("target_link") if isinstance(placement, dict) else None
    target = metrics.get("target-host", [])
    cpu = [r for r in target if not r.get("cpu.reset")]
    charts = []
    for name, rows in metrics.items():
        if name.startswith("fio-") and name not in ("fio-prefill", "fio-calibration"):
            charts.append(
                _chart(
                    f"{name} latency (ms)",
                    {"p50": _points(rows, "p50_ms"), "p99": _points(rows, "p99_ms")},
                    phases,
                )
            )
            charts.append(
                _chart(f"{name} IOPS", {"iops": _points(rows, "iops")}, phases)
            )
    stress = metrics.get("stress", [])
    charts += [
        _chart(
            "Load (pps)",
            {
                "requested": _points(stress, "requested_pps"),
                "sender tx": _points(stress, "tx_pps"),
                "target rx": _points(metrics.get("rx", []), "rx_pps"),
            },
            phases,
        ),
        _chart(
            "Target CPU (%)",
            {
                "busy": _points(cpu, "cpu.busy_pct"),
                "iowait": _points(cpu, "cpu.iowait_pct"),
                "softirq": _points(cpu, "cpu.softirq_pct"),
            },
            phases,
        ),
        _chart(
            "Drops on the target path (per s)",
            {
                "link": _points(target, f"{link}.rx_dropped_per_s") if link else [],
                "NIC ring": _summed(target, ".rx_out_of_buffer_per_s"),
                "OVS lost upcalls": _points(target, "ovs.lost_per_s"),
                "guest tap": _points(target, "tap.tx_dropped_per_s"),
            },
            phases,
        ),
        _chart(
            "Ceph slow ops",
            {"slow ops": _points(metrics.get("ceph", []), "ceph.slow_ops")},
            phases,
        ),
    ]
    run = result.get("run")
    run = run if isinstance(run, dict) else {}
    title = html_lib.escape(f"Run {run.get('id')} ({run.get('profile')})")
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title><style>"
        "body{font-family:sans-serif;margin:2em;max-width:960px}"
        "pre{background:#f6f6f6;padding:1em;overflow-x:auto}"
        "h3{font-size:1em;margin:1.5em 0 .3em}</style></head><body>"
        f"<pre>{html_lib.escape(summary_text(result))}</pre>"
        f"{''.join(charts)}</body></html>\n"
    )

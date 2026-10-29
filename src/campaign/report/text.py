"""Human summary of a run result: aligned tables, no verdict.

The report states what was measured; the operator compares it with their SLOs.
"""

from typing import cast

Row = dict[str, object]


def _fmt(value: object) -> str:
    """Format a value for a table cell; "-" for missing values."""
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3g}" if abs(value) < 1000 else f"{value:,.0f}"
    if isinstance(value, int) and not isinstance(value, bool):
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


def _dicts(value: object) -> dict[str, Row]:
    """Dict-valued entries of value when it is a dict."""
    return {k: v for k, v in _dict(value).items() if isinstance(v, dict)}


def _pct(value: object, digits: int = 0) -> str:
    """Fraction as a percentage."""
    return f"{value:.{digits}%}" if isinstance(value, int | float) else "-"


def ceiling(steps: list[Row]) -> str | None:
    """Plain sentence when TRex delivered less than requested at the top step."""
    if not steps:
        return None
    top = steps[-1]
    requested, sent = top.get("requested_pps"), top.get("tx_pps")
    if not isinstance(requested, int | float) or not isinstance(sent, int | float):
        return None
    if sent >= 0.95 * requested:
        return None
    return (
        f"TRex delivered {sent:,.0f} of {requested:,.0f} pps at the top step "
        f"({sent / requested:.0%}); the target was not measured above "
        f"{sent:,.0f} pps."
    )


def summary_text(result: dict[str, object]) -> str:
    """Human summary of a result with titled, aligned sections."""
    run = _dict(result.get("run"))
    lines = [
        f"Run {run.get('id')} ({run.get('profile')}, {run.get('duration')})",
        *_table(
            [
                ["EXECUTION", "FINALIZATION", "CODE"],
                [run.get("execution"), run.get("finalization"), run.get("code")],
            ]
        ),
    ]
    manifest = _dict(result.get("manifest"))
    placement = {
        **{k: manifest.get(k) for k in ("cloud", "model", "snap_revision")},
        **_dict(result.get("placement")),
    }
    rows: list[list[object]] = [
        [k.upper(), v] for k, v in placement.items() if v is not None
    ]
    if rows:
        lines += ["", "Placement:", *_table(rows)]

    indicators = _list(result.get("indicators"))
    if indicators:
        rows = [["INDICATOR", "WHERE", "VALUE"]]
        rows += [
            [i.get("name"), i.get("where"), f"{_fmt(i.get('value'))} {i.get('unit')}"]
            for i in indicators
        ]
        lines += ["", "SLO indicators (compare with your targets):", *_table(rows)]

    sender = _dict(result.get("sender"))
    target = _dict(result.get("target"))
    sent = {s.get("step"): s for s in _list(sender.get("steps"))}
    received = _list(target.get("steps"))
    if sent:
        rows = [["STEP", "REQUESTED", "SENT", "DELIVERED", "RECEIVED", "LOSS"]]
        for step in received or [{"step": k} for k in sent]:
            s = sent.get(step.get("step"), {})
            rows.append(
                [
                    step.get("step"),
                    s.get("requested_pps"),
                    s.get("tx_pps"),
                    _pct(s.get("delivered")),
                    step.get("rx_pps"),
                    _pct(step.get("loss"), 1),
                ]
            )
        lines += ["", "Network load (pps, sender to target):", *_table(rows)]
        note = ceiling(list(sent.values()))
        if note:
            lines.append(f"  {note}")
    receipt = _dict(target.get("receipt"))
    if receipt:
        lines.append(
            f"  preflight: target received {_pct(receipt.get('ratio'))} "
            f"of a {_fmt(receipt.get('sent_pps'))} pps burst"
        )

    fio_result = _dict(_dict(result.get("guest")).get("fio"))
    rows = [["CELL", "IOS", "IOPS", "P50 MS", "P99 MS", "COMPLETE"]]
    keys = ("total_ios", "iops", "p50_ms", "p99_ms", "complete")
    rows += [
        [c.get("name"), *(c.get(k) for k in keys)]
        for c in _list(fio_result.get("cells"))
        if "iops" in c and c.get("name") not in ("prefill", "calibration", "measured")
    ]
    rows += [
        [name, p.get("ios"), p.get("iops"), p.get("p50_ms"), p.get("p99_ms"), ""]
        for name, p in _dicts(fio_result.get("phases")).items()
    ]
    if len(rows) > 1:
        lines += ["", "Guest fio:", *_table(rows)]
    if fio_result.get("offered_iops") is not None:
        lines.append(f"  offered {_fmt(fio_result.get('offered_iops'))} IOPS")

    host = _dicts(target.get("host"))
    ceph = _dicts(result.get("ceph"))
    if host:
        rows = [["PHASE", "CPU BUSY %", "IOWAIT %", "SOFTIRQ %", "HOST DROPS"]]
        rows[0] += ["NIC RING DROPS", "LOST UPCALLS", "CEPH SLOW OPS"]
        columns = (
            "cpu_busy_pct",
            "cpu_iowait_pct",
            "cpu_softirq_pct",
            "host_drops",
            "nic_ring_drops",
            "lost_upcalls",
        )
        rows += [
            [n, *(h.get(k) for k in columns), ceph.get(n, {}).get("slow_ops_max")]
            for n, h in host.items()
        ]
        lines += ["", "Target host:", *_table(rows)]
    datapath = _dict(result.get("datapath"))
    if datapath:
        kind, offload = _fmt(datapath.get("kind")), _fmt(datapath.get("offload"))
        lines.append(f"  datapath {kind}, offload {offload}")

    issues = [
        *[str(c) for c in cast(list[object], run.get("causes") or [])],
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

"""Run result model: report.json, metric series, and the files built from them.

Everything is computed from the run bundle, so a partial bundle still produces a
result with its gaps listed rather than an error.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import cast

from campaign.model.profiles import format_duration
from campaign.model.state import RunState, events, now, outstanding, run_dir
from campaign.report.html import html
from campaign.report.text import summary_text
from campaign.workloads import collect, fio

Row = dict[str, object]
Window = tuple[str, float, float]

COLLECTORS = ("target-host", "sender-host", "ceph")
MAX_FILE_BYTES = 64 * 1024**2
MAX_CLOCK_OFFSET_S = 2.0
PLACEMENT = (
    "sender",
    "sender_link",
    "target",
    "network",
    "vlan",
    "ip_version",
    "mgmt_network",
    "image",
    "flavor",
    "volume_type",
    "target_link",
)


def _num(value: object) -> float | None:
    """Return a value as float when it is numeric, else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _mean(values: list[float]) -> float | None:
    """Rounded arithmetic mean, or None for no values."""
    return round(sum(values) / len(values), 2) if values else None


def _ratio(part: object, whole: object) -> float | None:
    """Rounded ratio, or None when either side is missing or zero."""
    a, b = _num(part), _num(whole)
    return round(a / b, 3) if a is not None and b else None


def _text(bundle: Path, rel: str, gaps: list[str]) -> str | None:
    """Read a bundle file unless it is missing or too large to load."""
    path = bundle / rel
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_FILE_BYTES:
        gaps.append(f"{rel}: larger than 64 MiB, not read")
        return None
    return path.read_text(errors="replace")


def _jsonl(bundle: Path, rel: str, gaps: list[str]) -> list[Row]:
    """Read JSON-object lines, recording malformed ones as a gap."""
    rows: list[Row] = []
    bad = 0
    for line in (_text(bundle, rel, gaps) or "").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            item = None
        if isinstance(item, dict):
            rows.append(item)
        else:
            bad += 1
    if bad:
        gaps.append(f"{bad} malformed lines in {rel}")
    return rows


def _rx_log(
    bundle: Path, gaps: list[str]
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Guest receive and drop counter samples as (epoch seconds, count) lists."""
    rel = "raw/rx/rx.log"
    points: list[tuple[float, float]] = []
    drops: list[tuple[float, float]] = []
    bad = 0
    for line in (_text(bundle, rel, gaps) or "").splitlines():
        if not line.strip():
            continue
        try:
            t, count, *rest = line.split()
            points.append((float(t), float(count)))
            if rest:
                drops.append((float(t), float(rest[0])))
        except ValueError:
            bad += 1
    if bad:
        gaps.append(f"{bad} malformed lines in {rel}")
    return sorted(points), sorted(drops)


def _delta(points: list[tuple[float, float]], start: float, end: float) -> int | None:
    """Counter increase inside [start, end], or None without a clean pair."""
    inside = [p for p in points if start <= p[0] <= end]
    if len(inside) < 2 or inside[-1][1] < inside[0][1]:
        return None
    return round(inside[-1][1] - inside[0][1])


def _rx_pps(rx: list[tuple[float, float]], start: float, end: float) -> float | None:
    """Received pps from the counter delta inside [start, end]."""
    inside = [p for p in rx if start <= p[0] <= end]
    if len(inside) < 2:
        return None
    (t0, c0), (t1, c1) = inside[0], inside[-1]
    if t1 <= t0 or c1 < c0:
        return None
    return round((c1 - c0) / (t1 - t0), 1)


def _rx_series(rx: list[tuple[float, float]]) -> list[Row]:
    """Per-interval receive rate rows for charting."""
    return [
        {"t": t1, "rx_pps": round((c1 - c0) / (t1 - t0), 1)}
        for (t0, c0), (t1, c1) in zip(rx, rx[1:], strict=False)
        if t1 > t0 and c1 >= c0
    ]


def _phases(state: RunState, evts: list[Row]) -> list[Window]:
    """Measured phase windows (name, start, end) from measured-phase events."""
    starts = [
        (str(e.get("phase")), datetime.fromisoformat(str(e["at"])).timestamp())
        for e in evts
        if e.get("event") == "measured-phase"
    ]
    ends = [
        datetime.fromisoformat(str(e["at"])).timestamp()
        for e in evts
        if e.get("event") in ("phase", "finalized")
    ]
    windows: list[Window] = []
    for index, (name, start) in enumerate(starts):
        if index + 1 < len(starts):
            end = starts[index + 1][1]
        else:
            # The last phase ends at its planned length or the first later
            # lifecycle event, whichever comes first.
            length = next(
                (p.length_s for p in state.schedule.phases if p.name == name), 0
            )
            end = min([start + length, *(t for t in ends if t > start)])
        windows.append((name, start, end))
    return windows


def _inside(rows: list[Row], start: float, end: float) -> list[Row]:
    """Rows whose time t falls in [start, end)."""
    return [r for r in rows if (t := _num(r.get("t"))) is not None and start <= t < end]


def _values(rows: list[Row], key: str) -> list[float]:
    """Numeric values of one key."""
    return [v for r in rows if (v := _num(r.get(key))) is not None]


def _fio_dir(
    bundle: Path, path: Path, gaps: list[str]
) -> tuple[Row, list[Row], dict[int, list[int]]]:
    """Read one fio output directory: summary, per-second rows, merged histogram.

    Per-job histograms are merged by summing bins so percentiles are computed over
    all I/Os, never as a maximum of per-job percentiles. Keys are epoch seconds.
    """
    jsons = sorted(path.glob("*.json"))
    label = jsons[0].stem if jsons else path.name
    rel = f"raw/{path.name}/{label}"
    info: Row = {"name": path.name, "label": label}
    text = _text(bundle, f"{rel}.json", gaps)
    try:
        info.update(fio.summary(text or ""))
    except (ValueError, KeyError, TypeError) as error:
        reason = f"unreadable fio result: {error}" if text else "no fio result"
        info.update({"complete": False, "error": reason})
    try:
        start = int(float((_text(bundle, f"{rel}.start", gaps) or "").strip()))
    except ValueError:
        info["error"] = info.get("error") or "no start time"
        return info, [], {}
    hist: dict[int, list[int]] = {}
    iops: dict[int, int] = {}
    for job in range(1, fio.NUMJOBS + 1):
        try:
            job_hist = fio.parse_hist(
                _text(bundle, f"{rel}_clat_hist.{job}.log", gaps) or ""
            )
            job_iops = fio.parse_iops(
                _text(bundle, f"{rel}_iops.{job}.log", gaps) or ""
            )
        except (ValueError, IndexError):
            gaps.append(f"{rel}: unreadable fio log for job {job}")
            continue
        for second, bins in job_hist.items():
            old = hist.get(start + second)
            hist[start + second] = (
                [a + b for a, b in zip(old, bins, strict=False)] if old else bins
            )
        for second, count in job_iops.items():
            iops[start + second] = iops.get(start + second, 0) + count
    rows: list[Row] = [
        {
            "t": second,
            "iops": iops.get(second),
            "p50_ms": fio.percentile(hist.get(second, []), 0.5),
            "p99_ms": fio.percentile(hist.get(second, []), 0.99),
        }
        for second in sorted(set(hist) | set(iops))
    ]
    return info, rows, hist


def _window_fio(
    rows: list[Row], hist: dict[int, list[int]], start: float, end: float
) -> Row:
    """Completed I/Os, IOPS and p50/p99 latency in ms over one window."""
    merged: list[int] = []
    for second, bins in hist.items():
        if start <= second < end:
            merged = (
                [a + b for a, b in zip(merged, bins, strict=False)] if merged else bins
            )
    counts = _values(_inside(rows, start, end), "iops")
    return {
        "ios": int(sum(counts)) if counts else None,
        "iops": _mean(counts),
        "p50_ms": fio.percentile(merged, 0.5),
        "p99_ms": fio.percentile(merged, 0.99),
    }


def _count(rows: list[Row], keys: set[str]) -> int | None:
    """Total events over rows from the per-second rates of the given keys."""
    found = [
        v * (_num(r.get("interval_s")) or 1.0)
        for r in rows
        for key in keys
        if (v := _num(r.get(key))) is not None
    ]
    return round(sum(found)) if found else None


def _host(
    target: list[Row],
    link: str | None,
    window: Window,
    guest_drops: list[tuple[float, float]],
) -> Row:
    """Target host evidence over one window.

    Bond-level drops and the members' driver ring drops count overlapping packets,
    so they are reported separately and never summed.
    """
    _, start, end = window
    inside = _inside(target, start, end)
    cpu = [r for r in inside if not r.get("cpu.reset")]
    rings = {k for r in inside for k in r if k.endswith(".rx_out_of_buffer_per_s")}
    return {
        "cpu_busy_pct": _mean(_values(cpu, "cpu.busy_pct")),
        "cpu_iowait_pct": _mean(_values(cpu, "cpu.iowait_pct")),
        "cpu_softirq_pct": _mean(_values(cpu, "cpu.softirq_pct")),
        "host_drops": _count(inside, {f"{link}.rx_dropped_per_s"}) if link else None,
        "nic_ring_drops": _count(inside, rings),
        "lost_upcalls": _count(inside, {"ovs.lost_per_s"}),
        "tap_drops": _count(inside, {"tap.tx_dropped_per_s"}),
        "guest_drops": _delta(guest_drops, start, end),
    }


def _ceph(ceph: list[Row], window: Window) -> Row:
    """Ceph slow operations over one window."""
    _, start, end = window
    slow = _values(_inside(ceph, start, end), "ceph.slow_ops")
    return {"slow_ops_max": max(slow) if slow else None}


def _load(samples: list[Row], rx: list[tuple[float, float]]) -> list[Row]:
    """Per TRex step: requested, sender tx and target rx pps, and loss."""
    steps: dict[int, list[Row]] = {}
    for row in samples:
        if isinstance(step := row.get("step"), int):
            steps.setdefault(step, []).append(row)
    out: list[Row] = []
    for step, rows in sorted(steps.items()):
        times = _values(rows, "t")
        tx = _mean(_values(rows, "tx_pps"))
        rx_pps = _rx_pps(rx, min(times), max(times)) if times else None
        out.append(
            {
                "step": step,
                "requested_pps": _num(rows[0].get("requested_pps")),
                "tx_pps": tx,
                "rx_pps": rx_pps,
                "delivered": _ratio(tx, rows[0].get("requested_pps")),
                # Sample windows can make rx exceed tx slightly; that is no loss.
                "loss": max(round(1 - rx_pps / tx, 3), 0.0)
                if rx_pps is not None and tx
                else None,
                "oerrors": max(_values(rows, "oerrors"), default=0),
            }
        )
    return out


def _indicator(name: str, where: str, value: object, unit: str = "") -> Row:
    """One measured value an operator compares with their SLO."""
    return {"name": name, "where": where, "value": value, "unit": unit}


def _indicators(
    steps: list[Row],
    fio_phases: dict[str, Row],
    host: dict[str, Row],
    ceph: dict[str, Row],
) -> list[Row]:
    """SLO-relevant measurements, without a verdict."""
    rows = []
    if steps:
        top = steps[-1]
        where = f"top step ({_fmt_pps(top['requested_pps'])} requested)"
        rows.append(_indicator("sender delivered", where, top["tx_pps"], "pps"))
        if (loss := _num(top["loss"])) is not None:
            rows.append(_indicator("target loss", where, round(100 * loss, 2), "%"))
    base = fio_phases.get("baseline", {})
    for phase, values in fio_phases.items():
        rows.append(_indicator("fio IOPS", phase, values.get("iops")))
        rows.append(_indicator("fio p99 latency", phase, values.get("p99_ms"), "ms"))
        if phase != "baseline":
            change = _ratio(values.get("p99_ms"), base.get("p99_ms"))
            if change is not None:
                rows.append(
                    _indicator(
                        "fio p99 vs baseline", phase, round(100 * (change - 1), 1), "%"
                    )
                )
    stress = host.get("stress", {})
    for key, name in (
        ("host_drops", "target host drops"),
        ("nic_ring_drops", "target NIC ring drops"),
        ("lost_upcalls", "OVS lost upcalls"),
        ("tap_drops", "guest tap drops"),
        ("guest_drops", "guest NIC drops"),
    ):
        if stress.get(key) is not None:
            rows.append(_indicator(name, "stress", stress[key], "packets"))
    slow = [v for c in ceph.values() if (v := _num(c.get("slow_ops_max"))) is not None]
    if slow:
        rows.append(_indicator("Ceph slow ops (max)", "run", max(slow)))
    return rows


def _fmt_pps(value: object) -> str:
    """Rate with thousands separators."""
    number = _num(value)
    return f"{number:,.0f} pps" if number is not None else "-"


def _run(state: RunState, finished: str) -> Row:
    """Run identity and outcome fields from the state."""
    return {
        "id": state.id,
        "profile": state.profile,
        "duration": format_duration(state.duration_s),
        "created": state.created_at.isoformat(),
        "finished": finished,
        "execution": state.execution,
        "finalization": state.finalization,
        "code": state.code,
        "causes": state.causes,
    }


def build(state: RunState) -> dict[str, object]:
    """Compute the run result from the bundle without writing anything."""
    bundle = run_dir(state.id)
    plan = state.plan
    evts = events(state.id)
    gaps: list[str] = []
    try:
        target: Row = json.loads(_text(bundle, "target.json", gaps) or "")
    except ValueError:
        target = {}
    try:
        manifest: object = json.loads(_text(bundle, "manifest.json", gaps) or "")
    except ValueError:
        manifest = None
        gaps.append("manifest.json missing")
    # A run that never reached measurement is not assessed; its cause says why.
    windows = _phases(state, evts)
    required = ["target-host"]
    if state.profile in ("flow-churn", "network-ceph"):
        required.append("sender-host")
    if state.profile in ("fio-sweep", "network-ceph"):
        required.append("ceph")

    metrics: dict[str, list[Row]] = {}
    problems: dict[str, list[str]] = {}
    for name in COLLECTORS:
        found = problems.setdefault(name, [])
        raw = _jsonl(bundle, f"raw/{name}.jsonl", gaps)
        try:
            rows = collect.normalize("\n".join(json.dumps(r) for r in raw))
        except (KeyError, TypeError) as error:
            rows = []
            found.append(f"unreadable samples ({error})")
        if not rows:
            found.append("no samples")
        metrics[name] = rows
        if any(r.get("gap") for r in rows):
            found.append("sample gaps")
        resets = sorted({k for r in rows for k in r if k.endswith(".reset")})
        if resets:
            found.append(f"counter resets ({', '.join(resets)})")
    for item in evts:
        kind = item.get("event")
        if kind in ("collector-failed", "evidence-lost"):
            gaps.append(f"{kind}: {item.get('name', '')} {item.get('message', '')}")
        elif kind == "collector":
            offset = _num(item.get("clock_offset_s"))
            if offset is not None and abs(offset) > MAX_CLOCK_OFFSET_S:
                problems.setdefault(str(item.get("name")), []).append(
                    f"clock offset {offset:.1f}s"
                )
    if windows:
        gaps += [f"{n}: {p}" for n in required for p in problems.get(n, [])]

    fio_cells: list[Row] = []
    fio_phases: dict[str, Row] = {}
    raw_dir = bundle / "raw"
    for path in (
        sorted(p for p in raw_dir.iterdir() if p.is_dir()) if raw_dir.is_dir() else []
    ):
        if path.name == "rx":
            continue
        info, rows, hist = _fio_dir(bundle, path, gaps)
        metrics[f"fio-{path.name}"] = rows
        if path.name == "measured":
            fio_phases = {w[0]: _window_fio(rows, hist, w[1], w[2]) for w in windows}
        elif path.name not in ("prefill", "calibration"):
            info.update(_window_fio(rows, hist, 0, float("inf")))
        fio_cells.append(info)
    calibration = next((e for e in evts if e.get("event") == "calibration"), {})
    fio_result: Row = {
        "cells": fio_cells,
        "offered_iops": calibration.get("offered_iops"),
        "calibrated_iops": calibration.get("measured_iops"),
        "phases": fio_phases,
    }
    base = fio_phases.get("baseline", {})
    for name in ("stress", "recovery") if fio_phases else ():
        for key in ("iops", "p50_ms", "p99_ms"):
            fio_result[f"{name}_vs_baseline_{key}"] = _ratio(
                fio_phases.get(name, {}).get(key), base.get(key)
            )

    rx, guest_drops = _rx_log(bundle, gaps)
    samples = _jsonl(bundle, "raw/stress.jsonl", gaps)
    metrics["stress"] = samples
    metrics["rx"] = _rx_series(rx)
    steps = _load(samples, rx)

    # Offload is observed only when offloaded flows rise from baseline to stress.
    offload = "not-applicable"
    if plan.target_datapath == "doca" and plan.target_hw_offload:
        means = {
            name: _mean(
                _values(
                    _inside(metrics["target-host"], start, end), "ovs.offloaded_flows"
                )
            )
            for name, start, end in windows
        }
        stress, baseline = means.get("stress"), means.get("baseline")
        rising = stress is not None and baseline is not None and stress > baseline
        offload = "observed" if rising else "unverified"

    host = {
        w[0]: _host(metrics["target-host"], plan.target_link, w, guest_drops)
        for w in windows
    }
    ceph = {w[0]: _ceph(metrics["ceph"], w) for w in windows} if metrics["ceph"] else {}
    return {
        "run": _run(state, state.updated_at.isoformat()),
        "manifest": manifest,
        "placement": {**{k: getattr(plan, k) for k in PLACEMENT}, **target},
        "tools": {
            str(e.get("name")): e.get("version")
            for e in evts
            if e.get("event") == "tool"
        },
        "schedule": [p.model_dump() for p in state.schedule.phases],
        "phases": [{"name": n, "start": s, "end": e} for n, s, e in windows],
        "sender": {
            "planned_pps": state.schedule.trex_steps_pps,
            "steps": [
                {k: step[k] for k in ("step", "requested_pps", "tx_pps", "delivered")}
                | {"oerrors": step["oerrors"]}
                for step in steps
            ],
        },
        "target": {
            "receipt": next((e for e in evts if e.get("event") == "receipt"), None),
            "steps": [
                {k: step[k] for k in ("step", "tx_pps", "rx_pps", "loss")}
                for step in steps
            ],
            "host": host,
        },
        "guest": {"fio": fio_result},
        "ceph": ceph,
        "indicators": _indicators(steps, fio_phases, host, ceph),
        "datapath": {
            "kind": "kernel"
            if plan.target_datapath in (None, "system")
            else plan.target_datapath,
            "hw_offload": plan.target_hw_offload,
            "offload": offload,
        },
        "errors": [str(e.get("message")) for e in evts if e.get("event") == "error"],
        "gaps": gaps,
        "cleanup": [
            {"kind": e.kind, "name": e.name, "action": e.action, "where": e.where}
            for e in outstanding(state.id)
        ],
        # Popped by write(): metric series are written to metrics/, not report.json.
        "_metrics": metrics,
    }


def exit_code(state: RunState) -> int:
    """Process exit code: 0 completed, 30 cancelled, 20 failed or not cleaned up."""
    if state.finalization != "complete" or state.execution in ("failed", "interrupted"):
        return 20
    return 30 if state.execution == "cancelled" else 0


def write(state: RunState, result: dict[str, object]) -> None:
    """Stamp the final state into result and write every report file from it."""
    bundle = run_dir(state.id)
    metrics = cast(dict[str, list[Row]], result.pop("_metrics", {}))
    result["run"] = _run(state, now().isoformat())
    (bundle / "metrics").mkdir(parents=True, exist_ok=True)
    for name, rows in metrics.items():
        if name in COLLECTORS or name.startswith("fio-"):
            text = "".join(json.dumps(row) + "\n" for row in rows)
            (bundle / "metrics" / f"{name}.jsonl").write_text(text)
    (bundle / "report.json").write_text(json.dumps(result, indent=2, default=str))
    (bundle / "summary.txt").write_text(summary_text(result))
    (bundle / "report.html").write_text(html(result, metrics))

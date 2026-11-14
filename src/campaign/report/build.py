"""Run result: report.json, metric series and graphs, built from the run bundle.

Everything is computed from the bundle, so a partial bundle still produces a
result with its gaps listed rather than an error. Missing samples stay missing
and are never counted as zero. Values are per VM or per fio job; nothing is
added up across VMs.
"""

import json
from pathlib import Path
from typing import NamedTuple, cast

from campaign.model.state import RunState, events, now, outstanding, run_dir
from campaign.model.target import Target
from campaign.report import plots
from campaign.workloads import collect, fio
from constants import (
    FIO_DIRECTIONS,
    FIO_LOG_MS,
    MAX_CLOCK_OFFSET_S,
    MAX_REPORT_FILE_BYTES,
    SAMPLE_INTERVAL_S,
)
from probe.models import Workload

Row = dict[str, object]
Samples = list[tuple[float, float]]
DIRECTIONS = tuple(FIO_DIRECTIONS.values())


def _num(value: object) -> float | None:
    """Return a value as float when it is numeric, else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _text(bundle: Path, rel: str, gaps: list[str]) -> str | None:
    """Read a bundle file unless it is missing or too large to load."""
    path = bundle / rel
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_REPORT_FILE_BYTES:
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


def _samples(rows: list[Row], key: str) -> Samples:
    """(time, value) of one numeric key, in time order."""
    return sorted(
        (t, v)
        for r in rows
        if (t := _num(r.get("t"))) is not None and (v := _num(r.get(key))) is not None
    )


def _windows(state: RunState, start: float, workload: Workload) -> list[Row]:
    """Planned measured windows of one workload as absolute times."""
    schedule = getattr(state.schedule, workload)
    return [
        {
            "profile": w.profile,
            "start": start + w.start_s,
            "end": start + w.start_s + w.length_s,
        }
        for w in schedule
    ]


def _timeline(evts: list[Row]) -> tuple[float, float, float] | None:
    """Warm-up start, measured start and end, if the run reached measurement."""
    item = next((e for e in evts if e.get("event") == "timeline"), None)
    if not item:
        return None
    values = [
        _num(item.get(k)) for k in ("warmup_start", "measured_start", "measured_end")
    ]
    if any(v is None for v in values):
        return None
    warm, start, end = (float(v or 0) for v in values)
    return warm, start, end


def _rate_inside(samples: Samples, a: float, b: float) -> Row | None:
    """Rate of a counter between its first and last sample inside [a, b].

    None when there are fewer than two samples or the counter went backwards.
    """
    inside = [p for p in samples if a <= p[0] <= b]
    if len(inside) < 2 or inside[-1][0] <= inside[0][0]:
        return None
    if any(c1 < c0 for (_, c0), (_, c1) in zip(inside, inside[1:], strict=False)):
        return None
    (t0, c0), (t1, c1) = inside[0], inside[-1]
    return {"rate": (c1 - c0) / (t1 - t0), "from": t0, "to": t1}


def _spread(rates: dict[str, Row | None], start: float) -> Row:
    """Per-VM rates in a window and their range; each over its own sample span."""
    found = {vm: r for vm, r in rates.items() if r}
    values = sorted(float(str(r["rate"])) for r in found.values())
    return {
        "reporting": len(found),
        "of": len(rates),
        "pps_min": round(values[0], 1) if values else None,
        "pps_median": round(values[len(values) // 2], 1) if values else None,
        "pps_max": round(values[-1], 1) if values else None,
        "vms": {
            vm: {
                "pps": round(float(str(r["rate"])), 1),
                "from_s": round(float(str(r["from"])) - start, 3),
                "to_s": round(float(str(r["to"])) - start, 3),
            }
            for vm, r in found.items()
        },
    }


def _network(
    state: RunState,
    bundle: Path,
    start: float,
    evts: list[Row],
    gaps: list[str],
    metrics: dict[str, list[Row]],
) -> Row:
    """TRex requested and transmitted traffic, and packets counted at receivers."""
    trex = state.config.workloads.trex
    assert trex
    senders = [t for t in state.targets if t.role == "sender"]
    receivers = [t for t in state.targets if t.role == "receiver"]
    connections = state.plan.connections
    packets: dict[str, Samples] = {}
    step_starts: dict[int, list[float]] = {}
    for vm in senders:
        rows = _jsonl(bundle, f"raw/{vm.name}/trex/flood.jsonl", gaps)
        if not rows:
            gaps.append(f"{vm.name}: no TRex samples")
        stats = [r for r in rows if "event" not in r]
        packets[vm.name] = _samples(stats, "opackets")
        for row in rows:
            if row.get("event") == "step" and isinstance(row.get("step"), int):
                t = _num(row.get("t"))
                if t is not None:
                    step_starts.setdefault(int(str(row["step"])), []).append(t)
        metrics[f"network-{vm.name}"] = _counter_rows(
            vm.name, packets[vm.name], _samples(stats, "obytes"), "tx", start, gaps
        )
    for vm in receivers:
        rows = _jsonl(bundle, f"raw/{vm.name}/sampler/samples.jsonl", gaps)
        mine = [c.sender for c in connections if c.receiver == vm.name]
        keys = [f"nft.from_{s}.{k}" for s in mine for k in ("packets", "bytes")]
        complete = [r for r in rows if all(_num(r.get(k)) is not None for k in keys)]
        if len(complete) < len(rows):
            gaps.append(
                f"{vm.name}: {len(rows) - len(complete)} of {len(rows)} samples "
                "without every receive counter"
            )
        totals = [
            {
                "t": r["t"],
                "packets": sum(float(str(r[f"nft.from_{s}.packets"])) for s in mine),
                "bytes": sum(float(str(r[f"nft.from_{s}.bytes"])) for s in mine),
            }
            for r in complete
        ]
        if not totals:
            gaps.append(f"{vm.name}: no receive counter samples")
        packets[vm.name] = _samples(totals, "packets")
        metrics[f"network-{vm.name}"] = _counter_rows(
            vm.name, packets[vm.name], _samples(totals, "bytes"), "rx", start, gaps
        )

    profiles = []
    for window in _windows(state, start, "trex"):
        a, b = float(str(window["start"])), float(str(window["end"]))
        profiles.append(
            {
                "profile": window["profile"],
                "pps_per_sender": trex.profiles[
                    int(str(window["profile"]))
                ].pps_per_sender,
                "start_s": round(a - start),
                "end_s": round(b - start),
                "tx": _spread(
                    {s.name: _rate_inside(packets[s.name], a, b) for s in senders},
                    start,
                ),
                "rx": _spread(
                    {r.name: _rate_inside(packets[r.name], a, b) for r in receivers},
                    start,
                ),
            }
        )
    actual = {
        str(step): {
            "first": round(min(times) - start, 3),
            "last": round(max(times) - start, 3),
            "senders": len(times),
        }
        for step, times in sorted(step_starts.items())
    }
    loads: dict[str, float] = {}
    for c in connections:
        pps = trex.profiles[0].pps_per_sender
        loads[c.receiver] = loads.get(c.receiver, 0.0) + pps * c.share
    receipt = next((e for e in evts if e.get("event") == "receipt"), None)
    return {
        "mode": trex.mode,
        "focus": state.plan.focus,
        "senders": len(senders),
        "receivers": len(receivers),
        "connections": [c.model_dump() for c in connections],
        "receiver_load_first_profile_pps": loads,
        "profiles": profiles,
        "count_baselines_s": {
            vm: round(first[0] - start, 3)
            for vm, samples in packets.items()
            if (first := next((p for p in samples if p[0] >= start), None))
        },
        "step_starts": actual,
        "receipt": receipt,
    }


def _counter_rows(
    vm: str,
    packets: Samples,
    octets: Samples,
    side: str,
    start: float,
    gaps: list[str],
) -> list[Row]:
    """Packet counts and packet and byte rates from one VM's counter samples.

    Counts are packets since the VM's first sample at or after the measured
    start; that sample's time is the count's baseline.
    """
    by_time = dict(octets)
    baseline = next(((t, c) for t, c in packets if t >= start), None)
    rows: list[Row] = []
    if baseline:
        rows.append({"t": round(baseline[0] - start, 3), f"{side}_packets": 0})
    resets = 0
    for (t0, c0), (t1, c1) in zip(packets, packets[1:], strict=False):
        if t1 <= t0:
            continue
        if c1 < c0:
            resets += 1
            baseline = None
            continue
        row: Row = {
            "t": round(t1 - start, 3),
            "interval_s": round(t1 - t0, 3),
            f"{side}_pps": round((c1 - c0) / (t1 - t0), 1),
        }
        if baseline and t1 > baseline[0]:
            row[f"{side}_packets"] = c1 - baseline[1]
        if t0 in by_time and t1 in by_time and by_time[t1] >= by_time[t0]:
            row[f"{side}_bytes_per_s"] = round(
                (by_time[t1] - by_time[t0]) / (t1 - t0), 1
            )
        rows.append(row)
    rows.sort(key=lambda r: float(str(r["t"])))
    if resets:
        gaps.append(f"{vm}: {side} packet counter went backwards {resets} times")
    return rows


class Interval(NamedTuple):
    """One fio log entry with a conservative plotted span, in epoch seconds."""

    start: float
    end: float
    direction: str
    value: object


def _intervals_from(
    entries: list[tuple[float, str, object]], job_start: float | None
) -> list[Interval]:
    """Turn fio log entries into intervals of at most FIO_LOG_MS each.

    fio can delay entries or skip windows without new IO, so timestamp spacing
    alone cannot establish lost entries. An entry is shown over
    at most one log period, starting no earlier than the job or the previous
    entry of its direction; longer spacing stays a break in the plot.
    """
    period = FIO_LOG_MS / 1000
    found: list[Interval] = []
    previous: dict[str, float] = {}
    for end, direction, value in sorted(entries, key=lambda e: e[0]):
        begin = previous.get(direction, job_start)
        if begin is None or end - begin > period:
            begin = end - period
        found.append(Interval(begin, end, direction, value))
        previous[direction] = end
    return found


def _fio_run(
    bundle: Path, vm: str, run: str, numjobs: int, start: float, gaps: list[str]
) -> tuple[Row, list[Row]]:
    """One VM's fio run: its timing, and each job's result and interval logs."""
    base = f"raw/{vm}/fio/{run}"
    info: Row = {"vm": vm}
    try:
        times = json.loads(_text(bundle, f"{base}.times", gaps) or "")
    except ValueError:
        times = {}
        gaps.append(f"{base}.times: missing or unreadable")
    for key in ("start", "end"):
        if (value := _num(times.get(key))) is not None:
            info[f"{key}_s"] = round(value - start, 3)
    for key in ("stopped_at_deadline", "exit_code", "problem"):
        if key in times:
            info[key] = times[key]
    if "problem" in times:
        gaps.append(f"{base}: {times['problem']}")
    try:
        results = fio.results(_text(bundle, f"{base}.json", gaps) or "")
    except (ValueError, KeyError, TypeError):
        results = []
        gaps.append(f"{base}.json: no readable fio result")
    jobs = []
    for number in range(1, numjobs + 1):
        result = results[number - 1] if number <= len(results) else None
        job: Row = {"vm": vm, "job": number, "result": result}
        if result and result.get("error"):
            gaps.append(f"{base} job {number}: fio error {result['error']}")
        job_start = _num(result.get("start")) if result else None
        for kind in ("iops", "bw", "clat_hist"):
            text = _text(bundle, f"{base}_{kind}.{number}.log", gaps)
            if text is None:
                gaps.append(f"{base}: no {kind} log for job {number}")
                continue
            try:
                entries = (
                    fio.parse_hist(text) if kind == "clat_hist" else fio.parse_log(text)
                )
            except (ValueError, IndexError):
                gaps.append(f"{base}: unreadable {kind} log for job {number}")
                continue
            if not entries:
                gaps.append(f"{base}: empty {kind} log for job {number}")
                continue
            job[kind] = _intervals_from([(t, d, v) for t, d, v in entries], job_start)
        jobs.append(job)
    return info, jobs


def _range(values: list[float | None]) -> Row | None:
    """Minimum, median and maximum of per-job values."""
    found = sorted(v for v in values if v is not None)
    if not found:
        return None
    return {
        "min": round(found[0], 3),
        "median": round(found[len(found) // 2], 3),
        "max": round(found[-1], 3),
    }


def _job_result(job: Row, start: float) -> Row:
    """One job's result, with absolute timing only when fio reports its start."""
    result = job["result"]
    if not isinstance(result, dict):
        return {"vm": job["vm"], "job": job["job"], "result": None}
    begin = _num(result.get("start"))
    return {
        "vm": job["vm"],
        "job": job["job"],
        "error": result.get("error"),
        "runtime_s": result["runtime_s"],
        "start_s": round(begin - start, 3) if begin is not None else None,
        "end_s": (
            round(begin + float(str(result["runtime_s"])) - start, 3)
            if begin is not None
            else None
        ),
        **{d: result[d] for d in DIRECTIONS if d in result},
    }


def _ms(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _storage(
    state: RunState,
    bundle: Path,
    start: float,
    gaps: list[str],
    metrics: dict[str, list[Row]],
) -> Row:
    """Fio results per job, their range per profile, and each job's interval logs.

    Each job reports over its own runtime, so job results are not added up.
    """
    config = state.config.workloads.fio
    assert config
    numjobs = config.numjobs
    vms = [t.name for t in state.targets]
    profiles = []
    for window in _windows(state, start, "fio"):
        index = int(str(window["profile"]))
        profile = config.profiles[index]
        run = f"{index + 1:02d}-{profile.name}"
        per_vm, jobs = [], []
        for vm in vms:
            info, vm_jobs = _fio_run(bundle, vm, run, numjobs, start, gaps)
            per_vm.append(info)
            jobs += vm_jobs
        summary: Row = {
            "profile": index,
            "name": profile.name,
            "settings": profile.model_dump(exclude_none=True),
            "start_s": round(float(str(window["start"])) - start),
            "end_s": round(float(str(window["end"])) - start),
            "jobs_expected": len(jobs),
            "vms": per_vm,
        }
        summary["jobs"] = [_job_result(job, start) for job in jobs]
        for d in DIRECTIONS:
            stats = [
                cast(Row, cast(Row, j["result"])[d])
                for j in jobs
                if isinstance(j["result"], dict)
                and not j["result"].get("error")
                and isinstance(j["result"].get(d), dict)
            ]
            if not stats:
                continue
            summary[d] = {
                "jobs": len(stats),
                "iops": _range([_num(st.get("iops")) for st in stats]),
                "bw_kib_s": _range([_num(st.get("bw_kib_s")) for st in stats]),
                "p99_ms": _range([_num(st.get("p99_ms")) for st in stats]),
            }
            if len(stats) < len(jobs):
                gaps.append(
                    f"fio {run} {d}: results from {len(stats)} of {len(jobs)} jobs"
                )
        profiles.append(summary)
        for job in jobs:
            rows: list[Row] = []
            for kind, unit in (("iops", "iops"), ("bw", "bw_kib_s")):
                rows += [
                    {
                        "t": round(i.end - start, 3),
                        "interval_s": round(i.end - i.start, 3),
                        f"{i.direction}_{unit}": i.value,
                    }
                    for i in cast(list[Interval], job.get(kind, []))
                ]
            rows += [
                {
                    "t": round(i.end - start, 3),
                    "interval_s": round(i.end - i.start, 3),
                    f"{i.direction}_p50_ms": _ms(
                        fio.percentile(cast(list[int], i.value), 0.5)
                    ),
                    f"{i.direction}_p99_ms": _ms(
                        fio.percentile(cast(list[int], i.value), 0.99)
                    ),
                }
                for i in cast(list[Interval], job.get("clat_hist", []))
            ]
            name = f"fio-{job['vm']}-job{job['job']}"
            metrics[name] = sorted(
                metrics.get(name, []) + rows, key=lambda r: float(str(r["t"]))
            )
    return {
        "volume_size_gib": config.volume_size_gib,
        "numjobs": numjobs,
        "profiles": profiles,
    }


def _collectors(
    state: RunState,
    bundle: Path,
    start: float | None,
    end: float | None,
    evts: list[Row],
    gaps: list[str],
    metrics: dict[str, list[Row]],
) -> None:
    """Normalize host, Ceph and guest samples; list their coverage problems."""
    sources = {f"host-{h.name}": f"raw/host-{h.name}.jsonl" for h in state.plan.hosts}
    if state.config.workloads.fio and state.plan.ceph_mon:
        sources["ceph"] = "raw/ceph.jsonl"
    sources |= {
        f"guest-{t.name}": f"raw/{t.name}/sampler/samples.jsonl" for t in state.targets
    }
    for name, rel in sources.items():
        if start is None and not (bundle / rel).is_file():
            continue
        raw = _jsonl(bundle, rel, gaps)
        try:
            rows = collect.normalize(raw)
        except (KeyError, TypeError) as error:
            rows = []
            gaps.append(f"{name}: unreadable samples ({error})")
        if not rows:
            gaps.append(f"{name}: no samples")
        if any(r.get("gap") for r in rows):
            gaps.append(f"{name}: sample gaps")
        resets = sorted({k for r in rows for k in r if k.endswith(".reset")})
        if resets:
            gaps.append(f"{name}: counter resets ({', '.join(resets)})")
        errors = [str(r[k]) for r in raw for k in r if k.endswith(".error")]
        if errors:
            gaps.append(f"{name}: {len(errors)} failed reads ({errors[-1]})")
        field = "ceph.health" if name == "ceph" else "cpu.busy_pct"
        without = sum(field not in r for r in rows)
        if rows and without:
            gaps.append(f"{name}: {without} of {len(rows)} samples without {field}")
        if start is not None and end is not None and rows:
            for row in rows:
                row["t"] = round(float(str(row["t"])) - start, 3)
            first = float(str(rows[0]["t"])) - float(str(rows[0]["interval_s"]))
            last = float(str(rows[-1]["t"]))
            cadence = SAMPLE_INTERVAL_S[name.split("-")[0]]
            if first > cadence:
                gaps.append(f"{name}: no samples in the first {first:.0f}s")
            if end - start - last > cadence:
                gaps.append(f"{name}: no samples in the last {end - start - last:.0f}s")
        metrics[name] = rows
    for item in evts:
        kind = item.get("event")
        if kind in ("collector-failed", "evidence-lost"):
            gaps.append(f"{kind}: {item.get('name', '')} {item.get('message', '')}")
        elif kind in ("collector", "clock"):
            offset = _num(item.get("clock_offset_s", item.get("offset_s")))
            uncertainty = _num(item.get("uncertainty_s")) or 0.0
            if offset is not None and abs(offset) - uncertainty > MAX_CLOCK_OFFSET_S:
                who = item.get("name") or item.get("vm")
                gaps.append(f"{who}: clock differs by {offset:.1f}s")


def _run(state: RunState, finished: str) -> Row:
    """Run identity and outcome fields from the state."""
    return {
        "id": state.id,
        "workloads": state.config.workloads.enabled,
        "duration_s": state.duration_s,
        "model": state.model,
        "cloud": state.cloud,
        "created": state.created_at.isoformat(),
        "finished": finished,
        "execution": state.execution,
        "finalization": state.finalization,
        "code": state.code,
        "causes": state.causes,
    }


def _fleet(state: RunState, targets: list[Target]) -> Row:
    """Hosts, VMs and resources the run used."""
    plan = state.plan
    return {
        "network": plan.network,
        "vlan": plan.vlan,
        "ip_version": plan.ip_version,
        "mgmt_network": plan.mgmt_network,
        "flavor": plan.flavor,
        "volume_type": plan.volume_type,
        "focus": plan.focus,
        "hosts": [h.model_dump() for h in plan.hosts],
        "vms": [t.model_dump() for t in targets],
    }


def build(state: RunState) -> tuple[Row, dict[str, list[Row]]]:
    """Compute the run result and metric series without writing anything."""
    bundle = run_dir(state.id)
    evts = events(state.id)
    gaps: list[str] = []
    metrics: dict[str, list[Row]] = {}
    timeline = _timeline(evts)
    start = timeline[1] if timeline else None
    _collectors(
        state, bundle, start, timeline[2] if timeline else None, evts, gaps, metrics
    )
    result: Row = {
        "config": state.config.model_dump(),
        "fleet": _fleet(state, state.targets),
        "tools": [
            {k: e.get(k) for k in ("vm", "name", "version")}
            for e in evts
            if e.get("event") == "tool"
        ],
        "timeline": None,
    }
    if timeline:
        warm, start_t, end = timeline
        late = next(
            (_num(e.get("late_s")) for e in evts if e.get("event") == "dispatched"),
            None,
        )
        if late and late > 0:
            gaps.append(f"workloads were started {late:.0f}s after the warm-up began")
        result["timeline"] = {
            "warmup_start": warm,
            "measured_start": start_t,
            "measured_end": end,
            "warmup_s": state.schedule.warmup_s,
            "trex": [w.model_dump() for w in state.schedule.trex],
            "fio": [w.model_dump() for w in state.schedule.fio],
        }
        if state.config.workloads.trex:
            result["network"] = _network(state, bundle, start_t, evts, gaps, metrics)
        if state.config.workloads.fio:
            result["storage"] = _storage(state, bundle, start_t, gaps, metrics)
    result |= {
        "errors": [str(e.get("message")) for e in evts if e.get("event") == "error"],
        "gaps": gaps,
        "cleanup": [
            {"kind": e.kind, "name": e.name, "action": e.action, "where": e.where}
            for e in outstanding(state.id)
        ],
    }
    return result, metrics


def exit_code(state: RunState) -> int:
    """Process exit code: 0 completed, 30 cancelled, 20 failed or not cleaned up.

    Resources retained on request do not fail the run.
    """
    if state.finalization not in ("complete", "retained") or state.execution in (
        "failed",
        "interrupted",
    ):
        return 20
    return 30 if state.execution == "cancelled" else 0


def write(state: RunState, result: Row, metrics: dict[str, list[Row]]) -> None:
    """Write report.json, headed by the final run state, its metrics and graphs."""
    bundle = run_dir(state.id)
    result = {"run": _run(state, now().isoformat()), **result}
    (bundle / "metrics").mkdir(parents=True, exist_ok=True)
    for name, rows in metrics.items():
        text = "".join(json.dumps(row) + "\n" for row in rows)
        (bundle / "metrics" / f"{name}.jsonl").write_text(text)
    result["graphs"] = plots.render(result, metrics, bundle / "graphs")
    (bundle / "report.json").write_text(json.dumps(result, indent=2))

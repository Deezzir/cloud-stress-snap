"""Remote samplers: host CPU, NIC and OVS counters, and Ceph health.

The sampler runs as a run-owned transient unit and writes JSONL locally; the
worker fetches the file once after the run. Missing counters are left out of a
sample rather than reported as zero; a counter that goes backwards is a reset.
"""

import json
from importlib import resources

SAMPLER = (resources.files("campaign.workloads") / "files" / "sampler.py").read_text()

CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
GAUGES = {"ovs.flows", "ovs.flow_limit", "ovs.offloaded_flows"}


def normalize(text: str) -> list[dict[str, object]]:
    """Turn cumulative counters into per-second rates and CPU percentages.

    Each row keeps the remote timestamp and sequence; a gap in sequence or a
    counter that went backwards is marked so it is not read as a real value.
    """
    samples = [json.loads(line) for line in text.splitlines() if line.strip()]
    rows: list[dict[str, object]] = []
    for prev, cur in zip(samples, samples[1:], strict=False):
        elapsed = cur["t"] - prev["t"]
        row: dict[str, object] = {"t": cur["t"], "interval_s": round(elapsed, 3)}
        if cur["seq"] != prev["seq"] + 1:
            row["gap"] = True
        if "cpu.idle" in cur and "cpu.idle" in prev:
            deltas = {f: cur[f"cpu.{f}"] - prev[f"cpu.{f}"] for f in CPU_FIELDS}
            total = sum(deltas.values())
            if total <= 0 or min(deltas.values()) < 0:
                row["cpu.reset"] = True
            else:
                for field in ("system", "irq", "softirq", "iowait"):
                    row[f"cpu.{field}_pct"] = round(100 * deltas[field] / total, 2)
                idle = deltas["idle"] + deltas["iowait"]
                row["cpu.busy_pct"] = round(100 * (1 - idle / total), 2)
        for key, value in cur.items():
            if key in ("t", "seq") or key.startswith("cpu."):
                continue
            if key.startswith("ceph.") or key in GAUGES:
                row[key] = value
            elif key in prev and elapsed > 0:
                delta = value - prev[key]
                if delta < 0:
                    row[f"{key}.reset"] = True
                else:
                    row[f"{key}_per_s"] = round(delta / elapsed, 1)
        rows.append(row)
    return rows


def start_args(link: str | None, offload: bool | None, tap: str | None = None) -> str:
    """Sampler arguments as a JSON string."""
    return json.dumps({"link": link, "offload": bool(offload), "tap": tap})

"""Remote samplers: host CPU, NIC and OVS counters, and Ceph health.

The sampler runs as a run-owned transient unit and writes JSONL locally; the
worker fetches the file once after the run. Missing counters are left out of a
sample rather than reported as zero; a counter that goes backwards is a reset.
"""

import json

SAMPLER = r"""
import json, re, subprocess, sys, time

mode, out_path, interval = sys.argv[1], sys.argv[2], float(sys.argv[3])
args = json.loads(sys.argv[4])
out = open(out_path, "a", buffering=1)


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def cpu():
    fields = open("/proc/stat").readline().split()[1:9]
    return dict(zip(("user", "nice", "system", "idle", "iowait", "irq", "softirq",
                     "steal"), map(int, fields)))


def read(path):
    try:
        return open(path).read()
    except OSError:
        return ""


def nic(link):
    members = read(f"/sys/class/net/{link}/bonding/slaves").split() or [link]
    found = {}
    for name in dict.fromkeys([link, *members]):
        for counter in ("rx_packets", "tx_packets", "rx_dropped", "rx_missed_errors"):
            value = read(f"/sys/class/net/{name}/statistics/{counter}").strip()
            if value.isdigit():
                found[f"{name}.{counter}"] = int(value)
    for name in members:
        for line in run("ethtool", "-S", name).splitlines():
            key, _, value = line.strip().partition(": ")
            if value.isdigit() and key in (
                    "rx_out_of_buffer", "rx_packets_phy", "rx_discards_phy"):
                found[f"{name}.{key}"] = int(value)
    return found


def ovs(offload):
    found = {}
    show = run("ovs-appctl", "dpctl/show")
    if m := re.search(r"lost:(\d+)", show):
        found["ovs.lost"] = int(m[1])
    if m := re.search(r"flows:\s*(\d+)", show):
        found["ovs.flows"] = int(m[1])
    if m := re.search(r"\(limit (\d+)", run("ovs-appctl", "upcall/show")):
        found["ovs.flow_limit"] = int(m[1])
    if m := re.search(r"^dpif_flow_put_error\s.*total: (\d+)",
                      run("ovs-appctl", "coverage/show"), re.M):
        found["ovs.flow_put_errors"] = int(m[1])
    if offload:
        dump = run("ovs-appctl", "dpctl/dump-flows", "type=offloaded")
        found["ovs.offloaded_flows"] = dump.count("\n")
    return found


def ceph():
    try:
        status = json.loads(run("ceph", "status", "-f", "json"))
    except ValueError:
        return {}
    checks = status.get("health", {}).get("checks", {})
    slow = checks.get("SLOW_OPS", {}).get("summary", {}).get("message", "")
    pgmap = status.get("pgmap", {})
    return {
        "ceph.health": status.get("health", {}).get("status"),
        "ceph.slow_ops": int(m[1]) if (m := re.match(r"(\d+)", slow)) else 0,
        "ceph.pgs_unclean": sum(s["count"] for s in pgmap.get("pgs_by_state", [])
                                if s["state_name"] != "active+clean"),
        "ceph.read_ops": pgmap.get("read_op_per_sec", 0),
        "ceph.write_ops": pgmap.get("write_op_per_sec", 0),
    }


seq = 0
while True:
    started = time.time()
    if mode == "ceph":
        sample = ceph()
    else:
        sample = {f"cpu.{k}": v for k, v in cpu().items()}
        sample.update(nic(args["link"]))
        sample.update(ovs(args.get("offload")))
    out.write(json.dumps({"t": started, "seq": seq, **sample}) + "\n")
    seq += 1
    time.sleep(max(interval - (time.time() - started), 0))
"""

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


def start_args(link: str | None, offload: bool | None) -> str:
    """Sampler arguments as a JSON string."""
    return json.dumps({"link": link, "offload": bool(offload)})

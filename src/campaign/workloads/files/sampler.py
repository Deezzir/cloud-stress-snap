"""Host or Ceph sampler; appends one JSON line per interval."""

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
        if tap := args.get("tap"):
            # The guest's tap: tx_dropped counts packets the guest did not take.
            for counter in ("tx_packets", "tx_dropped"):
                value = read(f"/sys/class/net/{tap}/statistics/{counter}").strip()
                if value.isdigit():
                    sample[f"tap.{counter}"] = int(value)
        sample.update(ovs(args.get("offload")))
    out.write(json.dumps({"t": started, "seq": seq, **sample}) + "\n")
    seq += 1
    time.sleep(max(interval - (time.time() - started), 0))

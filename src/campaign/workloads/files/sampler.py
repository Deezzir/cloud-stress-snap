"""Host, guest or Ceph sampler; appends one JSON line per interval."""

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


def nft(table):
    try:
        data = json.loads(run("nft", "-j", "list", "counters", "table", "inet", table))
    except ValueError:
        return {"nft.error": "nft did not return counters"}
    found = {}
    for item in data.get("nftables", []):
        if counter := item.get("counter"):
            found[f"nft.{counter['name']}.packets"] = counter["packets"]
            found[f"nft.{counter['name']}.bytes"] = counter["bytes"]
    return found


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
        return {"ceph.error": "ceph status did not return JSON"}
    checks = status.get("health", {}).get("checks", {})
    slow = checks.get("SLOW_OPS", {}).get("summary", {}).get("message", "")
    pgmap = status.get("pgmap", {})
    found = {
        "ceph.health": status.get("health", {}).get("status"),
        "ceph.slow_ops": int(m[1]) if (m := re.match(r"(\d+)", slow)) else 0,
        "ceph.pgs_unclean": sum(s["count"] for s in pgmap.get("pgs_by_state", [])
                                if s["state_name"] != "active+clean"),
    }
    for key, name in (("read_op_per_sec", "read_ops"), ("write_op_per_sec", "write_ops")):
        if key in pgmap:
            found[f"ceph.{name}"] = pgmap[key]
    return found


seq = 0
while True:
    started = time.time()
    if mode == "ceph":
        sample = ceph()
    else:
        sample = {f"cpu.{k}": v for k, v in cpu().items()}
        if args.get("link"):
            sample.update(nic(args["link"]))
        for tap in args.get("taps", []):
            # A guest's tap: tx_dropped counts packets the guest did not take.
            for counter in ("tx_packets", "tx_dropped"):
                value = read(f"/sys/class/net/{tap}/statistics/{counter}").strip()
                if value.isdigit():
                    sample[f"{tap}.{counter}"] = int(value)
        if mode == "host":
            sample.update(ovs(args.get("offload")))
        if args.get("nft"):
            sample.update(nft(args["nft"]))
    out.write(json.dumps({"t": started, "seq": seq, **sample}) + "\n")
    if "nft.error" in sample:
        # Receive counters are required: stop so the failure stays visible.
        sys.exit(1)
    seq += 1
    time.sleep(max(interval - (time.time() - started), 0))

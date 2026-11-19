"""Read-only host TRex prerequisites, run using the host's Python standard library."""

import glob
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def read(path):
    return Path(path).read_text().strip()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=30).strip()


def cpu_ticks():
    ticks = {}
    for line in read("/proc/stat").splitlines():
        name, *fields = line.split()
        if name.startswith("cpu") and name[3:].isdigit():
            # Guest ticks are already included in user/nice; iowait is not idle.
            ticks[int(name[3:])] = (sum(map(int, fields[:8])), int(fields[3]))
    return ticks


def least_busy_cpus(candidates, needed):
    before = cpu_ticks()
    time.sleep(1)
    after = cpu_ticks()
    scores = {}
    for cpu in candidates & before.keys() & after.keys():
        total = after[cpu][0] - before[cpu][0]
        idle = after[cpu][1] - before[cpu][1]
        scores[cpu] = idle / total if total > 0 else 0
    require(len(scores) >= needed, "too few eligible CPUs remained online during sampling")
    return sorted(scores, key=lambda cpu: (-scores[cpu], cpu))[:needed]


def native_candidates(available):
    candidates = available.copy()
    # Unrestricted scheduler affinity is normal host work. Explicitly pinned
    # userspace threads on these cores indicate that they are already assigned.
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if not (process / "cmdline").read_bytes() or int(process.name) == os.getpid():
                continue
            for thread in (process / "task").iterdir():
                try:
                    affinity = os.sched_getaffinity(int(thread.name))
                except ProcessLookupError:
                    continue
                if not affinity >= available:
                    candidates.difference_update(affinity)
        except (FileNotFoundError, ProcessLookupError):
            continue
    return candidates


def select_cpus(cpus, processes, device):
    available = os.sched_getaffinity(0)
    needed = 3 * processes
    require(set(cpus) <= available, f"selected CPUs must be online and available: {cpus}")
    if device is None:
        # TRex v3.08 indexes a 120-entry platform table with all three role IDs.
        candidates = available.intersection(range(120))
        invalid = [cpu for cpu in cpus if cpu not in candidates]
        require(not invalid, f"AF_PACKET TRex CPU IDs must be 0-119; invalid: {invalid}")
        if not cpus:
            require(len(candidates) >= needed,
                    f"need {needed} available CPUs in AF_PACKET TRex range 0-119; found {len(candidates)}")
            cpus = least_busy_cpus(candidates, needed)
    else:
        candidates = native_candidates(available)
        if cpus:
            require(set(cpus) <= candidates, "selected CPUs overlap a pinned thread")
        else:
            node = int(read(device / "numa_node"))
            if node < 0:
                system_nodes = glob.glob("/sys/devices/system/node/node[0-9]*")
                require(len(system_nodes) == 1, "cannot determine device NUMA locality; specify cpus")
                node = int(Path(system_nodes[0]).name[4:])
            candidates = {cpu for cpu in candidates
                          if Path(f"/sys/devices/system/cpu/cpu{cpu}/node{node}").exists()}
            require(len(candidates) >= needed,
                    f"need {needed} available CPUs without pinned users on NUMA node {node}; found {len(candidates)}")
            cpus = sorted(candidates)[:needed]
    nodes = []
    for cpu in cpus[2::3]:
        paths = glob.glob(f"/sys/devices/system/cpu/cpu{cpu}/node[0-9]*")
        nodes.append(int(Path(paths[0]).name[4:]) if paths else 0)
    return cpus, nodes


def installed_package(backend):
    name = "trex-generator"
    response = json.loads(command("snap", "debug", "api", f"/v2/snaps/{name}"))
    require(response.get("type") == "sync" and response.get("status-code") == 200,
            "cannot inspect installed trex-generator identity")
    snap = response["result"]
    require(snap["name"] == name and snap["status"] == "active",
            "installed trex-generator is not active")
    app = "native" if backend == "dpdk" else "t-rex-64"
    missing_apps = [a for a in (app, "python")
                    if not os.access(f"/snap/bin/{name}.{a}", os.X_OK)]
    require(not missing_apps,
            f"{name} revision {snap['revision']} lacks required apps: {', '.join(missing_apps)}")
    connections = command("snap", "connections", "trex-generator")
    plugs = ["network-control", "hardware-observe", "process-control", "system-observe"]
    if backend == "dpdk":
        plugs += ["vfio", "hugepages-control"]
    slots = {plug: sorted(parts[2] for line in connections.splitlines()
                          if len(parts := line.split()) >= 3 and parts[1] == f"{name}:{plug}"
                          and parts[2] != "-") for plug in plugs}
    missing = [plug for plug in plugs if not slots[plug]]
    require(not missing, f"{name} revision {snap['revision']} missing connections: {', '.join(missing)}")
    return {"snap_id": snap.get("id"), "revision": snap["revision"], "version": snap["version"],
            "channel": snap.get("tracking-channel"), "slots": slots}


def check(args):
    backend, interface = args["backend"], args["interface"]
    package = installed_package(backend)
    device = Path("/sys/bus/pci/devices") / interface if backend == "dpdk" else None
    cpus, nodes = select_cpus(args["cpus"], args["processes"], device)
    # The upstream server binds wildcard TCP listeners in both families.
    for port in range(args["rpc_port"] - 1, args["rpc_port"] + 2 * args["processes"] - 1):
        for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                if family == socket.AF_INET6:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                try:
                    sock.bind((address, port))
                except OSError as error:
                    raise RuntimeError(f"TRex TCP port {port} unavailable; leave existing listeners intact") from error
    if backend == "af_packet":
        links = json.loads(command("ip", "-d", "-j", "link", "show", "dev", interface))
        link = links[0]
        kind = link.get("linkinfo", {}).get("info_kind", "")
        require(kind in ("", "bond") and "UP" in link["flags"],
                "AF_PACKET requires an up, untagged physical or bond provider link")
        require(link["mtu"] >= min(args["mtu"], 1500), "provider interface MTU is too small")
        mappings = command("ovs-vsctl", "get", "Open_vSwitch", ".", "external_ids:ovn-bridge-mappings").strip('"')
        bridges = dict(item.split(":", 1) for item in mappings.split(",") if ":" in item)
        bridge = bridges.get(args["physnet"])
        require(bridge, "provider physnet is absent from live OVN bridge mappings")
        require(interface in command("ovs-vsctl", "list-ifaces", bridge).splitlines(),
                "selected interface is not on the live provider bridge")
    else:
        require(device.exists() and (device / "class").read_text().startswith("0x02"),
                "PCI BDF must identify an operator-prepared network device")
        require(read(device / "vendor") != "0x15b3", "mlx5 is not supported by this VFIO native path")
        require((device / "driver").resolve().name == "vfio-pci",
                "operator must bind the dedicated NIC/VF to vfio-pci")
        group_link = device / "iommu_group"
        require(group_link.exists(), "native TRex needs a working IOMMU group")
        group = group_link.resolve()
        require(Path(f"/dev/vfio/{group.name}").exists(), "VFIO group device is unavailable")
        unsafe = Path("/sys/module/vfio/parameters/enable_unsafe_noiommu_mode")
        require(not unsafe.exists() or read(unsafe) in ("N", "0"),
                "unsafe no-IOMMU VFIO is not supported")
        for member in (group / "devices").iterdir():
            require((member / "driver").resolve().name == "vfio-pci",
                    f"IOMMU group {group.name} is not dedicated to VFIO")
        for fd in Path("/proc").glob("[0-9]*/fd/*"):
            try:
                require(os.readlink(fd) != f"/dev/vfio/{group.name}",
                        f"VFIO group {group.name} is already open; do not steal its device")
            except (FileNotFoundError, ProcessLookupError):
                continue
        mounts = [line.split() for line in Path("/proc/mounts").read_text().splitlines()]
        require(any(m[1:3] == ["/dev/hugepages", "hugetlbfs"] for m in mounts),
                "operator must mount hugetlbfs at /dev/hugepages")
        # hugetlbfs reports its mounted pool's page size, not the system default.
        page_size = os.statvfs("/dev/hugepages").f_bsize
        pool = Path(f"/sys/kernel/mm/hugepages/hugepages-{page_size // 1024}kB")
        free = int(read(pool / "free_hugepages"))
        reserved = int(read(pool / "resv_hugepages"))
        require((free - reserved) * page_size >= 512 * 1024**2,
                "native TRex needs at least 512 MiB of unreserved hugepages")
    return {"cpus": cpus, "sockets": nodes, "package": package,
            "version": f"{package['version']} {package['revision']}"}


if __name__ == "__main__":
    try:
        print(json.dumps(check(json.loads(sys.argv[1]))))
    except Exception as error:
        sys.exit(f"host TRex preflight: {error}")

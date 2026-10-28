"""fio on the run-owned Cinder volume inside the target guest."""

import json
import shlex

from campaign.guest import Guest
from campaign.profiles import FioCell

NUMJOBS = 2
# fio latency histogram layout (FIO_IO_U_PLAT_BITS/VAL); bins are in ns.
PLAT_BITS, PLAT_VAL = 6, 64


def find_device(guest: Guest, volume_id: str, size_gb: int) -> tuple[str | None, str]:
    """Resolve the run's volume in the guest, or say why it is not safe to use."""
    link = f"/dev/disk/by-id/virtio-{volume_id[:20]}"
    out = guest.run(
        f"test -e {link} || exit 0; dev=$(readlink -f {link}); echo $dev; "
        "blockdev --getsize64 $dev; lsblk -nr -o NAME $dev | wc -l; "
        "lsblk -ndo PKNAME $(findmnt -nvo SOURCE /)"
    ).split()
    if not out:
        return None, f"the volume is not visible in the guest at {link}"
    device, size, nodes = out[0], int(out[1]), int(out[2])
    if out[3:] and device == f"/dev/{out[3]}":
        return None, f"{device} is the guest's root disk"
    if size != size_gb * 1024**3:
        return None, f"{device} is {size} bytes, not {size_gb} GiB"
    if nodes != 1:
        return None, f"{device} has partitions"
    return device, ""


def prefill_command(device: str, out_dir: str) -> str:
    """Sequentially write the whole device once before measured reads."""
    return (
        f"mkdir -p {out_dir} && fio --name=prefill --filename={device} "
        "--ioengine=libaio --direct=1 --rw=write --bs=1m --iodepth=32 "
        f"--output-format=json --output={out_dir}/prefill.json"
    )


def job_file(cell: FioCell, device: str, size_gb: int, rate_iops: int | None) -> str:
    """io-kitchen-style libaio job: direct I/O, nonoverlapping regions per job."""
    region = size_gb * 1024 // NUMJOBS
    lines = [
        "[global]",
        "ioengine=libaio",
        "direct=1",
        f"filename={device}",
        f"rw={cell.rw}",
        f"bs={cell.bs}",
        f"iodepth={cell.iodepth}",
        f"numjobs={NUMJOBS}",
        f"size={region}m",
        f"offset_increment={region}m",
        "time_based=1",
        f"runtime={cell.length_s}",
        "log_avg_msec=1000",
        "log_hist_msec=1000",
        "log_hist_coarseness=0",
        "per_job_logs=1",
    ]
    if cell.rwmixread is not None:
        lines.append(f"rwmixread={cell.rwmixread}")
    if rate_iops:
        per_job = max(rate_iops // NUMJOBS, 1)
        if cell.rwmixread is not None:
            reads = per_job * cell.rwmixread // 100
            lines.append(f"rate_iops={reads},{per_job - reads}")
        else:
            lines.append(f"rate_iops={per_job}")
    return "\n".join([*lines, f"[{cell.label}]", ""])


def run_command(cell: FioCell, job: str, out_dir: str) -> str:
    """Write the job and run fio with JSON, IOPS and latency-histogram logs."""
    base = f"{out_dir}/{cell.label}"
    return (
        f"mkdir -p {out_dir} && printf %s {shlex.quote(job)} > {base}.fio && "
        f"date +%s.%N > {base}.start && "
        f"fio {base}.fio --output-format=json --output={base}.json "
        f"--write_iops_log={base} --write_hist_log={base}"
    )


def bin_value_ns(index: int) -> float:
    """Latency represented by a fio histogram bin."""
    if index < PLAT_VAL << 1:
        return float(index)
    error_bits = (index >> PLAT_BITS) - 1
    base = 1 << (error_bits + PLAT_BITS)
    return base + (index % PLAT_VAL + 0.5) * (1 << error_bits)


def percentile(bins: list[int], fraction: float) -> float | None:
    """Latency in ms below which the given fraction of I/Os completed."""
    total = sum(bins)
    if not total:
        return None
    goal, seen = total * fraction, 0
    for index, count in enumerate(bins):
        seen += count
        if seen >= goal:
            return bin_value_ns(index) / 1e6
    return bin_value_ns(len(bins) - 1) / 1e6


def parse_hist(text: str) -> dict[int, list[int]]:
    """Per-second histogram from a fio hist log: {second: bins}."""
    seconds: dict[int, list[int]] = {}
    for line in text.splitlines():
        fields = [int(f) for f in line.split(",")]
        bins = fields[3:]
        second = fields[0] // 1000
        if second in seconds:
            seconds[second] = [
                a + b for a, b in zip(seconds[second], bins, strict=False)
            ]
        else:
            seconds[second] = bins
    return seconds


def parse_iops(text: str) -> dict[int, int]:
    """Completed I/Os per second from a fio IOPS log."""
    seconds: dict[int, int] = {}
    for line in text.splitlines():
        fields = line.split(",")
        second = int(fields[0]) // 1000
        seconds[second] = seconds.get(second, 0) + int(fields[1])
    return seconds


def prefill_complete(text: str, size_gb: int) -> bool:
    """Whether the prefill job wrote the whole volume without errors."""
    data = json.loads(text[text.index("{") :])
    jobs = data.get("jobs", [])
    return (
        len(jobs) == 1
        and not jobs[0].get("error")
        and jobs[0]["write"]["io_bytes"] >= size_gb * 1024**3
    )


def summary(text: str) -> dict[str, object]:
    """Check every fio job finished cleanly; keep totals and errors."""
    data = json.loads(text[text.index("{") :])
    jobs = data.get("jobs", [])
    errors = [job["jobname"] for job in jobs if job.get("error")]
    total_ios = sum(job[d]["total_ios"] for job in jobs for d in ("read", "write"))
    iops = sum(job[d]["iops"] for job in jobs for d in ("read", "write"))
    return {
        "version": data.get("fio version"),
        "jobs": len(jobs),
        "failed_jobs": errors,
        "total_ios": total_ios,
        "iops": round(iops, 1),
        "complete": len(jobs) == NUMJOBS and not errors,
    }

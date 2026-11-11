"""fio on each VM's run-owned Cinder volume: device check, prefill, jobs and logs."""

import json
import shlex
from importlib import resources

from campaign.model.profiles import FioConfig, FioProfile
from campaign.transport.guest import Guest
from constants import (
    FIO_DEADLINE_EXIT_CODES,
    FIO_DIRECTIONS,
    FIO_LOG_MS,
    FIO_PLAT_BITS,
    FIO_PLAT_VAL,
    FIO_STOP_GRACE_S,
    PREFILL_JOBS,
)

GIB = 1024**3

RUNNER = (resources.files("campaign.workloads") / "files" / "fio_run.py").read_text()


def find_device(guest: Guest, volume_id: str, fio: FioConfig) -> tuple[str | None, str]:
    """Resolve the run's volume in the guest, or say why it is not safe to use."""
    link = f"/dev/disk/by-id/virtio-{volume_id[:20]}"
    out = guest.run(
        f"test -e {link} || exit 0; dev=$(readlink -f {link}); echo $dev; "
        "blockdev --getsize64 $dev; blockdev --getss $dev; "
        "lsblk -nr -o NAME $dev | wc -l; "
        "lsblk -ndo PKNAME $(findmnt -nvo SOURCE /)"
    ).split()
    if not out:
        return None, f"the volume is not visible in the guest at {link}"
    device, size, sector, nodes = out[0], int(out[1]), int(out[2]), int(out[3])
    size_gib = fio.volume_size_gib
    if out[4:] and device == f"/dev/{out[4]}":
        return None, f"{device} is the guest's root disk"
    if size != size_gib * GIB:
        return None, f"{device} is {size} bytes, not {size_gib} GiB"
    if nodes != 1:
        return None, f"{device} has partitions"
    for profile in fio.profiles:
        if profile.block_bytes % sector:
            return None, (
                f"{profile.name}: bs {profile.bs} is not a multiple of the "
                f"{sector}-byte sectors of {device}, required for direct I/O"
            )
    return device, ""


def prefill_job(device: str, size_gib: int) -> str:
    """Write the whole device once with four writers on disjoint quarters."""
    region = size_gib * GIB // PREFILL_JOBS
    return "\n".join(
        [
            "[global]",
            "ioengine=libaio",
            "direct=1",
            f"filename={device}",
            "rw=write",
            "bs=1M",
            "iodepth=64",
            f"numjobs={PREFILL_JOBS}",
            f"size={region}",
            f"offset_increment={region}",
            "[prefill]",
            "",
        ]
    )


def prefill_command(job: str, out_dir: str) -> str:
    """Write the prefill job and run it with a JSON result."""
    return (
        f"mkdir -p {out_dir} && printf %s {shlex.quote(job)} > {out_dir}/prefill.fio"
        f" && fio {out_dir}/prefill.fio --output-format=json "
        f"--output={out_dir}/prefill.json"
    )


def prefill_complete(text: str, size_gib: int) -> bool:
    """Whether the prefill writers wrote the whole volume without errors."""
    data = json.loads(text[text.index("{") :])
    jobs = data.get("jobs", [])
    return (
        len(jobs) == PREFILL_JOBS
        and not any(job.get("error") for job in jobs)
        and sum(job["write"]["io_bytes"] for job in jobs) >= size_gib * GIB
    )


def job_file(profile: FioProfile, fio: FioConfig, device: str) -> str:
    """Time-based libaio job; fio reads the runtime from $RUNTIME at start.

    One job covers the whole device; several jobs get equal, disjoint regions
    aligned to 1 MiB.
    """
    lines = [
        "[global]",
        "ioengine=libaio",
        "direct=1",
        f"filename={device}",
        f"rw={profile.rw}",
        f"bs={profile.bs}",
        f"iodepth={profile.iodepth}",
        f"numjobs={fio.numjobs}",
        "time_based=1",
        "runtime=${RUNTIME}",
        f"log_avg_msec={FIO_LOG_MS}",
        f"log_hist_msec={FIO_LOG_MS}",
        "log_hist_coarseness=0",
        "log_unix_epoch=1",
        "per_job_logs=1",
    ]
    if fio.numjobs > 1:
        region = fio.region_bytes
        lines += [f"size={region}", f"offset_increment={region}"]
    if profile.rwmixread is not None:
        lines.append(f"rwmixread={profile.rwmixread}")
    return "\n".join([*lines, f"[{profile.name}]", ""])


def run_command(jobs: list[dict[str, object]], out_dir: str) -> str:
    """Run jobs in order, each from its start until its end time (epoch seconds).

    Each job is a dict with name, file (job text), numjobs, start and end.
    """
    args = {
        "out": out_dir,
        "jobs": jobs,
        "grace_s": FIO_STOP_GRACE_S,
        "deadline_exit_codes": FIO_DEADLINE_EXIT_CODES,
    }
    return (
        f"mkdir -p {out_dir} && printf %s {shlex.quote(RUNNER)} > {out_dir}/run.py"
        f" && python3 {out_dir}/run.py {shlex.quote(json.dumps(args))}"
    )


def bin_value_ns(index: int) -> float:
    """Latency represented by a fio histogram bin."""
    if index < FIO_PLAT_VAL << 1:
        return float(index)
    error_bits = (index >> FIO_PLAT_BITS) - 1
    base = 1 << (error_bits + FIO_PLAT_BITS)
    return base + (index % FIO_PLAT_VAL + 0.5) * (1 << error_bits)


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


def parse_hist(text: str) -> list[tuple[float, str, list[int]]]:
    """(epoch seconds, direction, bins) per interval of a fio hist log."""
    return [
        (fields[0] / 1000, FIO_DIRECTIONS.get(fields[1], str(fields[1])), fields[3:])
        for fields in ([int(f) for f in line.split(",")] for line in text.splitlines())
    ]


def parse_log(text: str) -> list[tuple[float, str, int]]:
    """(epoch seconds, direction, value) per interval of a fio IOPS or bw log."""
    found = []
    for line in text.splitlines():
        fields = [int(f) for f in line.split(",")]
        found.append(
            (fields[0] / 1000, FIO_DIRECTIONS.get(fields[2], str(fields[2])), fields[1])
        )
    return found


def results(text: str) -> list[dict[str, object]]:
    """Per-job results from fio's JSON output, in job order.

    Each job has its error code, runtime and, for each direction with I/O, its
    IOPS, bandwidth (KiB/s) and p50/p99 latency (ms) over the job's runtime.
    The start (epoch seconds) is None when fio does not report job_start.
    """
    data = json.loads(text[text.index("{") :])
    found = []
    for job in data["jobs"]:
        started = job.get("job_start")
        item: dict[str, object] = {
            "error": job.get("error", 0),
            "start": started / 1000 if started is not None else None,
            "runtime_s": job["job_runtime"] / 1000,
        }
        for direction in FIO_DIRECTIONS.values():
            stats = job[direction]
            if not stats["total_ios"]:
                continue
            percentiles = stats.get("clat_ns", {}).get("percentile", {})
            item[direction] = {
                "ios": stats["total_ios"],
                "iops": stats["iops"],
                "bw_kib_s": stats["bw"],
                "p50_ms": _ns_ms(percentiles.get("50.000000")),
                "p99_ms": _ns_ms(percentiles.get("99.000000")),
            }
        found.append(item)
    return found


def _ns_ms(value: object) -> float | None:
    return value / 1e6 if isinstance(value, int | float) else None

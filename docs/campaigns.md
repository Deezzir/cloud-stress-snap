# Runs

A run executes the workloads selected in a configuration file on a fixed fleet
of VMs and records time-series measurements of the workloads, hosts and Ceph.
One run is active at a time.

`cloud-stress check` shows workload readiness, resource choices and warnings.
Use `cloud-stress check --full` for the full host, network and Ceph inventories,
or `--json` for structured output.

`--verbose` (`-v`) prints debug logs to stderr on `check`, `run start`, `run status`
and `run stop`. Combine it with `--full` for expanded output and debug logs.

```bash
cp /snap/cloud-stress/current/share/cloud-stress/config.yaml ~/stress.yaml
cloud-stress run start --config ~/stress.yaml
```

The run prints its resources, VM counts and schedule, then starts. Add `--full`
to show every host, the selected image and timeout. The
[configuration reference](configuration.md) describes every setting, placement
and how the duration is divided; [`config.yaml`](../config.yaml) is the
example.

Every VM belongs to one fleet: `vms: 50` creates 50 VMs in total. When fio is
enabled, every VM gets a data volume and runs fio. The run checks that every VM
landed on its planned hypervisor and fails otherwise.

## TRex

Each sender is paired with one or more receivers so that every connection
crosses hypervisors.

Packets use the guests' own MAC and IP addresses on the provider network; the
UDP source port changes with each packet and checksums stay valid. A run-owned
security group admits the destination port only from the run's own ports.

Senders install the `trex-generator` snap and run TRex on their data interface
through a Linux socket, without binding the device. Receivers count packets
from each assigned sender with nftables at the raw prerouting hook, matching
source and destination address, UDP and the destination port, then drop them.
Counts therefore show benchmark packets that reached the guest's network
stack, not packets read by an application. Before measuring, the run sends a
short low-rate burst on every connection and requires it to arrive.

Guests must reach the Snap Store and an Ubuntu package mirror.

## fio

Jobs use libaio, direct I/O and a time-based runtime over the whole device.
With more than one job, each job gets its own region of equal size.

Before the volume is used, the run checks that the device in the guest is the
run's volume, unpartitioned and not the root disk. It then writes the whole
volume with four writers on separate quarters.

## Timeline

Each workload divides `duration` among its own profiles; the lists are not
combined. With `duration: 10m`, two TRex profiles and four fio profiles:

| Measured time | TRex profile | fio profile |
| --- | --- | --- |
| 0:00-2:30 | 1 | 1 |
| 2:30-5:00 | 1 | 2 |
| 5:00-7:30 | 2 | 3 |
| 7:30-10:00 | 2 | 4 |

Every guest receives the full schedule before it starts and changes profile
at the planned times; a TRex rate change does not restart fio, and the reverse.
A 10-second warm-up with the first profiles runs before the measured time and
is excluded from results. Preparation, prefill and cleanup are outside the
measured time.

If a workload or a receiver's packet counting stops early in any VM, the run
stops and is reported as failed. Each fio job is stopped at the end of its
window.

## Results

Each run writes a bundle to `/var/snap/cloud-stress/common/runs/<run-id>/`:

| Path | Contents |
| --- | --- |
| `report.json` | Resolved configuration, fleet, connections, timeline and summaries |
| `graphs/` | PNG graphs on the measured time axis, with profile changes marked |
| `metrics/` | Time series per VM, per fio job, per host and Ceph |
| `raw/` | TRex statistics, nftables and host samples, fio jobs, logs and JSON |
| `events.jsonl` | Run events, the commands started, tool versions and clock offsets |

```bash
cloud-stress run status [<run-id>]
cloud-stress run status [<run-id>] --full
cloud-stress run status [<run-id>] --json
cloud-stress run status [<run-id>] --html report.html
```

The terminal summary shows errors, cleanup status and measurements. `--full`
includes the fleet and all data gaps; HTML and JSON reports include full details.

How values are computed:

- Network rates come from cumulative counters between consecutive samples
  of each VM: packets and frame bytes sent by TRex in each sender, and IP
  packets and bytes counted by nftables in each receiver. Graphs show each
  VM's rates, with the requested rate per sender, and its packets counted
  since its first sample in the measured time. Per profile, the report lists the
  range of per-VM rates over each VM's samples inside the window.
- fio values come from each job's logs, one value per logged interval, and
  from fio's result for each job over its own runtime. Per profile, the
  report lists the range of per-job IOPS, bandwidth and p99 latency.
  fio versions without `job_start` have null per-job `start_s` and `end_s`;
  their runtime, statistics and timestamped log samples are retained.
  fio can delay or skip log entries without new IO, so wider spacing alone is
  not reported as lost data; each entry is plotted over at most one log period.
- Missing samples, sample gaps, uncovered time at the start or end exceeding the
  source's sample interval (host 5 s, Ceph 10 s, guest 1 s), failed reads,
  counter resets and clock offsets above 2 seconds are listed as gaps. After
  the measured time, the run keeps collecting for the slowest sample interval.

Exit codes: `0` completed, `20` failed, interrupted or not cleaned up, `30`
cancelled.

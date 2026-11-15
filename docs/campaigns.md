# Runs

A run executes TRex, fio or both on a fixed VM fleet. Only one run can be active.

## Start

```bash
cloud-stress check
cp /snap/cloud-stress/current/share/cloud-stress/config.yaml ~/stress.yaml
cloud-stress run start --config ~/stress.yaml
```

`check` reports readiness and resource choices; `--full` adds inventories and
`--json` returns structured output. `run start --full` expands the fleet and
schedule details. Use `--verbose` (`-v`) for debug logs on `check`, `run start`,
`run status` and `run stop`.

See [configuration](configuration.md) for placement, profiles and resource
overrides. VM counts are fleet totals; placement on each hypervisor is verified.
Guests need access to the Snap Store and an Ubuntu package mirror.

## Workloads and timing

- **TRex:** UDP traffic over the provider network in `fan-in`, `fan-out` or
  `many-to-many` mode. `pps_per_sender` is the total per VM, shared across its
  receivers and `processes`. A low-rate receipt check precedes measurement.
  Receiver counters measure packets reaching the guest network stack, not
  application delivery.
- **fio:** direct, asynchronous I/O on one run-owned data volume per VM.
  The device is checked against the volume identity and must be unpartitioned
  and separate from the root disk. It is fully prefilled before measurement;
  multiple jobs use equal, separate regions.
- **Timing:** each workload divides `duration` equally among its own profiles.
  A 10-second warm-up uses the first profiles. Warm-up, preparation, prefill and
  cleanup are excluded from measured time. Workloads share a timeline but change
  profiles independently.
- **Failure:** an early workload or receiver-counter stop fails the run.

## List and inspect

```bash
cloud-stress run list
cloud-stress run list --json
cloud-stress run status [<run-id>]
cloud-stress run status [<run-id>] --full
cloud-stress run status [<run-id>] --json
cloud-stress run status [<run-id>] --html report.html
```

`run list` shows local runs newest first: full ID, creation time (UTC), status,
workloads, configured VM count, duration and cleanup state. Unreadable runs warn
on stderr without hiding readable runs. `--json` returns summary objects with
ISO timestamps and durations in seconds.

`run status` defaults to the latest run. `--full` includes the fleet and all
data gaps; HTML and JSON include full details. HTML charts work offline and
support zooming and filtering by VM, job, host or counter.

## Results

Bundles are stored in `/var/snap/cloud-stress/common/runs/<run-id>/`:

| Path | Contents |
| --- | --- |
| `report.json` | Configuration, fleet, timeline, summaries and gaps |
| `graphs/` | PNG graphs |
| `metrics/` | Per-VM, per-job, host and Ceph time series |
| `raw/` | Workload output, counters, configurations and logs |
| `events.jsonl` | Events, commands, tool versions and clock offsets |

Network rates use cumulative counters per VM: TX counts Ethernet frame bytes;
RX counts IP bytes. TRex process counters are summed per VM, with individual
output under `raw/<vm>/trex/process-<index>/`. fio reports IOPS, bandwidth and
p99 latency per job. Profile summaries use each VM's or job's available samples.
Missing data stays missing; failed reads, counter resets and excessive clock
offsets are reported as gaps.

## Stop and clean up

```bash
cloud-stress run stop [<run-id>]
cloud-stress run stop --clean [<run-id>]
```

`stop` stops workloads or retries cleanup. Resources are deleted after evidence
is retrieved; `--abandon-evidence` allows deletion if retrieval fails.

`run start --retain` keeps resources after completion, failure or cancellation;
workloads still stop and evidence is collected. `run stop --clean` deletes those
resources. Without an ID, `stop` selects the active run, then the newest retained
run.

Exit codes: `0` completed, `20` failed, interrupted or not cleaned up, `30`
cancelled.

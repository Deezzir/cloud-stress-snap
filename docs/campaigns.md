# Runs

A run executes TRex, fio or both with a fixed VM fleet and optional explicit
TRex host sources. Only one run can be active.

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
Guests need an Ubuntu package mirror; VM TRex senders also need Snap Store access.

## Workloads and timing

- **TRex:** UDP traffic over the provider network in `fan-in`, `fan-out`,
  `many-to-many` or `host-to-vm` mode. `pps_per_sender` is the total per source,
  shared across its receivers and `processes`. A low-rate receipt check precedes
  measurement.
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

## Host preparation

Host sources require an operator-installed `trex-generator` snap and nftables.
Connect the four TRex plugs listed in [permissions](security-reference.md#juju-access).
Selected CPUs must be online and available to the Juju-launched process, and kept
out of VM and other workload allocations. Preflight detects pinned overlaps but
does not configure scheduler isolation. The campaign checks this preparation without
changing host snap installation, permissions, NIC bindings or hugepage allocation.

- **AF_PACKET** (`trex-generator.t-rex-64`): use an up physical or bond interface
  on the provider's live OVN bridge. Existing uplinks are supported.
- **Native DPDK** (`trex-generator.native`): dedicate a NIC/VF bound to `vfio-pci`
  with a working IOMMU and a viable VFIO group. Supply and connect a
  group-specific custom-device slot to `trex-generator:vfio`
  (`custom-device: trex-vfio`), and connect `hugepages-control` plus the plugs
  above. Mount hugetlbfs at `/dev/hugepages`, with at least 512 MiB of unreserved
  hugepages. mlx5, unsafe no-IOMMU and shared-uplink native use are unsupported.
  Allow the reserved source MAC/IP and VLAN on the NIC/VF and upstream switch.

Preflight rejects unavailable CPUs, overlapping pinned userspace threads, occupied
RPC/publisher ports and open VFIO groups. Control ports are restricted to loopback;
see [security](security-reference.md#juju-access). Configure sources using the
[host-to-VM example](configuration.md#direct-host-sources).

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
| `metrics/` | Per-source, per-VM, per-job, host and Ceph time series |
| `raw/` | Workload output, counters, configurations and logs |
| `events.jsonl` | Events, commands, tool versions and clock offsets |

Network rates use cumulative counters per participant: TX counts Ethernet frame
bytes (including host VLAN tags); RX counts IP bytes. TRex process counters are
summed per source, with individual output under
`raw/<source>/trex/process-<index>/` and aggregate `raw/<source>/trex/flood.jsonl`.
Host sources have stable names such as `hv001`, shown separately from VM placement.
fio reports IOPS, bandwidth and p99 latency per job. Profile summaries use each
participant's or job's available samples.
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

An unsuccessful stop or evidence retrieval stays incomplete and retryable,
including when `--retain` was requested.

Host floods stop first, including exact run-owned processes outside systemd's
cgroup. The RPC guard remains while any host sender process is not confirmed
stopped; `--abandon-evidence` cannot bypass this. Host cleanup removes only the
run's `/var/snap/trex-generator/common/cloud-stress-<run-id>` directory, its own
RPC table and run-owned collector/access files. Operator snap/device preparation
remains in place.

Exit codes: `0` completed, `20` failed, interrupted or not cleaned up, `30`
cancelled.

# Campaigns

cloud-stress runs one campaign at a time. A campaign is a built-in profile with
a fixed workload and phase layout; `--duration` sets the measured window and the
phases are split from it by weight.

| Profile | Load | Phases (weight) | Minimum duration |
| --- | --- | --- | --- |
| `host-network` | UDP flood, sender host to target guest | baseline 1, stress 4, recovery 1 | 6m |
| `ceph-block` | fio on a Ceph volume in the target guest | sweep 1 | 6m |
| `network-ceph` | `host-network` flood and fio together | baseline 1, stress 3, recovery 1 | 10m |

Preparation, calibration and finalization run outside the measured window and
have their own time budgets.

## Participants

Every run uses the same placement, chosen by `cloud-stress check` and shown
before the run starts:

- **Sender**: a hypervisor that generates the flood with TRex. TRex comes from
  the `trex-generator` snap, installed for the run and removed afterwards.
- **Target**: a different hypervisor that hosts the run's guest.
- **Guest**: one instance on the target, attached to the tested provider
  network (VLAN) and to an overlay network used only for management.
- **Ceph**: the cluster backing the guest's Cinder volume, sampled for
  profiles that run fio.

All resources are named `cloud-stress-<run-id>-*` and are deleted at the end
of the run.

## host-network

Measures how the target host's network path holds up under packet load from
another hypervisor.

1. **Calibration**: TRex sends a 1,000 pps burst for 15 s; the run continues
   only if the target receives it.
2. **Baseline**: no load; host and guest counters are sampled.
3. **Stress**: TRex sends UDP to the guest's provider address in four equal
   steps up to 1,000,000 pps (250k, 500k, 750k, 1M). Each packet has a new
   source address and source port, so OVS cannot aggregate them into one
   megaflow. Source addresses come from documentation and benchmarking ranges
   (`2001:db8:1::/48`, `198.18.0.0/15`), so replies never reach a real host.
4. **Recovery**: load stops; counters are sampled until the window ends.

Reported per step: requested, sent and received pps, and loss. Reported per
phase on the target: CPU busy, softirq and iowait; link, NIC ring and guest tap
drops; OVS lost upcalls; guest NIC drops.

If TRex cannot send the requested rate, the report says so: the step's
"delivered" column shows what the sender achieved, and loss is computed against
what was sent, not what was requested.

## ceph-block

Measures block storage latency and throughput as seen from a guest.

1. **Preparation**: a 20 GiB volume is created and attached. The run checks
   that the device in the guest is the run's volume, is unpartitioned and is
   not the root disk.
2. **Prefill**: the whole volume is written once, so reads hit allocated data.
3. **Sweep**: 12 cells run one after another, each for an equal share of the
   window:

   | Workload | Block size | Queue depths |
   | --- | --- | --- |
   | random read | 4k | 1, 16, 64 |
   | random write | 4k | 1, 16, 64 |
   | sequential read | 1m | 1, 16, 64 |
   | sequential write | 1m | 1, 16, 64 |

fio uses libaio with direct I/O and two jobs on separate halves of the volume.

Reported per cell: completed I/Os, IOPS, and p50 and p99 latency. Ceph health, slow
ops, unclean PGs and cluster read/write ops are sampled throughout.

## network-ceph

Measures whether network load on a hypervisor degrades storage on the same
host.

1. **Calibration**: the receipt check from `host-network`, then a volume prefill,
   then a 60 s fio run to measure the volume's maximum IOPS.
2. **Baseline**: fio runs at half the calibrated IOPS (4k random, 70% read,
   queue depth 16) with no network load.
3. **Stress**: the `host-network` flood steps run while fio continues at the same
   rate.
4. **Recovery**: the flood stops; fio continues.

fio runs at a fixed rate so that latency changes between phases come from the
flood, not from fio competing with itself. Compare baseline, stress and
recovery for fio IOPS and p99 latency (also shown as change from baseline),
target host drops and Ceph slow ops.

## Results

Each run writes a bundle to `/var/snap/cloud-stress/common/runs/<run-id>/`
with the raw tool output, sampled metrics and a summary. Results list SLO
indicators without a pass or fail verdict; compare them with your own targets.

```bash
cloud-stress run status [<run-id>]
cloud-stress run status [<run-id>] --json
cloud-stress run status [<run-id>] --html report.html
```

Exit codes: `0` completed, `20` failed, interrupted or not cleaned up, `30`
cancelled.

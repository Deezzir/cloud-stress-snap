# Configuration reference

A run is described by one YAML file. The example
[`config.yaml`](../config.yaml) lists every setting with its default; the snap
ships a read-only copy at
`/snap/cloud-stress/current/share/cloud-stress/config.yaml`.

Copy it to a regular file in your home directory and pass that file:

```bash
cp /snap/cloud-stress/current/share/cloud-stress/config.yaml ~/stress.yaml
cloud-stress run start --config ~/stress.yaml
```

`cloud-stress` reads the file when you start the run and sends the validated
configuration to the run worker. Unknown keys and invalid values are errors.

## Top level

| Key | Required | Meaning |
| --- | --- | --- |
| `duration` | yes | Total measured time: `90s`, `30m`, `2h`, `1h30m`. At most 24h. |
| `placement` | yes | Where the VMs run. |
| `workloads` | yes | Enabled workloads; at least one of `trex` and `fio`. |

Enable a workload by including its section, even empty (`fio: {}`). Omit a
section to disable it. Both sections together run concurrently on the same VMs.

## Defaults and profile lists

A setting omitted from an enabled workload section takes the value from the
example `config.yaml`. A `profiles` list you give replaces the default list as
a whole; profiles are not merged. An empty `profiles` list is an error.

## `placement`

| Key | Default | Meaning |
| --- | --- | --- |
| `vms` | - | Total VM count, placed automatically. |
| `hosts` | - | Exact VM count per Nova hypervisor, by short or full name. |
| `architectures` | `[amd64]` | Hypervisor architectures to use. `[]` removes the filter. |

Use exactly one of `vms` and `hosts`. Counts are positive integers.

Architecture names are normalized: `x86_64` is `amd64`, `aarch64` is `arm64`,
`ppc64le` is `ppc64el`. A hypervisor whose architecture is unknown is never
used. With `[]`, other requirements still apply: a guest image for the host's
architecture must exist, and TRex needs amd64.

A guest image is chosen per architecture from the newest Ubuntu image. An image
without an architecture property is treated as amd64.

### Automatic placement (`vms`)

- Without `trex`, VMs are spread round-robin over eligible hypervisors in name
  order, within each hypervisor's capacity.
- With `trex`, half of the VMs, rounded up, run on the focus host. The rest are
  spread round-robin over the other eligible hypervisors. `trex` needs at least
  two VMs and two eligible hypervisors.

### Exact placement (`hosts`)

The counts are used as given. A named hypervisor that does not exist or is not
eligible is an error. With `trex`, at least two hypervisors must be listed.

Capacity is what Placement reports as left on each hypervisor for the flavor,
after reserved amounts, allocation ratios and current usage. When Placement
does not report it, capacity is unknown and not limited. The run checks the
hypervisor of every VM after creation and fails if Nova placed a VM elsewhere.

## `workloads.trex`

| Key | Default | Meaning |
| --- | --- | --- |
| `mode` | `fan-in` | `fan-in` or `fan-out`. |
| `focus_host` | chosen | Hypervisor the traffic fans in to or out of. |
| `udp_payload_bytes` | `18` | UDP payload size, 2-1400 bytes. |
| `udp_dst_port` | `33434` | UDP destination port. |
| `udp_src_ports` | `[1024, 65535]` | Range of UDP source ports. |
| `profiles` | one profile, 100000 | List of `{pps_per_sender: <rate>}`. |

| Mode | Senders | Receivers |
| --- | --- | --- |
| `fan-in` | VMs on other hypervisors | VMs on the focus host |
| `fan-out` | VMs on the focus host | VMs on other hypervisors |

Without `focus_host`, the focus host is the eligible hypervisor with the
healthiest provider link, then the first by name. With `hosts`, `focus_host`
must be one of the listed hypervisors.

`pps_per_sender` is a sender's total rate, shared equally among the receivers
assigned to it. Every VM sends or receives, and every receiver is assigned at
least one sender.

TRex hypervisors must be amd64 and carry the provider network. The guest flavor
needs at least 3 vCPUs for the TRex threads; the smallest flavor that fits is
chosen. TRex packets, IP and UDP headers included, must fit the MTU of the
provider network and of the guest interface.

## `workloads.fio`

| Key | Default | Meaning |
| --- | --- | --- |
| `volume_size_gib` | `100` | Size of each VM's data volume. |
| `numjobs` | `1` | fio jobs per VM, 1-64. |
| `profiles` | four profiles | List of fio profiles. |

A profile has:

| Key | Meaning |
| --- | --- |
| `name` | Unique name: letters, digits, `.`, `_` and `-`. |
| `rw` | `randread`, `randwrite`, `randrw`, `read`, `write` or `rw`. |
| `bs` | Block size in bytes, or with `k` or `M`, such as `4k` or `1M`. It must fit in each job's region and be a multiple of the volume's sector size. |
| `iodepth` | Queue depth per job. |
| `rwmixread` | Read percentage, only with `randrw` or `rw`. |

The default profiles are 4k random read and random write at queue depth 4, and
1M random read and random write at queue depth 64.

## Duration and profiles

Each workload divides `duration` equally among its own profiles, in order.
When the duration does not divide evenly, earlier profiles get one second
more. Every profile window must be at least 10 seconds.

A 10-second warm-up with the first profile of each workload runs before the
measured time and is not part of `duration`.

## Resource overrides

Networks, images, flavor and volume type are discovered. Override them on the
command line:

| Option | Selects |
| --- | --- |
| `--provider-network` | VLAN provider network for TRex traffic. |
| `--ip-version` | `4` or `6` on the provider network. |
| `--network` | Overlay network for guest access. |
| `--image` | Guest image, used for every VM. |
| `--flavor` | Guest flavor. |
| `--volume-type` | Ceph-backed Cinder volume type. |

## Examples

fio only, exactly 10 VMs on one hypervisor:

```yaml
duration: 20m
placement:
  hosts:
    node-a: 10
workloads:
  fio: {}
```

TRex fanning out of `node-a` to 12 VMs elsewhere, at two rates:

```yaml
duration: 10m
placement:
  vms: 24
workloads:
  trex:
    mode: fan-out
    focus_host: node-a
    profiles:
      - pps_per_sender: 50000
      - pps_per_sender: 100000
```

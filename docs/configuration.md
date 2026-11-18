# Configuration reference

A run is described by one YAML file. The example
[`config.yaml`](../config.yaml) supplies the default VM configuration; the snap
ships a read-only copy at
`/snap/cloud-stress/current/share/cloud-stress/config.yaml`.

Copy it to your home directory and pass the copy:

```bash
cp /snap/cloud-stress/current/share/cloud-stress/config.yaml ~/stress.yaml
cloud-stress run start --config ~/stress.yaml
```

The file is read and validated at `run start`. Unknown keys and invalid values
are errors.

## Top level

| Key | Required | Meaning |
| --- | --- | --- |
| `duration` | yes | Total measured time: `90s`, `30m`, `2h`, `1h30m`. At most 24h. |
| `placement` | yes | Where the VMs run. |
| `workloads` | yes | Enabled workloads; at least one of `trex` and `fio`. |

Enable a workload by including its section, even empty (`fio: {}`). Omit a
section to disable it. Enabled workloads run concurrently; fio uses the placed
VMs, while TRex roles depend on its mode.

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
| `exclude_hosts` | `[]` | Hypervisors to exclude, by exact short or full Nova name. |

Use exactly one of `vms` and `hosts`. Counts are positive integers.

Architecture names are normalized: `x86_64` is `amd64`, `aarch64` is `arm64`,
`ppc64le` is `ppc64el`. A hypervisor whose architecture is unknown is never
used. With `[]`, other requirements still apply: a guest image for the host's
architecture must exist, and TRex senders need amd64.

`exclude_hosts` applies to both automatic and exact placement. Explicitly
requesting an excluded host in `hosts` or `trex.focus_host` is an error.

```yaml
placement:
  vms: 20
  architectures: [amd64]
  exclude_hosts: [node-a, node-b.example.com]
```

A guest image is chosen per architecture from the newest Ubuntu image. An image
without an architecture property is treated as amd64.

### Automatic placement (`vms`)

- Without `trex`, VMs are spread round-robin over eligible hypervisors in name
  order, within each hypervisor's capacity.
- With `trex` in `fan-in` or `fan-out`, `trex.focus_percent` of the VMs run on
  the focus host: `ceil(vms * focus_percent / 100)`, capped at `vms - 1` so
  both groups have at least one VM. The default is 50%. The rest are spread
  round-robin over the other eligible hypervisors, within their capacity.
- With `trex` in `many-to-many`, VMs are spread round-robin over the eligible
  hypervisors, within each hypervisor's capacity.
- VM sender modes need at least two VMs on at least two eligible hypervisors.
- With `host-to-vm`, receiver VMs are spread across eligible hypervisors with
  a source on another host. Host sources are selected separately.

### Exact placement (`hosts`)

The counts are used as given; `trex.focus_percent` is ignored. A named hypervisor
that does not exist or is not eligible is an error. With TRex VM sender modes,
at least two hypervisors must be listed.

Capacity is what Placement reports as left on each hypervisor for the flavor,
after reserved amounts, allocation ratios and current usage. When Placement
does not report it, capacity is unknown and not limited. The run checks the
hypervisor of every VM after creation and fails if Nova placed a VM elsewhere.

## `workloads.trex`

| Key | Default | Meaning |
| --- | --- | --- |
| `mode` | `fan-in` | `fan-in`, `fan-out`, `many-to-many` or `host-to-vm`. |
| `focus_host` | chosen | Hypervisor the traffic fans in to or out of; rejected with `many-to-many` or `host-to-vm`. |
| `focus_percent` | `50` | Integer percentage, 1-99, of `placement.vms` on the focus host: receivers in `fan-in`, senders in `fan-out`. Ignored with `placement.hosts` or `many-to-many`; rejected with `host-to-vm`. |
| `processes` | `1` | Independent single-core TRex servers per sender, integer 1-64. |
| `host_senders` | `{}` | Required only for `host-to-vm`: discovered host names mapped to `backend`, `interface` and `cpus`. |
| `udp_payload_bytes` | `18` | UDP payload size, 2-1400 bytes. |
| `udp_dst_port` | `33434` | UDP destination port. |
| `udp_src_ports` | `[1024, 65535]` | Range of UDP source ports. |
| `profiles` | one profile, 100000 | List of `{pps_per_sender: <rate>}`. |

| Mode | Senders | Receivers |
| --- | --- | --- |
| `fan-in` | VMs on other hypervisors | VMs on the focus host |
| `fan-out` | VMs on the focus host | VMs on other hypervisors |
| `many-to-many` | Every VM | Every other VM, including VMs on the same host |
| `host-to-vm` | Explicit physical hosts | Every placed VM on other hypervisors |

In `fan-in` and `fan-out`, without `focus_host`, the focus host is the eligible
hypervisor with the healthiest provider link, then the first by name. With
`hosts`, `focus_host` must be one of the listed hypervisors. `many-to-many` has
no focus host and rejects `focus_host`.

`pps_per_sender` is a sender's total rate, shared equally among the receivers
assigned to it. Every VM sends or receives, and every receiver is assigned at
least one sender. In `many-to-many`, every VM sends and receives, and each of N
VMs sends `pps_per_sender / (N - 1)` to each other VM.

With multiple processes, that total is also divided equally among processes:
`processes: 2` at 500000 PPS requests 250000 PPS from each process, not one
million PPS per VM. Processes share the data interface, with separate CPU
triples, RPC/publisher ports, runtime prefixes and source-port ranges.
`udp_src_ports` must contain at least one port per process.

VM sender modes require amd64 hypervisors carrying the provider network. The guest
flavor needs at least 3 vCPUs and 4 GiB RAM per process, plus 20 GB disk; the smallest
flavor that fits is chosen. This shared flavor applies to receivers too.
For example, two processes require at least 6 vCPUs and 8 GiB RAM.
TRex packets, IP and UDP headers included, must fit the MTU of the
provider network and of the guest interface.

### Direct host sources

`host-to-vm` makes every placed VM a receiver. Each selected host sends equally
to **all** receiver VMs on other hypervisors; local receivers are excluded because
physical traffic cannot rely on switch hairpin. Every source and receiver must
have a connection. `pps_per_sender` remains the total across processes and peers.

`host_senders` keys are discovered short or full hypervisor names. Values require:

| Key | Meaning |
| --- | --- |
| `backend` | `af_packet` or `dpdk`. |
| `interface` | AF_PACKET: the discovered physical/bond provider link, not a VLAN subinterface. DPDK: full PCI BDF of a dedicated operator-prepared NIC/VF. |
| `cpus` | Distinct nonnegative CPU IDs, exactly three per process: master, receive, traffic. |

Focus defaults do not apply in this mode; remove `focus_host` and `focus_percent`
from a copied default configuration. DPDK requires `processes: 1`; AF_PACKET supports
multiple processes. `placement.exclude_hosts` excludes sources too.
Receiver VMs need at least 2 vCPUs, 4 GiB RAM and 20 GB disk regardless of source
process count. Source-only hosts use no VM capacity or guest image. Sources need
amd64. The network must have a discovered VLAN ID in 1–4094 and a valid known MTU.

```yaml
duration: 2m
placement:
  hosts:
    node-b: 2
workloads:
  trex:
    mode: host-to-vm
    host_senders:
      node-a:
        backend: af_packet
        interface: bond0
        cpus: [4, 5, 6]
    profiles:
      - pps_per_sender: 100000
```

For native DPDK, use `backend: dpdk` and a PCI BDF such as
`interface: "0000:41:00.1"`. See [host preparation](campaigns.md#host-preparation).
Source MAC/IP identities are reserved from the data network, separate from host
management addresses.

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
more. Every profile window must be at least 10 seconds. The 10-second warm-up
is not part of `duration`; see [Timeline](campaigns.md#timeline).

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

TRex with 4 senders on `node-a` fanning out to 36 receivers elsewhere, at two rates:

```yaml
duration: 10m
placement:
  vms: 40
workloads:
  trex:
    mode: fan-out
    focus_host: node-a
    focus_percent: 10
    profiles:
      - pps_per_sender: 50000
      - pps_per_sender: 100000
```

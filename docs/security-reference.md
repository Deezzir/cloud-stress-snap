# Security and permissions

cloud-stress acts with the credentials of the user running it. Run it on a
machine you trust with those credentials, and use accounts limited to the
deployment under test.

## Snap connections

Strict confinement limits local file access; it does not limit what your
credentials allow remotely. Check what is connected:

```sh
snap connections cloud-stress
```

| Connection | What it allows |
| --- | --- |
| `network` | Connections to deployment APIs. |
| `network-bind` | The worker's local control socket. |
| `shared-memory` | Shared memory private to this snap. |
| `home` | Non-hidden files in your home directory, such as CA certificates. |
| `dot-local-share-juju` | Juju client configuration and credentials in `~/.local/share/juju`. |

Juju access needs a manual connection:

```sh
sudo snap connect cloud-stress:dot-local-share-juju
```

Run cloud-stress as the user whose client configuration it should use.

## Credentials

- Client configuration and the snap's user storage can contain passwords,
  tokens and keys. Keep them out of bug reports.
- OpenStack configurations that disable TLS verification are rejected, and
  password authentication requires HTTPS.
- The MAAS endpoint and API key are read from the Juju model's cloud
  credential (`juju show-credential --show-secrets`), so the Juju user must own
  it. The key is used in memory only; over an `http://` endpoint it is sent
  unencrypted.

## Juju access

`--juju-model` needs admin access to the model, which also allows changes to
it. Without it, `check` reports the Ceph and OVS reads as issues.

Host commands run through `juju exec` (as `ubuntu`); `run` uses `sudo`:

| Command | On | Does |
| --- | --- | --- |
| `check` | `ceph-mon`, `ovn-chassis`, hypervisors | Read-only Ceph actions, `ceph`, `ovs-vsctl` and hardware reads. |
| `run` | Hypervisors in the fleet and `ceph-mon` | Starts run-owned transient units and files under `/var/lib/cloud-stress/<run-id>`. |
| `run` | Hypervisors in the fleet | Hold the run's guest key and run SSH to their guests from the management network's OVN metadata namespace. |

In TRex sender guests, `run` installs the `trex-generator` snap from the Snap
Store with these connections:

| Connection | What it allows |
| --- | --- |
| `network-control` | Raw packet sockets. |
| `hardware-observe` | PCI and NIC details. |
| `process-control` | Its own core dump filter. |
| `system-observe` | Other running processes. |

## OpenStack resources

`run` creates a key pair, a security group and, for each VM, a server, its ports
and, with fio, a volume in your project, and deletes them during finalization.
Guests accept SSH only from the management network's metadata address, with a
key generated for the run, and TRex traffic only from the run's own ports.

## Sharing output

Output contains project IDs, hostnames, IP and MAC addresses and resource IDs.
Review text and JSON output before sharing it.

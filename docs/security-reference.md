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
| `dot-config-openstack` | OpenStack configuration in `~/.config/openstack`. |
| `etc-openstack` | OpenStack configuration in `/etc/openstack`. |

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
- Pass the MAAS API key in `MAAS_TOKEN`, not `--maas-token`. Use an `https://`
  MAAS URL; over `http://` the key is sent unencrypted.

## Juju access

`--juju-model` needs admin access to the model, which also allows changes to
it. Without it, `check` reports the Ceph and OVS reads as issues.

Host commands run through `juju exec`; `run` uses `sudo` on machines, where `juju exec` runs as `ubuntu`:

| Command | On | Does |
| --- | --- | --- |
| `check` | `ceph-mon`, `ovn-chassis`, hypervisors | Read-only Ceph actions, `ceph`, `ovs-vsctl` and hardware reads. |
| `run` | Sender, target and `ceph-mon` hosts | Starts run-owned transient units and files under `/var/lib/cloud-stress/<run-id>`. |
| `run` | Target | Holds the run's guest key and runs SSH to the guest from the management network's OVN metadata namespace. |
| `run` | Sender | Installs the `trex-generator` snap, then purges it. Needs Snap Store access. |

`trex-generator` connections:

| Connection | What it allows |
| --- | --- |
| `network-control` | Raw packet sockets. |
| `hardware-observe` | PCI and NIC details. |
| `process-control` | Its own core dump filter. |
| `system-observe` | Other running processes. |

## OpenStack resources

`run` creates a guest, two ports, a security group and a volume in your project,
and deletes them during finalization. The guest accepts SSH only from the
management network's metadata address, with a key generated for the run.

## Sharing output

Output contains project IDs, hostnames, IP and MAC addresses and resource IDs.
Review text and JSON output before sharing it.

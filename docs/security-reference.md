# Security and permissions

cloud-stress connects to your deployment using the credentials of the user
running it. Run it on a machine you trust with those credentials.

## Snap permissions

The snap uses strict confinement to limit access to local files. This does not
restrict what your credentials allow on a remote API. Use accounts
with only the permissions needed for discovery.

The snap declares the following connections. Some require manual approval;
check which are connected on your machine:

```sh
snap connections cloud-stress
```

| Connection | What it allows |
| --- | --- |
| `network` | Connections to deployment APIs. |
| `shared-memory` | Shared memory isolated to this snap. |
| `home` | Reading and writing non-hidden files in your home directory, including CA certificate files. |
| `dot-local-share-juju` | Reading and writing your Juju client configuration and authentication data in `~/.local/share/juju`. |
| `dot-config-openstack` | Reading OpenStack configuration in `~/.config/openstack`. |
| `etc-openstack` | Reading system OpenStack configuration in `/etc/openstack`. |

Connect only what you need. To enable Juju discovery:

```sh
sudo snap connect cloud-stress:dot-local-share-juju
```

Run the tool as the user whose client configuration you intend to use. Installing the snap with `sudo` does not make later invocations run as root
or give them access to another user's files.

## Credentials and connection checks

cloud-stress uses your existing client configuration; snap connections can
expose files containing passwords, tokens, cookies, or private keys. Do not
include those files in bug reports or support attachments.

OpenStack discovery rejects configurations that disable TLS certificate
verification, and password authentication requires HTTPS. Configure the correct
CA certificates before running discovery.

Pass the MAAS API key through the `MAAS_TOKEN` environment variable rather than
`--maas-token`, so it does not appear in shell history or process listings.
Over an `http://` MAAS URL the key is sent unencrypted; prefer `https://`.

Treat the snap's user storage as sensitive: it can contain authentication data.
Do not include it in bug reports or support attachments.

## Juju access

With `--juju-model`, `check` reads host hardware, Ceph, and OVS state through
Juju. It runs read-only Ceph charm actions and a few read-only commands with
`juju exec`, which runs them as root: `ceph` on `ceph-mon`, `ovs-vsctl` on
`ovn-chassis`, and hardware reads (`lscpu`, `uname`, `/proc/meminfo`, `ip`,
`lsblk`, `lspci`, NIC speeds) on the machines hosting Nova hypervisors.

This needs admin access to the model. Admin access also allows changes to the
model, so use a Juju account limited to the models you check. Without it, `check`
lists the Ceph and OVS reads under issues and continues with the rest.

## Sharing command output

Discovery output includes deployment details such as project IDs, hostnames,
IP and MAC addresses, and resource IDs. Review both text and JSON output before
posting it publicly, and remove details you are not allowed to share.

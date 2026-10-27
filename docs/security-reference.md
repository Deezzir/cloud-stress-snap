# Snap security

cloud-stress generates deliberate infrastructure load. Restrict its use to
authorized operators, approved target scopes, and agreed measurement windows.
Stress traffic can affect other tenants when hardware or backend paths are
shared; confinement does not isolate the cloud from the workloads it authorizes.

## Controller and access

Install the snap on a protected bastion or infra-management node. Strict
confinement limits the controller's local access; it does not replace API or SSH
authorization. Use narrowly scoped credentials where the required operations
permit, preserve CA and known-host verification, and grant only needed snap
interfaces.

| Interface | Purpose |
| --- | --- |
| `network` | Outbound API and SSH access to the selected deployment. |
| `ssh-keys` | Optional read access to the invoking identity's SSH material. |
| `juju-client-observe` | Optional read access to Juju client configuration. |
| `openstack-user-config` | Optional read access to `$HOME/.config/openstack`. |
| `maas-user-config` | Optional read access to `$HOME/.maas.db`. |
| `openstack-system-config` | Optional read access to `/etc/openstack`. |

The installation identity is root, so same-identity file grants refer to root's
configuration. They do not grant arbitrary access to another user's home.
Personal/system file interfaces require the applicable Store declarations and
operator grants; do not assume auto-connection.

## Credentials and evidence

Keep imported credentials in restricted installation storage, separate from
exportable evidence. Preserve encrypted keys at rest. Never include passphrases,
decrypted keys, or raw authentication configuration in logs, reports, or bundles.
Review retained evidence for sensitive infrastructure identifiers before sharing.

## Campaign safeguards

Discovery must remain read-only. Before a campaign, review its targets, resource
budgets, delivered-load requirements, and cleanup scope. Workloads need locally
enforced lifetimes even when the controller becomes unreachable. Cancellation
and recovery must affect only run-owned resources and retain unresolved cleanup
items explicitly.

Do not bypass unsupported offload or QoS behavior by changing production device
configuration. Local control sockets must be restricted to the execution
identity; local visualization must preserve authentication and bind to loopback
unless explicitly configured otherwise.

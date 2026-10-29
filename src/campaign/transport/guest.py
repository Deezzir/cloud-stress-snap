"""SSH to run-owned guests from their host's OVN metadata namespace.

The guest's management NIC sits on an overlay network. On the target host, that
network's `ovnmeta-<network-id>` namespace has an address on the same subnet, so
SSH runs there through `juju exec`, with the run's key and pinned host key.
"""

import shlex
import subprocess
from pathlib import Path

import jubilant

from campaign.model.state import RunState
from campaign.model.target import Target
from campaign.transport import remote
from campaign.transport.remote import STOP

SSH_TIMEOUT = 120


def keygen(path: Path) -> str:
    """Create an ed25519 key pair at path and return the public key line."""
    command = "ssh-keygen -q -t ed25519 -C cloud-stress -N".split()
    subprocess.run([*command, "", "-f", str(path)], check=True)
    return path.with_suffix(".pub").read_text().strip()


class Guest:
    """One run-owned guest reached as the image's default user."""

    def __init__(
        self,
        juju: jubilant.Juju,
        machine: str,
        network_id: str,
        address: str,
        host_dir: str,
    ) -> None:
        """Remember the jump host, namespace, guest address and key paths."""
        self.juju = juju
        self.machine = machine
        self.namespace = f"ovnmeta-{network_id}"
        self.address = address
        self.key = f"{host_dir}/guest/id_ed25519"
        self.known_hosts = f"{host_dir}/guest/known_hosts"

    def install_keys(self, private_key: str, host_public_key: str) -> None:
        """Place the run's key and the guest's pinned host key on the jump host."""
        remote.push(self.juju, self.machine, self.key, private_key)
        remote.push(
            self.juju,
            self.machine,
            self.known_hosts,
            f"{self.address} {host_public_key}\n",
        )
        remote.sh(self.juju, self.machine, f"chmod 600 {shlex.quote(self.key)}")

    def namespace_ready(self) -> bool:
        """Whether the management network's metadata namespace exists on the host."""
        out = remote.sh(self.juju, self.machine, "ip netns list")
        return any(line.split()[:1] == [self.namespace] for line in out.splitlines())

    def run(self, command: str, timeout: int = SSH_TIMEOUT) -> str:
        """Run a command with sudo on the guest and return stdout."""
        options = (
            "BatchMode=yes",
            "IdentitiesOnly=yes",
            "StrictHostKeyChecking=yes",
            f"UserKnownHostsFile={self.known_hosts}",
            "ConnectTimeout=10",
        )
        ssh = shlex.join(
            [
                "ip",
                "netns",
                "exec",
                self.namespace,
                "ssh",
                "-i",
                self.key,
                *(arg for option in options for arg in ("-o", option)),
                f"ubuntu@{self.address}",
                f"sudo bash -c {shlex.quote(command)}",
            ]
        )
        try:
            return remote.sh(self.juju, self.machine, ssh, wait=timeout)
        except (jubilant.CLIError, jubilant.TaskError, TimeoutError) as error:
            detail = getattr(error, "stderr", "") or str(error)
            raise RuntimeError(
                "guest command failed: " + " ".join(str(detail).split())[:300]
            ) from None

    def reachable(self) -> bool:
        """Whether the guest accepts the run's key."""
        try:
            self.run("true", timeout=30)
            return True
        except RuntimeError:
            return False

    def start_unit(self, unit: str, max_s: int, command: str, rc_path: str) -> None:
        """Start a transient unit that records its exit code in rc_path."""
        script = f"{command}; echo $? > {shlex.quote(rc_path)}"
        self.run(
            f"systemd-run --unit {unit} --collect "
            f"--property RuntimeMaxSec={max_s} --property KillMode=control-group "
            f"bash -c {shlex.quote(script)}"
        )

    def unit_active(self, unit: str) -> bool:
        """Whether a transient unit is still running."""
        out = self.run(f"systemctl is-active {unit}.service || true")
        return out.strip() in ("active", "activating", "deactivating")

    def exit_code(self, rc_path: str) -> int | None:
        """Exit code a finished unit recorded, or None if it never finished."""
        out = self.run(f"cat {shlex.quote(rc_path)} 2>/dev/null || true").strip()
        return int(out) if out else None

    def stop_unit(self, unit: str) -> None:
        """Stop a transient unit and confirm it is gone or inactive."""
        self.run(STOP.format(unit=unit))


def guest_for(juju: jubilant.Juju, state: RunState, target: Target) -> Guest:
    """The run's target guest, reached from the target host."""
    return Guest(
        juju,
        state.plan.target_machine or "",
        state.plan.mgmt_network_id or "",
        target.mgmt_address,
        f"/var/lib/cloud-stress/{state.id}",
    )

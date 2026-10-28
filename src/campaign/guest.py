"""SSH to run-owned guests with the run's key and a pinned guest host key."""

import shlex
import subprocess
from pathlib import Path

from campaign.remote import STOP

SSH_TIMEOUT = 120


def keygen(path: Path) -> str:
    """Create an ed25519 key pair at path and return the public key line."""
    command = "ssh-keygen -q -t ed25519 -C cloud-stress -N".split()
    subprocess.run([*command, "", "-f", str(path)], check=True)
    return path.with_suffix(".pub").read_text().strip()


class Guest:
    """One run-owned guest reached as the image's default user."""

    def __init__(self, address: str, key: Path, known_hosts: Path) -> None:
        """Remember the guest address and the run's key files."""
        self.address = address
        self.key = key
        self.known_hosts = known_hosts

    def pin(self, host_public_key: str) -> None:
        """Trust only the host key the run injected through cloud-init."""
        self.known_hosts.write_text(f"{self.address} {host_public_key}\n")

    def run(self, command: str, timeout: int = SSH_TIMEOUT) -> str:
        """Run a command with sudo on the guest and return stdout."""
        options = (
            "BatchMode=yes",
            "IdentitiesOnly=yes",
            "StrictHostKeyChecking=yes",
            f"UserKnownHostsFile={self.known_hosts}",
            "ConnectTimeout=10",
        )
        result = subprocess.run(
            [
                "ssh",
                "-i",
                str(self.key),
                *(arg for option in options for arg in ("-o", option)),
                f"ubuntu@{self.address}",
                f"sudo bash -c {shlex.quote(command)}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"guest command failed ({result.returncode}): "
                + " ".join(result.stderr.split())[:300]
            )
        return result.stdout

    def reachable(self) -> bool:
        """Whether the guest accepts the run's key."""
        try:
            self.run("true", timeout=30)
            return True
        except (RuntimeError, subprocess.TimeoutExpired):
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

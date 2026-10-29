"""Run-owned files and processes on Juju machines and units, through `juju exec`."""

import base64
import gzip
import shlex

import jubilant

EXEC_TIMEOUT = 120
MAX_FETCH = 64 * 1024 * 1024


def target(where: str) -> dict[str, str]:
    """Juju exec target: a machine ID or a unit name."""
    return {"unit": where} if "/" in where else {"machine": where}


def sh(juju: jubilant.Juju, where: str, script: str, wait: int = EXEC_TIMEOUT) -> str:
    """Run a shell script as root on a machine or unit and return stdout."""
    command = f"bash -c {shlex.quote(script)}"
    if "/" not in where:
        # `juju exec --machine` runs as the ubuntu user; units run as root.
        command = f"sudo -n {command}"
    return juju.exec(command, wait=wait, **target(where)).stdout


def push(juju: jubilant.Juju, where: str, path: str, content: str) -> None:
    """Write a small file on the remote side, creating its directory."""
    data = base64.b64encode(gzip.compress(content.encode())).decode()
    sh(
        juju,
        where,
        f"install -d -m 0700 {shlex.quote(path.rsplit('/', 1)[0])} && "
        f"echo {data} | base64 -d | gunzip > {shlex.quote(path)}",
    )


def fetch(juju: jubilant.Juju, where: str, path: str) -> str | None:
    """Read a remote file, or None when it does not exist."""
    quoted = shlex.quote(path)
    size = sh(juju, where, f"stat -c %s {quoted} 2>/dev/null || true").strip()
    if not size:
        return None
    if int(size) > MAX_FETCH:
        raise ValueError(f"{path} is {size} bytes, over the {MAX_FETCH} byte limit")
    out = sh(juju, where, f"gzip -c {quoted} | base64 -w0", wait=300).strip()
    return gzip.decompress(base64.b64decode(out)).decode(errors="replace")


def start_unit(
    juju: jubilant.Juju, where: str, unit: str, max_runtime_s: int, command: str
) -> None:
    """Start a transient systemd unit that stops itself after max_runtime_s."""
    sh(
        juju,
        where,
        f"systemd-run --unit {unit} --collect "
        f"--property RuntimeMaxSec={max_runtime_s} "
        f"--property KillMode=control-group "
        f"bash -c {shlex.quote(command)}",
    )


STOP = """\
if systemctl list-units --all --plain --no-legend {unit}.service | grep -q .; then
  systemctl stop {unit}.service
fi
! systemctl is-active --quiet {unit}.service
"""


def stop_unit(juju: jubilant.Juju, where: str, unit: str) -> None:
    """Stop a run-owned transient unit and confirm it is gone or inactive."""
    sh(juju, where, STOP.format(unit=unit))


def remove_dir(juju: jubilant.Juju, where: str, path: str) -> None:
    """Remove a run-owned directory under /var/lib/cloud-stress."""
    if not path.startswith("/var/lib/cloud-stress/"):
        raise ValueError(f"refusing to remove {path}")
    quoted = shlex.quote(path)
    sh(juju, where, f"rm -rf {quoted} && test ! -e {quoted}")


def remove_snap(juju: jubilant.Juju, where: str, name: str) -> None:
    """Purge a run-owned snap; a snap that is already gone counts as removed."""
    quoted = shlex.quote(name)
    sh(
        juju,
        where,
        f"snap remove --purge {quoted} || ! snap list {quoted} >/dev/null 2>&1",
        wait=300,
    )

"""Run-owned files and processes on Juju machines and units, through `juju exec`."""

import base64
import gzip
import json
import re
import shlex
from datetime import datetime

import jubilant

from constants import EXEC_TIMEOUT_S, MAX_FETCH_BYTES, TREX_SNAP


def sh(juju: jubilant.Juju, where: str, script: str, wait: int = EXEC_TIMEOUT_S) -> str:
    """Run a shell script as root on a machine or unit and return stdout."""
    command = f"bash -c {shlex.quote(script)}"
    if "/" in where:
        return juju.exec(command, wait=wait, unit=where).stdout
    # `juju exec --machine` runs as the ubuntu user; units run as root.
    return juju.exec(f"sudo -n {command}", wait=wait, machine=where).stdout


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
    out = sh(
        juju,
        where,
        f"set -eo pipefail; cd {shlex.quote(path.rsplit('/', 1)[0])}; "
        f"test -e {quoted} || exit 0; size=$(stat -c %s {quoted}); "
        f'test "$size" -le {MAX_FETCH_BYTES} || '
        '{ echo "evidence too large" >&2; exit 1; }; '
        f"gzip -c {quoted} | base64 -w0",
        wait=300,
    ).strip()
    return (
        gzip.decompress(base64.b64decode(out)).decode(errors="replace") if out else None
    )


def unit_command(
    unit: str,
    max_runtime_s: int,
    command: str,
    rc_path: str | None = None,
) -> str:
    """Build a transient systemd unit command bounded by max_runtime_s.

    snapd moves a snap app into its own cgroup scope, out of the unit's reach, so
    `timeout` bounds the command too and forwards the unit's stop signal to it.
    """
    script = f"timeout --kill-after=10 {max_runtime_s} bash -c {shlex.quote(command)}"
    if rc_path:
        # Bash defers TERM until the foreground timeout finishes stopping its child.
        record = f"echo $? > {shlex.quote(rc_path)}"
        script = f"trap {shlex.quote(record)} EXIT; trap 'exit 143' TERM; {script}"
    script = script.replace("$", "$$")
    return (
        f"systemd-run --unit {shlex.quote(unit)} --collect "
        f"--property RuntimeMaxSec={max_runtime_s + 30} "
        f"--property KillMode=control-group "
        f"bash -c {shlex.quote(script)}"
    )


STOP = """\
units=$(systemctl list-units --all --plain --no-legend {unit}.service) || exit 1
if [ -n "$units" ]; then
  systemctl stop {unit}.service || exit 1
  active=$(systemctl list-units --state=active,activating,reloading,deactivating \
--plain --no-legend {unit}.service) || exit 1
  test -z "$active" || exit 1
fi
"""


# Processes whose command line names the run's directory; the bracket keeps the
# pattern from matching this script's own command line.
REAP = """\
pkill -f {pattern} || true
for _ in 1 2 3 4 5; do pgrep -f {pattern} >/dev/null || exit 0; sleep 1; done
pkill -KILL -f {pattern} || true
sleep 1
! pgrep -f {pattern} >/dev/null
"""


def stop_unit(
    juju: jubilant.Juju,
    where: str,
    unit: str,
    run_dir: str | None,
    *,
    process_path: str | None = None,
) -> None:
    """Stop a run-owned transient unit and confirm it is gone or inactive.

    With run_dir, also stop any process that escaped the unit and still names
    that directory, such as a snap app in its own cgroup scope.
    """
    script = STOP.format(unit=unit)
    if run_dir:
        pattern = shlex.quote(f"[{run_dir[0]}]{re.escape(run_dir[1:])}/")
        script += REAP.format(pattern=pattern)
    if process_path:
        if not re.fullmatch(
            r"/var/snap/trex-generator/common/cloud-stress-[0-9a-f-]+/"
            r"(?:flood\.py|process-\d+/trex_cfg\.yaml)",
            process_path,
        ):
            raise ValueError(f"refusing to reap processes for {process_path}")
        pattern = shlex.quote(f"[/]{re.escape(process_path[1:])}( |$)")
        script += REAP.format(pattern=pattern)
    sh(juju, where, script)


def remove_dir(juju: jubilant.Juju, where: str, path: str, run_id: str) -> None:
    """Remove only an exact run-owned directory."""
    if path not in (
        f"/var/lib/cloud-stress/{run_id}",
        f"/var/snap/{TREX_SNAP}/common/cloud-stress-{run_id}",
    ) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", run_id):
        raise ValueError(f"refusing to remove {path}")
    sh(
        juju,
        where,
        f'test ! -e {path} || {{ test "$(cat {path}/.owner)" = {run_id} '
        f"&& rm -rf {path}; }}",
    )


def remove_rpc_guard(juju: jubilant.Juju, where: str, table: str) -> None:
    """Delete the exact run-owned RPC table, or accept its confirmed absence."""
    if not re.fullmatch(r"cloud_stress_[0-9a-f]{32}_rpc", table):
        raise ValueError(f"refusing to remove nftables table {table}")
    tables = sh(juju, where, "nft list tables").splitlines()
    if f"table inet {table}" in tables:
        sh(juju, where, f"nft delete table inet {table}")


def trex_install_date(response: str) -> datetime | None:
    """Distinguish a confirmed absence from an unknown installation identity."""
    name = TREX_SNAP
    result = json.loads(response)
    if (
        result.get("status-code") == 404
        and result.get("type") == "error"
        and result["result"].get("kind") == "snap-not-found"
        and result["result"].get("value") == name
    ):
        return None
    if result.get("status-code") != 200 or result.get("type") != "sync":
        raise RuntimeError(f"cannot inspect {name}: snapd lookup failed")
    snap = result["result"]
    installed_at = snap.get("install-date")
    if snap.get("name") != name or not installed_at:
        raise RuntimeError(f"preserved {name}: cannot verify install ownership")
    return datetime.fromisoformat(installed_at)


def remove_owned_snap(
    juju: jubilant.Juju,
    where: str,
    name: str,
    intent: datetime | None,
    created: datetime | None,
    identity: str | None,
) -> None:
    """Purge only the installation observed in the producer's install window."""
    if name != TREX_SNAP:
        raise ValueError(f"unexpected snap: {name}")
    installed = trex_install_date(sh(juju, where, f"snap debug api /v2/snaps/{name}"))
    if installed is None:
        return
    if not intent or not created:
        raise RuntimeError(f"preserved {name}: cannot verify install ownership")
    if identity is not None and identity != installed.isoformat():
        raise RuntimeError(f"preserved {name}: installation identity changed")
    if not intent <= installed <= created:
        raise RuntimeError(f"preserved {name}: install date outside run install window")
    sh(juju, where, f"snap remove --purge {name}", wait=300)

"""Command-line interface for Prodstack infrastructure stress testing."""

import json
import logging
import socket
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import click

from campaign.lifecycle import access
from campaign.model import profiles
from campaign.model import state as run_state
from campaign.report.text import summary_text
from helpers import (
    PLAN_CHOICES,
    datapath_notices,
    discover_cloud,
    link_juju_to_hypervisors,
    nova_state_notices,
    print_check_result,
    print_plan,
    print_table,
    profile_readiness,
    resolve_plan,
)
from probe.ceph_probe import probe_ceph
from probe.infra_probe import probe_infra
from probe.juju_probe import probe_juju
from probe.maas_probe import probe_maas
from probe.models import CheckResult, Profile
from probe.openstack_probe import resolve_cloud
from probe.ovs_probe import probe_ovs


def access_options[F: Callable[..., object]](func: F) -> F:
    """Options that select the cloud, Juju model and optional MAAS."""
    for option in reversed(
        [
            click.option("--cloud", help="Select a named cloud in clouds.yaml."),
            click.option("--juju-model", help="Read membership from this Juju model."),
            click.option(
                "--maas-url", help="Read MAAS records for scoped Juju machines."
            ),
            click.option(
                "--maas-token",
                envvar="MAAS_TOKEN",
                help="MAAS API key (consumer:token:secret); defaults to $MAAS_TOKEN.",
            ),
            click.option(
                "-v", "--verbose", is_flag=True, help="Print diagnostic details."
            ),
        ]
    ):
        func = option(func)
    return func


def discover(
    cloud: str | None,
    juju_model: str | None,
    maas_url: str | None,
    maas_token: str | None,
    verbose: bool,
    overrides: dict[str, str] | None = None,
) -> CheckResult:
    """Run live, read-only discovery and resolve the run plan."""
    if verbose:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("debug: %(message)s"))
        for name in ("helpers", "probe"):
            logging.getLogger(name).addHandler(handler)
            logging.getLogger(name).setLevel(logging.DEBUG)
    if maas_url and not maas_token:
        raise click.UsageError("--maas-url requires --maas-token or MAAS_TOKEN")
    with ThreadPoolExecutor(max_workers=3) as pool:
        juju_future = pool.submit(probe_juju, juju_model)
        cloud, openstack, openstack_notices = discover_cloud(cloud)
        juju, juju_notices = juju_future.result()
        scoped_ids = {
            machine.instance_id for machine in juju.machines if machine.instance_id
        }
        maas, maas_notices = probe_maas(maas_url, maas_token, scoped_ids)
        link_notices = link_juju_to_hypervisors(juju, maas, openstack.hosts)
        infra_future = pool.submit(probe_infra, juju_model, juju, openstack.hosts)
        ceph_future = pool.submit(probe_ceph, juju_model, juju)
        ovs_future = pool.submit(probe_ovs, juju_model, juju)
        infra, infra_notices = infra_future.result()
        ceph_result, ceph_notices = ceph_future.result()
        ovs_hosts, ovs_notices = ovs_future.result()
    result = CheckResult(
        cloud=cloud,
        openstack=openstack,
        juju=juju,
        maas=maas,
        infra=infra,
        ceph=ceph_result,
        ovs=ovs_hosts,
        notices=[
            *openstack_notices,
            *juju_notices,
            *maas_notices,
            *link_notices,
            *infra_notices,
            *ceph_notices,
            *ovs_notices,
        ],
    )
    result.plan, missing = resolve_plan(result, overrides)
    result.readiness = profile_readiness(result, missing)
    result.notices[:0] = nova_state_notices(result)
    result.notices.extend(datapath_notices(result))
    return result


@click.group(no_args_is_help=True)
def app() -> None:
    """Stress test Prodstack network, storage, and compute infrastructure."""


@app.command()
@access_options
@click.option("--json", "as_json", is_flag=True, help="Print live results as JSON.")
def check(
    cloud: str | None,
    juju_model: str | None,
    maas_url: str | None,
    maas_token: str | None,
    verbose: bool,
    as_json: bool,
) -> None:
    """Check access and print live, read-only infrastructure observations."""
    result = discover(cloud, juju_model, maas_url, maas_token, verbose)
    print_check_result(result, as_json)
    if not result.openstack.hosts:
        click.echo("No compute hosts were discovered", err=True)
        raise SystemExit(2)
    if not juju_model:
        click.echo("Host probes require --juju-model", err=True)
        raise SystemExit(2)
    if not any(
        probe.status in ("queried", "partial") for probe in result.infra.host_probes
    ):
        click.echo("No host probes succeeded", err=True)
        raise SystemExit(2)


@app.group()
def run() -> None:
    """Start, follow and stop stress runs."""


def request(payload: dict[str, object], check: bool = True) -> dict[str, object]:
    """Send one request to the worker and return its reply."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(str(run_state.SOCKET))
            conn.sendall(json.dumps(payload).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > 1024 * 1024:
                    raise OSError("reply too large")
    except OSError as error:
        raise click.ClickException(
            f"The run worker is not reachable at {run_state.SOCKET}: {error}. "
            "See: sudo journalctl -u snap.cloud-stress.worker"
        ) from None
    reply: dict[str, object] = json.loads(data)
    if check and not reply.get("ok"):
        click.echo(f"Error: {reply.get('error')}", err=True)
        raise SystemExit(2)
    return reply


def operator_access(cloud: str) -> access.Access:
    """The invoking operator's access, for the worker."""
    try:
        return access.collect(resolve_cloud(cloud))
    except (OSError, ValueError) as error:
        raise click.UsageError(str(error)) from None


def follow(run_id: str, after_attempt: int = 0) -> None:
    """Print progress until a finalize attempt after `after_attempt` ends.

    Ctrl+C only detaches; the run continues in the worker.
    """
    shown = None
    try:
        while True:
            state = run_state.load(run_id)
            line = state.phase + (f": {state.detail}" if state.detail else "")
            if line != shown:
                click.echo(f"{datetime.now(UTC):%H:%M:%S}  {line}")
                shown = line
            if state.code is not None and state.finalize_attempts > after_attempt:
                break
            time.sleep(2)
    except KeyboardInterrupt:
        click.echo(
            f'\nDetached. The run continues; follow it with "cloud-stress run '
            f'status {run_id}" or stop it with "cloud-stress run stop".',
            err=True,
        )
        raise SystemExit(130) from None
    click.echo()
    show_result(state)
    raise SystemExit(state.code or 0)


def show_result(state: run_state.RunState) -> None:
    """Print a run's summary, or its outcome when no result was written."""
    result_file = run_state.run_dir(state.id) / "report.json"
    if result_file.is_file():
        click.echo(summary_text(json.loads(result_file.read_text())))
    else:
        for cause in state.causes:
            click.echo(f"  - {cause}")
    click.echo(f"Results: {run_state.run_dir(state.id)}")


@run.command()
@click.option(
    "--profile",
    required=True,
    type=click.Choice(list(profiles.PROFILES)),
    help="Stress profile to run.",
)
@click.option("--duration", required=True, help="Measured window, e.g. 30m or 1h.")
@access_options
@click.option("--sender", help="Sender hypervisor (short hostname).")
@click.option("--target", help="Target hypervisor (short hostname).")
@click.option("--network", help="VLAN provider network name or ID.")
@click.option(
    "--ip-version",
    type=click.Choice(["4", "6"]),
    help="Target address family; default IPv6 when the network has it.",
)
@click.option("--mgmt-network", help="Overlay network name or ID for guest access.")
@click.option("--image", help="Guest image name or ID.")
@click.option("--flavor", help="Guest flavor name or ID.")
@click.option("--volume-type", help="Cinder volume type name or ID.")
@click.option("--yes", is_flag=True, help="Start without asking for confirmation.")
def start(
    profile: str,
    duration: str,
    cloud: str | None,
    juju_model: str | None,
    maas_url: str | None,
    maas_token: str | None,
    verbose: bool,
    yes: bool,
    **choices: str | None,
) -> None:
    """Start a stress run with a selected profile and duration."""
    try:
        schedule = profiles.resolve(profile, profiles.parse_duration(duration))  # type: ignore[arg-type]
    except ValueError as error:
        raise click.UsageError(str(error)) from None
    if not juju_model:
        raise click.UsageError("run start requires --juju-model")
    if pending := run_state.unfinished():
        raise click.UsageError(
            f"Run {pending.id} is not finished; "
            'see "cloud-stress run status" or run "cloud-stress run stop".'
        )
    overrides = {key: value for key, value in choices.items() if value}
    assert set(overrides) <= set(PLAN_CHOICES)
    result = discover(cloud, juju_model, maas_url, maas_token, verbose, overrides)
    readiness = next(item for item in result.readiness if item.profile == profile)
    if not readiness.ready:
        click.echo(f"{profile} cannot start:", err=True)
        for blocker in readiness.blockers:
            click.echo(f"  - {blocker}", err=True)
        raise SystemExit(2)
    click.echo(f"{profile} for {duration} on model {juju_model}\n\nPlacement:")
    print_plan(result.plan, cast(Profile, profile))
    fmt = profiles.format_duration
    measured = ", ".join(f"{p.name} {fmt(p.length_s)}" for p in schedule.phases)
    if len(schedule.phases) > 1:
        measured = f"{fmt(schedule.duration_s)}: {measured}"
    rows = [["Measured", measured]]
    if schedule.trex_steps_pps:
        stress = next(p for p in schedule.phases if p.name == "stress")
        step = fmt(stress.length_s // len(schedule.trex_steps_pps))
        steps = " / ".join(f"{pps:,}" for pps in schedule.trex_steps_pps)
        rows.append(["Flood", f"{steps} pps, {step} each"])
    rows.append(
        [
            "Limits",
            f"preparation {fmt(schedule.prepare_budget_s)}, "
            f"calibration {fmt(schedule.calibrate_budget_s)}, "
            f"finalization {fmt(schedule.finalize_budget_s)}; "
            f"{fmt(schedule.max_elapsed_s)} in total",
        ]
    )
    click.echo("\nSchedule:")
    print_table(rows)
    click.echo()
    if not yes:
        if not sys.stdin.isatty():
            raise click.UsageError("Confirmation needed; pass --yes to start.")
        click.confirm("Start this run?", abort=True)
    reply = request(
        {
            "op": "start",
            "profile": profile,
            "duration_s": schedule.duration_s,
            "model": juju_model,
            "cloud": result.cloud,
            "plan": result.plan.model_dump(),
            "access": operator_access(result.cloud).model_dump(),
        }
    )
    click.echo(f"Run {reply['run_id']} accepted.")
    follow(str(reply["run_id"]))


@run.command()
@click.argument("run_id", required=False)
@click.option("--cloud", help="Select a named cloud in clouds.yaml.")
@click.option(
    "--abandon-evidence",
    is_flag=True,
    help="Clean up even when evidence on an unreachable host or guest is lost.",
)
def stop(run_id: str | None, cloud: str | None, abandon_evidence: bool) -> None:
    """Stop the active run, or finish cleanup of an interrupted one."""
    pending = run_state.unfinished()
    if pending is None:
        raise click.UsageError("No active or interrupted run.")
    payload: dict[str, object] = {
        "op": "stop",
        "run_id": run_id,
        "abandon_evidence": abandon_evidence,
    }
    reply = request(payload, check=False)
    if reply.get("needs_access"):
        payload["access"] = operator_access(cloud or pending.cloud).model_dump()
        reply = request(payload)
    elif not reply.get("ok"):
        click.echo(f"Error: {reply.get('error')}", err=True)
        raise SystemExit(2)
    click.echo(f"Stopping run {reply['run_id']}.")
    follow(str(reply["run_id"]), pending.finalize_attempts)


@run.command()
@click.argument("run_id", required=False)
@click.option("--json", "as_json", is_flag=True, help="Print the run result as JSON.")
@click.option(
    "--html",
    "html_file",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write the run's HTML report to this file; needs RUN_ID.",
)
def status(run_id: str | None, as_json: bool, html_file: Path | None) -> None:
    """Show the active run, or a retained run (the latest by default)."""
    if html_file and not run_id:
        raise click.UsageError("--html needs the run ID.")
    if html_file and str(html_file.resolve()).startswith(("/tmp/", "/var/tmp/")):
        raise click.UsageError(
            "The snap has its own /tmp; write the report to your home directory."
        )
    try:
        state = run_state.load(run_id) if run_id else next(iter(run_state.runs()), None)
    except FileNotFoundError:
        raise click.UsageError(f"No run {run_id}") from None
    if state is None:
        click.echo("No runs.")
        return
    bundle = run_state.run_dir(state.id)
    if html_file:
        if not (bundle / "report.html").is_file():
            raise click.UsageError("This run has no HTML report yet.")
        html_file.write_text((bundle / "report.html").read_text())
        click.echo(f"Wrote {html_file.resolve()}")
        return
    if as_json:
        result_file = bundle / "report.json"
        click.echo(
            result_file.read_text()
            if result_file.is_file()
            else state.model_dump_json(indent=2)
        )
        return
    if state.phase == "finished":
        show_result(state)
        return
    end = datetime.now(UTC)
    elapsed = int((end - state.created_at).total_seconds())
    print_table(
        [
            ["Run", state.id],
            ["Profile", state.profile],
            ["Duration", profiles.format_duration(state.duration_s)],
            ["Phase", state.phase + (f" ({state.detail})" if state.detail else "")],
            ["Outcome", _outcome(state)],
            ["Started", state.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")],
            ["Elapsed", profiles.format_duration(elapsed)],
        ]
    )
    click.echo("\nPlacement:")
    print_plan(state.plan, state.profile)
    pending = run_state.outstanding(state.id)
    if state.finalization == "incomplete" and pending:
        click.echo(f"\nCleanup pending: {len(pending)} item(s).")
        for entry in pending:
            click.echo(
                f"  - {entry.kind} {entry.name} on {entry.where}: {entry.action}"
            )
        click.echo('Log in again if needed, then run "cloud-stress run stop".')


def _outcome(state: run_state.RunState) -> str:
    if state.code is None:
        return "-"
    parts = [state.execution, f"exit {state.code}"]
    return ", ".join(part for part in parts if part)


if __name__ == "__main__":
    app()

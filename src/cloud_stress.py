"""Command-line interface for Prodstack infrastructure stress testing."""

import json
import logging
import socket
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import click

from campaign.lifecycle import access
from campaign.model import profiles
from campaign.model import state as run_state
from campaign.report.html import render_html
from campaign.report.text import summary_text
from probe.ceph_probe import probe_ceph
from probe.discover import (
    datapath_notices,
    discover_cloud,
    link_juju_to_hypervisors,
    nova_state_notices,
)
from probe.display import (
    print_check_result,
    print_fleet,
    print_notices,
    print_resources,
    print_table,
)
from probe.infra_probe import probe_infra
from probe.juju_probe import probe_juju
from probe.maas_probe import probe_maas
from probe.models import WORKLOADS, CephResult, CheckResult, ProbeNotice, Workload
from probe.openstack_probe import resolve_cloud
from probe.ovs_probe import probe_ovs
from probe.plan import resolve_plan, workload_readiness

DEFAULT_CONFIG = "/snap/cloud-stress/current/share/cloud-stress/config.yaml"
log = logging.getLogger(__name__)


def debug_logging(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    """Enable CLI and probe debug logs for this command, on stderr."""
    if not value or ctx.resilient_parsing:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("debug: %(message)s"))
    loggers = [logging.getLogger(name) for name in (__name__, "probe")]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

    def restore() -> None:
        for logger, level in zip(loggers, levels, strict=True):
            logger.removeHandler(handler)
            logger.setLevel(level)
        handler.close()

    ctx.call_on_close(restore)


def output_options[F: Callable[..., object]](func: F) -> F:
    """Independent controls for expanded output and debug logs."""
    for option in reversed(
        [
            click.option(
                "--full",
                is_flag=True,
                help="Show full inventory, fleet and result details.",
            ),
            click.option(
                "-v",
                "--verbose",
                is_flag=True,
                expose_value=False,
                callback=debug_logging,
                help="Print debug logs to stderr.",
            ),
        ]
    ):
        func = option(func)
    return func


def discover(
    juju_model: str,
    overrides: dict[str, str] | None = None,
    force: bool = False,
    config: profiles.RunConfig | None = None,
) -> CheckResult:
    """Run live, read-only discovery and resolve the run plan.

    With a configuration, the plan includes the fleet for its workloads.
    Force skips preflight queries while retaining resource discovery.
    """
    if not juju_model:
        raise click.UsageError("--juju-model must not be empty")
    with ThreadPoolExecutor(max_workers=3) as pool:
        juju_future = pool.submit(probe_juju, juju_model)
        cloud, openstack, openstack_notices = discover_cloud()
        juju, juju_notices = juju_future.result()
        scoped_ids = {
            machine.instance_id for machine in juju.machines if machine.instance_id
        }
        maas, maas_notices = probe_maas(juju_model, scoped_ids)
        link_notices = link_juju_to_hypervisors(juju, maas, openstack.hosts)
        infra_future = pool.submit(probe_infra, juju_model, juju, openstack.hosts)
        ceph_future = None if force else pool.submit(probe_ceph, juju_model, juju)
        ovs_future = pool.submit(probe_ovs, juju_model, juju)
        infra, infra_notices = infra_future.result()
        if ceph_future:
            ceph_result, ceph_notices = ceph_future.result()
        else:
            ceph_result = CephResult()
            ceph_notices = [
                ProbeNotice(
                    source="ceph",
                    reason="not-queried",
                    message="Preflight checks skipped (--force).",
                )
            ]
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
    missing: dict[Workload, dict[str, str]] = {}
    if config:
        enabled = set(config.workloads.enabled)
        result.plan, found = resolve_plan(result, overrides, enabled, config)
        missing = {name: found for name in WORKLOADS if name in enabled}
    else:
        result.plan, _ = resolve_plan(result, overrides, set(WORKLOADS))
        for name in WORKLOADS:
            missing[name] = resolve_plan(result, overrides, {name})[1]
    result.readiness = workload_readiness(result, missing, force)
    result.notices[:0] = nova_state_notices(result)
    result.notices.extend(datapath_notices(result))
    return result


@click.group(
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"], "show_default": True},
)
def app() -> None:
    """Run TRex network and fio storage workloads on a Prodstack cloud."""


@app.command()
@click.option(
    "--juju-model", default="openstack", help="Read membership from this Juju model."
)
@output_options
@click.option("--json", "as_json", is_flag=True, help="Print live results as JSON.")
def check(
    juju_model: str,
    full: bool,
    as_json: bool,
) -> None:
    """Check cloud access, infrastructure and workload readiness."""
    result = discover(juju_model)
    print_check_result(result, as_json, full)
    if not result.openstack.hosts:
        click.echo("No compute hosts were discovered", err=True)
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
    log.debug("Worker: %s request via %s", payload.get("op"), run_state.SOCKET)
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


def follow(
    run_id: str, after_attempt: int = 0, full: bool = False, clean: bool = False
) -> None:
    """Print progress until a finalize attempt after `after_attempt` ends.

    With clean, a result that still retains resources is not the end. Ctrl+C
    only detaches; the run continues in the worker.
    """
    shown = None
    try:
        while True:
            state = run_state.load(run_id)
            ended = state.code is not None and state.finalize_attempts > after_attempt
            if ended and not (clean and state.finalization == "retained"):
                break
            line = state.detail or state.phase
            if line != shown and state.phase != "accepted":
                click.echo(f"{datetime.now(UTC):%H:%M:%S}  {line}")
                shown = line
            time.sleep(2)
    except KeyboardInterrupt:
        click.echo(
            f"\nRun continues.\n"
            f"Status: cloud-stress run status {run_id}\n"
            f"Stop:   cloud-stress run stop {run_id}",
            err=True,
        )
        raise SystemExit(130) from None
    click.echo()
    show_result(state, full)
    raise SystemExit(state.code or 0)


def show_result(state: run_state.RunState, full: bool = False) -> None:
    """Print a run's summary, or its outcome when no result was written."""
    result_file = run_state.run_dir(state.id) / "report.json"
    if result_file.is_file():
        click.echo(summary_text(json.loads(result_file.read_text()), full), nl=False)
    else:
        click.echo(f"Run {state.id}")
        print_table(
            [
                ["Status", f"{state.execution or state.phase} (exit {state.code})"],
                ["Cleanup", state.finalization or "pending"],
            ]
        )
        for cause in state.causes:
            click.echo(f"  {cause}")
        if state.finalization == "incomplete":
            click.echo(f"Retry: cloud-stress run stop {state.id}")
    if state.finalization == "retained":
        click.echo(
            f"Resources retained. Clean up: cloud-stress run stop --clean {state.id}"
        )
    click.echo(f"Results: {run_state.run_dir(state.id)}")


@run.command(
    help="Start a stress run from a configuration file.\n\n"
    f"Copy the example configuration from {DEFAULT_CONFIG} and edit it."
)
@click.option(
    "--config",
    "config_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Run configuration (YAML).",
)
@click.option(
    "--juju-model", default="openstack", help="Read membership from this Juju model."
)
@output_options
@click.option(
    "--provider-network",
    "network",
    help="Neutron VLAN provider network name or ID for TRex traffic.",
)
@click.option(
    "--ip-version",
    type=click.Choice(["4", "6"]),
    help="IP version on the provider network; default 6 when it has an IPv6 subnet.",
)
@click.option(
    "--network",
    "mgmt_network",
    help="IPv4 overlay network for guest management (SSH, DHCP and DNS).",
)
@click.option("--image", help="Glance image name or ID for the guests.")
@click.option("--flavor", help="Nova flavor name or ID for the guests.")
@click.option("--volume-type", help="Cinder volume type name or ID.")
@click.option("--force", is_flag=True, help="Skip preflight checks.")
@click.option(
    "--retain",
    is_flag=True,
    help="Keep the VMs and run-owned resources for inspection; remove them with "
    '"cloud-stress run stop --clean".',
)
def start(
    config_file: Path,
    juju_model: str,
    full: bool,
    force: bool,
    retain: bool,
    **choices: str | None,
) -> None:
    """Start a stress run from a configuration file."""
    try:
        config = profiles.load_config(config_file.read_text())
        schedule = profiles.resolve_schedule(config)
    except (OSError, ValueError) as error:
        raise click.UsageError(f"{config_file}: {error}") from None
    if pending := run_state.unfinished():
        raise click.UsageError(
            f"Run {pending.id} is not finished; "
            'see "cloud-stress run status" or run "cloud-stress run stop".'
        )
    overrides = {key: value for key, value in choices.items() if value}
    result = discover(
        juju_model,
        overrides,
        force=force,
        config=config,
    )
    blockers = [b for item in result.readiness for b in item.blockers]
    if blockers:
        click.echo("Cannot start:", err=True)
        for blocker in dict.fromkeys(blockers):
            click.echo(f"  {blocker}", err=True)
        if full:
            print_notices(result.notices, full=True)
        raise SystemExit(2)
    click.echo(f"Run plan ({juju_model})\n\nResources:")
    print_resources(result.plan, full)
    click.echo("\nFleet:")
    print_fleet(result.plan, full)
    click.echo("\nSchedule:")
    print_table(schedule_rows(config, schedule, full))
    if full:
        print_notices(result.notices, full=True)
    click.echo()
    reply = request(
        {
            "op": "start",
            "config": config.model_dump(),
            "model": juju_model,
            "cloud": result.cloud,
            "plan": result.plan.model_dump(),
            "access": operator_access(result.cloud).model_dump(),
            "retain": retain,
        }
    )
    click.echo(f"Run {reply['run_id']} accepted.")
    follow(str(reply["run_id"]), full=full)


def schedule_rows(
    config: profiles.RunConfig, schedule: profiles.Schedule, full: bool = False
) -> list[list[str]]:
    """Warm-up and each workload's profile windows on the measured timeline."""
    fmt = profiles.format_duration
    rows = [
        ["Warm-up", f"{fmt(schedule.warmup_s)} before measurement"],
        ["Measured", fmt(schedule.duration_s)],
    ]
    trex_config, fio_config = config.workloads.trex, config.workloads.fio
    for window in schedule.trex if trex_config else []:
        pps = trex_config.profiles[window.profile].pps_per_sender if trex_config else 0
        rows.append(
            [
                "TRex" if window.profile == 0 else "",
                f"{fmt(window.start_s)}-{fmt(window.start_s + window.length_s)}  "
                f"{pps:,} pps per sender",
            ]
        )
    for window in schedule.fio if fio_config else []:
        name = fio_config.profiles[window.profile].name if fio_config else ""
        end = fmt(window.start_s + window.length_s)
        rows.append(
            [
                "fio" if window.profile == 0 else "",
                f"{fmt(window.start_s)}-{end}  {name}",
            ]
        )
    if full:
        rows.append(
            ["Timeout", f"{fmt(schedule.max_elapsed_s)} including setup and cleanup"]
        )
    return rows


@run.command()
@click.argument("run_id", required=False)
@output_options
@click.option(
    "--abandon-evidence",
    is_flag=True,
    help="Clean up even when evidence on an unreachable host or guest is lost.",
)
@click.option(
    "--clean",
    is_flag=True,
    help="Delete resources kept by --retain, also after the run has ended.",
)
def stop(run_id: str | None, abandon_evidence: bool, clean: bool, full: bool) -> None:
    """Stop the active run, or finish cleanup of an interrupted or retained one."""
    pending = run_state.unfinished()
    if pending is None and clean:
        try:
            pending = run_state.load(run_id) if run_id else run_state.retained()
        except FileNotFoundError:
            raise click.UsageError(f"No run {run_id}") from None
        if pending is not None and pending.finalization != "retained":
            click.echo(f"Run {pending.id} has no retained resources.")
            return
    if pending is None:
        raise click.UsageError(
            "No active, interrupted or retained run."
            if clean
            else "No active or interrupted run."
        )
    payload: dict[str, object] = {
        "op": "stop",
        "run_id": run_id,
        "abandon_evidence": abandon_evidence,
        "clean": clean,
    }
    reply = request(payload, check=False)
    if reply.get("needs_access"):
        payload["access"] = operator_access(pending.cloud).model_dump()
        reply = request(payload)
    elif not reply.get("ok"):
        click.echo(f"Error: {reply.get('error')}", err=True)
        raise SystemExit(2)
    click.echo(f"Stopping run {reply['run_id']}.")
    follow(str(reply["run_id"]), pending.finalize_attempts, full, clean)


@run.command()
@click.argument("run_id", required=False)
@output_options
@click.option("--json", "as_json", is_flag=True, help="Print the run result as JSON.")
@click.option(
    "--html",
    "html_file",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write the run's HTML report to this file.",
)
def status(
    run_id: str | None, as_json: bool, html_file: Path | None, full: bool
) -> None:
    """Show the active run, or a retained run (the latest by default)."""
    if html_file and str(html_file.resolve()).startswith(("/tmp/", "/var/tmp/")):
        raise click.UsageError(
            "The snap has its own /tmp; write the report to your home directory."
        )
    log.debug("Run: reading %s", run_id or "latest state")
    try:
        state = run_state.load(run_id) if run_id else next(iter(run_state.runs()), None)
    except FileNotFoundError:
        raise click.UsageError(f"No run {run_id}") from None
    if state is None:
        click.echo("No runs.")
        return
    bundle = run_state.run_dir(state.id)
    if html_file:
        report = render_html(state.id)
        if report is None:
            raise click.UsageError("This run has no report yet.")
        html_file.write_text(report)
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
    if state.code is not None or state.phase == "finished":
        show_result(state, full)
        return
    end = datetime.now(UTC)
    elapsed = int((end - state.created_at).total_seconds())
    rows = [
        ["Run", state.id],
        ["Status", state.detail or state.phase],
        ["Workloads", ", ".join(state.config.workloads.enabled)],
        ["VMs", f"{len(state.targets)}/{len(state.plan.vms)} created"],
        ["Planned duration", profiles.format_duration(state.duration_s)],
        ["Elapsed", profiles.format_duration(elapsed)],
    ]
    if full:
        rows.append(["Started", state.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")])
    print_table(rows)
    if full:
        click.echo("\nPlanned fleet:")
        print_fleet(state.plan)
    pending = run_state.outstanding(state.id)
    if state.finalization == "incomplete" and pending:
        click.echo(f"\nCleanup pending: {len(pending)} item(s).")
        for entry in pending:
            click.echo(
                f"  - {entry.kind} {entry.name} on {entry.where}: {entry.action}"
            )
        click.echo(f"Retry: cloud-stress run stop {state.id}")


if __name__ == "__main__":
    app()

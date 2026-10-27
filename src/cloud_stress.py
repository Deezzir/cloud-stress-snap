"""Command-line interface for Prodstack infrastructure stress testing."""

import logging
from concurrent.futures import ThreadPoolExecutor

import click

from helpers import (
    datapath_notices,
    discover_cloud,
    link_juju_to_hypervisors,
    nova_state_notices,
    print_check_result,
    profile_readiness,
    resolve_plan,
)
from probe.ceph_probe import probe_ceph
from probe.infra_probe import probe_infra
from probe.juju_probe import probe_juju
from probe.maas_probe import probe_maas
from probe.models import CheckResult
from probe.ovs_probe import probe_ovs

_COMMANDS = {
    "run": (
        "Run a stress campaign with a selected profile and duration.",
        "Stress execution is not yet available.",
    ),
    "report": (
        "Generate a stress-test report from retained campaign evidence.",
        "Report generation is not yet available.",
    ),
    "dashboard": (
        "View stress-test metrics and workload impact in Grafana.",
        "Dashboard generation is not yet available.",
    ),
}


@click.group(no_args_is_help=True)
def app() -> None:
    """Stress test Prodstack network, storage, and compute infrastructure."""


@app.command()
@click.option("--cloud", help="Select a named cloud in clouds.yaml.")
@click.option("--juju-model", help="Read membership from this Juju model.")
@click.option("--maas-url", help="Read MAAS records for scoped Juju machines.")
@click.option(
    "--maas-token",
    envvar="MAAS_TOKEN",
    help="MAAS API key (consumer:token:secret); defaults to $MAAS_TOKEN.",
)
@click.option("--json", "as_json", is_flag=True, help="Print live results as JSON.")
@click.option(
    "-v", "--verbose", is_flag=True, help="Print diagnostic details to stderr."
)
def check(
    cloud: str | None,
    juju_model: str | None,
    maas_url: str | None,
    maas_token: str | None,
    as_json: bool,
    verbose: bool,
) -> None:
    """Check access and print live, read-only infrastructure observations."""
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
    result.plan, missing = resolve_plan(result)
    result.readiness = profile_readiness(result, missing)
    result.notices[:0] = nova_state_notices(result)
    result.notices.extend(datapath_notices(result))
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


@click.pass_context
def unavailable(ctx: click.Context) -> None:
    """Reject an unavailable operation without accessing infrastructure or state."""
    assert ctx.command.name is not None
    message = _COMMANDS[ctx.command.name][1]
    click.echo(
        f"Unsupported feature: {message} No operation performed.",
        err=True,
    )
    ctx.exit(2)


for _name, (_help, _) in _COMMANDS.items():
    app.add_command(
        click.Command(
            _name,
            callback=unavailable,
            help=_help,
            context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
        )
    )


if __name__ == "__main__":
    app()

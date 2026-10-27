"""Command-line interface for Prodstack infrastructure stress testing."""

import click

_COMMANDS = {
    "connect": (
        "Configure access to the Prodstack deployment under test.",
        "Connection setup is not implemented.",
    ),
    "inspect": (
        "Inspect infrastructure topology and stress-workload capabilities.",
        "Discovery inspection is not implemented.",
    ),
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

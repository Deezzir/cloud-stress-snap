"""Read Juju membership without switching models or changing resources."""

import logging

import jubilant

from probe.models import JujuApp, JujuMachine, JujuResult, JujuUnit, ProbeNotice

log = logging.getLogger(__name__)


def probe_juju(
    model: str | None,
) -> tuple[JujuResult, list[ProbeNotice]]:
    """Discover Juju machines, and units for the specified model."""
    juju: JujuResult = JujuResult(model=model, machines=[], units=[])
    if model is None:
        return juju, [
            ProbeNotice(
                source="juju",
                reason="not-queried",
                message="Juju membership was not requested; add --juju-model "
                "to inspect a model.",
            )
        ]
    try:
        log.debug("Juju: reading status of model %r", model)
        status = jubilant.Juju(model=model).status()
    except FileNotFoundError:
        return juju, [
            ProbeNotice(
                source="juju",
                reason="missing-tool",
                message="The juju CLI is missing. Install it to read Juju membership.",
            )
        ]
    except jubilant.CLIError as error:
        log.debug("juju status failed: %s", (error.stderr or "").strip())
        return juju, [
            ProbeNotice(
                source="juju",
                reason="unavailable",
                message="Could not read the selected Juju model. Verify its name, "
                "Juju authentication, and controller access.",
            )
        ]
    except (KeyError, TypeError, ValueError, AttributeError):
        return juju, [
            ProbeNotice(
                source="juju",
                reason="unsupported-version",
                message="Juju status has an unsupported format. "
                "Check Juju and Jubilant version compatibility.",
            )
        ]
    juju.machines = [
        JujuMachine(
            id=key,
            parent_id=None,
            instance_id=machine.instance_id or None,
            hostname=machine.hostname or None,
            ip_addresses=machine.ip_addresses[:64],
        )
        for key, machine in status.machines.items()
    ]
    for parent, machine in status.machines.items():
        for container in machine.containers:
            juju.machines.append(JujuMachine(id=container, parent_id=parent))
    juju.units = [
        JujuUnit(
            name=name,
            machine_id=unit.machine or None,
            parent_machine_id=unit.machine.split("/")[0] if unit.machine else None,
        )
        for app in status.apps.values()
        for name, unit in app.units.items()
    ]
    juju.units.extend(
        JujuUnit(
            name=name,
            machine_id=unit.machine or None,
            parent_machine_id=unit.machine.split("/")[0] if unit.machine else None,
            principal=principal,
        )
        for app in status.apps.values()
        for principal, unit in app.units.items()
        for name in unit.subordinates
    )
    juju.apps = [
        JujuApp(
            name=name,
            charm=app.charm_name,
            version=app.version or None,
            bindings=dict(app.endpoint_bindings),
        )
        for name, app in status.apps.items()
    ]
    return juju, []

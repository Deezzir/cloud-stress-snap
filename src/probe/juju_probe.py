"""Read Juju membership without switching models or changing resources."""

import logging
from collections.abc import Callable

import jubilant

from probe.models import JujuApp, JujuMachine, JujuResult, JujuUnit, ProbeNotice

log = logging.getLogger(__name__)


def juju_read[T](
    source: str,
    description: str,
    notices: list[ProbeNotice],
    read: Callable[[], T],
) -> T | None:
    """Run one Juju read and turn failures into probe notices."""
    try:
        return read()
    except FileNotFoundError:
        notices.append(
            ProbeNotice(
                source=source,
                reason="missing-tool",
                message="The juju CLI is missing. Install it to read Ceph and OVS.",
            )
        )
    except (jubilant.CLIError, jubilant.TaskError) as error:
        detail = str(getattr(error, "stderr", "") or error)
        log.debug("%s failed: %s", description, " ".join(detail.split())[:300])
        denied = "permission denied" in detail.lower()
        notices.append(
            ProbeNotice(
                source=source,
                reason="permission-denied" if denied else "unavailable",
                message=f"{description}: "
                + (
                    "access denied. Check the Juju user's access to the model."
                    if denied
                    else "failed. Check the unit and its charm revision."
                ),
            )
        )
    except TimeoutError:
        notices.append(
            ProbeNotice(
                source=source,
                reason="transport-unreachable",
                message=f"{description}: timed out on the Juju unit.",
            )
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        notices.append(
            ProbeNotice(
                source=source,
                reason="unsupported-version",
                message=f"{description}: unsupported output format.",
            )
        )
    return None


def probe_juju(model: str) -> tuple[JujuResult, list[ProbeNotice]]:
    """Discover Juju machines, and units for the specified model."""
    juju = JujuResult(model=model, machines=[], units=[])
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
        stderr = (error.stderr or "").strip()
        log.debug("juju status failed: %s", stderr)
        detail = stderr.splitlines()[-1].removeprefix("ERROR ") if stderr else ""
        return juju, [
            ProbeNotice(
                source="juju",
                reason="unavailable",
                message=f"Could not read Juju model {model}"
                + (f": {detail}" if detail else "")
                + ". Verify its name, Juju authentication, and controller access.",
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

"""Read OVS bridge datapath types and hw-offload through ovn-chassis units.

Only the read-only ovs-vsctl commands below are run.
"""

import json
import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import jubilant

from probe.ceph_probe import juju_read
from probe.models import JujuResult, OvsHost, ProbeNotice

EXEC_TIMEOUT = 180
CHASSIS_CHARMS = ("ovn-chassis", "ovn-dedicated-chassis")
BRIDGES = ("--format=json", "--columns=name,datapath_type", "list", "Bridge")
HW_OFFLOAD = ("--if-exists", "get", "Open_vSwitch", ".", "other_config:hw-offload")

log = logging.getLogger(__name__)


def _bridges(stdout: str) -> dict[str, str | None]:
    table = json.loads(stdout)
    rows = [dict(zip(table["headings"], row, strict=True)) for row in table["data"]]
    return {str(row["name"]): str(row["datapath_type"]) or "system" for row in rows}


def _hw_offload(stdout: str) -> bool:
    return stdout.strip().strip('"').lower() == "true"


def _physnets(config: Mapping[str, Any]) -> dict[str, str]:
    """Map each provider physnet to its host interface via the bridge name."""
    pairs = [
        item.split(":", 1)
        for key in ("ovn-bridge-mappings", "bridge-interface-mappings")
        for item in str(config.get(key) or "").split()
        if ":" in item
    ]
    bridges = {bridge: iface for bridge, iface in pairs if bridge.startswith("br-")}
    return {
        physnet: bridges[bridge]
        for physnet, bridge in pairs
        if not physnet.startswith("br-") and bridge in bridges
    }


def probe_ovs(
    model: str | None, juju: JujuResult
) -> tuple[list[OvsHost], list[ProbeNotice]]:
    """Read OVS state on each hypervisor that has an ovn-chassis unit."""
    chassis = {app.name for app in juju.apps if app.charm in CHASSIS_CHARMS}
    hypervisors = {
        machine.id: machine.hypervisor_id
        for machine in juju.machines
        if machine.hypervisor_id
    }
    targets = [
        OvsHost(host_id=hypervisors[unit.parent_machine_id], unit=unit.name)
        for unit in juju.units
        if unit.name.split("/")[0] in chassis
        and unit.parent_machine_id in hypervisors
        and unit.machine_id == unit.parent_machine_id
    ]
    if model is None or not targets:
        return [], [
            ProbeNotice(
                source="ovs",
                reason="not-queried",
                message="OVS was not inspected; add --juju-model for a model "
                "with ovn-chassis on the hypervisors."
                if model is None
                else "OVS was not inspected; no ovn-chassis unit is on a "
                "verified hypervisor.",
            )
        ]
    client = jubilant.Juju(model=model)

    def ovs_vsctl(unit: str, args: tuple[str, ...]) -> str:
        log.debug("Juju: exec on %s: ovs-vsctl %s", unit, " ".join(args))
        return client.exec("ovs-vsctl", *args, unit=unit, wait=EXEC_TIMEOUT).stdout

    def read(ovs: OvsHost) -> list[ProbeNotice]:
        notices: list[ProbeNotice] = []
        source, description = f"ovs:{ovs.host_id}", f"OVS on {ovs.unit}"
        ovs.bridges = (
            juju_read(
                source,
                description,
                notices,
                lambda: _bridges(ovs_vsctl(ovs.unit, BRIDGES)),
            )
            or {}
        )
        if not ovs.bridges:
            return notices
        ovs.hw_offload = juju_read(
            source,
            description,
            notices,
            lambda: _hw_offload(ovs_vsctl(ovs.unit, HW_OFFLOAD)),
        )
        return notices

    notices: list[ProbeNotice] = []
    for app in chassis:
        config = juju_read(
            f"juju.config:{app}",
            f"Juju config of {app}",
            notices,
            partial(client.config, app),
        )
        for ovs in targets:
            if config and ovs.unit.split("/")[0] == app:
                ovs.physnets = _physnets(config)
    with ThreadPoolExecutor(max_workers=8) as pool:
        notices += [n for found in pool.map(read, targets) for n in found]
    return targets, notices

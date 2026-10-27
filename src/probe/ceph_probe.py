"""Read Ceph health and topology from the ceph-mon leader through Juju.

Only the allowlisted read-only charm actions and ceph commands below are run.
"""

import json
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import jubilant

from probe.models import (
    CephCrushRule,
    CephOsd,
    CephPool,
    CephResult,
    JujuResult,
    ProbeNotice,
)

ACTION_TIMEOUT = 120
# Pool names configured on the Ceph client charms; unset means the app name.
POOL_CONFIG = {
    "cinder-ceph": "rbd-pool-name",
    "glance": "rbd-pool-name",
    "nova-compute": "rbd-pool",
}

log = logging.getLogger(__name__)


def _version(text: str) -> str:
    match = re.search(r"ceph version (\S+)", text)
    return match[1] if match else text[:64]


def juju_read(
    source: str,
    description: str,
    notices: list[ProbeNotice],
    read: Callable[[], Any],
) -> Any:
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


def _versions(stdout: str) -> dict[str, list[str]]:
    versions = json.loads(stdout)
    return {
        daemon: sorted({_version(text) for text in versions.get(daemon) or {}})
        for daemon in ("mon", "mgr", "osd")
        if versions.get(daemon)
    }


def _status(result: CephResult, stdout: str) -> None:
    status = json.loads(stdout)
    health = status["health"]
    result.health = str(health["status"])
    result.health_checks = [
        f"{name}: {check['summary']['message']}"
        for name, check in (health.get("checks") or {}).items()
    ]
    result.pg_states = {
        str(state["state_name"]): int(state["count"])
        for state in status["pgmap"].get("pgs_by_state") or []
    }
    result.quorum = [str(name) for name in status.get("quorum_names") or []]


def _osds(stdout: str) -> list[CephOsd]:
    nodes = json.loads(stdout)["nodes"]
    hosts = {
        child: str(node["name"])
        for node in nodes
        if node.get("type") == "host"
        for child in node.get("children") or []
    }
    return [
        CephOsd(
            id=int(node["id"]),
            host=hosts.get(node["id"]),
            device_class=node.get("device_class"),
            size_bytes=int(node["kb"]) * 1024 if node.get("kb") is not None else None,
            used_percent=node.get("utilization"),
            pgs=node.get("pgs"),
            status=node.get("status"),
        )
        for node in nodes
        if node.get("type") == "osd"
    ]


def _pools(stdout: str) -> list[CephPool]:
    return [
        CephPool(
            id=int(pool["pool"]),
            name=str(pool["pool_name"]),
            type="erasure" if pool["type"] == 3 else "replicated",
            size=int(pool["size"]),
            min_size=int(pool["min_size"]),
            pg_num=int(pool["pg_num"]),
            crush_rule=int(pool["crush_rule"]),
            erasure_profile=pool.get("erasure_code_profile") or None,
            applications=sorted(pool.get("application_metadata") or {}),
        )
        for pool in json.loads(stdout)
    ]


def _crush_rules(stdout: str) -> list[CephCrushRule]:
    rules = []
    for rule in json.loads(stdout):
        steps = rule.get("steps") or []
        take = next((s["item_name"] for s in steps if s.get("op") == "take"), "")
        domain = next(
            (s.get("type") for s in steps if str(s.get("op", "")).startswith("choose")),
            None,
        )
        rules.append(
            CephCrushRule(
                id=int(rule["rule_id"]),
                name=str(rule["rule_name"]),
                failure_domain=domain,
                device_class=take.split("~")[1] if "~" in take else None,
            )
        )
    return rules


def _health_notices(result: CephResult) -> list[ProbeNotice]:
    notices = []
    if result.health and result.health != "HEALTH_OK":
        checks = "; ".join(result.health_checks[:5]) or "no details"
        notices.append(
            ProbeNotice(
                source="ceph.health",
                reason="unavailable",
                message=f"Ceph reports {result.health} ({checks}).",
            )
        )
    unclean = sum(
        count for state, count in result.pg_states.items() if state != "active+clean"
    )
    if unclean:
        total = sum(result.pg_states.values())
        notices.append(
            ProbeNotice(
                source="ceph.pgs",
                reason="unavailable",
                message=f"{unclean} of {total} Ceph placement groups are not "
                "active+clean; wait for recovery before stress testing.",
            )
        )
    found = {version for versions in result.versions.values() for version in versions}
    if len(found) > 1:
        detail = ", ".join(
            f"{daemon} {'/'.join(versions)}"
            for daemon, versions in result.versions.items()
        )
        notices.append(
            ProbeNotice(
                source="ceph.versions",
                reason="unsupported-version",
                message=f"Ceph daemons run mixed versions ({detail}). Mons must "
                "not be older than OSDs.",
            )
        )
    return notices


def probe_ceph(
    model: str | None, juju: JujuResult
) -> tuple[CephResult, list[ProbeNotice]]:
    """Read Ceph state with read-only ceph-mon actions and ceph commands."""
    apps = {app.charm: app for app in juju.apps}
    mon = apps.get("ceph-mon")
    if model is None or mon is None:
        return CephResult(), [
            ProbeNotice(
                source="ceph",
                reason="not-queried",
                message="Ceph was not inspected; add --juju-model for a model "
                "with ceph-mon."
                if model is None
                else "Ceph was not inspected; the Juju model has no ceph-mon.",
            )
        ]
    osd = apps.get("ceph-osd")
    bindings = osd.bindings if osd else mon.bindings
    result = CephResult(
        public_space=bindings.get("public") or None,
        cluster_space=(osd.bindings.get("cluster") or None) if osd else None,
    )
    notices: list[ProbeNotice] = []
    client = jubilant.Juju(model=model)
    unit = f"{mon.name}/leader"

    def ceph(*args: str) -> str:
        log.debug("Juju: exec on %s: ceph %s", unit, " ".join(args))
        return client.exec("ceph", *args, unit=unit, wait=ACTION_TIMEOUT).stdout

    def action(name: str) -> str:
        log.debug("Juju: run %s %s", unit, name)
        task = client.run(unit, name, {"format": "json"}, wait=ACTION_TIMEOUT)
        return str(task.results["message"])

    clients = [app for app in juju.apps if app.charm in POOL_CONFIG]
    with ThreadPoolExecutor(max_workers=8) as pool:

        def submit(source: str, description: str, read: Callable[[], Any]) -> Any:
            found: list[ProbeNotice] = []
            future = pool.submit(juju_read, source, description, found, read)
            return future, found

        reads = [
            submit("ceph.status", "Ceph status", partial(ceph, "status", "-f", "json")),
            submit(
                "ceph.versions",
                "Ceph versions",
                partial(ceph, "versions", "-f", "json"),
            ),
            submit(
                "ceph.osds",
                "Ceph OSD tree (show-disk-free)",
                lambda: _osds(action("show-disk-free")),
            ),
            submit(
                "ceph.pools",
                "Ceph pools (list-pools)",
                lambda: _pools(action("list-pools")),
            ),
            submit(
                "ceph.crush",
                "CRUSH rules (list-crush-rules)",
                lambda: _crush_rules(action("list-crush-rules")),
            ),
            *(
                submit(
                    f"juju.config:{app.name}",
                    f"Juju config of {app.name}",
                    partial(client.config, app.name),
                )
                for app in clients
            ),
        ]
        values = [future.result() for future, _ in reads]
    for _, found in reads:
        notices.extend(found)
    status, versions, osds, pools_found, rules, *configs = values
    if status:
        juju_read(
            "ceph.status", "Ceph status", notices, partial(_status, result, status)
        )
    if versions:
        result.versions = (
            juju_read(
                "ceph.versions",
                "Ceph versions",
                notices,
                partial(_versions, versions),
            )
            or {}
        )
    result.osds = osds or []
    result.pools = pools_found or []
    result.crush_rules = rules or []
    pools = {item.name: item for item in result.pools}
    for app, config in zip(clients, configs, strict=True):
        if config is None:
            continue
        key = POOL_CONFIG[app.charm]
        if app.charm == "nova-compute":
            if config.get("libvirt-image-backend") != "rbd":
                continue
            pool_name = str(config.get(key) or "nova")
        else:
            pool_name = str(config.get(key) or app.name)
        if pool_name in pools:
            pools[pool_name].used_by.append(app.name)
    return result, [*notices, *_health_notices(result)]

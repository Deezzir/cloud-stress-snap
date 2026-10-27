"""Read scoped existing MAAS records, never commissioning or modifying nodes."""

import json
import logging
import secrets
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from probe.models import MaasInterface, MaasMachine, MaasResult, ProbeNotice

MAX_RESPONSE_BYTES = 32 * 1024 * 1024

log = logging.getLogger(__name__)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _machines_request(url: str, token: str) -> Request:
    if urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("MAAS URL must use http or https")
    consumer_key, token_key, token_secret = token.split(":")
    base = url.rstrip("/")
    if not base.endswith("/api/2.0"):
        base += "/api/2.0"
    params = {
        "oauth_version": "1.0",
        "oauth_signature_method": "PLAINTEXT",
        "oauth_consumer_key": consumer_key,
        "oauth_token": token_key,
        "oauth_signature": f"&{token_secret}",
        "oauth_nonce": secrets.token_hex(16),
        "oauth_timestamp": str(int(time.time())),
    }
    header = ", ".join(f'{k}="{quote(v, safe="")}"' for k, v in params.items())
    return Request(
        f"{base}/machines/",
        headers={"Authorization": f"OAuth {header}", "Accept": "application/json"},
    )


def _interface(item: dict[str, Any]) -> MaasInterface:
    """Keep the interface fields needed to map traffic spaces to links."""
    vlan = item.get("vlan") or {}
    params = item.get("params")
    if not isinstance(params, dict):
        params = {}
    subnets = [link["subnet"] for link in item.get("links") or [] if link.get("subnet")]
    spaces = {str(s["space"]) for s in subnets if s.get("space")}
    if vlan.get("space"):
        spaces.add(str(vlan["space"]))
    spaces.discard("undefined")
    return MaasInterface(
        name=str(item["name"]),
        type=str(item.get("type") or "unknown"),
        parents=[str(parent) for parent in item.get("parents") or []],
        mtu=item.get("effective_mtu"),
        link_speed_mbps=item.get("link_speed") or None,
        bond_mode=params.get("bond_mode"),
        vlan_id=vlan.get("vid"),
        fabric=vlan.get("fabric"),
        spaces=sorted(spaces),
        subnets=sorted(str(s["cidr"]) for s in subnets if s.get("cidr")),
    )


def _interfaces(items: list[dict[str, Any]]) -> list[MaasInterface]:
    """Parse interfaces; bond members carry no spaces of their own."""
    interfaces = [_interface(item) for item in items]
    members = {
        parent
        for iface in interfaces
        if iface.type == "bond"
        for parent in iface.parents
    }
    for iface in interfaces:
        if iface.name in members:
            iface.spaces = []
    return interfaces


def probe_maas(
    url: str | None, token: str | None, selected_ids: set[str]
) -> tuple[MaasResult, list[ProbeNotice]]:
    """Select only machines whose system IDs occur in the chosen Juju model."""
    maas: MaasResult = MaasResult(machines=[])
    if url is None or token is None:
        return maas, [
            ProbeNotice(
                source="maas",
                reason="not-queried",
                message="MAAS was not queried; add --maas-url and --maas-token "
                "to match Juju machines to hypervisors.",
            )
        ]
    if not selected_ids:
        return maas, [
            ProbeNotice(
                source="maas",
                reason="unresolved-identity",
                message="No Juju machine instance IDs are available to scope MAAS. "
                "Select a readable --juju-model with machine instance IDs.",
            )
        ]
    try:
        request = _machines_request(url, token)
    except ValueError:
        return maas, [
            ProbeNotice(
                source="maas",
                reason="permission-denied",
                message="The MAAS URL or API key is malformed. Use an http(s) "
                "MAAS URL and a key of the form consumer:token:secret.",
            )
        ]
    try:
        log.debug("MAAS: GET %s", request.full_url)
        with build_opener(_NoRedirect).open(request, timeout=30) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        log.debug("MAAS: HTTP %s %s", error.code, error.reason)
        denied = error.code in (401, 403)
        return maas, [
            ProbeNotice(
                source="maas",
                reason="permission-denied" if denied else "unavailable",
                message="MAAS rejected the API key. Check the key and its read "
                "permissions."
                if denied
                else f"MAAS returned HTTP {error.code}. Check the MAAS URL.",
            )
        ]
    except (URLError, TimeoutError) as error:
        log.debug("MAAS: request failed: %s", getattr(error, "reason", error))
        return maas, [
            ProbeNotice(
                source="maas",
                reason="transport-unreachable",
                message="Cannot reach the MAAS API. Check the URL, connectivity, "
                "and TLS certificates.",
            )
        ]
    if len(body) > MAX_RESPONSE_BYTES:
        return maas, [
            ProbeNotice(
                source="maas",
                reason="unavailable",
                message="The MAAS response exceeds the 32 MiB limit. "
                "Review the API key's inventory scope.",
            )
        ]
    try:
        records = json.loads(body)
        if not isinstance(records, list) or len(records) > 1000:
            raise TypeError
        log.debug("MAAS: %d machines returned", len(records))
        maas.machines = [
            MaasMachine(
                system_id=machine["system_id"],
                hostname=machine.get("hostname"),
                architecture=machine.get("architecture"),
                cpu_count=machine.get("cpu_count"),
                memory_mb=machine.get("memory"),
                block_devices=[
                    device["name"]
                    for device in machine.get("blockdevice_set", [])
                    if device.get("name")
                ],
                ip_addresses=[str(ip) for ip in machine.get("ip_addresses") or []],
                interfaces=_interfaces(machine.get("interface_set") or []),
            )
            for machine in records
            if machine.get("system_id") in selected_ids
        ]
    except (ValueError, KeyError, TypeError, AttributeError):
        return maas, [
            ProbeNotice(
                source="maas",
                reason="unsupported-version",
                message="MAAS returned an unsupported machine inventory "
                "or exceeded the 1,000-record limit. Check its version and scope.",
            )
        ]
    if len(maas.machines) < len(selected_ids):
        return maas, [
            ProbeNotice(
                source="maas",
                reason="unresolved-identity",
                message="Some Juju machine instance IDs have no matching MAAS "
                "record. Verify the selected model, MAAS URL, and machine IDs.",
            )
        ]
    return maas, []

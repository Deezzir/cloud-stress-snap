"""Hand the operator's OpenStack and Juju access to the root worker.

`run` reads the invoking operator's resolved cloud and Juju client files and sends
them over the control socket. The worker writes them under the run's private
directory and uses only that copy, so it never reads another user's home.
"""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import openstack
import yaml
from openstack.config.cloud_region import CloudRegion
from openstack.connection import Connection
from pydantic import BaseModel, ConfigDict

MAX_FILE = 1024 * 1024
JUJU_FILES = ("controllers.yaml", "accounts.yaml", "models.yaml")
CLOUD_KEYS = (
    "auth_type",
    "auth",
    "region_name",
    "interface",
    "identity_api_version",
    "api_timeout",
)


class Access(BaseModel):
    """Credential material for one run; never logged or written to run exports."""

    model_config = ConfigDict(extra="forbid")

    cloud: dict[str, Any]
    cacert_pem: str | None = None
    juju_files: dict[str, str]


def juju_data() -> Path:
    """The invoking operator's Juju client directory."""
    if os.environ.get("JUJU_DATA"):
        return Path(os.environ["JUJU_DATA"])
    home = os.environ.get("SNAP_REAL_HOME") or os.path.expanduser("~")
    return Path(home) / ".local/share/juju"


def _read(path: Path) -> str:
    if path.stat().st_size > MAX_FILE:
        raise ValueError(f"{path.name} is too large")
    return path.read_text()


def collect(cloud: CloudRegion) -> Access:
    """Gather what the worker needs from the operator's resolved access."""
    config = {key: cloud.config[key] for key in CLOUD_KEYS if key in cloud.config}
    cacert = cloud.config.get("cacert")
    data = juju_data()
    files = {name: _read(data / name) for name in JUJU_FILES if (data / name).is_file()}
    cookies = data / "cookies"
    if cookies.is_dir():
        for path in cookies.glob("*.json"):
            files[f"cookies/{path.name}"] = _read(path)
    if "controllers.yaml" not in files:
        raise ValueError(f"no Juju client state in {data}; log in to Juju first")
    return Access(
        cloud=config,
        cacert_pem=_read(Path(cacert)) if cacert else None,
        juju_files=files,
    )


def login_problem(access: Access, model: str, until: datetime) -> str | None:
    """Why the selected controller's login may not last until `until`, if so."""
    controllers = yaml.safe_load(access.juju_files.get("controllers.yaml", "")) or {}
    controller = (
        model.split(":", 1)[0]
        if ":" in model
        else controllers.get("current-controller")
    )
    if not controller:
        return "no current Juju controller; pass --juju-model controller:model"
    accounts = yaml.safe_load(access.juju_files.get("accounts.yaml", "")) or {}
    account = (accounts.get("controllers") or {}).get(controller) or {}
    if account.get("password"):
        return None
    expiries = []
    for cookie in json.loads(access.juju_files.get(f"cookies/{controller}.json", "[]")):
        expires = cookie.get("Expires")
        if expires and not expires.startswith("0001"):
            expiries.append(datetime.fromisoformat(expires).astimezone(UTC))
    if not expiries:
        return f"the Juju login to {controller} has no verifiable expiry; log in again"
    if min(expiries) < until:
        return f"the Juju login to {controller} expires before the run could finish"
    return None


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(text)


def install(access: Access, directory: Path) -> None:
    """Replace the access copy in a run's private directory, root-only."""
    juju = directory / "juju"
    shutil.rmtree(juju, ignore_errors=True)
    (directory / "ca.pem").unlink(missing_ok=True)
    (juju / "cookies").mkdir(mode=0o700, parents=True)
    for name, text in access.juju_files.items():
        if name not in JUJU_FILES and not (
            name.startswith("cookies/") and "/" not in name[8:] and ".." not in name
        ):
            raise ValueError(f"unexpected Juju file {name}")
        _write_private(juju / name, text)
    _write_private(directory / "cloud.json", json.dumps(access.cloud))
    if access.cacert_pem:
        _write_private(directory / "ca.pem", access.cacert_pem)


def connect(directory: Path) -> Connection:
    """Connect to OpenStack with a run's access copy."""
    config = json.loads((directory / "cloud.json").read_text())
    cacert = directory / "ca.pem"
    return openstack.connect(
        cacert=str(cacert) if cacert.is_file() else None,
        load_yaml_config=False,
        load_envvars=False,
        **config,
    )


def use_juju(directory: Path) -> None:
    """Point this process's Juju client at a run's access copy."""
    os.environ["JUJU_DATA"] = str(directory / "juju")


def verify(directory: Path, model: str) -> list[str]:
    """Check that an access copy reaches OpenStack and the Juju model."""
    problems = []
    try:
        connect(directory).authorize()
    except Exception as error:  # noqa: BLE001 - any failure means no access
        problems.append(f"OpenStack access failed: {type(error).__name__}")
    result = subprocess.run(
        ["juju", "show-model", model, "--format", "json"],
        env={**os.environ, "JUJU_DATA": str(directory / "juju")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        detail = " ".join(result.stderr.split())[:200]
        problems.append(f"Juju access failed: {detail}; log in to Juju again")
    return problems

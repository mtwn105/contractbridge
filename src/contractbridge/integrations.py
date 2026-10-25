"""Business-independent registration, access keys, and declarative client targets."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path, PurePosixPath
from uuid import UUID

from .models import check_shape
from .storage import NotFound
from .util import strict_loads

CLIENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "modulePath", "bundlePath"],
    "properties": {
        "kind": {"const": "python-declarative"},
        "modulePath": {"type": "string", "maxLength": 240},
        "bundlePath": {"type": "string", "maxLength": 240},
        "importNamespace": {
            "type": "string",
            "pattern": r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*$",
        },
    },
}

APPLICATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["baseUrl", "credentialEnv"],
    "properties": {
        "baseUrl": {"type": "string", "maxLength": 300},
        "credentialEnv": {"type": "string", "pattern": r"^[A-Z][A-Z0-9_]{0,79}$"},
        "releasePath": {"type": "string", "maxLength": 200},
        "probePath": {"type": "string", "maxLength": 200},
        "environment": {"type": "string", "pattern": r"^[A-Za-z0-9_.-]{1,80}$"},
    },
}


def gateway_token(secret, identifier):
    if len(secret) < 24:
        raise ValueError("Configure GATEWAY_SIGNING_SECRET before issuing client credentials")
    identifier = str(UUID(identifier))
    signature = hmac.new(
        secret.encode(), ("contractbridge:gateway:" + identifier).encode(), hashlib.sha256
    ).hexdigest()
    return "cbg." + identifier + "." + signature


def gateway_authorized(secret, identifier, authorization):
    if len(secret) < 24:
        return False
    try:
        expected = ("Bearer " + gateway_token(secret, identifier)).encode()
        return hmac.compare_digest(authorization.encode(), expected)
    except ValueError, TypeError:
        return False


def validate_client(value):
    check_shape(CLIENT_SCHEMA, value)
    paths = [PurePosixPath(value[k]) for k in ("modulePath", "bundlePath")]
    if any(
        p.is_absolute() or ".." in p.parts or any(part.startswith(".") for part in p.parts) for p in paths
    ):
        raise ValueError("Client files must be regular repository-relative paths")
    if paths[0].suffix != ".py" or paths[1].suffix != ".json" or paths[0].parent != paths[1].parent:
        raise ValueError("The Python client module and JSON bundle must share a directory")
    return value


def validate_application(value):
    check_shape(APPLICATION_SCHEMA, value)
    for name, default in (("releasePath", "/release"), ("probePath", "/probe")):
        path = value.get(name, default)
        if (
            not path.startswith("/")
            or path.startswith("//")
            or any(part in path for part in ("..", "?", "#", "\\"))
        ):
            raise ValueError("Unsupported application verification path")
    return value


def baseline_client(root, client):
    validate_client(client)
    template = (root / "packages/adapter-templates/permanent_client.py").read_text()
    return template.replace(
        "from bridge_core.", "from " + client.get("importNamespace", "contractbridge") + "."
    ).replace('BUNDLE_NAME = "bundle.json"', "BUNDLE_NAME = None")


def load_integrations(monitor):
    if not monitor.settings.integrations_file:
        return
    config = Path(monitor.settings.integrations_file).resolve()
    catalog = (
        Path(monitor.settings.integrations_root).resolve()
        if monitor.settings.integrations_root
        else config.parent
    )
    if not catalog.is_dir():
        raise ValueError("INTEGRATIONS_ROOT must be an existing directory")
    try:
        config.relative_to(catalog)
    except ValueError as exc:
        raise ValueError("INTEGRATIONS_FILE must stay below INTEGRATIONS_ROOT") from exc
    value = strict_loads(config.read_bytes())
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("INTEGRATIONS_FILE must contain a bounded integration list")
    for entry in value:
        data = dict(entry)
        identifier = str(UUID(data.pop("id")))
        allowed = {
            "name",
            "upstreamBaseUrl",
            "specPath",
            "sourcePath",
            "targetPath",
            "sourceSpecFile",
            "fixturesFile",
            "fixtureSource",
            "autoRepair",
            "autoPublishDraft",
            "dualRepair",
            "repository",
            "baseBranch",
            "client",
            "application",
            "sentryProject",
        }
        if set(data) - allowed:
            raise ValueError("Integration registration contains unsupported fields")
        try:
            monitor.resources.get("connections", identifier)
            continue  # Registration never rewrites an existing operator policy.
        except NotFound:
            pass
        for key, output in [
            ("sourceSpecFile", "sourceSpec"),
            ("fixturesFile", "fixtures"),
        ]:
            if key in data:
                source = (config.parent / data.pop(key)).resolve()
                try:
                    source.relative_to(catalog)
                except ValueError as exc:
                    raise ValueError("Integration files must stay below the configuration directory") from exc
                if not source.is_file() or source.stat().st_size > 1_000_000:
                    raise ValueError("Integration file is missing or exceeds 1 MB")
                data[output] = (
                    source.read_text() if output == "sourceSpec" else strict_loads(source.read_bytes())
                )
        if data.get("autoPublishDraft") and not monitor.publisher_for(data).configured:
            data["autoPublishDraft"] = False
        monitor.create(data, identifier=identifier)

import copy
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from contractbridge.api import create_app
from contractbridge.config import Settings
from contractbridge.integrations import baseline_client, gateway_token, validate_client
from contractbridge.mitigation_policy import behavior, require_authorization
from contractbridge.storage import Conflict
from contractbridge.util import canonical, digest

ROOT = Path(__file__).resolve().parents[1]
OPERATOR = "generic-integration-operator-token-32-chars"
GATEWAY_SECRET = "generic-gateway-signing-secret-32-chars"


class UnusedRunner:
    pass


def contract(path, request_field, response_field):
    return {
        "openapi": "3.1.0",
        "info": {"title": "Independent provider", "version": "1"},
        "paths": {
            path: {
                "post": {
                    "operationId": path.strip("/").replace("/", "_"),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [request_field],
                                    "properties": {request_field: {"type": "string"}},
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "created",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": [response_field],
                                        "properties": {response_field: {"type": "string"}},
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }


async def test_connection_tokens_are_scoped_across_unrelated_integrations(tmp_path):
    origins = ("http://inventory-provider:8080", "http://identity-provider:8080")
    settings = Settings(
        ROOT,
        tmp_path / "artifacts",
        "sqlite:///" + str(tmp_path / "state.db"),
        OPERATOR,
        upstream_allowlist=origins,
        gateway_secret=GATEWAY_SECRET,
    )
    app = create_app(settings, diff=object(), planner=object(), runner=UnusedRunner())
    inventory = app.state.monitor.create(
        {
            "name": "Inventory reservation",
            "upstreamBaseUrl": origins[0],
            "sourceSpec": canonical(contract("/reserve", "sku", "reservation")),
            "fixtureSource": "synthetic",
        }
    )
    identity = app.state.monitor.create(
        {
            "name": "Identity enrollment",
            "upstreamBaseUrl": origins[1],
            "sourceSpec": canonical(contract("/enroll", "email", "identity")),
            "fixtureSource": "synthetic",
        }
    )
    calls = []

    def provider(request):
        calls.append((request.url.host, request.url.path))
        if request.url.host == "inventory-provider":
            return httpx.Response(201, json={"reservation": "r-1"})
        return httpx.Response(201, json={"identity": "i-1"})

    app.state.monitor.transport = httpx.MockTransport(provider)
    inventory_auth = {"Authorization": "Bearer " + gateway_token(GATEWAY_SECRET, inventory["id"])}
    identity_auth = {"Authorization": "Bearer " + gateway_token(GATEWAY_SECRET, identity["id"])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control") as client:
        assert (
            await client.post(
                f"/api/v1/gateway/{inventory['id']}/reserve",
                headers=inventory_auth,
                json={"sku": "book"},
            )
        ).status_code == 201
        assert (
            await client.post(
                f"/api/v1/gateway/{identity['id']}/enroll",
                headers=identity_auth,
                json={"email": "synthetic@example.test"},
            )
        ).status_code == 201
        assert (
            await client.post(
                f"/api/v1/gateway/{identity['id']}/enroll",
                headers=inventory_auth,
                json={"email": "synthetic@example.test"},
            )
        ).status_code == 401
        assert (await client.get("/api/v1/connections", headers=inventory_auth)).status_code == 401
    assert calls == [("inventory-provider", "/reserve"), ("identity-provider", "/enroll")]


def test_integration_catalog_and_referenced_files_cannot_escape_integrations_root(tmp_path):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("[]")
    settings = Settings(
        ROOT,
        tmp_path / "artifacts-outside",
        "sqlite:///" + str(tmp_path / "outside.db"),
        OPERATOR,
        integrations_file=str(outside),
        integrations_root=str(catalog),
    )
    with pytest.raises(ValueError, match="INTEGRATIONS_FILE must stay below INTEGRATIONS_ROOT"):
        create_app(settings, diff=object(), planner=object(), runner=UnusedRunner())

    escaped_spec = tmp_path / "private-openapi.json"
    escaped_spec.write_text(canonical(contract("/private", "input", "output")))
    config = catalog / "integrations.json"
    config.write_text(
        canonical(
            [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "name": "Traversal attempt",
                    "upstreamBaseUrl": "http://provider:8080",
                    "sourceSpecFile": "../private-openapi.json",
                    "fixtureSource": "synthetic",
                }
            ]
        )
    )
    settings = Settings(
        ROOT,
        tmp_path / "artifacts-reference",
        "sqlite:///" + str(tmp_path / "reference.db"),
        OPERATOR,
        upstream_allowlist=("http://provider:8080",),
        integrations_file=str(config),
        integrations_root=str(catalog),
    )
    with pytest.raises(ValueError, match="Integration files must stay below"):
        create_app(settings, diff=object(), planner=object(), runner=UnusedRunner())


async def test_dual_repair_dispatch_is_enabled_per_connection(tmp_path, monkeypatch):
    settings = Settings(
        ROOT,
        tmp_path / "dispatch-artifacts",
        "sqlite:///" + str(tmp_path / "dispatch.db"),
        OPERATOR,
        upstream_allowlist=("http://provider:8080",),
        dual_repair=True,
        temporal_address="temporal:7233",
    )
    app = create_app(settings, diff=object(), planner=object(), runner=UnusedRunner())
    durable = app.state.monitor.create(
        {
            "name": "Durable dual repair",
            "upstreamBaseUrl": "http://provider:8080",
            "sourceSpec": canonical(contract("/durable", "input", "output")),
            "fixtureSource": "synthetic",
            "dualRepair": True,
        }
    )
    classic = app.state.monitor.create(
        {
            "name": "Classic repair",
            "upstreamBaseUrl": "http://provider:8080",
            "sourceSpec": canonical(contract("/classic", "input", "output")),
            "fixtureSource": "synthetic",
            "dualRepair": False,
        }
    )
    durable_incident, _ = app.state.monitor.record_failure(durable["id"], "CONTRACT_MISMATCH", 404)
    classic_incident, _ = app.state.monitor.record_failure(classic["id"], "CONTRACT_MISMATCH", 404)
    started, investigated = [], []

    async def start_repair(_settings, identifier):
        started.append(identifier)

    async def repair(identifier, resume=False):
        investigated.append((identifier, resume))

    orchestration = ModuleType("contractbridge.orchestration")
    orchestration.start_repair = start_repair
    monkeypatch.setitem(sys.modules, "contractbridge.orchestration", orchestration)
    monkeypatch.setattr(app.state.monitor, "repair", repair)
    await app.state.monitor.dispatch_repair(durable_incident["id"])
    await app.state.monitor.dispatch_repair(classic_incident["id"])
    assert started == [durable_incident["id"]]
    assert investigated == [(classic_incident["id"], False)]
    assert not durable_incident.get("lifecycle")


def test_declarative_client_target_is_generic_and_repository_relative():
    target = validate_client(
        {
            "kind": "python-declarative",
            "modulePath": "src/acme_connectors/billing.py",
            "bundlePath": "src/acme_connectors/billing.json",
            "importNamespace": "acme.runtime",
        }
    )
    source = baseline_client(ROOT, target)
    assert "from acme.runtime." in source
    assert "BUNDLE_NAME = None" in source
    assert "shop" not in source.lower()
    with pytest.raises(ValueError):
        validate_client(
            {
                "kind": "python-declarative",
                "modulePath": "../application.py",
                "bundlePath": "../application.json",
            }
        )
    with pytest.raises(ValueError):
        validate_client(
            {
                "kind": "python-declarative",
                "modulePath": "src/a.py",
                "bundlePath": "config/a.json",
            }
        )


def test_runtime_mitigation_policy_binds_exact_contracts_fixtures_and_behavior(bundle):
    migration = {
        "specs": {"old": bundle["oldSpec"], "new": bundle["newSpec"]},
        "fixtures": bundle["fixtures"],
        "plan": copy.deepcopy(bundle["plan"]),
    }
    policy = {
        "oldHash": digest(migration["specs"]["old"]),
        "newHash": digest(migration["specs"]["new"]),
        "fixturesHash": digest(migration["fixtures"]),
        "behaviorHash": digest(behavior(migration["plan"])),
        "semanticReview": "Reviewed against provider documentation and independent synthetic fixtures.",
    }
    settings = Settings(
        ROOT, ROOT / ".local/tests-unused", "sqlite:///:memory:", OPERATOR, auto_mitigate=True
    )
    assert require_authorization(settings, {"mitigationAuthorization": policy}, migration) == policy

    explanatory_edit = copy.deepcopy(migration)
    explanatory_edit["plan"]["requestMappings"][0]["reason"] = "Equivalent explanation"
    require_authorization(settings, {"mitigationAuthorization": policy}, explanatory_edit)

    semantic_edit = copy.deepcopy(migration)
    enum_rule = next(rule for rule in semantic_edit["plan"]["responseMappings"] if rule["op"] == "enum_map")
    enum_rule["values"] = {**enum_rule["values"], "PLACED": "invented-business-state"}
    with pytest.raises(Conflict, match="exceeds the reviewed semantic policy"):
        require_authorization(settings, {"mitigationAuthorization": policy}, semantic_edit)

    changed_fixture = copy.deepcopy(migration)
    changed_fixture["fixtures"][0]["clientResponse"] = {
        **changed_fixture["fixtures"][0]["clientResponse"],
        "unexpected": True,
    }
    with pytest.raises(Conflict, match="exceeds the reviewed semantic policy"):
        require_authorization(settings, {"mitigationAuthorization": policy}, changed_fixture)

    disabled = Settings(ROOT, ROOT / ".local/tests-unused", "sqlite:///:memory:", OPERATOR)
    with pytest.raises(Conflict, match="recorded integration-specific semantic policy"):
        require_authorization(disabled, {"mitigationAuthorization": policy}, migration)

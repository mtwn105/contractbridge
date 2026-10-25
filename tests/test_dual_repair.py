from pathlib import Path

import httpx
import pytest

from contractbridge.config import Settings
from contractbridge.dual_repair import DualRepair
from contractbridge.monitoring import RuntimeMonitor
from contractbridge.storage import Store
from contractbridge.util import canonical
from contractbridge.workflow import Workflow

ROOT = Path(__file__).resolve().parents[1]
OPERATOR = "generic-integration-operator-token-32-chars"
APPLICATION_TOKEN = "synthetic-application-verification-token"


def contract(path, request_field, response_field):
    return {
        "openapi": "3.1.0",
        "info": {"title": "Independent provider", "version": "1"},
        "paths": {
            path: {
                "post": {
                    "operationId": "generic_operation",
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


class RetirementRunner:
    def __init__(self, fail_first_stop=False):
        self.stop_calls = []
        self.fail_first_stop = fail_first_stop

    async def stop(self, identifier):
        self.stop_calls.append(identifier)
        if self.fail_first_stop and len(self.stop_calls) == 1:
            raise RuntimeError("simulated supervisor interruption")
        return {"passed": True}


def retirement_system(tmp_path, monkeypatch, *, probe_ok=True, fail_first_stop=False):
    spec = contract("/submit", "input", "output")
    repair_hash = "a" * 64
    merge_sha = "b" * 40
    build_id = "11111111-1111-4111-8111-111111111111"
    settings = Settings(
        ROOT,
        tmp_path / "artifacts",
        "sqlite:///" + str(tmp_path / "state.db"),
        OPERATOR,
        upstream_allowlist=("http://provider:8080",),
        allow_synthetic_probes=True,
        verification_origins=("http://application:8080",),
        verification_credentials=("APPLICATION_TEST_TOKEN",),
        verification_samples=2,
        verification_interval=0,
        deployment_environment="production",
    )
    runner = RetirementRunner(fail_first_stop)
    store = Store(settings.database_url)
    workflow = Workflow(settings, store, object(), object(), runner)
    calls = []

    def handle(request):
        calls.append((request.method, request.url.host, request.url.path))
        if request.url.host == "provider" and request.url.path == "/openapi.json":
            return httpx.Response(200, json=spec)
        assert request.headers["authorization"] == "Bearer " + APPLICATION_TOKEN
        if request.url.path == "/release":
            return httpx.Response(
                200,
                json={"commit": merge_sha, "repairHash": repair_hash, "mode": "native"},
            )
        if request.url.path == "/probe":
            return httpx.Response(
                200,
                json={
                    "commit": merge_sha,
                    "repairHash": repair_hash,
                    "translation": "bypassed" if probe_ok else "active",
                    "status": "CONFIRMED",
                },
            )
        raise AssertionError((request.method, str(request.url)))

    transport = httpx.MockTransport(handle)
    monitor = RuntimeMonitor(workflow, settings, transport=transport)
    connection = monitor.create(
        {
            "name": "Arbitrary application integration",
            "upstreamBaseUrl": "http://provider:8080",
            "sourceSpec": canonical(spec),
            "fixtureSource": "synthetic",
            "application": {
                "baseUrl": "http://application:8080",
                "credentialEnv": "APPLICATION_TEST_TOKEN",
                "releasePath": "/release",
                "probePath": "/probe",
                "environment": "production",
            },
        }
    )
    migration = store.create("Permanent application repair")

    def prepare_migration(value):
        value.update(
            specs={"old": spec, "new": spec},
            sourcePath="/submit",
            targetPath="/submit",
            connectionId=connection["id"],
            approvedUpstream=connection["upstreamBaseUrl"],
        )

    migration = store.mutate(migration["id"], "PREPARED_FOR_RELEASE_TEST", prepare_migration)
    incident = monitor.resources.create(
        "incidents",
        {
            "connectionId": connection["id"],
            "state": "READY_FOR_REVIEW",
            "running": False,
            "migrationId": migration["id"],
            "candidate": {"inputHash": repair_hash},
            "publication": None,
            "lifecycle": {
                "phase": "DEPLOYED",
                "sharedMappingHash": repair_hash,
                "merge": {"sha": merge_sha, "number": 17},
                "deployment": {"sha": merge_sha, "state": "success", "environment": "production"},
                "mitigation": {
                    "status": "ACTIVE",
                    "migrationId": migration["id"],
                    "buildId": build_id,
                    "artifactHash": "c" * 64,
                },
            },
        },
    )

    def activate(value):
        value.update(
            route={"mode": "BRIDGE", "migrationId": migration["id"], "buildId": build_id},
            openIncidentId=incident["id"],
            repairIncidentId=incident["id"],
        )

    monitor.resources.update("connections", connection["id"], "TEST_BRIDGE_ACTIVE", activate)
    monkeypatch.setenv("APPLICATION_TEST_TOKEN", APPLICATION_TOKEN)
    return DualRepair(monitor, transport=transport), monitor, incident, runner, calls


async def test_merge_without_matching_deployment_never_retires_runtime_bridge(tmp_path, monkeypatch):
    controller, monitor, incident, runner, calls = retirement_system(tmp_path, monkeypatch)

    def remove_deployment(value):
        value["lifecycle"]["deployment"] = None
        value["lifecycle"]["phase"] = "PR_MERGED"

    monitor.resources.update("incidents", incident["id"], "TEST_MERGE_ONLY", remove_deployment)
    result = await controller.verify(incident["id"])
    connection = monitor.resources.get("connections", incident["connectionId"])
    assert result == {"ready": False, "retired": False}
    assert connection["route"]["mode"] == "BRIDGE"
    assert runner.stop_calls == []
    assert calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("sha", "c" * 40), ("environment", "different-environment")],
)
async def test_wrong_deployment_identity_never_retires_runtime_bridge(tmp_path, monkeypatch, field, value):
    controller, monitor, incident, runner, calls = retirement_system(tmp_path, monkeypatch)

    def alter_deployment(document):
        document["lifecycle"]["deployment"][field] = value

    monitor.resources.update("incidents", incident["id"], "TEST_WRONG_DEPLOYMENT_IDENTITY", alter_deployment)
    result = await controller.verify(incident["id"])
    connection = monitor.resources.get("connections", incident["connectionId"])
    assert result == {"ready": False, "retired": False}
    assert connection["route"]["mode"] == "BRIDGE"
    assert runner.stop_calls == []
    assert calls == []


async def test_failed_bypass_probe_keeps_temporary_bridge_active(tmp_path, monkeypatch):
    controller, monitor, incident, runner, _ = retirement_system(tmp_path, monkeypatch, probe_ok=False)
    result = await controller.verify(incident["id"])
    connection = monitor.resources.get("connections", incident["connectionId"])
    life = monitor.resources.get("incidents", incident["id"])["lifecycle"]
    assert result == {"ready": True, "retired": False}
    assert connection["route"]["mode"] == "BRIDGE"
    assert runner.stop_calls == []
    assert life["phase"] == "VERIFY_FAILED"
    assert life["verification"]["passed"] is False


async def test_matching_deployment_and_multiple_native_samples_retire_bridge(tmp_path, monkeypatch):
    controller, monitor, incident, runner, calls = retirement_system(tmp_path, monkeypatch)
    result = await controller.verify(incident["id"])
    connection = monitor.resources.get("connections", incident["connectionId"])
    life = monitor.resources.get("incidents", incident["id"])["lifecycle"]
    assert result == {"ready": True, "retired": True}
    assert connection["route"] == {"mode": "DIRECT"}
    assert runner.stop_calls == ["11111111-1111-4111-8111-111111111111"]
    assert [path for _, host, path in calls if host == "application"] == [
        "/release",
        "/probe",
        "/release",
        "/probe",
    ]
    assert life["phase"] == "RESOLVED"
    assert life["mitigation"]["status"] == "RETIRED"
    assert len(life["verification"]["samples"]) == 2


async def test_retirement_resumes_safely_after_supervisor_interruption(tmp_path, monkeypatch):
    controller, monitor, incident, runner, _ = retirement_system(tmp_path, monkeypatch, fail_first_stop=True)
    try:
        await controller.verify(incident["id"])
    except RuntimeError as exc:
        assert "supervisor interruption" in str(exc)
    result = await controller.verify(incident["id"])
    connection = monitor.resources.get("connections", incident["connectionId"])
    assert result == {"ready": True, "retired": True}
    assert connection["route"] == {"mode": "DIRECT"}
    assert runner.stop_calls == [
        "11111111-1111-4111-8111-111111111111",
        "11111111-1111-4111-8111-111111111111",
    ]

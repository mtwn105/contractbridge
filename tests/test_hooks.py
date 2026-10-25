import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx

from contractbridge.api import create_app
from contractbridge.config import Settings
from contractbridge.util import canonical

ROOT = Path(__file__).resolve().parents[1]
OPERATOR = "generic-integration-operator-token-32-chars"
SENTRY_SECRET = "sentry-hook-signing-secret-at-least-32"
GITHUB_SECRET = "github-hook-signing-secret-at-least-32"
REPOSITORY = "example/application"


class UnusedRunner:
    pass


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


def signature(secret, raw, prefix=""):
    return prefix + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def hook_app(tmp_path, *, deployment_environment="staging"):
    settings = Settings(
        ROOT,
        tmp_path / "artifacts",
        "sqlite:///" + str(tmp_path / "state.db"),
        OPERATOR,
        upstream_allowlist=("http://provider:8080",),
        github_repository=REPOSITORY,
        github_token="test-only-github-token",
        sentry_webhook_secret=SENTRY_SECRET,
        github_webhook_secret=GITHUB_SECRET,
        deployment_environment=deployment_environment,
        verification_origins=("http://application:8080",),
        verification_credentials=("APPLICATION_TEST_TOKEN",),
    )
    app = create_app(settings, diff=object(), planner=object(), runner=UnusedRunner())
    connection = app.state.monitor.create(
        {
            "name": "Generic provider",
            "upstreamBaseUrl": "http://provider:8080",
            "sourceSpec": canonical(contract("/submit", "input", "output")),
            "fixtureSource": "synthetic",
            "repository": REPOSITORY,
            "sentryProject": "monitored-application",
            "application": {
                "baseUrl": "http://application:8080",
                "credentialEnv": "APPLICATION_TEST_TOKEN",
                "environment": "production",
            },
        }
    )
    return app, connection


async def signed_post(client, path, payload, *, source, event=None, timestamp=None):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    if source == "sentry":
        headers = {
            "Content-Type": "application/json",
            "Sentry-Hook-Signature": signature(SENTRY_SECRET, raw),
            "Sentry-Hook-Timestamp": str(timestamp if timestamp is not None else time.time()),
            "Sentry-Hook-Resource": "event_alert",
        }
    else:
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature(GITHUB_SECRET, raw, "sha256="),
            "X-GitHub-Event": event,
        }
    return await client.post(path, content=raw, headers=headers)


async def test_sentry_hook_is_project_bound_deduplicated_and_does_not_store_payload(tmp_path):
    app, connection = hook_app(tmp_path)
    dispatched = []

    async def dispatch(identifier):
        dispatched.append(identifier)

    app.state.monitor.dispatch_repair = dispatch
    payload = {
        "action": "triggered",
        "data": {
            "event": {
                "project": 451234,
                "url": "https://us.sentry.io/api/0/projects/example/monitored-application/events/abc123/",
                "message": "private body must never be stored",
                "tags": [["contractbridge.connection_id", connection["id"]]],
            }
        },
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control") as client:
        first = await signed_post(client, "/api/v1/hooks/sentry", payload, source="sentry")
        incident_after_first = app.state.monitor.resources.get("incidents", first.json()["incidentId"])
        receipt_after_first = app.state.monitor.resources.list("hooks")[0]
        connection_after_first = app.state.monitor.resources.get("connections", connection["id"])
        second = await signed_post(client, "/api/v1/hooks/sentry", payload, source="sentry")
        assert first.status_code == second.status_code == 202
        assert first.json()["duplicate"] is False
        assert second.json()["duplicate"] is True
    incident_id = first.json()["incidentId"]
    assert dispatched == [incident_id]
    stored = canonical(app.state.monitor.resources.list("hooks"))
    assert "private body must never be stored" not in stored
    assert len(app.state.monitor.resources.list("hooks")) == 1
    incident_after_duplicate = app.state.monitor.resources.get("incidents", incident_id)
    receipt_after_duplicate = app.state.monitor.resources.list("hooks")[0]
    connection_after_duplicate = app.state.monitor.resources.get("connections", connection["id"])
    assert incident_after_duplicate["occurrences"] == 1
    assert incident_after_duplicate["revision"] == incident_after_first["revision"]
    assert receipt_after_duplicate["revision"] == receipt_after_first["revision"]
    assert connection_after_duplicate["revision"] == connection_after_first["revision"]


async def test_sentry_hook_rejects_bad_signature_stale_timestamp_and_wrong_project(tmp_path):
    app, connection = hook_app(tmp_path)
    payload = {
        "action": "triggered",
        "data": {
            "event": {
                "project_slug": "different-project",
                "tags": {"contractbridge.connection_id": connection["id"]},
            }
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control") as client:
        bad = await client.post(
            "/api/v1/hooks/sentry",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "Sentry-Hook-Signature": "bad",
                "Sentry-Hook-Timestamp": str(time.time()),
                "Sentry-Hook-Resource": "event_alert",
            },
        )
        stale = await signed_post(
            client,
            "/api/v1/hooks/sentry",
            payload,
            source="sentry",
            timestamp=time.time() - 601,
        )
        wrong_project = await signed_post(client, "/api/v1/hooks/sentry", payload, source="sentry")
    assert bad.status_code == 401
    assert stale.status_code == 401
    assert wrong_project.status_code == 202
    assert wrong_project.json() == {"accepted": False, "reason": "Unregistered Sentry project"}
    assert not app.state.monitor.resources.list("incidents")


def seed_published_incident(monitor, connection):
    return monitor.resources.create(
        "incidents",
        {
            "connectionId": connection["id"],
            "state": "READY_FOR_REVIEW",
            "running": False,
            "migrationId": None,
            "candidate": {"artifactHash": "a" * 64},
            "publication": {
                "status": "CREATED",
                "url": "https://github.com/example/application/pull/17",
                "repository": REPOSITORY,
                "number": 17,
                "branch": "contractbridge/repair/incident/application",
                "headSha": "b" * 40,
                "base": "main",
            },
            "lifecycle": {"phase": "PR_OPEN"},
        },
    )


def deployment_payload(sha, environment="staging", state="success", created_at="2026-10-25T03:00:00Z"):
    return {
        "repository": {"full_name": REPOSITORY},
        "deployment": {"sha": sha, "environment": environment},
        "deployment_status": {"state": state, "created_at": created_at},
    }


def merge_payload(incident, sha):
    publication = incident["publication"]
    return {
        "action": "closed",
        "number": publication["number"],
        "repository": {"full_name": REPOSITORY},
        "pull_request": {
            "merged": True,
            "merge_commit_sha": sha,
            "head": {"ref": publication["branch"], "sha": publication["headSha"]},
            "base": {"ref": publication["base"]},
        },
    }


async def test_github_release_evidence_reconciles_out_of_order_and_is_idempotent(tmp_path):
    app, connection = hook_app(tmp_path)
    incident = seed_published_incident(app.state.monitor, connection)
    sha = "c" * 40
    deployment = deployment_payload(sha, environment="production")
    merge = merge_payload(incident, sha)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control") as client:
        first = await signed_post(
            client,
            "/api/v1/hooks/github",
            deployment,
            source="github",
            event="deployment_status",
        )
        assert first.status_code == 202
        assert app.state.monitor.resources.get("incidents", incident["id"])["lifecycle"]["phase"] == "PR_OPEN"
        merged = await signed_post(
            client,
            "/api/v1/hooks/github",
            merge,
            source="github",
            event="pull_request",
        )
        revision = app.state.monitor.resources.get("incidents", incident["id"])["revision"]
        duplicate = await signed_post(
            client,
            "/api/v1/hooks/github",
            merge,
            source="github",
            event="pull_request",
        )
    life = app.state.monitor.resources.get("incidents", incident["id"])["lifecycle"]
    assert merged.status_code == duplicate.status_code == 202
    assert duplicate.json()["duplicate"] is True
    assert life["phase"] == "DEPLOYED"
    assert life["merge"]["sha"] == life["deployment"]["sha"] == sha
    assert app.state.monitor.resources.get("incidents", incident["id"])["revision"] == revision


async def test_github_deployment_must_match_registered_application_environment(tmp_path):
    app, connection = hook_app(tmp_path, deployment_environment="global-staging")
    incident = seed_published_incident(app.state.monitor, connection)
    sha = "d" * 40
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control") as client:
        await signed_post(
            client,
            "/api/v1/hooks/github",
            merge_payload(incident, sha),
            source="github",
            event="pull_request",
        )
        await signed_post(
            client,
            "/api/v1/hooks/github",
            deployment_payload(sha, environment="production"),
            source="github",
            event="deployment_status",
        )
    life = app.state.monitor.resources.get("incidents", incident["id"])["lifecycle"]
    assert life["phase"] == "DEPLOYED"
    assert life["deployment"]["environment"] == "production"


async def test_github_hook_rejects_unregistered_repository_without_recording_evidence(tmp_path):
    app, _ = hook_app(tmp_path)
    payload = deployment_payload("e" * 40)
    payload["repository"]["full_name"] = "attacker/unregistered"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control") as client:
        response = await signed_post(
            client,
            "/api/v1/hooks/github",
            payload,
            source="github",
            event="deployment_status",
        )
    assert response.status_code == 202
    assert response.json()["accepted"] is False
    assert not app.state.monitor.resources.list("hooks")

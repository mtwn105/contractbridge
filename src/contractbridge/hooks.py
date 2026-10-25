"""Signed, bounded incident/release webhooks. Persist metadata, never event bodies."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from starlette.background import BackgroundTask
from starlette.responses import JSONResponse
from starlette.routing import Route

from .storage import NotFound
from .util import strict_loads


def signature_valid(raw, secret, signature, prefix=""):
    if len(secret) < 24 or not isinstance(signature, str):
        return False
    expected = prefix + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode())


async def raw_body(request):
    raw = b""
    async for part in request.stream():
        raw += part
        if len(raw) > 262144:
            raise ValueError("Webhook exceeds size limit")
    return raw


def uuid(value):
    try:
        return str(UUID(value))
    except ValueError, TypeError, AttributeError:
        return None


def sentry_project(event):
    """Prefer a slug, falling back to Sentry's signed event API URL.

    Issue-alert payloads commonly expose ``project`` as a numeric ID while the
    event URL contains ``/projects/{org}/{project}/events/{id}/``.
    """
    explicit = event.get("project_slug")
    if isinstance(explicit, str) and explicit:
        return explicit
    project = event.get("project")
    if isinstance(project, str) and not project.isdigit():
        return project
    url = event.get("url")
    if not isinstance(url, str) or len(url) > 1000:
        return None
    parts = [part for part in urlsplit(url).path.split("/") if part]
    try:
        index = parts.index("projects")
        return parts[index + 2] if parts[index + 3] == "events" else None
    except (ValueError, IndexError):
        return None


def receipt(resources, source, raw, metadata):
    checksum = hashlib.sha256(raw).hexdigest()
    identifier = str(uuid5(NAMESPACE_URL, source + ":" + checksum))
    try:
        return resources.get("hooks", identifier), False
    except NotFound:
        try:
            return resources.create(
                "hooks", {"source": source, "payloadHash": checksum, **metadata}, identifier
            ), True
        except Exception:
            # A concurrent delivery may win the unique insert. Only treat that
            # as a duplicate when the deterministic receipt now exists.
            return resources.get("hooks", identifier), False


def reconcile_github(monitor):
    receipts = [r for r in monitor.resources.list("hooks") if r["source"] == "github"]
    wake = []
    for i in monitor.resources.list("incidents"):
        publication = i.get("publication") or {}
        if not i.get("lifecycle") or not publication.get("url") or i["lifecycle"]["phase"] == "RESOLVED":
            continue
        relevant = [r for r in receipts if r.get("repository") == publication.get("repository")]
        connection = monitor.resources.get("connections", i["connectionId"])
        expected_environment = (connection.get("application") or {}).get(
            "environment", monitor.settings.deployment_environment
        )
        merges = [
            r
            for r in relevant
            if r.get("event") == "pull_request"
            and r.get("number") == publication.get("number")
            and r.get("branch") == publication.get("branch")
            and r.get("headSha") == publication.get("headSha")
            and r.get("base") == publication.get("base")
        ]
        if not merges:
            continue
        merged = merges[0]
        merge = {"sha": merged["sha"], "number": merged["number"], "receiptId": merged["id"]}
        deployments = sorted(
            [
                r
                for r in relevant
                if r.get("event") == "deployment_status"
                and r.get("sha") == merge["sha"]
                and r.get("environment") == expected_environment
            ],
            key=lambda r: r.get("eventCreatedAt") or r["createdAt"],
            reverse=True,
        )
        deployment = (
            (
                {k: deployments[0][k] for k in ("sha", "environment", "state")}
                | {"receiptId": deployments[0]["id"]}
            )
            if deployments
            else None
        )
        if i["lifecycle"].get("merge") == merge and i["lifecycle"].get("deployment") == deployment:
            continue

        def update(v):
            v["lifecycle"].update(
                merge=merge,
                deployment=deployment,
                phase="DEPLOYED" if deployment and deployment["state"] == "success" else "PR_MERGED",
            )

        monitor.resources.update("incidents", i["id"], "GITHUB_RELEASE_EVIDENCE", update)
        wake.append(i["id"])
    return wake


def hook_routes(monitor):
    settings, resources = monitor.settings, monitor.resources

    async def sentry(request):
        raw = await raw_body(request)
        if not signature_valid(
            raw, settings.sentry_webhook_secret, request.headers.get("sentry-hook-signature")
        ):
            return JSONResponse({"title": "Invalid Sentry webhook signature"}, status_code=401)
        try:
            if abs(time.time() - float(request.headers.get("sentry-hook-timestamp", "0"))) > 600:
                raise ValueError("Expired webhook")
        except ValueError:
            return JSONResponse({"title": "Expired Sentry webhook"}, status_code=401)
        payload = strict_loads(raw)
        if (
            request.headers.get("sentry-hook-resource") != "event_alert"
            or payload.get("action") != "triggered"
        ):
            return JSONResponse({"accepted": False, "reason": "Unsupported Sentry resource"}, status_code=202)
        data = payload.get("data") or {}
        event = data.get("event") or {}
        project = sentry_project(event)
        tags = event.get("tags") or []
        if isinstance(tags, list):
            try:
                tags = dict(tags)
            except TypeError, ValueError:
                tags = {}
        if not isinstance(tags, dict):
            tags = {}
        connection_id = uuid(tags.get("contractbridge.connection_id"))
        incident_id = uuid(tags.get("contractbridge.incident_id"))
        if not connection_id:
            return JSONResponse(
                {"accepted": False, "reason": "No registered integration context"}, status_code=202
            )
        try:
            connection = resources.get("connections", connection_id)
        except NotFound:
            return JSONResponse(
                {"accepted": False, "reason": "No registered integration context"}, status_code=202
            )
        if str(project) != connection.get("sentryProject"):
            return JSONResponse({"accepted": False, "reason": "Unregistered Sentry project"}, status_code=202)
        tagged_incident = None
        if incident_id:
            try:
                tagged_incident = resources.get("incidents", incident_id)
            except NotFound:
                return JSONResponse(
                    {"accepted": False, "reason": "No registered incident context"}, status_code=202
                )
            if tagged_incident["connectionId"] != connection["id"]:
                return JSONResponse(
                    {"accepted": False, "reason": "Incident context does not match integration"},
                    status_code=202,
                )
        record, created = receipt(
            resources, "sentry", raw, {"connectionId": connection_id, "incidentId": incident_id}
        )
        if not incident_id:
            if created:
                incident, _ = monitor.record_failure(connection_id, "SENTRY_INTEGRATION_FAILURE", 0)
                incident_id = incident["id"]
                resources.update(
                    "hooks", record["id"], "INCIDENT_LINKED", lambda v: v.update(incidentId=incident_id)
                )
            else:
                incident_id = record.get("incidentId")
                if not incident_id:
                    raise ValueError("Sentry receipt is missing its linked incident")
        incident = tagged_incident or resources.get("incidents", incident_id)
        if incident["state"] == "RESOLVED":
            return JSONResponse({"accepted": True, "duplicate": True}, status_code=202)
        background = BackgroundTask(monitor.dispatch_repair, incident_id) if created else None
        return JSONResponse(
            {"accepted": True, "duplicate": not created, "incidentId": incident_id},
            status_code=202,
            background=background,
        )

    async def github(request):
        raw = await raw_body(request)
        if not signature_valid(
            raw, settings.github_webhook_secret, request.headers.get("x-hub-signature-256"), "sha256="
        ):
            return JSONResponse({"title": "Invalid GitHub webhook signature"}, status_code=401)
        event, payload = request.headers.get("x-github-event"), strict_loads(raw)
        repository = (payload.get("repository") or {}).get("full_name")
        registered = {monitor.publisher_for(c).repository for c in resources.list("connections")}
        if not repository or repository not in registered:
            return JSONResponse({"accepted": False, "reason": "Unregistered repository"}, status_code=202)
        metadata = {"event": event, "repository": repository}
        if event == "pull_request":
            pr = payload.get("pull_request") or {}
            if payload.get("action") != "closed" or pr.get("merged") is not True:
                return JSONResponse(
                    {"accepted": False, "reason": "Not a merged pull request"}, status_code=202
                )
            metadata.update(
                number=payload.get("number"),
                sha=pr.get("merge_commit_sha"),
                branch=(pr.get("head") or {}).get("ref"),
                headSha=(pr.get("head") or {}).get("sha"),
                base=(pr.get("base") or {}).get("ref"),
            )
        elif event == "deployment_status":
            d, status = payload.get("deployment") or {}, payload.get("deployment_status") or {}
            metadata.update(
                sha=d.get("sha"),
                environment=d.get("environment"),
                state=status.get("state"),
                eventCreatedAt=status.get("created_at"),
            )
        else:
            return JSONResponse({"accepted": False, "reason": "Unsupported GitHub event"}, status_code=202)
        if not isinstance(metadata.get("sha"), str) or not re.fullmatch(
            r"(?:[a-f0-9]{40}|[a-f0-9]{64})", metadata["sha"]
        ):
            raise ValueError("Invalid release revision")
        _, created = receipt(resources, "github", raw, metadata)
        identifiers = reconcile_github(monitor)

        async def wake():
            if settings.temporal_address:
                from .orchestration import wake_repair

                for identifier in identifiers:
                    try:
                        await wake_repair(settings, identifier)
                    except Exception:
                        pass  # Persisted evidence is also polled by the durable workflow.

        return JSONResponse(
            {"accepted": True, "duplicate": not created}, status_code=202, background=BackgroundTask(wake)
        )

    return [
        Route("/api/v1/hooks/sentry", sentry, methods=["POST"]),
        Route("/api/v1/hooks/github", github, methods=["POST"]),
    ]

from __future__ import annotations

import re

from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .models import Fixture, check_shape
from .storage import Conflict
from .util import reject_secrets


def runtime_routes(monitor, body, revision):
    prefix, resources = "/api/v1", monitor.resources

    def shape(data, fields, required):
        check_shape(
            {"type": "object", "additionalProperties": False, "properties": fields, "required": required},
            data,
        )

    def identifier(request):
        return str(request.path_params["id"])

    async def connections(request):
        if request.method == "GET":
            return JSONResponse(
                {
                    "connections": [monitor.public_connection(c) for c in resources.list("connections")],
                    "github": {
                        "configured": monitor.publisher.configured,
                        "repository": monitor.publisher.repository or None,
                        "base": monitor.publisher.base,
                    },
                    "approvedUpstreams": list(monitor.settings.upstream_allowlist),
                }
            )
        data = await body(request)
        shape(
            data,
            {
                "name": {"type": "string", "minLength": 1, "maxLength": 120},
                "upstreamBaseUrl": {"type": "string", "maxLength": 300},
                "specPath": {"type": "string", "maxLength": 256},
                "sourceSpec": {"type": "string", "maxLength": 1_000_000},
                "sourcePath": {"type": "string", "maxLength": 256},
                "targetPath": {"type": "string", "maxLength": 256},
                "fixtureSource": {"enum": ["synthetic", "sanitized"]},
                "fixtures": {"type": "array", "maxItems": 100},
                "autoRepair": {"type": "boolean"},
                "autoPublishDraft": {"type": "boolean"},
                "repository": {"type": "string"},
                "baseBranch": {"type": "string"},
                "client": {"type": "object"},
                "application": {"type": "object"},
                "mitigationPolicy": {"type": "object"},
                "sentryProject": {"type": "string"},
                "dualRepair": {"type": "boolean"},
            },
            ["name", "upstreamBaseUrl", "sourceSpec", "fixtureSource"],
        )
        return JSONResponse(monitor.public_connection(monitor.create(data)), status_code=201)

    async def connection(request):
        return JSONResponse(monitor.public_connection(resources.get("connections", identifier(request))))

    async def status(request):
        c = resources.get("connections", identifier(request))
        latest = next((i for i in resources.list("incidents") if i["connectionId"] == c["id"]), None)
        return JSONResponse(
            {
                "enabled": True,
                "connection": monitor.public_connection(c),
                "incident": monitor.public_incident(latest) if latest else None,
                "github": {
                    "configured": monitor.publisher_for(c).configured,
                    "repository": monitor.publisher_for(c).repository or None,
                },
                "sentryUrl": "https://" + monitor.settings.sentry_org + ".sentry.io/issues/?project=-1"
                if re.fullmatch(r"[A-Za-z0-9-]+", monitor.settings.sentry_org)
                else None,
            }
        )

    async def access(request):
        from .integrations import gateway_token

        c = resources.get("connections", identifier(request))
        return JSONResponse(
            {
                "gatewayPath": monitor.public_connection(c)["gatewayPath"],
                "token": gateway_token(monitor.settings.gateway_secret, c["id"]),
                "scope": c["id"],
            }
        )

    async def discover(request):
        data = await body(request)
        shape(
            data,
            {
                "name": {"type": "string", "minLength": 1, "maxLength": 120},
                "upstreamBaseUrl": {"type": "string", "maxLength": 300},
                "specPath": {"type": "string", "maxLength": 256},
                "sourcePath": {"type": "string", "maxLength": 256},
                "targetPath": {"type": "string", "maxLength": 256},
                "fixtureSource": {"enum": ["synthetic", "sanitized"]},
                "fixtures": {"type": "array", "maxItems": 100},
                "autoRepair": {"type": "boolean"},
                "autoPublishDraft": {"type": "boolean"},
                "repository": {"type": "string", "maxLength": 240},
                "baseBranch": {"type": "string", "maxLength": 120},
                "client": {"type": "object"},
                "application": {"type": "object"},
                "mitigationPolicy": {"type": "object"},
                "sentryProject": {"type": "string", "maxLength": 120},
                "dualRepair": {"type": "boolean"},
            },
            ["name", "upstreamBaseUrl"],
        )
        origin = monitor.origin(data["upstreamBaseUrl"])
        path = data.get("specPath", "/openapi.json")
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", path) or path.startswith("//") or ".." in path:
            raise ValueError("Unsupported specification path")
        raw, code, _ = await monitor.exchange("GET", origin + path)
        if code != 200:
            raise ValueError("The current OpenAPI document is unavailable")
        data["sourceSpec"] = raw.decode("utf-8")
        data.setdefault("fixtureSource", "synthetic")
        return JSONResponse(monitor.public_connection(monitor.create(data)), status_code=201)

    async def connection_fixtures(request):
        data = await body(request)
        shape(
            data,
            {"fixtures": {"type": "array"}, "source": {"enum": ["synthetic", "sanitized"]}},
            ["fixtures", "source"],
        )
        Fixture.parse_many(data["fixtures"])
        reject_secrets(data["fixtures"])
        c = resources.update(
            "connections",
            identifier(request),
            "FIXTURES_UPDATED",
            lambda v: v.update(fixtures=data["fixtures"], fixtureSource=data["source"]),
            revision(request),
        )
        return JSONResponse(monitor.public_connection(c))

    async def automation(request):
        data = await body(request)
        shape(data, {"confirmed": {"const": True}, "enabled": {"type": "boolean"}}, ["confirmed", "enabled"])
        result = monitor.configure_publication(identifier(request), data["enabled"], revision(request))
        return JSONResponse(monitor.public_connection(result))

    async def target(request):
        data = await body(request)
        shape(
            data,
            {"targetPath": {"type": "string", "pattern": "^/[A-Za-z0-9_/-]+$", "maxLength": 256}},
            ["targetPath"],
        )
        c = resources.update(
            "connections",
            identifier(request),
            "TARGET_OPERATION_SELECTED",
            lambda v: v.update(targetPath=data["targetPath"]),
            revision(request),
        )
        return JSONResponse(monitor.public_connection(c))

    async def incidents(request):
        return JSONResponse([monitor.public_incident(i) for i in resources.list("incidents")[:200]])

    async def incident(request):
        return JSONResponse(monitor.public_incident(resources.get("incidents", identifier(request))))

    async def gateway(request):
        if request.url.query:
            return JSONResponse({"title": "Query mappings are unsupported"}, status_code=400)
        return await monitor.gateway(
            str(request.path_params["connection_id"]),
            request.method,
            "/" + request.path_params["operation"],
            await body(request),
        )

    async def retry(request):
        data = await body(request)
        shape(data, {}, [])

        def reset(v):
            if v.get("running"):
                raise Conflict("Repair is already running")
            if v["state"] == "RESOLVED":
                raise Conflict("This incident is resolved; new failures create new incidents")
            v.update(state="DETECTED", candidate=None, error=None, publicationError=None)

        value = resources.update("incidents", identifier(request), "REPAIR_RETRIED", reset, revision(request))
        return JSONResponse(
            value, status_code=202, background=BackgroundTask(monitor.dispatch_repair, value["id"])
        )

    async def patch(request):
        data = await monitor.patch(identifier(request))
        return Response(
            data,
            media_type="text/x-diff",
            headers={"Content-Disposition": f'attachment; filename="repair-{identifier(request)}.patch"'},
        )

    async def preview(request):
        i, m, files = await monitor.package(identifier(request))
        return JSONResponse(
            {
                "title": "Repair API contract drift: " + i["id"][:8],
                "body": monitor.pr_body(i, m),
                "files": [
                    {"path": name, "bytes": len(text.encode())} for name, text in sorted(files.items())
                ],
                "artifactHash": i["candidate"]["artifactHash"],
                "repository": monitor.publisher_for(
                    resources.get("connections", i["connectionId"])
                ).repository
                or None,
                "githubConfigured": monitor.publisher_for(
                    resources.get("connections", i["connectionId"])
                ).configured,
                "draft": True,
                "published": bool(
                    (i.get("publication") or {}).get("url")
                    and (i.get("publication") or {}).get("artifactHash") == i["candidate"]["artifactHash"]
                ),
            }
        )

    async def publish(request):
        data = await body(request)
        shape(
            data,
            {"confirmed": {"const": True}, "artifactHash": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
            ["confirmed", "artifactHash"],
        )
        return JSONResponse(
            await monitor.publish(identifier(request), revision(request), data["artifactHash"])
        )

    async def route(request):
        data = await body(request)
        shape(
            data,
            {"confirmed": {"const": True}, "migrationId": {"type": "string", "format": "uuid"}},
            ["confirmed", "migrationId"],
        )
        return JSONResponse(await monitor.route(identifier(request), data["migrationId"], revision(request)))

    async def direct(request):
        data = await body(request)
        shape(data, {"confirmed": {"const": True}}, ["confirmed"])
        c = resources.update(
            "connections",
            identifier(request),
            "DIRECT_ROUTING_APPROVED",
            lambda v: v.update(
                route={"mode": "DIRECT"},
                state="MONITORING",
                openIncidentId=None,
                generation=v["generation"] + 1,
            ),
            revision(request),
        )
        return JSONResponse(monitor.public_connection(c))

    async def audit(request):
        return JSONResponse(resources.audit(identifier(request)))

    return [
        Route(prefix + "/connections/discover", discover, methods=["POST"]),
        Route(prefix + "/connections", connections, methods=["GET", "POST"]),
        Route(prefix + "/connections/{id:uuid}/status", status),
        Route(prefix + "/connections/{id:uuid}/access", access, methods=["POST"]),
        Route(prefix + "/connections/{id:uuid}", connection),
        Route(prefix + "/connections/{id:uuid}/fixtures", connection_fixtures, methods=["PUT"]),
        Route(prefix + "/connections/{id:uuid}/route", route, methods=["POST"]),
        Route(prefix + "/connections/{id:uuid}/target", target, methods=["PUT"]),
        Route(prefix + "/connections/{id:uuid}/draft-automation", automation, methods=["PUT"]),
        Route(prefix + "/connections/{id:uuid}/direct", direct, methods=["POST"]),
        Route(prefix + "/connections/{id:uuid}/audit", audit),
        Route(prefix + "/incidents", incidents),
        Route(prefix + "/incidents/{id:uuid}", incident),
        Route(prefix + "/incidents/{id:uuid}/retry", retry, methods=["POST"]),
        Route(prefix + "/incidents/{id:uuid}/patch", patch),
        Route(prefix + "/incidents/{id:uuid}/pr-preview", preview),
        Route(prefix + "/incidents/{id:uuid}/publish", publish, methods=["POST"]),
        Route(prefix + "/incidents/{id:uuid}/audit", audit),
        Route(prefix + "/gateway/{connection_id:uuid}/{operation:path}", gateway, methods=["POST"]),
    ]

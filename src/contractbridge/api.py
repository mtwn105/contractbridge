from __future__ import annotations

import json
import secrets
from uuid import uuid4

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .analysis import OasDiff
from .config import Settings
from .contracts import limited_tree
from .hooks import hook_routes
from .integrations import gateway_authorized, load_integrations
from .models import check_shape
from .monitoring import RuntimeMonitor
from .monitoring_api import runtime_routes
from .planner import OllamaPlanner
from .runtime import problem
from .sandbox import SandboxRunner
from .storage import Conflict, NotFound, Store
from .telemetry import capture_failure, initialize
from .util import sha256_file, strict_loads
from .workflow import Workflow


async def body(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise ValueError("Expected application/json")
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > 1_000_000:
            raise ValueError("Request body exceeds 1 MB")
    value = strict_loads(data or b"{}")
    if not isinstance(value, dict):
        raise ValueError("Request body must be an object")
    limited_tree(value)
    return value


def shape(value: dict, properties: dict, required=()):
    check_shape(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(required),
        },
        value,
    )


def revision(request: Request) -> int:
    value = request.headers.get("if-match", "").strip('"')
    if not value.isdigit():
        raise Conflict("Supply the current revision in If-Match")
    return int(value)


def create_app(
    settings: Settings | None = None, *, store=None, diff=None, planner=None, runner=None
) -> Starlette:
    settings = settings or Settings.environment()
    store = store or Store(settings.database_url)
    store.recover()
    settings.artifacts.mkdir(parents=True, exist_ok=True)
    initialize(settings.sentry_dsn)
    workflow = Workflow(
        settings,
        store,
        diff or OasDiff(),
        planner or OllamaPlanner(settings.ollama_url, settings.model),
        runner or SandboxRunner(settings.artifacts, settings.runner_timeout),
    )

    monitor = RuntimeMonitor(workflow, settings)
    monitor.recover()
    if settings.gateway_secret and (
        len(settings.gateway_secret) < 24 or settings.gateway_secret == settings.operator_token
    ):
        raise RuntimeError("Configure a distinct GATEWAY_SIGNING_SECRET of at least 24 characters")
    load_integrations(monitor)

    def migration(request):
        return store.get(str(request.path_params["id"]))

    def snapshot(m, status=200):
        return JSONResponse(workflow.public(m), status_code=status, headers={"ETag": f'"{m["revision"]}"'})

    async def health(_request):
        return JSONResponse(
            {
                "status": "UP",
                "version": "0.1.0",
                "runtime": "Python 3.14",
                "model": settings.model,
                "tracingConfigured": bool(settings.sentry_dsn),
            }
        )

    async def migrations(request):
        if request.method == "GET":
            return JSONResponse(
                [
                    {
                        k: m.get(k)
                        for k in (
                            "id",
                            "title",
                            "revision",
                            "state",
                            "createdAt",
                            "updatedAt",
                            "pendingJob",
                            "model",
                        )
                    }
                    for m in store.list()[:200]
                ]
            )
        value = await body(request)
        shape(value, {"title": {"type": "string", "minLength": 1, "maxLength": 120}}, ["title"])
        return snapshot(store.create(value["title"].strip()), 201)

    async def read_migration(request):
        return snapshot(migration(request))

    async def specs(request):
        value = await body(request)
        shape(value, {"content": {"type": "string", "maxLength": 1_000_000}}, ["content"])
        return snapshot(
            workflow.upload_spec(
                str(request.path_params["id"]),
                request.path_params["label"],
                value["content"],
                revision(request),
            )
        )

    async def fixtures(request):
        value = await body(request)
        shape(
            value,
            {"source": {"enum": ["synthetic", "sanitized"]}, "fixtures": {"type": "array"}},
            ["source", "fixtures"],
        )
        return snapshot(
            workflow.upload_fixtures(
                str(request.path_params["id"]), value["fixtures"], value["source"], revision(request)
            )
        )

    async def plan(request):
        m = migration(request)
        if request.method == "GET":
            return JSONResponse(
                {"plan": m["plan"], "revision": m["revision"], "issues": workflow.public(m)["issues"]},
                headers={"ETag": f'"{m["revision"]}"'},
            )
        return snapshot(workflow.edit_plan(m["id"], await body(request), revision(request)))

    async def approve(request):
        await monitor.assert_current(migration(request))
        value = await body(request)
        shape(
            value,
            {
                "decisions": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "minLength": 10, "maxLength": 1000},
                }
            },
            ["decisions"],
        )
        return snapshot(
            workflow.approve(str(request.path_params["id"]), value["decisions"], revision(request))
        )

    async def queue(request):
        value = await body(request)
        kind = {"analyze": "analyze", "proposals": "propose", "builds": "build", "activate": "activate"}[
            request.url.path.split("/")[-1]
        ]
        if kind == "analyze":
            shape(value, {"sourcePath": {"type": "string"}, "targetPath": {"type": "string"}})
        elif kind == "activate":
            shape(
                value,
                {
                    "confirmed": {"const": True},
                    "artifactHash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                },
                ["confirmed", "artifactHash"],
            )
            value["buildId"] = str(request.path_params["build_id"])
        else:
            shape(value, {})
        if kind in ("build", "activate"):
            await monitor.assert_current(migration(request))
        job = workflow.submit(str(request.path_params["id"]), kind, revision(request), value)
        return JSONResponse(
            job,
            status_code=202,
            headers={"Location": f"/api/v1/jobs/{job['id']}"},
            background=BackgroundTask(workflow.execute, job["id"]),
        )

    async def read_diff(request):
        m = migration(request)
        if m["diff"] is None:
            raise NotFound("No completed diff")
        return JSONResponse(m["diff"])

    async def job(request):
        return JSONResponse(store.get_job(str(request.path_params["job_id"])))

    async def build(request):
        m = workflow.public(migration(request))
        return JSONResponse(workflow.find_build(m, str(request.path_params["build_id"])))

    async def audit(request):
        return JSONResponse(store.audit(str(request.path_params["id"])))

    async def export(request):
        m = migration(request)
        b = workflow.find_build(m, str(request.path_params["build_id"]))
        if (
            b["status"] != "PASSED"
            or m["approvalHash"] != workflow.fingerprint(m)
            or b["inputHash"] != m["approvalHash"]
        ):
            raise Conflict("Only a passing build of the current approved inputs can be exported")
        path = settings.artifacts / "builds" / b["id"] / "output/adapter.zip"
        if not path.exists() or sha256_file(path) != b["artifactHash"]:
            raise Conflict("Artifact missing or checksum changed")
        if path.stat().st_size > 10_000_000:
            raise Conflict("Artifact exceeds the 10 MB export limit")
        return Response(
            path.read_bytes(),
            media_type="application/zip",
            headers={
                "X-Artifact-SHA256": b["artifactHash"],
                "Content-Disposition": f'attachment; filename="contractbridge-{b["id"]}.zip"',
            },
        )

    async def replay(request):
        m = migration(request)
        if m["state"] != "ACTIVE":
            raise Conflict("Activate a verified adapter before replaying traffic")
        value = await body(request)
        build = workflow.find_build(m, m["activeBuildId"])
        # Destination derives from a server-generated UUID, never from model data or a request URL.
        url = f"http://cb-proxy-{build['id']}:8080" + m["plan"]["sourceOperation"]["path"]
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            async with client.stream(
                "POST", url, json=value, headers={"X-Bridge-Inspect": build["runtime"]["inspectToken"]}
            ) as response:
                result = b""
                async for chunk in response.aiter_bytes():
                    result += chunk
                    if len(result) > 1_000_000:
                        raise ValueError("Replay response exceeded size limit")
        return JSONResponse(json.loads(result), status_code=response.status_code)

    async def archive(request):
        m = migration(request)
        if m["revision"] != revision(request):
            raise Conflict("Revision changed; reload before archiving")
        if m.get("pendingJob") or m["state"] == "BUILDING":
            raise Conflict("Wait for the current job")
        if m["state"] == "ACTIVE":
            await workflow.runner.stop(m["activeBuildId"])

        def finish(value):
            value.update(state="ARCHIVED", activeBuildId=None)

        return snapshot(store.mutate(m["id"], "ARCHIVED", finish, revision(request)))

    async def openapi(_request):
        path = settings.root / "docs/control-plane.openapi.json"
        return JSONResponse(json.loads(path.read_text()))

    prefix = "/api/v1"
    routes = [
        Route(prefix + "/health", health),
        Route(prefix + "/openapi.json", openapi),
        Route(prefix + "/migrations", migrations, methods=["GET", "POST"]),
        Route(prefix + "/migrations/{id:uuid}", read_migration),
        Route(prefix + "/migrations/{id:uuid}/specs/{label}", specs, methods=["POST"]),
        Route(prefix + "/migrations/{id:uuid}/fixtures", fixtures, methods=["POST"]),
        Route(prefix + "/migrations/{id:uuid}/plan", plan, methods=["GET", "PUT"]),
        Route(prefix + "/migrations/{id:uuid}/plan/approve", approve, methods=["POST"]),
        Route(prefix + "/migrations/{id:uuid}/diff", read_diff),
        Route(prefix + "/migrations/{id:uuid}/audit", audit),
        Route(prefix + "/migrations/{id:uuid}/builds/{build_id:uuid}", build),
        Route(prefix + "/migrations/{id:uuid}/builds/{build_id:uuid}/activate", queue, methods=["POST"]),
        Route(prefix + "/migrations/{id:uuid}/exports/{build_id:uuid}", export),
        Route(prefix + "/migrations/{id:uuid}/replay", replay, methods=["POST"]),
        Route(prefix + "/migrations/{id:uuid}/archive", archive, methods=["POST"]),
        Route(prefix + "/jobs/{job_id:uuid}", job),
    ]
    routes += [
        Route(prefix + "/migrations/{id:uuid}/" + name, queue, methods=["POST"])
        for name in ("analyze", "proposals", "builds")
    ]

    async def dual_step(request):
        if not settings.dual_repair:
            raise Conflict("Dual repair is not enabled")
        from .dual_repair import DualRepair

        value = await body(request)
        shape(value, {"phase": {"enum": ["investigate", "mitigate", "permanent", "verify"]}}, ["phase"])
        controller = DualRepair(monitor)
        return JSONResponse(await getattr(controller, value["phase"])(str(request.path_params["id"])))

    async def native(request):
        from .dual_repair import DualRepair

        value, status = await DualRepair(monitor).native(
            str(request.path_params["id"]),
            "/" + request.path_params["operation"],
            await body(request),
            request.headers.get("x-contractbridge-client-revision"),
        )
        return JSONResponse(value, status_code=status, headers={"X-ContractBridge-Translation": "bypassed"})

    routes += [
        Route(prefix + "/dual/{id:uuid}/step", dual_step, methods=["POST"]),
        Route(prefix + "/native/{id:uuid}/{operation:path}", native, methods=["POST"]),
    ]
    routes += runtime_routes(monitor, body, revision)
    routes += hook_routes(monitor)
    app = Starlette(routes=routes)
    app.state.workflow = workflow
    app.state.monitor = monitor

    async def guard(request: Request, call_next):
        request_id = str(uuid4())
        if request.url.path not in {prefix + "/health", prefix + "/openapi.json"}:
            authorization = request.headers.get("authorization", "").encode()
            operator = secrets.compare_digest(authorization, ("Bearer " + settings.operator_token).encode())
            parts = request.url.path.split("/")
            scoped = (
                request.method == "POST"
                and len(parts) >= 6
                and parts[3] in ("gateway", "native")
                and gateway_authorized(
                    settings.gateway_secret, parts[4], request.headers.get("authorization", "")
                )
            )
            webhook = request.method == "POST" and request.url.path in {
                prefix + "/hooks/sentry",
                prefix + "/hooks/github",
            }
            if not operator and not scoped and not webhook:
                return problem(401, "Operator or connection-scoped authentication required")
        try:
            response = await call_next(request)
        except NotFound as exc:
            response = problem(404, str(exc))
        except Conflict as exc:
            response = problem(409, str(exc))
        except ValueError, TypeError, KeyError:
            response = problem(
                422, "Invalid input; inspect the schema, required fields, or selected operation"
            )
        except httpx.HTTPError as exc:
            capture_failure(exc, "api.upstream")
            response = problem(503, "External service unavailable")
        except Exception as exc:
            capture_failure(exc, "api.unhandled")
            response = problem(500, "Operation failed; no activation was approved")
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=guard)
    return app

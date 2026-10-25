"""Fixed runtime included in exported adapters. No model or orchestration imports."""

from __future__ import annotations

import secrets
import sys
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .contracts import Contract, ContractError, validate_payload
from .engine import inspect_plan, transform
from .models import Plan
from .util import redact, strict_loads


def problem(status: int, detail: str) -> JSONResponse:
    return JSONResponse(
        {"type": "about:blank", "title": detail, "status": status},
        status_code=status,
        media_type="application/problem+json",
    )


def create_proxy(
    bundle: dict, upstream: str, allowed: list[str], transport=None, inspect_token: str = ""
) -> Starlette:
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError("Generated adapters require Python 3.14")
    parsed = urlsplit(upstream)
    if (
        upstream not in allowed
        or parsed.scheme not in ("http", "https")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Upstream is not explicitly allowlisted")
    plan = Plan.parse(bundle["plan"])
    old = Contract.select(bundle["oldSpec"], plan.sourceOperation["path"])
    new = Contract.select(bundle["newSpec"], plan.targetOperation["path"])
    if any(not i.reviewRequired for i in inspect_plan(plan, old, new)):
        raise ValueError("Adapter contains an invalid plan")

    async def health(_request):
        return JSONResponse({"status": "UP", "inference": "none-at-runtime"})

    async def forward(request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return problem(415, "Only application/json is supported")
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > 1_000_000:
                return problem(413, "Request body exceeds 1 MB")
        try:
            value = strict_loads(raw)
            validate_payload(old.request, value)
        except ValueError, TypeError:
            return problem(400, "Request violates the legacy contract")
        try:
            mapped = transform(value, plan.requestMappings)
            validate_payload(new.request, mapped)
            async with httpx.AsyncClient(
                transport=transport, timeout=httpx.Timeout(5, connect=2), follow_redirects=False
            ) as client:
                async with client.stream("POST", upstream.rstrip("/") + new.path, json=mapped) as response:
                    if response.status_code != plan.http["upstreamSuccessStatus"]:
                        return problem(502, "Upstream returned an unmapped HTTP status")
                    if response.headers.get("content-type", "").split(";")[0] != "application/json":
                        return problem(502, "Upstream returned an unsupported media type")
                    body = b""
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > 1_000_000:
                            return problem(502, "Upstream body exceeds 1 MB")
            result = strict_loads(body)
            validate_payload(new.response, result)
            output = transform(result, plan.responseMappings)
            validate_payload(old.response, output)
            if inspect_token and secrets.compare_digest(
                request.headers.get("x-bridge-inspect", "").encode(), inspect_token.encode()
            ):
                return JSONResponse(
                    {
                        "clientRequest": redact(value),
                        "upstreamRequest": redact(mapped),
                        "upstreamResponse": redact(result),
                        "clientResponse": redact(output),
                        "upstreamStatus": response.status_code,
                        "clientStatus": plan.http["clientSuccessStatus"],
                        "observed": True,
                    },
                    status_code=plan.http["clientSuccessStatus"],
                )
            return JSONResponse(output, status_code=plan.http["clientSuccessStatus"])
        except httpx.TimeoutException:
            return problem(504, "Upstream timed out; request was not retried")
        except httpx.HTTPError, ContractError, ValueError, TypeError, KeyError:
            return problem(502, "Upstream or transformation failed validation")

    return Starlette(routes=[Route("/health", health), Route(old.path, forward, methods=["POST"])])

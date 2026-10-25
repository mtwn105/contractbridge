"""Loopback-published ingress; adapters remain on the isolated adapter network."""

from __future__ import annotations

from uuid import UUID

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .runtime import problem


def create_ingress(transport=None):
    async def forward(request: Request):
        parts = request.path_params["path"].split("/")
        try:
            build_id = str(UUID(parts[0]))
            if build_id != parts[0] or len(parts) < 2:
                return problem(404, "Unknown bridge route")
        except ValueError:
            return problem(404, "Unknown bridge route")
        host = "cb-proxy-" + build_id
        path = "/" + "/".join(parts[1:])
        # Hostnames are derived from a canonical UUID. No URL or headers
        # from a caller can choose another destination or enable private inspection.
        if request.url.query:
            return problem(400, "Query mappings are unsupported")
        data = b""
        async for chunk in request.stream():
            data += chunk
            if len(data) > 1_000_000:
                return problem(413, "Request exceeds 1 MB")
        headers = {"content-type": request.headers.get("content-type", "application/json")}
        try:
            async with httpx.AsyncClient(transport=transport, timeout=10, follow_redirects=False) as client:
                async with client.stream(
                    request.method, f"http://{host}:8080{path}", content=data, headers=headers
                ) as upstream:
                    output = b""
                    async for chunk in upstream.aiter_bytes():
                        output += chunk
                        if len(output) > 1_000_000:
                            return problem(502, "Upstream exceeds response limit")
                    return Response(
                        output,
                        status_code=upstream.status_code,
                        headers={"Content-Type": upstream.headers.get("content-type", "application/json")},
                    )
        except httpx.TimeoutException:
            return problem(504, "Bridge timed out; request was not retried")
        except httpx.HTTPError:
            return problem(502, "Bridge is unavailable")

    async def health(_request):
        return JSONResponse({"status": "UP"})

    return Starlette(
        routes=[Route("/health", health), Route("/{path:path}", forward, methods=["GET", "POST"])]
    )

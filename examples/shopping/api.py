"""Small synthetic storefront API. Orders always use the unchanged v1 contract."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from contractbridge.models import check_shape
from contractbridge.telemetry import capture_failure, initialize
from contractbridge.util import strict_loads
from examples.shopping.client import Client

from .settings import CONNECTION_ID as SHOP_CONNECTION_ID
from .settings import OPERATION as SHOP_OPERATION

PRODUCTS = [
    {
        "sku": "sku-blue",
        "name": "Everyday notebook",
        "description": "A quiet place for your next good idea.",
        "price": 999,
        "color": "blue",
    },
    {
        "sku": "sku-red",
        "name": "Canvas pencil case",
        "description": "Keep your favorite tools together.",
        "price": 999,
        "color": "coral",
    },
    {
        "sku": "sku-green",
        "name": "Pocket folio",
        "description": "For loose notes worth holding on to.",
        "price": 999,
        "color": "green",
    },
]
CART_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["sku", "quantity"],
                "properties": {
                    "sku": {"enum": [p["sku"] for p in PRODUCTS]},
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 10},
                },
            },
        }
    },
}


@dataclass(frozen=True)
class ShoppingSettings:
    api_token: str
    gateway_token: str
    gateway_url: str
    contract_file: str
    commit: str = "development"
    synthetic_probes: bool = False
    sentry_dsn: str = ""

    @classmethod
    def environment(cls):
        from pathlib import Path

        root = Path(os.getenv("CONTRACTBRIDGE_ROOT", Path(__file__).resolve().parents[2]))
        return cls(
            os.getenv("SHOP_API_TOKEN", ""),
            os.getenv("SHOP_GATEWAY_TOKEN", ""),
            os.getenv(
                "SHOP_GATEWAY_URL",
                "http://control-plane:8080/api/v1/gateway/" + SHOP_CONNECTION_ID + SHOP_OPERATION,
            ),
            str(root / "examples/orders-v1-to-v2/openapi-v1.yaml"),
            os.getenv("APP_COMMIT_SHA", "development"),
            os.getenv("ALLOW_SYNTHETIC_PROBES", "0") == "1",
            os.getenv("APP_SENTRY_DSN", ""),
        )


def create_app(settings=None, *, transport=None):
    settings = settings or ShoppingSettings.environment()
    if (
        min(len(settings.api_token), len(settings.gateway_token)) < 24
        or settings.api_token == settings.gateway_token
    ):
        raise RuntimeError(
            "Configure distinct SHOP_API_TOKEN and SHOP_GATEWAY_TOKEN values of at least 24 characters"
        )
    url = urlsplit(settings.gateway_url)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise RuntimeError("SHOP_GATEWAY_URL must be a fixed server-configured HTTP URL")
    integration = Client(settings.contract_file)
    initialize(settings.sentry_dsn)

    async def health(_request):
        return JSONResponse({"status": "UP"})

    async def catalog(_request):
        return JSONResponse({"products": PRODUCTS, "currency": "USD", "synthetic": True})

    async def checkout(request):
        try:
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                raise ValueError("Expected JSON")
            raw = b""
            async for chunk in request.stream():
                raw += chunk
                if len(raw) > 16_384:
                    return JSONResponse({"title": "Cart is too large"}, status_code=413)
            cart = strict_loads(raw)
            check_shape(CART_SCHEMA, cart)
            if len({item["sku"] for item in cart["items"]}) != len(cart["items"]):
                raise ValueError("Duplicate product")
        except ValueError, TypeError, KeyError:
            return JSONResponse(
                {"title": "Choose known products and quantities from 1 to 10"}, status_code=422
            )
        result, status, _ = await place(cart, "demo-customer")
        return JSONResponse(result, status_code=status)

    async def place(cart, customer):
        payload = integration.encode({"customerId": customer, "items": cart["items"]})
        endpoint = settings.gateway_url
        headers = {"Authorization": "Bearer " + settings.gateway_token}
        if integration.native:
            endpoint = (
                url.scheme + "://" + url.netloc + "/api/v1/native/" + SHOP_CONNECTION_ID + integration.path
            )
            headers["X-ContractBridge-Client-Revision"] = integration.repair_hash
        # Exactly one POST. The permanent client uses the untranslated native
        # operation, so its success cannot be masked by the temporary bridge.
        try:
            async with httpx.AsyncClient(
                transport=transport, timeout=15, follow_redirects=False, trust_env=False
            ) as client:
                async with client.stream("POST", endpoint, json=payload, headers=headers) as response:
                    data = b""
                    async for chunk in response.aiter_bytes():
                        data += chunk
                        if len(data) > 16_384:
                            raise ValueError("Invalid order response")
                    value = strict_loads(data)
                    if response.status_code != integration.new.status:
                        incident_id = response.headers.get("X-ContractBridge-Incident")
                        if incident_id:
                            capture_failure(
                                RuntimeError("A monitored API integration failed"),
                                "application.integration",
                                {
                                    "contractbridge.connection_id": SHOP_CONNECTION_ID,
                                    "contractbridge.incident_id": incident_id,
                                },
                            )
                        return (
                            {
                                "title": "Checkout is temporarily unavailable. Your cart is saved.",
                                "requestRetried": False,
                            },
                            502,
                            None,
                        )
                    value = integration.decode(value, response.status_code)
                    if value["totalAmount"] != sum(p["quantity"] * 999 for p in cart["items"]):
                        raise ValueError("Provider total differs from catalog")
                    return value, 201, response.headers.get("X-ContractBridge-Translation")
        except httpx.HTTPError, ValueError, KeyError, TypeError:
            return (
                {
                    "title": "We could not confirm your order. The request was not retried.",
                    "requestRetried": False,
                },
                503,
                None,
            )

    async def release(_request):
        return JSONResponse(
            {
                "commit": settings.commit,
                "repairHash": integration.repair_hash,
                "mode": "native" if integration.native else "legacy",
            }
        )

    async def probe(_request):
        if not settings.synthetic_probes or not integration.native:
            return JSONResponse(
                {"title": "Synthetic bypass verification is not authorized for this client"}, status_code=409
            )
        result, status, translation = await place(
            {"items": [{"sku": "sku-blue", "quantity": 1}]}, "contractbridge-synthetic-canary"
        )
        if status != 201 or translation != "bypassed":
            return JSONResponse(
                {"title": "The actual repaired client failed its bypass probe"}, status_code=409
            )
        return JSONResponse(
            {
                "commit": settings.commit,
                "repairHash": integration.repair_hash,
                "translation": translation,
                "status": result["status"],
            }
        )

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/catalog", catalog),
            Route("/release", release),
            Route("/probe", probe, methods=["POST"]),
            Route("/checkout", checkout, methods=["POST"]),
        ]
    )

    async def guard(request, call_next):
        if request.url.path != "/health" and not secrets.compare_digest(
            request.headers.get("authorization", "").encode(), ("Bearer " + settings.api_token).encode()
        ):
            return JSONResponse({"title": "Shopping service authentication required"}, status_code=401)
        result = await call_next(request)
        result.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return result

    app.add_middleware(BaseHTTPMiddleware, dispatch=guard)
    return app

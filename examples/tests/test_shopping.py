import json
from pathlib import Path

import httpx
import pytest

from examples.shopping.api import ShoppingSettings, create_app
from examples.shopping.settings import CONNECTION_ID as SHOP_CONNECTION_ID

ROOT = Path(__file__).resolve().parents[2]
API_TOKEN = "shopping-api-test-secret-at-least-32-characters"
GATEWAY_TOKEN = "shopping-gateway-test-secret-at-least-32-characters"
OPERATOR_TOKEN = "operator-test-secret-at-least-32-characters"
CART = {"items": [{"sku": "sku-blue", "quantity": 2}]}


def shopping(transport):
    return create_app(
        ShoppingSettings(
            API_TOKEN,
            GATEWAY_TOKEN,
            "http://control/api/v1/gateway/" + SHOP_CONNECTION_ID + "/v1/orders",
            str(ROOT / "examples/orders-v1-to-v2/openapi-v1.yaml"),
        ),
        transport=transport,
    )


async def test_checkout_calls_fixed_legacy_gateway_once_and_uses_only_scoped_token():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer " + GATEWAY_TOKEN
        assert json.loads(request.content) == {"customerId": "demo-customer", **CART}
        assert str(request.url) == "http://control/api/v1/gateway/" + SHOP_CONNECTION_ID + "/v1/orders"
        return httpx.Response(201, json={"orderId": "ord-test", "status": "CONFIRMED", "totalAmount": 1998})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=shopping(httpx.MockTransport(handle))),
        base_url="http://shop",
        headers={"Authorization": "Bearer " + API_TOKEN},
    ) as client:
        response = await client.post("/checkout", json=CART)
    assert response.status_code == 201 and response.json()["totalAmount"] == 1998
    assert len(calls) == 1


@pytest.mark.parametrize(
    "cart",
    [
        {},
        {"items": []},
        {"items": [{"sku": "unknown", "quantity": 1}]},
        {"items": [{"sku": "sku-blue", "quantity": True}]},
        {"items": [{"sku": "sku-blue", "quantity": 11}]},
        {"items": [{"sku": "sku-blue", "quantity": 1, "price": 1}]},
        {**CART, "customerId": "private-user"},
        {"items": [{"sku": "sku-blue", "quantity": 1}, {"sku": "sku-blue", "quantity": 1}]},
    ],
)
async def test_invalid_or_price_tampered_cart_never_reaches_upstream(cart):
    def unexpected(_request):
        raise AssertionError("Invalid cart reached upstream")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=shopping(httpx.MockTransport(unexpected))),
        base_url="http://shop",
        headers={"Authorization": "Bearer " + API_TOKEN},
    ) as client:
        response = await client.post("/checkout", json=cart)
    assert response.status_code == 422


async def test_provider_failure_is_not_replayed_and_private_error_body_is_not_returned():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(404, json={"secret": "private-provider-error-body"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=shopping(httpx.MockTransport(handle))),
        base_url="http://shop",
        headers={"Authorization": "Bearer " + API_TOKEN},
    ) as client:
        response = await client.post("/checkout", json=CART)
    assert response.status_code == 502 and response.json()["requestRetried"] is False
    assert "private-provider-error-body" not in response.text and len(calls) == 1


async def test_unknown_order_outcome_is_not_claimed_as_success_or_retried():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("private transport details", request=request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=shopping(httpx.MockTransport(handle))),
        base_url="http://shop",
        headers={"Authorization": "Bearer " + API_TOKEN},
    ) as client:
        response = await client.post("/checkout", json=CART)
    assert response.status_code == 503 and response.json()["requestRetried"] is False
    assert "private transport details" not in response.text and len(calls) == 1


async def test_provider_total_must_match_server_catalog():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(201, json={"orderId": "ord-test", "status": "CONFIRMED", "totalAmount": 1})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=shopping(transport)),
        base_url="http://shop",
        headers={"Authorization": "Bearer " + API_TOKEN},
    ) as client:
        assert (await client.post("/checkout", json=CART)).status_code == 503


async def test_shop_api_requires_service_authentication_and_exposes_no_customer_data():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=shopping(None)), base_url="http://shop"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/catalog")).status_code == 401
        assert (await client.post("/checkout", json=CART)).status_code == 401
        r = await client.get("/catalog", headers={"Authorization": "Bearer " + API_TOKEN})
    assert r.status_code == 200 and len(r.json()["products"]) == 3 and r.json()["synthetic"] is True

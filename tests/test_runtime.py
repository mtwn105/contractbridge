import json

import httpx
import pytest

from contractbridge.runtime import create_proxy


@pytest.mark.parametrize("index", [0, 1, 2, 3])
async def test_real_asgi_proxy_golden_and_no_duplicate_post(bundle, index):
    fixture = bundle["fixtures"][index]
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        assert calls[-1] == fixture["upstreamRequest"]
        return httpx.Response(fixture["upstreamStatus"], json=fixture["upstreamResponse"])
    proxy = create_proxy(bundle, "http://demo", ["http://demo"], httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://proxy") as client:
        result = await client.post("/v1/orders", json=fixture["clientRequest"])
    assert result.status_code == fixture["clientStatus"]
    assert len(calls) == (0 if fixture["expectation"] == "INVALID_REQUEST" else 1)
    if fixture["expectation"] == "SUCCESS":
        assert result.json() == fixture["clientResponse"]


@pytest.mark.parametrize("failure,expected", [("timeout", 504), ("connect", 502), ("status", 502), ("html", 502)])
async def test_upstream_failures_do_not_leak_or_retry(bundle, failure, expected):
    calls = []
    def handler(request):
        calls.append(1)
        if failure == "timeout":
            raise httpx.ReadTimeout("private timeout context")
        if failure == "connect":
            raise httpx.ConnectError("private connection context")
        return httpx.Response(500, json={"private": "secret"}) if failure == "status" else httpx.Response(201, text="private html")
    proxy = create_proxy(bundle, "http://demo", ["http://demo"], httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://proxy") as client:
        response = await client.post("/v1/orders", json=bundle["fixtures"][0]["clientRequest"])
    assert response.status_code == expected
    assert len(calls) == 1
    assert "private" not in response.text


def test_upstream_and_host_injection_rejected(bundle):
    with pytest.raises(ValueError):
        create_proxy(bundle, "http://169.254.169.254", ["http://demo"])
    bundle["plan"]["targetOperation"]["path"] = "http://evil"
    with pytest.raises(ValueError):
        create_proxy(bundle, "http://demo", ["http://demo"])

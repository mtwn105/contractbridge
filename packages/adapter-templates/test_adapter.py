import json
from pathlib import Path

import httpx
import pytest
from bridge_core.runtime import create_proxy

BUNDLE = json.loads((Path(__file__).parent / "bundle.json").read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", BUNDLE["fixtures"], ids=lambda f: f["name"])
async def test_golden_http(fixture):
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        assert request.url.path == BUNDLE["plan"]["targetOperation"]["path"]
        assert calls[-1] == fixture["upstreamRequest"]
        return httpx.Response(fixture["upstreamStatus"], json=fixture["upstreamResponse"])

    app = create_proxy(BUNDLE, "http://demo", ["http://demo"], httpx.MockTransport(handle))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
        response = await client.post(BUNDLE["plan"]["sourceOperation"]["path"], json=fixture["clientRequest"])
    assert response.status_code == fixture["clientStatus"]
    assert len(calls) == (0 if fixture["expectation"] == "INVALID_REQUEST" else 1)
    if fixture["expectation"] == "SUCCESS":
        assert response.json() == fixture["clientResponse"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [("500", 502), ("timeout", 504), ("unavailable", 502)])
async def test_upstream_errors_are_not_success(failure, expected):
    fixture = next(f for f in BUNDLE["fixtures"] if f["expectation"] == "SUCCESS")
    calls = []

    def handle(request):
        calls.append(1)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        if failure == "unavailable":
            raise httpx.ConnectError("synthetic unavailable")
        return httpx.Response(500, json={"private": "must not leak"})

    app = create_proxy(BUNDLE, "http://demo", ["http://demo"], httpx.MockTransport(handle))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
        response = await client.post(BUNDLE["plan"]["sourceOperation"]["path"], json=fixture["clientRequest"])
    assert response.status_code == expected
    assert len(calls) == 1
    assert "private" not in response.text


def test_generated_typed_dtos_import():
    import dtos
    assert dtos.ClientRequest.__dataclass_fields__
    assert dtos.ClientResponse.__dataclass_fields__

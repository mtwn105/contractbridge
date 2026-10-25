"""Exercise the permanent client against independent supplied fixtures."""

import json
from pathlib import Path

import pytest
from permanent_client import Client

BUNDLE = json.loads(Path("bundle.json").read_text())


@pytest.mark.parametrize("fixture", BUNDLE["fixtures"], ids=lambda f: f["name"])
def test_permanent_application_client(fixture):
    client = Client()
    if fixture["expectation"] == "INVALID_REQUEST":
        with pytest.raises(ValueError):
            client.encode(fixture["clientRequest"])
    elif fixture["expectation"] == "UPSTREAM_INVALID":
        with pytest.raises(ValueError):
            client.decode(fixture["upstreamResponse"], fixture["upstreamStatus"])
    else:
        assert client.encode(fixture["clientRequest"]) == fixture["upstreamRequest"]
        assert (
            client.decode(fixture["upstreamResponse"], fixture["upstreamStatus"]) == fixture["clientResponse"]
        )

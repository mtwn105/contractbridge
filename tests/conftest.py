import copy
import json
from pathlib import Path

import pytest

from contractbridge.contracts import Contract, parse_spec
from contractbridge.models import Fixture, Plan

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples/orders-v1-to-v2"


@pytest.fixture
def bundle():
    return {"oldSpec": parse_spec((DEMO / "openapi-v1.yaml").read_text()),
            "newSpec": parse_spec((DEMO / "openapi-v2.yaml").read_text()),
            "plan": json.loads((DEMO / "reference-plan.json").read_text()),
            "fixtures": json.loads((DEMO / "fixtures/golden.json").read_text())}


@pytest.fixture
def core(bundle):
    return (Plan.parse(copy.deepcopy(bundle["plan"])), Contract.select(bundle["oldSpec"]),
            Contract.select(bundle["newSpec"]), Fixture.parse_many(bundle["fixtures"]))

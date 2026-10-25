import copy
import json

import pytest

from contractbridge.analysis import structural_changes
from contractbridge.contracts import Contract, ContractError, parse_spec, validate_payload
from contractbridge.engine import inspect_plan, require_approval, transform, verify_fixtures
from contractbridge.models import Plan
from contractbridge.util import canonical, digest, inside, redact, reject_secrets, strict_loads


def test_golden_and_negative_evidence(core):
    plan, old, new, fixtures = core
    assert all(r.passed for r in verify_fixtures(fixtures, plan, old, new))
    assert transform(fixtures[1].clientRequest, plan.requestMappings)["lines"][1]["productCode"] == "sku-red"


def test_enum_review_cannot_be_overridden_by_model(bundle):
    bundle["plan"]["responseMappings"][1]["requiresHumanApproval"] = False
    issues = inspect_plan(Plan.parse(bundle["plan"]), Contract.select(bundle["oldSpec"]), Contract.select(bundle["newSpec"]))
    assert all(i.reviewRequired for i in issues)
    assert any(i.key.endswith("/semantics") for i in issues)


def test_approval_requires_every_decision_and_fixtures(core):
    plan, old, new, fixtures = core
    with pytest.raises(ValueError):
        require_approval(plan, old, new, fixtures, {})
    decisions = {i.key: "Confirmed against the synthetic provider lifecycle specification" for i in inspect_plan(plan, old, new)}
    require_approval(plan, old, new, fixtures, decisions)
    with pytest.raises(ValueError):
        require_approval(plan, old, new, [], decisions)


def test_required_field_blocks_and_constant_requires_review(bundle):
    schema = bundle["newSpec"]["paths"]["/v2/purchases"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    schema["properties"]["deliveryMethod"] = {"type": "string", "enum": ["STANDARD", "EXPRESS"]}
    schema["required"].append("deliveryMethod")
    old, new = Contract.select(bundle["oldSpec"]), Contract.select(bundle["newSpec"])
    assert any("deliveryMethod" in i.message and not i.reviewRequired for i in inspect_plan(Plan.parse(bundle["plan"]), old, new))
    bundle["plan"]["requestMappings"].append({"op": "constant", "to": "/deliveryMethod", "value": "STANDARD"})
    assert all(i.reviewRequired for i in inspect_plan(Plan.parse(bundle["plan"]), old, new))


@pytest.mark.parametrize("mutation", [
    {"op": "exec", "from": "/customerId", "to": "/buyer/id"},
    {"op": "copy", "from": "http://169.254.169.254", "to": "/buyer/id"},
    {"op": "copy", "from": "/customerId", "to": "/buyer/id", "shell": "touch evil"},
])
def test_non_ir_instructions_fail(bundle, mutation):
    bundle["plan"]["requestMappings"][0] = mutation
    with pytest.raises(ValueError):
        Plan.parse(bundle["plan"])


def test_duplicate_targets_nonexistent_pointers_and_type_changes(bundle):
    old, new = Contract.select(bundle["oldSpec"]), Contract.select(bundle["newSpec"])
    bundle["plan"]["requestMappings"].append(copy.deepcopy(bundle["plan"]["requestMappings"][0]))
    assert any("Duplicate" in i.message for i in inspect_plan(Plan.parse(bundle["plan"]), old, new))
    bundle["plan"]["requestMappings"][0]["from"] = "/missing"
    assert any("Unknown schema" in i.message for i in inspect_plan(Plan.parse(bundle["plan"]), old, new))


@pytest.mark.parametrize("text", [
    "!!python/object/apply:os.system ['touch evil']",
    "openapi: 3.0.3\ninfo: {title: x}\npaths: {}\nx: {$ref: 'http://169.254.169.254/'}",
    "openapi: 3.0.3\ninfo: {title: x}\npaths: {}\nx: &x [*x]",
    '{"openapi":"3.0.3","info":{"title":"x"},"paths":{},"paths":{}}',
])
def test_untrusted_spec_formats_rejected(text):
    with pytest.raises(ValueError):
        parse_spec(text)


def test_unknown_fields_boolean_integer_and_bounds_rejected(core):
    _, old, _, fixtures = core
    for value in (True, 0, 101, "2", None):
        request = copy.deepcopy(fixtures[0].clientRequest)
        request["items"][0]["quantity"] = value
        with pytest.raises(ContractError):
            validate_payload(old.request, request)
    request = copy.deepcopy(fixtures[0].clientRequest)
    request["evil"] = True
    with pytest.raises(ContractError):
        validate_payload(old.request, request)


@pytest.mark.parametrize("annotation,value", [("x-unit", "USD_DOLLARS"), ("x-semantic-id", "paid-instead-of-total")])
def test_same_looking_changed_meaning_blocked(core, annotation, value):
    plan, old, new, _ = core
    new.response["properties"]["pricing"]["properties"]["grandTotal"][annotation] = value
    assert any(i.key.endswith(annotation) and not i.reviewRequired for i in inspect_plan(plan, old, new))


def test_diff_is_structural_and_does_not_invent_renames(core):
    _, old, new, _ = core
    changes = structural_changes(old, new)
    assert any(c["kind"] == "ROUTE_CHANGED" for c in changes)
    assert any(c["pointer"] == "/customerId" and c["kind"] == "PROPERTY_REMOVED" for c in changes)
    assert not any(c["kind"] == "SEMANTIC_RENAME" for c in changes)


def test_redaction_paths_and_hashes(tmp_path):
    with pytest.raises(ValueError):
        reject_secrets({"Authorization": "secret"})
    assert redact({"buyer": {"id": "private"}, "customerId": "private"}) == {"buyer": {"id": "[REDACTED]"}, "customerId": "[REDACTED]"}
    with pytest.raises(ValueError):
        inside(tmp_path, "../../outside")
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    with pytest.raises(ValueError):
        canonical({"number": float("nan")})


def test_openapi31_nullable_subset(bundle):
    bundle["oldSpec"]["openapi"] = "3.1.0"
    schema = bundle["oldSpec"]["paths"]["/v1/orders"]["post"]["responses"]["201"]["content"]["application/json"]["schema"]
    schema["properties"]["orderId"]["type"] = ["string", "null"]
    parsed = Contract.select(parse_spec(json.dumps(bundle["oldSpec"])))
    value = {"orderId": None, "status": "CONFIRMED", "totalAmount": 1998}
    validate_payload(parsed.response, value)


def test_nullable_ancestor_cannot_be_read_as_guaranteed(core):
    plan, old, new, _ = core
    old.request["properties"]["customerId"]["type"] = ["string", "null"]
    assert any(not i.reviewRequired for i in inspect_plan(plan, old, new))
    new.response["properties"]["purchase"]["type"] = ["object", "null"]
    assert any("optional" in i.message for i in inspect_plan(plan, old, new))


@pytest.mark.parametrize("value", ['{"a":1,"a":2}', '{"n":NaN}', '{"n":Infinity}'])
def test_nonstandard_json_rejected(value):
    with pytest.raises(ValueError):
        strict_loads(value)

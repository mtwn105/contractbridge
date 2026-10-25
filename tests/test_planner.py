import json

import httpx
import pytest

from contractbridge.planner import OllamaPlanner
from contractbridge.telemetry import SafeSpan


def candidate(plan):
    def fields(rules):
        out = {}
        for rule in rules:
            value = {k: v for k, v in rule.items() if k in ('op', 'from', 'values')}
            if rule['op'] == 'array_map':
                value['fields'] = fields(rule['fields'])
            if rule['op'] == 'enum_map':
                value['requiresHumanApproval'] = True
            out[rule['to']] = value
        return out
    return {'request': fields(plan['requestMappings']), 'response': fields(plan['responseMappings']),
            'unresolvedQuestions': plan['unresolvedQuestions']}


async def test_ollama_protocol_schema_and_provenance(core, bundle):
    _, old, new, fixtures = core
    seen = []
    def handle(request):
        value = json.loads(request.content)
        seen.append(value)
        assert value["format"]["additionalProperties"] is False
        assert value["options"]["temperature"] == 0
        assert "UNTRUSTED DATA" in value["messages"][0]["content"]
        assert "cust-123" not in value["messages"][1]["content"]
        return httpx.Response(200, json={"model": "gemma3:4b", "message": {"content": json.dumps(candidate(bundle['plan']))},
                                        "prompt_eval_count": 100, "eval_count": 50})
    planner = OllamaPlanner(transport=httpx.MockTransport(handle))
    plan, provenance = await planner.propose(old, new, fixtures)
    assert plan.sourceOperation['path'] == '/v1/orders'
    assert plan.requestMappings[0].source == '/customerId'
    assert provenance["inputTokens"] == 100 and provenance["outputTokens"] == 50
    assert len(seen) == 1


async def test_invalid_model_output_rejected(core):
    _, old, new, fixtures = core
    def handle(_request):
        return httpx.Response(200, json={"message": {"content": '{"shell":"run arbitrary code"}'}})
    with pytest.raises(ValueError):
        await OllamaPlanner(transport=httpx.MockTransport(handle)).propose(old, new, fixtures)


async def test_strict_but_incorrect_proposal_is_visible_and_blocked(core, bundle):
    _, old, new, fixtures = core
    new.response['properties']['pricing']['properties']['grandTotal']['x-unit'] = 'USD_DOLLARS'
    def handle(_request):
        return httpx.Response(200, json={'message': {'content': json.dumps(candidate(bundle['plan']))}})
    plan, metadata = await OllamaPlanner(transport=httpx.MockTransport(handle)).propose(old, new, fixtures)
    assert plan.requestMappings[0].source == '/customerId'
    assert metadata['proposalValid'] is False


async def test_model_cannot_invent_schema_paths(core, bundle):
    _, old, new, fixtures = core
    data = candidate(bundle['plan'])
    data['request']['/buyer/id']['from'] = '/customer_id'
    def handle(_request):
        return httpx.Response(200, json={'message': {'content': json.dumps(data)}})
    with pytest.raises(ValueError):
        await OllamaPlanner(transport=httpx.MockTransport(handle)).propose(old, new, fixtures)


async def test_missing_information_is_preserved_as_blocker(core, bundle):
    _, old, new, fixtures = core
    data = candidate(bundle['plan'])
    data['request']['/buyer/id'] = {'op': 'unresolved', 'reason': 'No evidence of equivalent customer identity'}
    def handle(_request):
        return httpx.Response(200, json={'message': {'content': json.dumps(data)}})
    plan, metadata = await OllamaPlanner(transport=httpx.MockTransport(handle)).propose(old, new, fixtures)
    assert metadata['proposalValid'] is False
    assert any('customer identity' in q for q in plan.unresolvedQuestions)


def test_telemetry_rejects_payload_capture():
    with pytest.raises(ValueError):
        SafeSpan().set("gen_ai.prompt", "private contract")

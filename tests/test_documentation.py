import json
from pathlib import Path

from jsonschema import Draft202012Validator

from contractbridge.models import PLAN_SCHEMA

ROOT = Path(__file__).resolve().parents[1]


def test_exported_plan_schema_matches_runtime(bundle):
    schema = json.loads((ROOT / 'packages/plan-schema/plan.schema.json').read_text())
    assert schema == PLAN_SCHEMA
    Draft202012Validator(schema).validate(bundle['plan'])


def test_documented_control_plane_plan_references_resolve(bundle):
    doc = json.loads((ROOT / 'docs/control-plane.openapi.json').read_text())
    schema = {'$ref': '#/components/schemas/Plan', 'components': doc['components']}
    Draft202012Validator(schema).validate(bundle['plan'])
    assert '/migrations/{id}/builds/{buildId}/activate' in doc['paths']

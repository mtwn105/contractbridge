from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

POINTER = {"type": "string", "pattern": r"^(/[A-Za-z_][A-Za-z0-9_]*)+$", "maxLength": 512}
OPERATION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["method", "path"],
    "properties": {
        "method": {"const": "POST"},
        "path": {"type": "string", "pattern": r"^/[A-Za-z0-9_/-]+$", "maxLength": 256},
    },
}
RULE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["op"],
    "properties": {
        "op": {
            "enum": [
                "copy",
                "rename",
                "nest",
                "flatten",
                "array_map",
                "constant",
                "enum_map",
                "reject_if_missing",
            ]
        },
        "from": POINTER,
        "to": POINTER,
        "fields": {"type": "array", "maxItems": 100, "items": {"$ref": "#/$defs/rule"}},
        "values": {"type": "object", "maxProperties": 100, "additionalProperties": {"type": "string"}},
        "value": {},
        "requiresHumanApproval": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 1000},
    },
}
PLAN_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "$defs": {"rule": RULE_SCHEMA},
    "required": [
        "planVersion",
        "sourceOperation",
        "targetOperation",
        "requestMappings",
        "responseMappings",
        "http",
        "unresolvedQuestions",
    ],
    "properties": {
        "planVersion": {"const": "1.0"},
        "sourceOperation": OPERATION,
        "targetOperation": OPERATION,
        "requestMappings": {"type": "array", "maxItems": 100, "items": {"$ref": "#/$defs/rule"}},
        "responseMappings": {"type": "array", "maxItems": 100, "items": {"$ref": "#/$defs/rule"}},
        "http": {
            "type": "object",
            "additionalProperties": False,
            "required": ["upstreamSuccessStatus", "clientSuccessStatus"],
            "properties": {
                k: {"type": "integer", "minimum": 200, "maximum": 299}
                for k in ["upstreamSuccessStatus", "clientSuccessStatus"]
            },
        },
        "unresolvedQuestions": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 500},
        },
    },
}


def check_shape(schema: dict, value: Any) -> None:
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        e = errors[0]
        raise ValueError(f"Invalid shape at {'/'.join(map(str, e.absolute_path))}: {e.validator}")


@dataclass(frozen=True)
class Rule:
    op: str
    source: str | None = None
    to: str | None = None
    fields: list[Rule] | None = None
    values: dict[str, str] | None = None
    value: Any = None
    requiresHumanApproval: bool = False
    reason: str | None = None
    supplied: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def parse(cls, data: dict) -> Rule:
        return cls(
            op=data["op"],
            source=data.get("from"),
            to=data.get("to"),
            fields=[cls.parse(v) for v in data["fields"]] if "fields" in data else None,
            values=data.get("values"),
            value=data.get("value"),
            requiresHumanApproval=data.get("requiresHumanApproval", False),
            reason=data.get("reason"),
            supplied=frozenset(data),
        )

    def data(self) -> dict:
        out = {"op": self.op}
        for key in self.supplied - {"op"}:
            out[key] = (
                [v.data() for v in self.fields]
                if key == "fields"
                else self.source
                if key == "from"
                else getattr(self, key)
            )
        return out


@dataclass(frozen=True)
class Plan:
    sourceOperation: dict
    targetOperation: dict
    requestMappings: list[Rule]
    responseMappings: list[Rule]
    http: dict
    unresolvedQuestions: list[str]
    planVersion: str = "1.0"

    @classmethod
    def parse(cls, data: dict) -> Plan:
        check_shape(PLAN_SCHEMA, data)
        return cls(
            **{
                **data,
                "requestMappings": [Rule.parse(r) for r in data["requestMappings"]],
                "responseMappings": [Rule.parse(r) for r in data["responseMappings"]],
            }
        )

    def data(self) -> dict:
        return {
            **asdict(self),
            "requestMappings": [r.data() for r in self.requestMappings],
            "responseMappings": [r.data() for r in self.responseMappings],
        }


FIXTURE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name",
        "expectation",
        "clientRequest",
        "upstreamRequest",
        "upstreamResponse",
        "clientResponse",
        "upstreamStatus",
        "clientStatus",
    ],
    "properties": {
        "name": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,80}$"},
        "expectation": {"enum": ["SUCCESS", "INVALID_REQUEST", "UPSTREAM_INVALID"]},
        **{k: {} for k in ["clientRequest", "upstreamRequest", "upstreamResponse", "clientResponse"]},
        **{
            k: {"type": "integer", "minimum": 100, "maximum": 599} for k in ["upstreamStatus", "clientStatus"]
        },
    },
}


@dataclass(frozen=True)
class Fixture:
    name: str
    expectation: str
    clientRequest: Any
    upstreamRequest: Any
    upstreamResponse: Any
    clientResponse: Any
    upstreamStatus: int
    clientStatus: int

    @classmethod
    def parse_many(cls, value: Any) -> list[Fixture]:
        check_shape({"type": "array", "minItems": 1, "maxItems": 100, "items": FIXTURE_SCHEMA}, value)
        if len({v["name"] for v in value}) != len(value):
            raise ValueError("Fixture names must be unique")
        return [cls(**v) for v in value]


@dataclass(frozen=True)
class Issue:
    key: str
    message: str
    reviewRequired: bool = False


@dataclass(frozen=True)
class TestResult:
    name: str
    passed: bool
    detail: str

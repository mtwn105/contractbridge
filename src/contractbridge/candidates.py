"""Operation-specific model grammar. Models choose meaning; schemas constrain addresses."""

from __future__ import annotations

from .contracts import Contract, schema_type
from .engine import compatible, nullable
from .models import Plan, check_shape


def source_fields(schema: dict, prefix="") -> list[tuple[str, dict]]:
    out = []
    for name in schema.get("required", []):
        child = schema["properties"][name]
        path = prefix + "/" + name
        out.append((path, child))
        if schema_type(child) == "object" and not nullable(child):
            out += source_fields(child, path)
    return out


def record(properties: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def fields_schema(source: dict, destination: dict, prefix="", depth=0) -> dict:
    slots = {}
    available = source_fields(source)
    for name, target in destination["properties"].items():
        path = prefix + "/" + name
        if schema_type(target) == "object" and target["properties"]:
            slots.update(fields_schema(source, target, path, depth)["properties"])
            continue
        options = []
        copies = [p for p, s in available if compatible(s, target)]
        if copies:
            options.append(record({"op": {"const": "copy"}, "from": {"enum": copies}}))
        if schema_type(target) == "string" and "enum" in target:
            for origin, candidate in available:
                if schema_type(candidate) == "string" and "enum" in candidate and not nullable(candidate):
                    options.append(
                        record(
                            {
                                "op": {"const": "enum_map"},
                                "from": {"const": origin},
                                "values": record({v: {"enum": target["enum"]} for v in candidate["enum"]}),
                                "requiresHumanApproval": {"const": True},
                            }
                        )
                    )
        if schema_type(target) == "array" and schema_type(target["items"]) == "object" and depth < 6:
            for origin, candidate in available:
                if (
                    schema_type(candidate) != "array"
                    or schema_type(candidate["items"]) != "object"
                    or nullable(candidate)
                ):
                    continue
                if any(
                    k in target and (k not in candidate or candidate[k] < target[k]) for k in ["minItems"]
                ):
                    continue
                if any(
                    k in target and (k not in candidate or candidate[k] > target[k]) for k in ["maxItems"]
                ):
                    continue
                options.append(
                    record(
                        {
                            "op": {"const": "array_map"},
                            "from": {"const": origin},
                            "fields": fields_schema(candidate["items"], target["items"], depth=depth + 1),
                        }
                    )
                )
        # Missing information is representable without inventing defaults. It will
        # become a missing-coverage blocker in the canonical plan, never an approval.
        options.append(
            record(
                {
                    "op": {"const": "unresolved"},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 300},
                }
            )
        )
        slots[path] = {"anyOf": options}
    return record(slots)


def candidate_schema(old: Contract, new: Contract) -> dict:
    return record(
        {
            "request": fields_schema(old.request, new.request),
            "response": fields_schema(new.response, old.response),
            "unresolvedQuestions": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 500},
            },
        }
    )


def assemble(candidate: dict, old: Contract, new: Contract, schema: dict) -> Plan:
    check_shape(schema, candidate)
    questions = list(candidate["unresolvedQuestions"])

    def rules(fields: dict, direction: str):
        out = []
        for destination, choice in fields.items():
            if choice["op"] == "unresolved":
                questions.append(f"{direction} {destination}: {choice['reason']}")
                continue
            rule = {**choice, "to": destination}
            if choice["op"] == "array_map":
                rule["fields"] = rules(choice["fields"], direction + destination + "[]")
            out.append(rule)
        return out

    request = rules(candidate["request"], "request")
    response = rules(candidate["response"], "response")
    if len(questions) > 20:
        raise ValueError("Too many unresolved mappings; narrow the selected operation")
    return Plan.parse(
        {
            "planVersion": "1.0",
            "sourceOperation": {"method": "POST", "path": old.path},
            "targetOperation": {"method": "POST", "path": new.path},
            "requestMappings": request,
            "responseMappings": response,
            "http": {"upstreamSuccessStatus": new.status, "clientSuccessStatus": old.status},
            "unresolvedQuestions": questions,
        }
    )

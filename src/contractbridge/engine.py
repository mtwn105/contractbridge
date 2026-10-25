from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any

from .contracts import Contract, ContractError, schema_at, schema_type, segments, validate_payload
from .models import Fixture, Issue, Plan, Rule, TestResult


def read_pointer(value: Any, pointer: str | None) -> Any:
    for key in segments(pointer):
        if not isinstance(value, dict) or key not in value:
            raise ContractError(f"Mapping input missing: {pointer}")
        value = value[key]
    return value


def write_pointer(output: dict, pointer: str, value: Any) -> None:
    keys = segments(pointer)
    for key in keys[:-1]:
        output = output.setdefault(key, {})
        if not isinstance(output, dict):
            raise ContractError("Overlapping destination writes")
    if keys[-1] in output:
        raise ContractError("Duplicate destination write")
    output[keys[-1]] = copy.deepcopy(value)


def transform(value: Any, rules: list[Rule]) -> dict:
    output = {}
    for rule in rules:
        source = read_pointer(value, rule.source) if rule.op != "constant" else None
        match rule.op:
            case "copy" | "rename" | "nest" | "flatten":
                result = source
            case "constant":
                result = rule.value
            case "array_map":
                if not isinstance(source, list):
                    raise ContractError("array_map requires an array")
                result = [transform(v, rule.fields) for v in source]
            case "enum_map":
                if not isinstance(source, str) or source not in rule.values:
                    raise ContractError("Unmapped enum value")
                result = rule.values[source]
            case "reject_if_missing":
                if source is None:
                    raise ContractError("Required value is null")
                continue
            case _:
                raise ContractError("Unsupported operation")
        write_pointer(output, rule.to, result)
    return output


def nullable(schema: dict) -> bool:
    return isinstance(schema.get("type"), list) and "null" in schema["type"]


def compatible(source: dict, target: dict) -> bool:
    if schema_type(source) != schema_type(target) or (nullable(source) and not nullable(target)):
        return False
    for key in ("minimum", "minLength", "minItems"):
        if key in target and (key not in source or source[key] < target[key]):
            return False
    for key in ("maximum", "maxLength", "maxItems"):
        if key in target and (key not in source or source[key] > target[key]):
            return False
    if "enum" in target and ("enum" not in source or any(v not in target["enum"] for v in source["enum"])):
        return False
    if schema_type(source) == "object":
        if not set(target.get("required", [])) <= set(source.get("required", [])):
            return False
        return all(
            k in target["properties"] and compatible(s, target["properties"][k])
            for k, s in source["properties"].items()
        )
    return schema_type(source) != "array" or compatible(source["items"], target["items"])


def guaranteed(schema: dict, pointer: str) -> bool:
    for key in segments(pointer):
        if nullable(schema):
            return False
        if key not in schema.get("required", []):
            return False
        schema = schema["properties"][key]
    return True


def semantic_issues(source: dict, target: dict, key: str, pointers: str) -> list[Issue]:
    out = []
    for annotation in ("x-unit", "x-currency", "x-semantic-id"):
        if source.get(annotation) != target.get(annotation):
            out.append(
                Issue(
                    key + "/" + annotation,
                    f"Changed or missing {annotation}: copying cannot preserve meaning",
                )
            )
    if source.get("description") != target.get("description"):
        out.append(Issue(key + "/description", "Descriptions differ; confirm semantic equivalence", True))
    if schema_type(source) in ("number", "integer") and any(
        k in pointers.lower() for k in ("amount", "total", "price", "cost")
    ):
        if "x-unit" not in source or "x-unit" not in target:
            out.append(Issue(key + "/units", "Document monetary units before copying"))
    if schema_type(source) == schema_type(target) == "object":
        for name, child in source["properties"].items():
            if name in target["properties"]:
                out += semantic_issues(child, target["properties"][name], key + "/" + name, pointers + name)
    if schema_type(source) == schema_type(target) == "array":
        out += semantic_issues(source["items"], target["items"], key + "/items", pointers)
    return out


def inspect_rules(rules: list[Rule], source: dict, target: dict, context: str, depth: int = 0) -> list[Issue]:
    issues, writes = [], []
    if depth > 10:
        return [Issue(context, "Mapping nesting exceeds supported depth")]
    for index, rule in enumerate(rules):
        key = f"{context}/{index}"
        try:
            origin = schema_at(source, rule.source) if rule.op != "constant" else None
            if origin is not None and not guaranteed(source, rule.source):
                raise ContractError("Source pointer reads an optional field")
            if rule.op == "reject_if_missing":
                if rule.supplied & {"to", "fields", "values", "value"}:
                    raise ContractError("reject_if_missing only inspects a source")
                continue
            destination = schema_at(target, rule.to)
            if any(
                rule.to == p or rule.to.startswith(p + "/") or p.startswith(rule.to + "/") for p in writes
            ):
                raise ContractError("Duplicate or overlapping destination write")
            writes.append(rule.to)
            for extra, op in (("fields", "array_map"), ("values", "enum_map"), ("value", "constant")):
                if extra in rule.supplied and rule.op != op:
                    raise ContractError(f"Unexpected {extra} in mapping")
            match rule.op:
                case "constant":
                    if rule.source is not None or "value" not in rule.supplied:
                        raise ContractError("Constants need a value and no source")
                    validate_payload(destination, rule.value)
                    issues.append(
                        Issue(key + "/constant", "Confirm the default is valid business policy", True)
                    )
                case "array_map":
                    if (
                        schema_type(origin) != "array"
                        or schema_type(destination) != "array"
                        or nullable(origin)
                        or rule.fields is None
                    ):
                        raise ContractError("array_map requires non-null arrays and item mappings")
                    for bound, comparison in (
                        ("minItems", lambda a, b: a >= b),
                        ("maxItems", lambda a, b: a <= b),
                    ):
                        if bound in destination and (
                            bound not in origin or not comparison(origin[bound], destination[bound])
                        ):
                            raise ContractError("Array bounds are incompatible")
                    issues += inspect_rules(
                        rule.fields, origin["items"], destination["items"], key + "/items", depth + 1
                    )
                case "enum_map":
                    if (
                        schema_type(origin) != "string"
                        or schema_type(destination) != "string"
                        or nullable(origin)
                    ):
                        raise ContractError("enum_map requires non-null strings")
                    if (
                        "enum" not in origin
                        or "enum" not in destination
                        or rule.values is None
                        or set(rule.values) != set(origin["enum"])
                    ):
                        raise ContractError("Enum mapping must cover exactly every declared source value")
                    for value in rule.values.values():
                        validate_payload(destination, value)
                    issues.append(
                        Issue(
                            key + "/semantics",
                            "Confirm enum/lifecycle equivalence using fixture evidence",
                            True,
                        )
                    )
                case _:
                    if not compatible(origin, destination):
                        raise ContractError("Incompatible types, nullability, or constraints")
            if origin is not None:
                issues += semantic_issues(origin, destination, key, rule.source + rule.to)
            if rule.requiresHumanApproval:
                issues.append(Issue(key + "/requested", rule.reason or "Operator review requested", True))
        except (ContractError, TypeError, KeyError) as exc:
            issues.append(Issue(key, str(exc)))

    def cover(schema: dict, prefix: str = ""):
        for name in schema.get("required", []):
            path = prefix + "/" + name
            if any(path == p or path.startswith(p + "/") for p in writes):
                continue
            child = schema["properties"][name]
            if schema_type(child) == "object" and child.get("required"):
                cover(child, path)
            else:
                issues.append(
                    Issue(context + "/required" + path, f"Required destination is not supplied: {path}")
                )

    cover(target)
    return issues


def inspect_plan(plan: Plan, old: Contract, new: Contract) -> list[Issue]:
    issues = []
    if plan.sourceOperation != {"method": "POST", "path": old.path} or plan.targetOperation != {
        "method": "POST",
        "path": new.path,
    }:
        issues.append(Issue("operations", "Plan must match the selected operation pair"))
    if plan.http != {"upstreamSuccessStatus": new.status, "clientSuccessStatus": old.status}:
        issues.append(Issue("http", "Explicit success statuses must match the contracts"))
    issues += inspect_rules(plan.requestMappings, old.request, new.request, "request")
    issues += inspect_rules(plan.responseMappings, new.response, old.response, "response")
    issues += [Issue(f"question/{i}", text, True) for i, text in enumerate(plan.unresolvedQuestions)]
    return issues


def verify_fixtures(fixtures: list[Fixture], plan: Plan, old: Contract, new: Contract) -> list[TestResult]:
    results = []
    for f in fixtures:
        try:
            if f.expectation == "INVALID_REQUEST":
                if f.clientStatus != 400:
                    raise ContractError("Invalid requests must expect 400")
                try:
                    validate_payload(old.request, f.clientRequest)
                except ContractError:
                    results.append(TestResult(f.name, True, "Invalid request rejected"))
                    continue
                raise ContractError("Expected request rejection did not occur")
            validate_payload(old.request, f.clientRequest)
            rewritten = transform(f.clientRequest, plan.requestMappings)
            validate_payload(new.request, rewritten)
            if rewritten != f.upstreamRequest:
                raise ContractError("Upstream request differs from fixture")
            if f.expectation == "UPSTREAM_INVALID":
                if f.clientStatus != 502:
                    raise ContractError("Invalid upstream must expect 502")
                try:
                    validate_payload(new.response, f.upstreamResponse)
                except ContractError:
                    results.append(TestResult(f.name, True, "Invalid upstream response rejected"))
                    continue
                raise ContractError("Expected upstream rejection did not occur")
            validate_payload(new.response, f.upstreamResponse)
            output = transform(f.upstreamResponse, plan.responseMappings)
            validate_payload(old.response, output)
            if output != f.clientResponse or f.upstreamStatus != new.status or f.clientStatus != old.status:
                raise ContractError("Response or HTTP status differs from fixture")
            results.append(TestResult(f.name, True, "Golden request and response verified"))
        except (ValueError, KeyError, TypeError) as exc:
            results.append(TestResult(f.name, False, str(exc)))
    return results


def require_approval(
    plan: Plan, old: Contract, new: Contract, fixtures: list[Fixture], decisions: dict[str, str]
) -> None:
    issues = inspect_plan(plan, old, new)
    if any(not issue.reviewRequired for issue in issues):
        raise ContractError("Resolve unsupported mappings before approval")
    if not any(f.expectation == "SUCCESS" for f in fixtures) or any(
        not r.passed for r in verify_fixtures(fixtures, plan, old, new)
    ):
        raise ContractError("Approval requires passing success fixture evidence")
    if set(decisions) != {i.key for i in issues}:
        raise ContractError("Address exactly the current review issues")
    if any(not isinstance(v, str) or not 10 <= len(v.strip()) <= 1000 for v in decisions.values()):
        raise ContractError("Each issue requires a substantive operator justification")


def fixture_data(fixtures: list[Fixture]) -> list[dict]:
    return [asdict(f) for f in fixtures]

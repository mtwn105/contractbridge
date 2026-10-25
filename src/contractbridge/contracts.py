from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

import yaml
from jsonschema import Draft202012Validator

MAX_BYTES = 1_000_000
SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "nullable",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "description",
    "title",
    "example",
    "examples",
    "default",
    "deprecated",
}


class ContractError(ValueError):
    pass


class StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ContractError("YAML aliases are unsupported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
            raise ContractError("Mapping keys must be unique strings")
        return super().construct_mapping(node, deep=deep)


def limited_tree(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ContractError("Document nesting exceeds 32 levels")
    if isinstance(value, dict):
        if "$ref" in value and not re.fullmatch(
            r"#/components/schemas/[A-Za-z_][A-Za-z0-9_]*", value["$ref"]
        ):
            raise ContractError("Only document-local schema references are allowed")
        for child in value.values():
            limited_tree(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            limited_tree(child, depth + 1)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise ContractError("Only JSON-compatible values are supported")


def parse_spec(text: str) -> dict:
    if len(text.encode()) > MAX_BYTES:
        raise ContractError("Spec exceeds 1 MB")
    try:
        root = yaml.load(text, Loader=StrictLoader)
        limited_tree(root)
        json.dumps(root, allow_nan=False)
    except (yaml.YAMLError, RecursionError, TypeError, ValueError) as exc:
        raise ContractError("Invalid or unsupported YAML/JSON document") from exc
    if not isinstance(root, dict) or not re.fullmatch(r"3\.[01]\.\d+", str(root.get("openapi", ""))):
        raise ContractError("Expected OpenAPI 3.0 or 3.1")
    if not root.get("info", {}).get("title") or not isinstance(root.get("paths"), dict):
        raise ContractError("OpenAPI info.title and paths are required")
    return root


def resolve(root: dict, value: Any, seen: frozenset[str] = frozenset(), depth: int = 0) -> Any:
    if depth > 32:
        raise ContractError("Recursive/deep schemas are unsupported")
    if isinstance(value, dict):
        if "$ref" in value:
            ref = value["$ref"]
            if len(value) != 1 or ref in seen:
                raise ContractError("Reference cycles and ref siblings are unsupported")
            target = root
            try:
                for key in ref[2:].split("/"):
                    target = target[key]
            except (KeyError, TypeError) as exc:
                raise ContractError("Unresolved reference") from exc
            return resolve(root, target, seen | {ref}, depth + 1)
        return {k: resolve(root, v, seen, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(root, v, seen, depth + 1) for v in value]
    return value


def schema_type(schema: dict) -> str:
    value = schema.get("type")
    if isinstance(value, list) and len(value) == 2 and "null" in value:
        value = next(t for t in value if t != "null")
    if value not in ("object", "array", "string", "integer", "number", "boolean"):
        raise ContractError("Unsupported schema type or union")
    return value


def normalize_schema(schema: dict, depth: int = 0) -> dict:
    if not isinstance(schema, dict) or depth > 20:
        raise ContractError("Unsupported schema nesting")
    for key in schema:
        if key not in SCHEMA_KEYS and not key.startswith("x-"):
            raise ContractError(f"Unsupported schema keyword: {key}")
    out = copy.deepcopy(schema)
    kind = schema_type(out)
    if out.pop("nullable", False):
        out["type"] = [kind, "null"]
    if kind == "object":
        if out.get("additionalProperties") is not False or not isinstance(out.get("properties"), dict):
            raise ContractError("Objects require declared properties and additionalProperties: false")
        if any(k not in out["properties"] for k in out.get("required", [])):
            raise ContractError("Required property is not declared")
        out["properties"] = {k: normalize_schema(v, depth + 1) for k, v in out["properties"].items()}
    if kind == "array":
        out["items"] = normalize_schema(out.get("items"), depth + 1)
    Draft202012Validator.check_schema(out)
    return out


def validate_payload(schema: dict, value: Any) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        error = errors[0]
        pointer = "/" + "/".join(map(str, error.absolute_path))
        # Schema errors may include private values. Surface only location and rule.
        raise ContractError(f"{pointer}: violates {error.validator}")


@dataclass(frozen=True)
class Contract:
    document: dict
    path: str
    request: dict
    response: dict
    status: int

    @classmethod
    def select(cls, document: dict, path: str | None = None) -> Contract:
        paths = document.get("paths", {})
        if path is None:
            candidates = [p for p, op in paths.items() if "post" in op]
            if len(candidates) != 1:
                raise ContractError("Explicitly select one POST operation")
            path = candidates[0]
        if not re.fullmatch(r"/[A-Za-z0-9_/-]+", path) or "//" in path:
            raise ContractError("Only literal relative paths are supported")
        try:
            item, operation = paths[path], paths[path]["post"]
            if (
                document.get("security")
                or operation.get("security")
                or item.get("parameters")
                or operation.get("parameters")
            ):
                raise ContractError("UNSUPPORTED: authentication or parameter mapping")
            body = operation["requestBody"]
            if body.get("required") is not True or set(body["content"]) != {"application/json"}:
                raise ContractError("Only required application/json request bodies are supported")
            statuses = [int(s) for s in operation["responses"] if re.fullmatch(r"2\d\d", str(s))]
            if len(statuses) != 1:
                raise ContractError("Exactly one success response is required")
            content = operation["responses"][str(statuses[0])]["content"]
            if set(content) != {"application/json"}:
                raise ContractError("Only JSON responses are supported")
            request = normalize_schema(resolve(document, body["content"]["application/json"]["schema"]))
            response = normalize_schema(resolve(document, content["application/json"]["schema"]))
            if schema_type(request) != "object" or schema_type(response) != "object":
                raise ContractError("Payload roots must be objects")
            return cls(document, path, request, response, statuses[0])
        except (KeyError, TypeError) as exc:
            raise ContractError("Incomplete or unsupported operation") from exc


def segments(pointer: str | None) -> list[str]:
    if (
        not isinstance(pointer, str)
        or len(pointer) > 512
        or not re.fullmatch(r"(/[A-Za-z_][A-Za-z0-9_]*)+", pointer)
    ):
        raise ContractError("Unsupported JSON pointer")
    return pointer[1:].split("/")


def schema_at(schema: dict, pointer: str) -> dict:
    for key in segments(pointer):
        if schema_type(schema) != "object" or key not in schema["properties"]:
            raise ContractError(f"Unknown schema pointer: {pointer}")
        schema = schema["properties"][key]
    return schema

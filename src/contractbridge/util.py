from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def strict_loads(value: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    def invalid(_value):
        raise ValueError("Non-finite JSON numbers are unsupported")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        if any(re.search(r"authorization|cookie|password|secret|token|api.?key", k, re.I) for k in value):
            raise ValueError("Remove credentials and secret fields before importing fixtures")
        for item in value.values():
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "[REDACTED]"
            if re.search(
                r"(^id$|id$|authorization|cookie|password|secret|token|api.?key|email|phone|address)", k, re.I
            )
            else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path escapes artifact root")
    return path

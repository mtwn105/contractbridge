from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path

from .contracts import Contract, schema_type


def structural_changes(old: Contract, new: Contract) -> list[dict]:
    changes = []
    if old.path != new.path:
        changes.append(
            {
                "direction": "route",
                "pointer": old.path,
                "kind": "ROUTE_CHANGED",
                "detail": f"Selected target {new.path}; semantic equivalence is not inferred",
            }
        )

    def compare(a: dict, b: dict, direction: str, path: str = ""):
        def add(pointer, kind, detail):
            changes.append({"direction": direction, "pointer": pointer, "kind": kind, "detail": detail})

        if schema_type(a) != schema_type(b):
            add(path, "TYPE_CHANGED", f"{schema_type(a)} → {schema_type(b)}")
            return
        if schema_type(a) == "object":
            for key, value in a["properties"].items():
                if key not in b["properties"]:
                    add(path + "/" + key, "PROPERTY_REMOVED", "Absent from new contract; no rename inferred")
                else:
                    compare(value, b["properties"][key], direction, path + "/" + key)
            for key in b["properties"].keys() - a["properties"].keys():
                add(path + "/" + key, "PROPERTY_ADDED", "New property")
            for key in set(b.get("required", [])) - set(a.get("required", [])):
                add(
                    path + "/" + key, "REQUIRED_ADDED", "Required property needs an explicit source or policy"
                )
        if schema_type(a) == "array":
            compare(a["items"], b["items"], direction, path + "/items")
        for constraint in (
            "enum",
            "minimum",
            "maximum",
            "minItems",
            "maxItems",
            "x-unit",
            "x-semantic-id",
            "description",
        ):
            if a.get(constraint) != b.get(constraint):
                add(path, "CONSTRAINT_CHANGED", constraint + " changed")

    compare(old.request, new.request, "request")
    compare(old.response, new.response, "response")
    return sorted(changes, key=lambda c: (c["direction"], c["pointer"], c["kind"]))


class OasDiff:
    def __init__(self, binary: str = "oasdiff"):
        self.binary = binary

    async def analyze(self, old: Contract, new: Contract) -> dict:
        try:
            version_process = await asyncio.create_subprocess_exec(
                self.binary, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            version, _ = await asyncio.wait_for(version_process.communicate(), timeout=5)
            if version_process.returncode or not re.search(rb"\bv?1\.34\.1\b", version):
                raise RuntimeError("ContractBridge requires the pinned oasdiff 1.34.1 executable")
        except FileNotFoundError:
            raise RuntimeError("oasdiff is unavailable; run scripts/bootstrap.sh") from None

        # Feed a selected, fully resolved, description-free subset. The engine cannot
        # traverse references in unused components or contact URLs hidden in the input.
        def bounded(api):
            return {
                "openapi": "3.1.0",
                "info": {"title": "ContractBridge selected operation", "version": "1"},
                "paths": {
                    api.path: {
                        "post": {
                            "requestBody": {
                                "required": True,
                                "content": {"application/json": {"schema": api.request}},
                            },
                            "responses": {
                                str(api.status): {
                                    "description": "Success",
                                    "content": {"application/json": {"schema": api.response}},
                                }
                            },
                        }
                    }
                },
            }

        with tempfile.TemporaryDirectory(prefix="contractbridge-diff-") as folder:
            paths = [Path(folder) / "old.json", Path(folder) / "new.json"]
            for path, api in zip(paths, (old, new)):
                path.write_text(json.dumps(bounded(api)))
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.binary,
                    "breaking",
                    str(paths[0]),
                    str(paths[1]),
                    "--format",
                    "json",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError("oasdiff timed out") from None
            except FileNotFoundError:
                raise RuntimeError("oasdiff is unavailable; run scripts/bootstrap.sh") from None
            if proc.returncode != 0 or len(stdout) > 2_000_000:
                raise RuntimeError("oasdiff failed or exceeded output limit")
            report = json.loads(stdout or "[]")
        return {
            "engine": "oasdiff",
            "version": "1.34.1",
            "raw": report,
            "changes": structural_changes(old, new),
            "operationPair": {"source": old.path, "target": new.path},
            "pairSelectedByOperator": True,
        }

from __future__ import annotations

import keyword
from pathlib import Path

from .contracts import Contract, schema_type
from .engine import fixture_data, inspect_plan, nullable
from .models import Fixture, Plan
from .util import canonical, digest, sha256_file

CORE_FILES = ("__init__.py", "contracts.py", "models.py", "engine.py", "util.py", "runtime.py")


class Generator:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def template_hash(self) -> str:
        files = list((self.root / "packages/adapter-templates").rglob("*"))
        files += [self.root / "src/contractbridge" / name for name in CORE_FILES]
        files += [self.root / "requirements.lock"]
        return digest({str(p.relative_to(self.root)): sha256_file(p) for p in sorted(files) if p.is_file()})

    def fingerprint(self, plan: Plan, old: Contract, new: Contract, fixtures: list[Fixture]) -> str:
        return digest(
            {
                "plan": plan.data(),
                "old": old.document,
                "new": new.document,
                "fixtures": fixture_data(fixtures),
                "templatesHash": self.template_hash(),
            }
        )

    def generate(
        self,
        destination: Path,
        plan: Plan,
        old: Contract,
        new: Contract,
        fixtures: list[Fixture],
        reviews=None,
        model=None,
    ) -> dict:
        if any(not i.reviewRequired for i in inspect_plan(plan, old, new)):
            raise ValueError("Cannot generate an invalid plan")
        if destination.exists():
            raise ValueError("Output directory already exists")
        destination.mkdir(parents=True)
        for template in (self.root / "packages/adapter-templates").rglob("*"):
            if template.is_symlink():
                raise ValueError("Template symlinks are not allowed")
            if template.is_file():
                out = destination / template.relative_to(self.root / "packages/adapter-templates")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(template.read_bytes())
        package = destination / "bridge_core"
        package.mkdir()
        for name in CORE_FILES:
            (package / name).write_bytes((self.root / "src/contractbridge" / name).read_bytes())
        bundle = {
            "plan": plan.data(),
            "oldSpec": old.document,
            "newSpec": new.document,
            "fixtures": fixture_data(fixtures),
            "repairId": self.fingerprint(plan, old, new, fixtures),
        }
        (destination / "bundle.json").write_text(canonical(bundle))
        (destination / "LICENSE").write_bytes((self.root / "LICENSE").read_bytes())
        (destination / "requirements.lock").write_bytes((self.root / "requirements.lock").read_bytes())
        (destination / "review.json").write_text(
            canonical({"decisions": reviews or {}, "model": model or {"liveInference": False}})
        )
        (destination / "dtos.py").write_text(render_dtos(old, new))
        (destination / "provenance.json").write_text(
            canonical(
                {
                    "inputHash": self.fingerprint(plan, old, new, fixtures),
                    "templatesHash": self.template_hash(),
                    "runtime": "Python 3.14",
                }
            )
        )
        manifest = {
            str(p.relative_to(destination)): sha256_file(p)
            for p in sorted(destination.rglob("*"))
            if p.is_file()
        }
        (destination / "source-manifest.json").write_text(canonical(manifest))
        return {
            "sourceHash": digest(manifest),
            "inputHash": self.fingerprint(plan, old, new, fixtures),
            "manifest": manifest,
        }


def render_dtos(old: Contract, new: Contract) -> str:
    declarations = []

    def python_type(schema, name):
        kind = schema_type(schema)
        if kind == "object":
            fields = []
            for key, child in sorted(
                schema["properties"].items(), key=lambda kv: (kv[0] not in schema.get("required", []), kv[0])
            ):
                if (
                    not key.isascii()
                    or not key.isidentifier()
                    or keyword.iskeyword(key)
                    or key.startswith("_")
                ):
                    raise ValueError("Generated DTO fields require public Python identifiers")
                child_type = python_type(child, name + "_" + key)
                if key not in schema.get("required", []):
                    child_type += " | None"
                fields.append(
                    f"    {key}: {child_type}" + (" = None" if key not in schema.get("required", []) else "")
                )
            declarations.append(
                f"@dataclass(frozen=True, slots=True)\nclass {name}:\n" + ("\n".join(fields) or "    pass")
            )
            result = name
        elif kind == "array":
            result = "list[" + python_type(schema["items"], name + "_Item") + "]"
        else:
            result = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}[kind]
        return result + (" | None" if nullable(schema) else "")

    for name, schema in (
        ("ClientRequest", old.request),
        ("ClientResponse", old.response),
        ("UpstreamRequest", new.request),
        ("UpstreamResponse", new.response),
    ):
        python_type(schema, name)
    return (
        "from __future__ import annotations\nfrom dataclasses import dataclass\n\n"
        + "\n\n".join(declarations)
        + "\n"
    )

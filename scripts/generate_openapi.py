"""Export the control-plane wire contract and the canonical plan schema."""

import copy
import json
from pathlib import Path

from contractbridge.models import FIXTURE_SCHEMA, PLAN_SCHEMA

ROOT = Path(__file__).resolve().parents[1]
schemas = {
    "Problem": {
        "type": "object",
        "required": ["title", "status"],
        "properties": {
            "type": {"type": "string"},
            "title": {"type": "string"},
            "status": {"type": "integer"},
        },
    },
    "Migration": {
        "type": "object",
        "required": ["id", "title", "revision", "state"],
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "title": {"type": "string"},
            "revision": {"type": "integer"},
            "state": {
                "enum": [
                    "DRAFT",
                    "DIFF_READY",
                    "MAPPING_PROPOSED",
                    "NEEDS_REVIEW",
                    "APPROVED_FOR_BUILD",
                    "BUILDING",
                    "TEST_FAILED",
                    "READY_FOR_APPROVAL",
                    "ACTIVE",
                    "ARCHIVED",
                ]
            },
            "plan": {"anyOf": [{"$ref": "#/components/schemas/Plan"}, {"type": "null"}]},
            "issues": {"type": "array", "items": {"$ref": "#/components/schemas/Issue"}},
            "fixtures": {"type": "array", "items": {"$ref": "#/components/schemas/Fixture"}},
            "specs": {"type": "object"},
            "builds": {"type": "array", "items": {"type": "object"}},
            "diff": {},
            "model": {},
            "reviews": {"type": "object"},
            "reviewFeedback": {"type": "string"},
            "pendingJob": {"type": ["string", "null"]},
        },
    },
    "Issue": {
        "type": "object",
        "required": ["key", "message", "reviewRequired"],
        "properties": {
            "key": {"type": "string"},
            "message": {"type": "string"},
            "reviewRequired": {"type": "boolean"},
        },
    },
    "Fixture": FIXTURE_SCHEMA,
    "Job": {
        "type": "object",
        "required": ["id", "migrationId", "kind", "status"],
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "migrationId": {"type": "string", "format": "uuid"},
            "kind": {"enum": ["analyze", "propose", "build", "activate"]},
            "status": {"enum": ["QUEUED", "RUNNING", "PASSED", "FAILED"]},
            "error": {"type": "string"},
        },
    },
}
plan = copy.deepcopy(PLAN_SCHEMA)
rule = plan.pop("$defs")["rule"]
plan.pop("$schema", None)


def rewrite(value):
    if isinstance(value, dict):
        return {
            k: "#/components/schemas/Rule" if k == "$ref" and v == "#/$defs/rule" else rewrite(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [rewrite(v) for v in value]
    return value


schemas.update(Plan=rewrite(plan), Rule=rewrite(rule))
paths = {}


def operation(path, method, summary, request=None, response=None, status=200):
    spec = {
        "summary": summary,
        "operationId": method + "_" + path.replace("/", "_").replace("{", "").replace("}", ""),
        "responses": {
            str(status): {
                "description": "Accepted" if status == 202 else "Success",
                "content": {"application/json": {"schema": response or {"type": "object"}}},
            },
            **{
                str(code): {
                    "description": title,
                    "content": {
                        "application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}
                    },
                }
                for code, title in [
                    (401, "Unauthorized"),
                    (409, "Revision or state conflict"),
                    (422, "Invalid input"),
                ]
            },
        },
    }
    parameters = [
        {
            "name": name,
            "in": "path",
            "required": True,
            "schema": {
                "type": "string",
                **({"format": "uuid"} if name != "label" else {"enum": ["old", "new"]}),
            },
        }
        for name in ["id", "label", "buildId", "jobId", "connectionId"]
        if "{" + name + "}" in path
    ]
    if method in ("post", "put") and "{id}" in path and not path.endswith("/replay"):
        parameters.append(
            {
                "name": "If-Match",
                "in": "header",
                "required": True,
                "description": "Current migration revision, optionally quoted",
                "schema": {"type": "string"},
            }
        )
    if parameters:
        spec["parameters"] = parameters
    if request is not None:
        spec["requestBody"] = {"required": True, "content": {"application/json": {"schema": request}}}
    paths.setdefault(path, {})[method] = spec


def obj(properties, required=None):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required or [],
    }


M = {"$ref": "#/components/schemas/Migration"}
J = {"$ref": "#/components/schemas/Job"}
operation("/health", "get", "Control-plane health")
operation("/migrations", "get", "List migrations", response={"type": "array", "items": M})
operation(
    "/migrations",
    "post",
    "Create migration",
    obj({"title": {"type": "string", "minLength": 1, "maxLength": 120}}, ["title"]),
    M,
    201,
)
operation("/migrations/{id}", "get", "Read migration and current evidence", response=M)
operation(
    "/migrations/{id}/specs/{label}",
    "post",
    "Import a local specification",
    obj({"content": {"type": "string", "maxLength": 1000000}}, ["content"]),
    M,
)
operation(
    "/migrations/{id}/fixtures",
    "post",
    "Import synthetic or explicitly sanitized fixtures",
    obj(
        {
            "source": {"enum": ["synthetic", "sanitized"]},
            "fixtures": {"type": "array", "items": {"$ref": "#/components/schemas/Fixture"}},
        },
        ["source", "fixtures"],
    ),
    M,
)
operation(
    "/migrations/{id}/analyze",
    "post",
    "Analyze selected operation pair",
    obj({"sourcePath": {"type": "string"}, "targetPath": {"type": "string"}}),
    J,
    202,
)
operation("/migrations/{id}/diff", "get", "Read oasdiff and structural evidence")
operation("/migrations/{id}/proposals", "post", "Ask local Ollama for a mapping proposal", obj({}), J, 202)
operation("/migrations/{id}/plan", "get", "Read the plan and review issues")
operation(
    "/migrations/{id}/plan",
    "put",
    "Replace the plan and invalidate approval",
    {"$ref": "#/components/schemas/Plan"},
    M,
)
operation(
    "/migrations/{id}/plan/approve",
    "post",
    "Record substantive decisions for all review issues",
    obj(
        {
            "decisions": {
                "type": "object",
                "additionalProperties": {"type": "string", "minLength": 10, "maxLength": 1000},
            }
        },
        ["decisions"],
    ),
    M,
)
operation("/migrations/{id}/builds", "post", "Generate and verify the exact approved inputs", obj({}), J, 202)
operation("/migrations/{id}/builds/{buildId}", "get", "Read build result")
operation(
    "/migrations/{id}/builds/{buildId}/activate",
    "post",
    "Approve the exact passing artifact for local routing",
    obj(
        {"confirmed": {"const": True}, "artifactHash": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
        ["confirmed", "artifactHash"],
    ),
    J,
    202,
)
operation("/migrations/{id}/exports/{buildId}", "get", "Download the checksummed adapter bundle")
paths["/migrations/{id}/exports/{buildId}"]["get"]["responses"]["200"]["content"] = {
    "application/zip": {"schema": {"type": "string", "contentMediaType": "application/zip"}}
}
operation(
    "/migrations/{id}/audit",
    "get",
    "Read the append-only decision history",
    response={"type": "array", "items": {"type": "object"}},
)
operation(
    "/migrations/{id}/replay",
    "post",
    "Inspect one actual redacted request through the active proxy",
    {"type": "object"},
)
operation("/migrations/{id}/archive", "post", "Stop an active adapter and archive the migration", obj({}), M)
operation("/jobs/{jobId}", "get", "Poll a long-running job", response=J)
# Runtime monitoring, candidate review, and explicitly confirmed GitHub publication.
connection_fields = {
    "name": {"type": "string", "minLength": 1, "maxLength": 120},
    "upstreamBaseUrl": {"type": "string", "maxLength": 300},
    "sourceSpec": {"type": "string", "maxLength": 1000000},
    "specPath": {"type": "string", "maxLength": 256},
    "sourcePath": {"type": "string"},
    "targetPath": {"type": "string"},
    "fixtureSource": {"enum": ["synthetic", "sanitized"]},
    "fixtures": {"type": "array", "maxItems": 100, "items": {"$ref": "#/components/schemas/Fixture"}},
    "autoRepair": {"type": "boolean", "default": True},
    "autoPublishDraft": {"type": "boolean", "default": False},
    "dualRepair": {"type": "boolean", "default": False},
    "repository": {"type": "string", "maxLength": 240},
    "baseBranch": {"type": "string", "maxLength": 120},
    "client": {"type": "object"},
    "application": {"type": "object"},
    "sentryProject": {"type": "string", "maxLength": 120},
}
operation("/connections", "get", "List monitored connections, approved origins, and GitHub readiness")
operation(
    "/connections",
    "post",
    "Register a fixed client contract and approved upstream",
    obj(connection_fields, ["name", "upstreamBaseUrl", "sourceSpec", "fixtureSource"]),
    status=201,
)
discover_fields = dict(connection_fields)
discover_fields.pop("sourceSpec")
operation(
    "/connections/discover",
    "post",
    "Register an integration from its allowlisted live OpenAPI URL",
    obj(discover_fields, ["name", "upstreamBaseUrl"]),
    status=201,
)
operation("/connections/{id}", "get", "Read connection metadata and gateway path")
operation("/connections/{id}/status", "get", "Read concise integration, incident, PR, and lifecycle status")
operation("/connections/{id}/access", "post", "Issue the deterministic connection-scoped gateway credential")
paths["/connections/{id}/access"]["post"]["parameters"] = [
    p for p in paths["/connections/{id}/access"]["post"]["parameters"] if p["name"] != "If-Match"
]
operation(
    "/connections/{id}/fixtures",
    "put",
    "Update independent synthetic or sanitized verification fixtures",
    obj(
        {
            "source": {"enum": ["synthetic", "sanitized"]},
            "fixtures": {"type": "array", "items": {"$ref": "#/components/schemas/Fixture"}},
        },
        ["source", "fixtures"],
    ),
)
operation(
    "/connections/{id}/draft-automation",
    "put",
    "Authorize or revoke automatic verified-candidate draft PR publication to the configured repository",
    obj({"confirmed": {"const": True}, "enabled": {"type": "boolean"}}, ["confirmed", "enabled"]),
)
operation(
    "/connections/{id}/target",
    "put",
    "Select an unambiguous target operation before retrying investigation",
    obj({"targetPath": {"type": "string", "pattern": "^/[A-Za-z0-9_/-]+$"}}, ["targetPath"]),
)
operation(
    "/connections/{id}/route",
    "post",
    "Explicitly route through an active approved repair",
    obj(
        {"confirmed": {"const": True}, "migrationId": {"type": "string", "format": "uuid"}},
        ["confirmed", "migrationId"],
    ),
)
operation(
    "/connections/{id}/direct",
    "post",
    "Explicitly restore direct upstream routing",
    obj({"confirmed": {"const": True}}, ["confirmed"]),
)
operation(
    "/connections/{id}/audit",
    "get",
    "Read append-only runtime connection events",
    response={"type": "array", "items": {"type": "object"}},
)
operation(
    "/incidents",
    "get",
    "List actual runtime failure incidents and repair evidence",
    response={"type": "array", "items": {"type": "object"}},
)
operation("/incidents/{id}", "get", "Read the runtime incident, model provenance, and candidate report")
operation(
    "/incidents/{id}/retry",
    "post",
    "Retry interrupted or blocked investigation without replaying traffic",
    obj({}),
    status=202,
)
operation(
    "/incidents/{id}/review",
    "post",
    "Approve the verified candidate or reject it with a correction comment that the model applies to a new mapping",
    obj(
        {
            "decision": {"enum": ["approve", "reject"]},
            "comment": {"type": "string", "maxLength": 1000},
            "candidateHash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        },
        ["decision", "comment", "candidateHash"],
    ),
    status=202,
)
operation("/incidents/{id}/patch", "get", "Download the actual verified candidate code patch")
paths["/incidents/{id}/patch"]["get"]["responses"]["200"]["content"] = {
    "text/x-diff": {"schema": {"type": "string"}}
}
operation("/incidents/{id}/pr-preview", "get", "Preview draft PR body, files, and candidate checksum")
operation(
    "/incidents/{id}/publish",
    "post",
    "Explicitly publish the exact tested candidate as a real GitHub draft PR",
    obj(
        {"confirmed": {"const": True}, "artifactHash": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
        ["confirmed", "artifactHash"],
    ),
)
operation(
    "/incidents/{id}/audit",
    "get",
    "Read append-only runtime incident events",
    response={"type": "array", "items": {"type": "object"}},
)
operation(
    "/gateway/{connectionId}/{operation}",
    "post",
    "Forward a registered client request once; detect response contract drift and schedule background investigation",
    {"type": "object"},
)
paths["/gateway/{connectionId}/{operation}"]["post"]["parameters"].append(
    {
        "name": "operation",
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
        "description": "The registered literal operation path without its leading slash, such as v1/orders.",
    }
)
paths["/gateway/{connectionId}/{operation}"]["post"]["responses"]["201"] = {
    "description": "Successful response preserving the registered client contract",
    "content": {"application/json": {"schema": {"type": "object"}}},
}
operation(
    "/native/{connectionId}/{operation}",
    "post",
    "Forward one request using the verified permanent-client contract, bypassing temporary translation",
    {"type": "object"},
)
paths["/native/{connectionId}/{operation}"]["post"]["parameters"].append(
    {"name": "operation", "in": "path", "required": True, "schema": {"type": "string"}}
)
paths["/native/{connectionId}/{operation}"]["post"]["parameters"].append(
    {
        "name": "X-ContractBridge-Client-Revision",
        "in": "header",
        "required": True,
        "schema": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    }
)
operation(
    "/dual/{id}/step",
    "post",
    "Execute one idempotent durable-workflow activity",
    obj({"phase": {"enum": ["investigate", "revise", "mitigate", "permanent", "verify"]}}, ["phase"]),
)
paths["/dual/{id}/step"]["post"]["parameters"] = [
    p for p in paths["/dual/{id}/step"]["post"]["parameters"] if p["name"] != "If-Match"
]
operation(
    "/hooks/sentry", "post", "Accept a signed Sentry issue-alert trigger", {"type": "object"}, status=202
)
operation(
    "/hooks/github", "post", "Accept signed merge and deployment evidence", {"type": "object"}, status=202
)
doc = {
    "openapi": "3.1.0",
    "info": {"title": "ContractBridge Control Plane", "version": "0.1.0"},
    "servers": [{"url": "/api/v1"}],
    "security": [{"OperatorToken": []}],
    "paths": paths,
    "components": {
        "schemas": schemas,
        "securitySchemes": {
            "OperatorToken": {"type": "http", "scheme": "bearer"},
            "ConnectionToken": {"type": "http", "scheme": "bearer"},
            "SentrySignature": {"type": "apiKey", "in": "header", "name": "Sentry-Hook-Signature"},
            "GitHubSignature": {"type": "apiKey", "in": "header", "name": "X-Hub-Signature-256"},
        },
    },
}
doc["paths"]["/health"]["get"]["security"] = []
for route in ("/gateway/{connectionId}/{operation}", "/native/{connectionId}/{operation}"):
    doc["paths"][route]["post"]["security"] = [{"ConnectionToken": []}]
doc["paths"]["/hooks/sentry"]["post"]["security"] = [{"SentrySignature": []}]
doc["paths"]["/hooks/github"]["post"]["security"] = [{"GitHubSignature": []}]
(ROOT / "docs/control-plane.openapi.json").write_text(json.dumps(doc, indent=2) + "\n")
(ROOT / "packages/plan-schema/plan.schema.json").write_text(json.dumps(PLAN_SCHEMA, indent=2) + "\n")

from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

from .config import Settings
from .contracts import Contract, parse_spec
from .engine import inspect_plan, require_approval, verify_fixtures
from .generation import Generator
from .models import Fixture, Plan
from .storage import Conflict, NotFound, Store, now
from .telemetry import capture_failure, span
from .util import reject_secrets, sha256_file

EDITABLE = {
    "DRAFT",
    "DIFF_READY",
    "MAPPING_PROPOSED",
    "NEEDS_REVIEW",
    "APPROVED_FOR_BUILD",
    "TEST_FAILED",
    "READY_FOR_APPROVAL",
}


class Workflow:
    def __init__(self, settings: Settings, store: Store, diff, planner, runner):
        self.settings, self.store, self.diff, self.planner, self.runner = (
            settings,
            store,
            diff,
            planner,
            runner,
        )
        self.generator = Generator(settings.root)

    @staticmethod
    def editable(m: dict):
        if m["state"] not in EDITABLE or m.get("pendingJob"):
            raise Conflict("Migration is active, archived, or has a running job")

    @staticmethod
    def contracts(m: dict):
        if set(m["specs"]) != {"old", "new"}:
            raise ValueError("Import both specifications")
        return (
            Contract.select(m["specs"]["old"], m["sourcePath"]),
            Contract.select(m["specs"]["new"], m["targetPath"]),
        )

    def fingerprint(self, m: dict) -> str:
        old, new = self.contracts(m)
        return self.generator.fingerprint(Plan.parse(m["plan"]), old, new, Fixture.parse_many(m["fixtures"]))

    def public(self, m: dict) -> dict:
        result = dict(m)
        result["builds"] = [
            {**b, "runtime": {k: v for k, v in b.get("runtime", {}).items() if k != "inspectToken"}}
            for b in m["builds"]
        ]
        result["issues"] = []
        result["fixtureResults"] = []
        if m.get("plan"):
            old, new = self.contracts(m)
            plan = Plan.parse(m["plan"])
            result["issues"] = [asdict(i) for i in inspect_plan(plan, old, new)]
            if m["fixtures"]:
                result["fixtureResults"] = [
                    asdict(r) for r in verify_fixtures(Fixture.parse_many(m["fixtures"]), plan, old, new)
                ]
        return result

    def upload_spec(self, identifier: str, label: str, text: str, revision: int):
        if label not in {"old", "new"}:
            raise ValueError("Spec label must be old or new")
        spec = parse_spec(text)

        def edit(m):
            self.editable(m)
            m["specs"][label] = spec
            m.update(
                state="DRAFT",
                plan=None,
                diff=None,
                approvalHash=None,
                reviews={},
                model=None,
                sourcePath=None,
                targetPath=None,
            )

        return self.store.mutate(identifier, "SPEC_IMPORTED", edit, revision)

    def upload_fixtures(self, identifier: str, values: list, source: str, revision: int):
        if source not in {"synthetic", "sanitized"}:
            raise ValueError("Explicitly identify synthetic or sanitized fixture data")
        Fixture.parse_many(values)
        reject_secrets(values)

        def edit(m):
            self.editable(m)
            m.update(fixtures=values, fixtureSource=source, approvalHash=None, reviews={})
            if m["plan"]:
                m["state"] = "NEEDS_REVIEW"

        return self.store.mutate(identifier, "FIXTURES_IMPORTED", edit, revision)

    def edit_plan(self, identifier: str, value: dict, revision: int):
        plan = Plan.parse(value)

        def edit(m):
            self.editable(m)
            if not m["diff"]:
                raise Conflict("Analyze contracts before adding a plan")
            old, new = self.contracts(m)
            issues = inspect_plan(plan, old, new)
            prior = m.get("model") or {}
            provenance = (
                {
                    **prior,
                    "operatorEdited": True,
                    "originalProposal": prior.get("originalProposal", m["plan"]),
                }
                if prior.get("liveInference")
                else {"provider": "operator", "liveInference": False}
            )
            m.update(
                plan=plan.data(),
                state="NEEDS_REVIEW" if issues else "MAPPING_PROPOSED",
                reviews={},
                approvalHash=None,
                model=provenance,
            )

        return self.store.mutate(identifier, "PLAN_EDITED", edit, revision)

    def approve(self, identifier: str, decisions: dict, revision: int):
        def edit(m):
            self.editable(m)
            if not m["plan"]:
                raise Conflict("No plan to approve")
            old, new = self.contracts(m)
            require_approval(Plan.parse(m["plan"]), old, new, Fixture.parse_many(m["fixtures"]), decisions)
            m.update(reviews=decisions, approvalHash=self.fingerprint(m), state="APPROVED_FOR_BUILD")

        return self.store.mutate(identifier, "PLAN_APPROVED", edit, revision)

    def submit(self, identifier: str, kind: str, revision: int, options: dict | None = None) -> dict:
        options = options or {}
        job = {
            "id": str(uuid4()),
            "migrationId": identifier,
            "kind": kind,
            "status": "QUEUED",
            "createdAt": now(),
        }

        def claim(m):
            self.editable(m)
            if kind == "analyze":
                old, new = self.contracts(
                    {**m, "sourcePath": options.get("sourcePath"), "targetPath": options.get("targetPath")}
                )
                m.update(
                    sourcePath=old.path,
                    targetPath=new.path,
                    approvalHash=None,
                    plan=None,
                    reviews={},
                    model=None,
                    diff=None,
                    state="DRAFT",
                )
            elif kind == "propose":
                if m["diff"] is None:
                    raise Conflict("Analyze contracts first")
                m.update(approvalHash=None, reviews={}, plan=None, model=None, state="DIFF_READY")
            elif kind == "build":
                if m["state"] != "APPROVED_FOR_BUILD" or m["approvalHash"] != self.fingerprint(m):
                    raise Conflict("Approve the current exact inputs before building")
                job["buildId"] = str(uuid4())
                m["builds"].append(
                    {
                        "id": job["buildId"],
                        "status": "RUNNING",
                        "inputHash": m["approvalHash"],
                        "createdAt": now(),
                    }
                )
                m["state"] = "BUILDING"
            elif kind == "activate":
                build = self.find_build(m, options["buildId"])
                if (
                    m["state"] != "READY_FOR_APPROVAL"
                    or options.get("confirmed") is not True
                    or build["status"] != "PASSED"
                ):
                    raise Conflict("Activation requires a passing build and explicit confirmation")
                if m["approvalHash"] != self.fingerprint(m) or build["inputHash"] != m["approvalHash"]:
                    raise Conflict("Build inputs or templates changed; review and rebuild")
                artifact = self.settings.artifacts / "builds" / build["id"] / "output/adapter.zip"
                if (
                    not artifact.exists()
                    or sha256_file(artifact) != build["artifactHash"]
                    or options.get("artifactHash") != build["artifactHash"]
                ):
                    raise Conflict("Artifact approval must match the exact verified checksum")
                job["buildId"] = build["id"]
            else:
                raise ValueError("Unknown job type")
            m["pendingJob"] = job["id"]

        m = self.store.mutate(identifier, kind.upper() + "_QUEUED", claim, revision)
        job["inputRevision"] = m["revision"]
        return self.store.job(job)

    @staticmethod
    def find_build(m: dict, identifier: str):
        build = next((b for b in m["builds"] if b["id"] == identifier), None)
        if build is None:
            raise NotFound("Build not found")
        return build

    async def execute(self, identifier: str):
        job = self.store.get_job(identifier)
        job.update(status="RUNNING")
        self.store.job(job)
        m = self.store.get(job["migrationId"])
        try:
            old, new = self.contracts(m)
            result = None
            with span("contractbridge.pipeline", "Migration " + job["kind"]):
                if job["kind"] == "analyze":
                    result = await self.diff.analyze(old, new)
                elif job["kind"] == "propose":
                    result = await self.planner.propose(
                        old,
                        new,
                        Fixture.parse_many(m["fixtures"]) if m["fixtures"] else [],
                        m.get("reviewFeedback") or "",
                    )
                elif job["kind"] == "build":
                    build_id = job["buildId"]
                    with span("contractbridge.generate", "Generate trusted adapter"):
                        generated = self.generator.generate(
                            self.settings.artifacts / "builds" / build_id / "source",
                            Plan.parse(m["plan"]),
                            old,
                            new,
                            Fixture.parse_many(m["fixtures"]),
                            m["reviews"],
                            m["model"],
                        )
                    with span("contractbridge.verify", "Isolated compilation and tests") as trace:
                        result = await self.runner.build(build_id, generated["sourceHash"])
                        trace.set("build.passed", result.get("passed", False))
                        trace.set("fixture.count", len(m["fixtures"]))
                    if result.get("sourceHash") != generated["sourceHash"] or result.get("tests", 0) < len(
                        m["fixtures"]
                    ):
                        raise RuntimeError("Sandbox evidence does not match generated inputs")
                    artifact = self.settings.artifacts / "builds" / build_id / "output/adapter.zip"
                    if not artifact.exists() or sha256_file(artifact) != result.get("artifactHash"):
                        raise RuntimeError("Sandbox artifact checksum mismatch")
                else:
                    if m.get("connectionId"):
                        result = await self.runner.activate(
                            job["buildId"],
                            self.find_build(m, job["buildId"])["artifactHash"],
                            m["approvedUpstream"],
                        )
                    else:
                        result = await self.runner.activate(
                            job["buildId"], self.find_build(m, job["buildId"])["artifactHash"]
                        )

            def finish(value):
                if value.get("pendingJob") != identifier:
                    raise Conflict("Job no longer owns the migration")
                value["pendingJob"] = None
                match job["kind"]:
                    case "analyze":
                        value.update(diff=result, state="DIFF_READY")
                    case "propose":
                        plan, metadata = result
                        value.update(
                            plan=plan.data(),
                            model=metadata,
                            reviews={},
                            approvalHash=None,
                            state="NEEDS_REVIEW" if inspect_plan(plan, old, new) else "MAPPING_PROPOSED",
                        )
                    case "build":
                        build = self.find_build(value, job["buildId"])
                        build.update(status="PASSED", **result, completedAt=now())
                        value["state"] = "READY_FOR_APPROVAL"
                    case "activate":
                        build = self.find_build(value, job["buildId"])
                        build.update(runtime=result)
                        value.update(state="ACTIVE", activeBuildId=job["buildId"])

            self.store.mutate(m["id"], job["kind"].upper() + "_COMPLETED", finish, job["inputRevision"])
            job.update(status="PASSED", completedAt=now())
        except Exception as exc:
            capture_failure(exc, "migration." + job["kind"])
            # No raw model response, request body, exception repr, or credentials in errors.
            safe = (
                str(exc)
                if isinstance(exc, (ValueError, RuntimeError))
                else "External service failed; inspect local service health"
            )

            def failed(value):
                value["pendingJob"] = None
                if job["kind"] == "build":
                    self.find_build(value, job["buildId"]).update(
                        status="FAILED", error=safe, completedAt=now()
                    )
                    value["state"] = "TEST_FAILED"

            self.store.mutate(m["id"], job["kind"].upper() + "_FAILED", failed)
            job.update(status="FAILED", error=safe, completedAt=now())
        self.store.job(job)

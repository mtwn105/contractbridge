"""Runtime drift detection -> bounded spec fetch -> AI proposal -> isolated candidate verification.

Traffic is never replayed and candidate verification never grants activation.
"""

from __future__ import annotations

from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from .contracts import Contract, parse_spec, validate_payload
from .engine import inspect_plan, verify_fixtures
from .models import Fixture, Plan
from .publishing import GitHubPublisher, artifact_files, repair_patch
from .resources import Resources
from .runtime import problem
from .storage import Conflict, now
from .telemetry import capture_failure, span
from .util import canonical, digest, reject_secrets, strict_loads


class UpstreamContractDrift(RuntimeError):
    pass


class UpstreamCallFailure(RuntimeError):
    pass


TERMINAL = {
    "READY_FOR_REVIEW",
    "BLOCKED",
    "NEEDS_FIXTURES",
    "NO_CONTRACT_CHANGE",
    "FAILED",
    "RESOLVED",
    "ACTION_REQUIRED",
}


class RuntimeMonitor:
    def __init__(self, workflow, settings, transport=None, publisher=None):
        self.workflow, self.settings, self.transport = workflow, settings, transport
        self.resources = Resources(workflow.store)
        self.publisher = publisher or GitHubPublisher(
            settings.github_repository, settings.github_token, settings.github_base
        )

    def recover(self):
        for incident in self.resources.list("incidents"):
            # Temporal owns recovery for dual-repair incidents. Keep its durable
            # lifecycle intact instead of rewriting a long wait as a failure.
            if incident.get("lifecycle"):
                if incident.get("running"):
                    self.resources.update(
                        "incidents",
                        incident["id"],
                        "CONTROL_PLANE_RESTARTED",
                        lambda v: v.update(running=False),
                    )
            elif incident.get("running") or incident["state"] not in TERMINAL:
                self.stage(
                    incident["id"], "FAILED", error="Control plane restarted; retry this incident explicitly"
                )
            if (incident.get("publication") or {}).get("status") == "PUBLISHING":
                self.resources.update(
                    "incidents",
                    incident["id"],
                    "PR_INTERRUPTED",
                    lambda v: v.update(publication={"status": "RETRY_REQUIRED"}),
                )

    def stage(self, identifier, state, **data):
        return self.resources.update(
            "incidents",
            identifier,
            state,
            lambda v: v.update(state=state, running=state not in TERMINAL, **data),
        )

    def origin(self, value):
        u = urlsplit(value)
        if (
            u.scheme not in ("http", "https")
            or not u.hostname
            or u.username
            or u.password
            or u.path not in ("", "/")
            or u.query
            or u.fragment
        ):
            raise ValueError("Use an approved upstream origin without credentials, path, query, or fragment")
        origin = value.rstrip("/")
        if origin not in self.settings.upstream_allowlist:
            raise ValueError("Upstream is not in the server-configured UPSTREAM_ALLOWLIST")
        return origin

    def create(self, value, identifier=None):
        origin = self.origin(value["upstreamBaseUrl"])
        from .integrations import validate_application, validate_client

        client = validate_client(value["client"]) if value.get("client") else None
        application = validate_application(value["application"]) if value.get("application") else None
        if application:
            if (
                application.get("baseUrl") not in self.settings.verification_origins
                or application.get("credentialEnv") not in self.settings.verification_credentials
            ):
                raise ValueError("Application verification origin and credential must be server-approved")
        publisher = self.publisher_for(value)
        auto_publish = value.get("autoPublishDraft", False)
        if auto_publish and not publisher.configured:
            raise Conflict("Configure GitHub before authorizing automatic draft PR publication")
        spec_path = value.get("specPath", "/openapi.json")
        if (
            not spec_path.startswith("/")
            or spec_path.startswith("//")
            or "?" in spec_path
            or "#" in spec_path
            or ".." in spec_path
            or "\\" in spec_path
        ):
            raise ValueError("Specification path must be an absolute path on the approved upstream")
        old = Contract.select(parse_spec(value["sourceSpec"]), value.get("sourcePath"))
        fixtures = Fixture.parse_many(value["fixtures"]) if value.get("fixtures") else []
        reject_secrets(value.get("fixtures", []))
        for fixture in fixtures:
            if fixture.expectation == "SUCCESS":
                validate_payload(old.request, fixture.clientRequest)
                validate_payload(old.response, fixture.clientResponse)
        return self.resources.create(
            "connections",
            {
                "name": value["name"],
                "upstreamBaseUrl": origin,
                "specPath": spec_path,
                "sourceSpec": old.document,
                "sourcePath": old.path,
                "targetPath": value.get("targetPath"),
                "fixtures": value.get("fixtures", []),
                "fixtureSource": value["fixtureSource"],
                "autoRepair": value.get("autoRepair", True),
                "state": "MONITORING",
                "route": {"mode": "DIRECT"},
                "successCount": 0,
                "failureCount": 0,
                "generation": 0,
                "openIncidentId": None,
                "baselineHash": digest(old.document),
                "autoPublishDraft": auto_publish,
                "publicationAuthorization": self.publication_policy(value) if auto_publish else None,
                "repository": value.get("repository"),
                "baseBranch": value.get("baseBranch"),
                "client": client,
                "application": application,
                "sentryProject": value.get("sentryProject"),
                "dualRepair": value.get("dualRepair", False),
            },
            identifier,
        )

    def publication_policy(self, connection=None):
        publisher = self.publisher_for(connection) if connection else self.publisher
        return {
            "repository": publisher.repository,
            "base": publisher.base,
            "scope": "verified-adapter-draft-only",
        }

    def publisher_for(self, connection):
        if connection.get("repository"):
            return GitHubPublisher(
                connection["repository"],
                self.settings.github_token,
                connection.get("baseBranch") or self.settings.github_base,
                transport=getattr(self.publisher, "transport", None),
            )
        return self.publisher

    def configure_publication(self, identifier, enabled, expected):
        connection = self.resources.get("connections", identifier)
        if enabled and not self.publisher_for(connection).configured:
            raise Conflict("Configure GitHub before authorizing automatic draft PR publication")
        return self.resources.update(
            "connections",
            identifier,
            "AUTOMATIC_DRAFT_PUBLICATION_AUTHORIZED" if enabled else "AUTOMATIC_DRAFT_PUBLICATION_DISABLED",
            lambda v: v.update(
                autoPublishDraft=enabled,
                publicationAuthorization=self.publication_policy(connection) if enabled else None,
            ),
            expected,
        )

    def public_connection(self, connection):
        return {k: v for k, v in connection.items() if k not in {"fixtures", "sourceSpec"}} | {
            "fixtureCount": len(connection["fixtures"]),
            "exampleRequest": next(
                (f["clientRequest"] for f in connection["fixtures"] if f["expectation"] == "SUCCESS"), {}
            ),
            "gatewayPath": f"/api/v1/gateway/{connection['id']}" + connection["sourcePath"],
        }

    def public_incident(self, incident):
        result = dict(incident)
        if incident.get("migrationId"):
            m = self.workflow.public(self.workflow.store.get(incident["migrationId"]))
            result["migration"] = {
                k: m.get(k)
                for k in (
                    "id",
                    "revision",
                    "state",
                    "issues",
                    "model",
                    "fixtureResults",
                    "builds",
                    "plan",
                    "reviews",
                    "reviewFeedback",
                    "pendingJob",
                )
            }
        return result

    async def fetch_spec(self, connection):
        raw, status, _ = await self.exchange("GET", connection["upstreamBaseUrl"] + connection["specPath"])
        if status != 200:
            raise RuntimeError("Current upstream specification is unavailable")
        return parse_spec(raw.decode("utf-8"))

    async def exchange(self, method, url, payload=None):
        async with httpx.AsyncClient(
            transport=self.transport, timeout=10, follow_redirects=False, trust_env=False
        ) as client:
            async with client.stream(
                method, url, json=payload, headers={"Accept": "application/json"}
            ) as response:
                data = b""
                async for part in response.aiter_bytes():
                    data += part
                    if len(data) > 1_000_000:
                        raise RuntimeError("Upstream response exceeds 1 MB")
                return data, response.status_code, response.headers.get("content-type", "")

    def record_failure(self, connection_id, kind, status):
        # Single API process, revision locks, and a deterministic generation-scoped
        # incident ID prevent repeated traffic from launching duplicate AI work.
        with self.workflow.store.lock:
            c = self.resources.get("connections", connection_id)
            if c.get("openIncidentId"):
                incident = self.resources.get("incidents", c["openIncidentId"])
                created = False
            else:
                key = digest(
                    {
                        "connection": c["id"],
                        "generation": c["generation"],
                        "kind": kind,
                        "status": status,
                        "baseline": c["baselineHash"],
                    }
                )
                identifier = str(uuid5(NAMESPACE_URL, key))
                try:
                    incident = self.resources.get("incidents", identifier)
                    created = False
                except ValueError:
                    incident = self.resources.create(
                        "incidents",
                        {
                            "connectionId": c["id"],
                            "kind": kind,
                            "upstreamStatus": status,
                            "state": "DETECTED",
                            "running": False,
                            "migrationId": None,
                            "candidate": None,
                            "publication": None,
                            "occurrences": 0,
                            "baselineHash": c["baselineHash"],
                            "message": "A real upstream response no longer satisfies the registered client contract",
                        },
                        identifier,
                    )
                    created = True
            incident = self.resources.update(
                "incidents",
                incident["id"],
                "FAILURE_OBSERVED",
                lambda v: v.update(occurrences=v["occurrences"] + 1, lastSeen=now()),
            )
            self.resources.update(
                "connections",
                c["id"],
                "FAILURE_OBSERVED",
                lambda v: v.update(
                    state="DEGRADED", failureCount=v["failureCount"] + 1, openIncidentId=incident["id"]
                ),
            )
            return incident, created and c["autoRepair"]

    def record_success(self, connection_id):
        with self.workflow.store.lock:
            c = self.resources.get("connections", connection_id)
            recovered = False
            if c.get("openIncidentId"):
                incident = self.resources.get("incidents", c["openIncidentId"])
                if not incident.get("running") and not incident.get("lifecycle"):
                    self.stage(
                        incident["id"],
                        "RESOLVED",
                        message="A real subsequent response satisfied the client contract. Routing was not changed and no candidate was activated",
                    )
                    recovered = True

            def success(v):
                v.update(state="MONITORING", successCount=v["successCount"] + 1, lastSuccessAt=now())
                if recovered:
                    v.update(openIncidentId=None, generation=v["generation"] + 1)

            self.resources.update("connections", connection_id, "SUCCESS_OBSERVED", success)

    async def gateway(self, connection_id, method, operation, payload):
        with span("contractbridge.gateway", "Monitored API call"):
            return await self._gateway(connection_id, method, operation, payload)

    async def _gateway(self, connection_id, method, operation, payload):
        c = self.resources.get("connections", connection_id)
        old = Contract.select(c["sourceSpec"], c["sourcePath"])
        if method != "POST" or operation != old.path:
            return problem(404, "Operation is not registered on this monitored connection")
        try:
            validate_payload(old.request, payload)
        except ValueError:
            return problem(
                422, "Request does not satisfy the registered client contract; no upstream call was made"
            )
        target = c["upstreamBaseUrl"]
        if c["route"]["mode"] == "BRIDGE":
            m = self.workflow.store.get(c["route"]["migrationId"])
            if m["state"] != "ACTIVE" or m["activeBuildId"] != c["route"]["buildId"]:
                return problem(503, "The selected bridge is no longer active; select a verified route")
            target = "http://cb-proxy-" + m["activeBuildId"] + ":8080"
        kind, status = "CONTRACT_MISMATCH", 502
        try:
            raw, status, content_type = await self.exchange("POST", target + old.path, payload)
            if status == old.status:
                try:
                    result = strict_loads(raw)
                    validate_payload(old.response, result)
                except ValueError, TypeError, KeyError:
                    kind = "RESPONSE_SCHEMA_MISMATCH"
                else:
                    self.record_success(c["id"])
                    return JSONResponse(result, status_code=status)
            elif status in (401, 403, 429):
                kind = "ACCESS_OR_RATE_LIMIT"
            elif status >= 500:
                kind = "UPSTREAM_ERROR"
            else:
                kind = "HTTP_CONTRACT_MISMATCH"
        except httpx.TimeoutException:
            kind, status = "UPSTREAM_TIMEOUT", 504
        except httpx.HTTPError, RuntimeError:
            kind, status = "UPSTREAM_UNAVAILABLE", 502
        incident, schedule = self.record_failure(c["id"], kind, status)
        if incident["occurrences"] == 1:
            contract_failure = kind in {
                "CONTRACT_MISMATCH",
                "HTTP_CONTRACT_MISMATCH",
                "RESPONSE_SCHEMA_MISMATCH",
            }
            error_type = UpstreamContractDrift if contract_failure else UpstreamCallFailure
            try:
                raise error_type("A monitored upstream call failed its registered response checks")
            except (UpstreamContractDrift, UpstreamCallFailure) as exc:
                capture_failure(
                    exc,
                    "runtime.contract" if contract_failure else "runtime.upstream",
                    {
                        "contractbridge.connection_id": c["id"],
                        "contractbridge.incident_id": incident["id"],
                    },
                )
        response = JSONResponse(
            {
                "title": "Upstream call failed; incident recorded",
                "status": status if status >= 400 else 502,
                "incidentId": incident["id"],
                "requestRetried": False,
                "repairState": incident["state"],
            },
            status_code=status if status >= 400 else 502,
            headers={"X-ContractBridge-Incident": incident["id"]},
        )
        if schedule and self.settings.incident_trigger != "sentry":
            response.background = BackgroundTask(self.dispatch_repair, incident["id"])
        return response

    async def dispatch_repair(self, identifier):
        incident = self.resources.get("incidents", identifier)
        connection = self.resources.get("connections", incident["connectionId"])
        if not self.settings.dual_repair or not connection.get("dualRepair"):
            return await self.repair(identifier)
        try:
            if not self.settings.temporal_address:
                raise RuntimeError("Configure the durable workflow service")
            from .orchestration import start_repair

            await start_repair(self.settings, identifier)
        except Exception as exc:
            capture_failure(exc, "runtime.orchestration")
            self.resources.update(
                "incidents",
                identifier,
                "WORKFLOW_START_FAILED",
                lambda v: v.update(
                    lifecycle={**v.get("lifecycle", {}), "phase": "WORKFLOW_UNAVAILABLE"},
                    message="Workflow service unavailable; this incident is retained for retry",
                ),
            )

    async def repair(self, identifier, resume=False):
        def claim(v):
            if v.get("running") and not resume:
                raise Conflict("Repair is already running")
            v.update(running=True, state="FETCHING_SPEC", error=None)

        try:
            incident = self.resources.update("incidents", identifier, "REPAIR_STARTED", claim)
        except Conflict:
            return
        try:
            with span("contractbridge.repair", "Investigate runtime contract drift"):
                c = self.resources.get("connections", incident["connectionId"])
                if incident["kind"] in {"ACCESS_OR_RATE_LIMIT", "UPSTREAM_TIMEOUT", "UPSTREAM_UNAVAILABLE"}:
                    self.stage(
                        identifier,
                        "ACTION_REQUIRED",
                        message="Authentication, rate limiting, or connectivity requires operator action; no mapping was invented",
                    )
                    return
                new = await self.fetch_spec(c)
                if digest(new) == c["baselineHash"]:
                    self.stage(
                        identifier,
                        "NO_CONTRACT_CHANGE",
                        message="The published specification is unchanged; investigate provider behavior instead of inventing a repair",
                    )
                    return
                target_path = c.get("targetPath")
                if not target_path:
                    old_operation = c["sourceSpec"]["paths"][c["sourcePath"]]["post"].get("operationId")
                    matches = [
                        path
                        for path, item in new.get("paths", {}).items()
                        if "post" in item
                        and old_operation
                        and item["post"].get("operationId") == old_operation
                    ]
                    if len(matches) == 1:
                        target_path = matches[0]
                    elif c["sourcePath"] in new.get("paths", {}) and "post" in new["paths"][c["sourcePath"]]:
                        target_path = c["sourcePath"]
                try:
                    target = Contract.select(new, target_path)
                except ValueError:
                    self.stage(
                        identifier,
                        "ACTION_REQUIRED",
                        message="Select a supported target POST operation on the connection; the current specification has an ambiguous or unsupported target",
                    )
                    return
                m = self.workflow.store.create("Runtime repair · " + c["name"])
                m = self.workflow.upload_spec(m["id"], "old", canonical(c["sourceSpec"]), m["revision"])
                m = self.workflow.upload_spec(m["id"], "new", canonical(new), m["revision"])
                if c["fixtures"]:
                    m = self.workflow.upload_fixtures(
                        m["id"], c["fixtures"], c["fixtureSource"], m["revision"]
                    )
                m = self.workflow.store.mutate(
                    m["id"],
                    "RUNTIME_INCIDENT_LINKED",
                    lambda v: v.update(
                        connectionId=c["id"], incidentId=identifier, approvedUpstream=c["upstreamBaseUrl"]
                    ),
                    m["revision"],
                )
                self.stage(identifier, "ANALYZING", migrationId=m["id"], targetSpecHash=digest(new))
                m = await self.run_job(
                    m, "analyze", {"sourcePath": c["sourcePath"], "targetPath": target.path}
                )
                self.stage(identifier, "PROPOSING")
                m = await self.run_job(m, "propose")
                await self._verify_candidate(identifier, c, m)
        except Exception as exc:
            capture_failure(exc, "runtime.repair")
            self.stage(
                identifier,
                "FAILED",
                error="Repair failed; inspect the provider specification, local model, or sandbox. Retry after correcting the cause",
                errorCategory=type(exc).__name__,
            )

    async def _verify_candidate(self, identifier, c, m):
        old, new_contract = self.workflow.contracts(m)
        fixtures = Fixture.parse_many(m["fixtures"]) if m["fixtures"] else []
        issues = inspect_plan(Plan.parse(m["plan"]), old, new_contract)
        if any(not issue.reviewRequired for issue in issues):
            self.stage(
                identifier,
                "BLOCKED",
                message="The proposed mapping has unsupported or missing information; approval cannot override these blockers",
            )
            return False
        if not fixtures:
            self.stage(
                identifier,
                "NEEDS_FIXTURES",
                message="AI proposal is available. Add independent synthetic/sanitized verification fixtures before a candidate can be tested",
            )
            return False
        if not all(r.passed for r in verify_fixtures(fixtures, Plan.parse(m["plan"]), old, new_contract)):
            self.stage(
                identifier,
                "BLOCKED",
                message="The AI proposal does not satisfy the registered verification fixtures",
            )
            return False
        self.stage(identifier, "VERIFYING_CANDIDATE")
        candidate_id = str(uuid4())
        frozen = self.workflow.generator.generate(
            self.settings.artifacts / "builds" / candidate_id / "source",
            Plan.parse(m["plan"]),
            old,
            new_contract,
            fixtures,
            {},
            {**m["model"], "candidateOnly": True, "semanticReview": "PENDING"},
        )
        result = await self.workflow.runner.build(candidate_id, frozen["sourceHash"])
        if (
            not result.get("passed")
            or result.get("sourceHash") != frozen["sourceHash"]
            or result.get("tests", 0) < len(fixtures)
        ):
            raise RuntimeError("Candidate verification report did not match frozen inputs")
        await self.assert_current(m)
        candidate = {
            **result,
            "id": candidate_id,
            "inputHash": frozen["inputHash"],
            "semanticReview": "PENDING",
            "activationApproved": False,
        }
        artifact_files(self.artifact(candidate_id), candidate["artifactHash"])
        self.stage(
            identifier,
            "READY_FOR_REVIEW",
            candidate=candidate,
            message="The adapter passed isolated contract tests. Review the mapping and approve the exact candidate to build and route the temporary bridge",
        )
        if not (self.settings.dual_repair and c.get("dualRepair")):
            await self.auto_publish(identifier)
        return True

    async def review(self, identifier, decision, comment, candidate_hash):
        incident, _, _ = await self.package(identifier)
        candidate = incident.get("candidate") or {}
        if candidate.get("artifactHash") != candidate_hash:
            raise Conflict("Review the exact verified candidate artifact")
        review = {
            "decision": decision,
            "comment": comment,
            "candidateHash": candidate_hash,
            "actor": "operator",
            "at": now(),
        }

        def claim(v):
            if v["state"] != "READY_FOR_REVIEW" or not v.get("candidate"):
                raise Conflict("The incident is not awaiting review")
            if v["candidate"]["artifactHash"] != candidate_hash:
                raise Conflict("The verified candidate changed; reload before reviewing")
            if (v.get("lifecycle") or {}).get("phase") in ("APPROVING", "REVISING"):
                raise Conflict("A review action is already in progress")
            previous = (v.get("lifecycle") or {}).get("review") or {}
            if previous.get("candidateHash") == candidate_hash and previous.get("decision") == "approve":
                raise Conflict("This candidate is already approved")
            v["reviewHistory"] = (v.get("reviewHistory") or [])[-9:] + [review]
            v["review"] = review
            v["lifecycle"] = {
                **v.get("lifecycle", {}),
                "phase": "APPROVING" if decision == "approve" else "REVISING",
                "review": review,
                "updatedAt": now(),
            }
            v["message"] = (
                "Operator approved the verified candidate; the temporary bridge is being built and activated"
                if decision == "approve"
                else "Operator requested changes; the model is revising the mapping from the review comment"
            )

        return self.resources.update(
            "incidents",
            identifier,
            "REVIEW_APPROVED" if decision == "approve" else "REVIEW_CHANGES_REQUESTED",
            claim,
        )

    async def revise(self, identifier, comment, candidate_hash):
        incident = self.resources.get("incidents", identifier)
        if (incident.get("candidate") or {}).get("artifactHash") != candidate_hash:
            raise Conflict("The reviewed candidate changed before the revision started")
        c = self.resources.get("connections", incident["connectionId"])
        try:
            self.stage(
                identifier,
                "REVISING",
                message="Applying the operator's review comment to a new mapping proposal",
            )
            m = self.workflow.store.get(incident["migrationId"])
            m = self.workflow.store.mutate(
                m["id"],
                "REVIEW_COMMENT_RECORDED",
                lambda v: v.update(reviewFeedback=comment[:2000]),
                m["revision"],
            )
            m = await self.run_job(m, "propose")
            c = self.resources.get("connections", c["id"])
            await self._verify_candidate(identifier, c, m)
        except Exception as exc:
            capture_failure(exc, "runtime.revision")
            self.stage(
                identifier,
                "FAILED",
                error="Revision failed; inspect the local model or sandbox, then retry the incident",
                errorCategory=type(exc).__name__,
            )

    async def apply_review(self, identifier, decision):
        from .dual_repair import DualRepair

        try:
            controller = DualRepair(self)
            if decision == "approve":
                await controller.mitigate(identifier)
            else:
                await controller.revise(identifier)
        except Exception as exc:
            capture_failure(exc, "runtime.review")
            self.resources.update(
                "incidents",
                identifier,
                "REVIEW_ACTION_FAILED",
                lambda v: v.update(
                    message="Review action did not complete; inspect the local services and retry the incident",
                    lifecycle={
                        **v.get("lifecycle", {}),
                        "phase": "REVIEW_ACTION_FAILED",
                        "updatedAt": now(),
                    },
                ),
            )

    async def run_job(self, migration, kind, options=None):
        job = self.workflow.submit(migration["id"], kind, migration["revision"], options)
        await self.workflow.execute(job["id"])
        result = self.workflow.store.get_job(job["id"])
        if result["status"] != "PASSED":
            raise RuntimeError("Runtime repair pipeline stage failed: " + kind)
        return self.workflow.store.get(migration["id"])

    async def assert_current(self, migration):
        if migration.get("connectionId"):
            c = self.resources.get("connections", migration["connectionId"])
            if c["upstreamBaseUrl"] != migration.get("approvedUpstream") or digest(
                await self.fetch_spec(c)
            ) != digest(migration["specs"]["new"]):
                raise Conflict(
                    "Provider contract changed again; investigate a fresh repair before approval, publication, or activation"
                )

    def artifact(self, build_id):
        return self.settings.artifacts / "builds" / build_id / "output/adapter.zip"

    async def package(self, identifier):
        incident = self.resources.get("incidents", identifier)
        if incident["state"] != "READY_FOR_REVIEW" or not incident.get("candidate"):
            raise Conflict("A passing candidate is required before exporting or publishing a repair")
        m = self.workflow.store.get(incident["migrationId"])
        await self.assert_current(m)
        if self.workflow.fingerprint(m) != incident["candidate"]["inputHash"]:
            raise Conflict("Plan, fixtures, or templates changed; rerun candidate verification")
        files = artifact_files(
            self.artifact(incident["candidate"]["id"]), incident["candidate"]["artifactHash"]
        )
        return incident, m, files

    async def auto_publish(self, identifier):
        incident = self.resources.get("incidents", identifier)
        c = self.resources.get("connections", incident["connectionId"])
        if not c.get("autoPublishDraft"):
            return
        try:
            publisher = self.publisher_for(c)
            if not publisher.configured or c.get("publicationAuthorization") != self.publication_policy(c):
                raise Conflict("Automatic publication target changed; operator authorization is required")
            # Occurrence counters can change while traffic continues. The internal
            # preauthorization path binds the exact candidate hash in the locked
            # claim instead of requiring an unchanged occurrence-count revision.
            await self.publish(identifier, None, incident["candidate"]["artifactHash"])
        except Exception as exc:
            capture_failure(exc, "runtime.publication")
            self.resources.update(
                "incidents",
                identifier,
                "AUTOMATIC_PR_PUBLICATION_FAILED",
                lambda v: v.update(
                    publication={**(v.get("publication") or {}), "status": "RETRY_REQUIRED"},
                    publicationError="Candidate verification passed, but automatic draft publication did not complete. Check GitHub configuration, target authorization, and current provider contract",
                ),
            )

    async def publish(self, identifier, expected, checksum):
        incident, m, files = await self.package(identifier)
        if incident["candidate"]["artifactHash"] != checksum:
            raise Conflict("Approve the exact candidate artifact checksum before publishing")
        if self.settings.dual_repair and incident.get("lifecycle"):
            from .dual_repair import DualRepair

            self.resources.update(
                "incidents", identifier, "PERMANENT_PUBLICATION_CONFIRMED", lambda _: None, expected
            )
            await DualRepair(self).permanent(identifier, manual=True)
            return self.resources.get("incidents", identifier).get("publication") or {
                "status": "NOT_CONFIGURED"
            }
        c = self.resources.get("connections", incident["connectionId"])
        publisher = self.publisher_for(c)
        previous = incident.get("publication") or {}
        if (
            previous.get("url")
            and previous.get("artifactHash") == checksum
            and previous.get("repository") == publisher.repository
            and previous.get("base") == publisher.base
        ):
            return previous

        def claim(v):
            if (
                v["state"] != "READY_FOR_REVIEW"
                or not v.get("candidate")
                or v["candidate"]["artifactHash"] != checksum
            ):
                raise Conflict("The verified candidate changed before publication")
            if (v.get("publication") or {}).get("status") == "PUBLISHING":
                raise Conflict("PR publication is already running")
            if (v.get("publication") or {}).get("url"):
                v.setdefault("publicationHistory", []).append(v["publication"])
            v["publication"] = {
                "status": "PUBLISHING",
                "artifactHash": checksum,
                "authorization": "connection-policy" if expected is None else "operator-confirmation",
            }

        self.resources.update("incidents", identifier, "PR_PUBLICATION_APPROVED", claim, expected)
        try:
            report = self.pr_body(incident, m)
            result = await publisher.publish(incident, files, report)
            result = {
                **result,
                "artifactHash": checksum,
                "repository": publisher.repository,
                "base": publisher.base,
            }
            self.resources.update(
                "incidents",
                identifier,
                "DRAFT_PR_CREATED",
                lambda v: v.update(publication={"status": "CREATED", **result}, publicationError=None),
            )
            return result
        except Exception:
            self.resources.update(
                "incidents",
                identifier,
                "PR_PUBLICATION_FAILED",
                lambda v: v.update(publication={"status": "RETRY_REQUIRED"}),
            )
            raise

    def pr_body(self, incident, m):
        issues = self.workflow.public(m)["issues"]

        def mapping_rows(rules, source="", target=""):
            rows = []
            for rule in rules:
                if rule["op"] == "array_map":
                    rows.extend(
                        mapping_rows(
                            rule.get("fields", []), source + rule["from"] + "[]", target + rule["to"] + "[]"
                        )
                    )
                else:
                    note = (
                        canonical(rule.get("values", rule.get("value", "")))
                        if rule["op"] in ("enum_map", "constant")
                        else rule["op"]
                    )
                    rows.append(
                        "| `"
                        + source
                        + rule.get("from", "(constant)")
                        + "` | `"
                        + target
                        + rule.get("to", "")
                        + "` | `"
                        + note.replace("`", "'").replace("|", "\\|")
                        + "` |"
                    )
            return rows

        mapping = ""
        for direction, key in [("Request", "requestMappings"), ("Response", "responseMappings")]:
            rows = mapping_rows(m["plan"][key])
            mapping += (
                "\n## "
                + direction
                + " mappings\n\n| From | To | Transformation |\n| --- | --- | --- |\n"
                + "\n".join(rows[:100])
                + "\n"
            )
            if len(rows) > 100:
                mapping += "\nAdditional mappings are in the generated plan JSON.\n"
        # Escape issue text so untrusted schema/model strings cannot add links,
        # images, or Markdown structure to the review instructions.
        import re

        questions = "\n".join(
            "- " + re.sub(r"([\\`*_{}\[\]()<>#+!|])", r"\\\1", issue["message"].replace("\n", " "))[:1200]
            for issue in issues[:30]
        )
        body = (
            f"A real runtime failure was detected on a monitored API connection.\n\n"
            f"Incident: `{incident['id']}`\n\nProvider specification SHA-256: `{incident['targetSpecHash']}`\n\n"
            f"Artifact SHA-256: `{incident['candidate']['artifactHash']}`\n\n"
            f"Verification: {incident['candidate']['tests']} passing tests; {incident['candidate'].get('verificationMode', 'unknown')}\n\n"
            f"AI: `{(m.get('model') or {}).get('model', 'unknown')}`, prompt `{(m.get('model') or {}).get('promptVersion', 'unknown')}`.\n\n"
            f"Semantic review remains pending ({len(issues)} questions/assumptions). This draft does not approve activation or deployment.\n"
            "The adapter comes from trusted templates; the model supplied mapping data. Runtime payloads were not recorded in this PR.\n"
        )
        return (
            body
            + mapping[:30000]
            + "\n## Business assumptions to review\n\n"
            + (questions or "No additional semantic questions were reported.")
            + "\n\nReview the generated plan, fixture expectations, and adapter before merging. This PR does not activate or deploy a repair.\n"
        )

    async def route(self, connection_id, migration_id, expected, temporary=False):
        m = self.workflow.store.get(migration_id)
        c = self.resources.get("connections", connection_id)
        if m.get("connectionId") != c["id"] or m["state"] != "ACTIVE" or m["specs"]["old"] != c["sourceSpec"]:
            raise Conflict("Select an active, explicitly approved repair for this connection")
        await self.assert_current(m)
        self.resources.update(
            "connections",
            connection_id,
            "BRIDGE_ROUTING_APPROVED",
            lambda v: v.update(
                state="MONITORING",
                route={"mode": "BRIDGE", "migrationId": m["id"], "buildId": m["activeBuildId"]},
                repairIncidentId=v.get("openIncidentId") if temporary else v.get("repairIncidentId"),
                openIncidentId=v.get("openIncidentId") if temporary else None,
                generation=v["generation"] if temporary else v["generation"] + 1,
            ),
            expected,
        )
        if c.get("openIncidentId") and not temporary:
            self.stage(
                c["openIncidentId"],
                "RESOLVED",
                message="Operator selected an active verified bridge. Subsequent traffic is still monitored",
            )
        return self.public_connection(self.resources.get("connections", connection_id))

    async def patch(self, identifier):
        incident, _, files = await self.package(identifier)
        prefix = "contractbridge-adapters/" + incident["connectionId"] + "/" + incident["id"]
        return repair_patch(files, prefix)

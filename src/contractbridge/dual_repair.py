"""Shared mapping -> authorized temporary bridge -> permanent app PR -> verified retirement."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from .contracts import Contract, validate_payload
from .integrations import baseline_client, validate_client
from .storage import Conflict, now
from .util import canonical, digest, strict_loads


class DualRepair:
    def __init__(self, monitor, transport=None):
        self.monitor, self.resources, self.workflow = monitor, monitor.resources, monitor.workflow
        self.settings, self.transport = monitor.settings, transport

    def record(self, identifier, phase=None, **fields):
        def update(v):
            state = v.setdefault("lifecycle", {"phase": "DETECTED"})
            state.update(fields)
            if phase:
                state.update(phase=phase, updatedAt=now())
            if fields.get("message"):
                v["message"] = fields["message"]

        return self.resources.update("incidents", identifier, "DUAL_" + (phase or "UPDATED"), update)

    async def investigate(self, identifier):
        incident = self.resources.get("incidents", identifier)
        life = incident.get("lifecycle") or {}
        if life.get("sharedMappingHash") and incident.get("candidate"):
            # An activity retry must not move a mitigated/PR-open incident back
            # to the investigation phase.
            await self.monitor.package(identifier)
            return {"supported": True, "mappingHash": life["sharedMappingHash"]}
        if not incident.get("candidate") or incident["state"] != "READY_FOR_REVIEW":
            self.record(identifier, "INVESTIGATING")
            await self.monitor.repair(identifier, resume=True)
        incident = self.resources.get("incidents", identifier)
        if not incident.get("candidate") or incident["state"] != "READY_FOR_REVIEW":
            self.record(identifier, "UNSUPPORTED", message=incident.get("error") or incident.get("message"))
            return {"supported": False}
        await self.monitor.package(identifier)
        self.record(identifier, "CANDIDATE_VERIFIED", sharedMappingHash=incident["candidate"]["inputHash"])
        return {"supported": True, "mappingHash": incident["candidate"]["inputHash"]}

    async def mitigate(self, identifier):
        incident, migration, _ = await self.monitor.package(identifier)
        c = self.resources.get("connections", incident["connectionId"])
        review = (incident.get("lifecycle") or {}).get("review") or {}
        candidate = incident.get("candidate") or {}
        if (
            review.get("decision") != "approve"
            or review.get("candidateHash") != candidate.get("artifactHash")
        ):
            self.record(
                identifier,
                "AWAITING_RUNTIME_REVIEW",
                mitigation={
                    "status": "REVIEW_REQUIRED",
                    "message": "Approve the verified candidate in the control plane to activate a temporary bridge",
                },
            )
            return {"active": False}
        comment = (review.get("comment") or "").strip()
        if len(comment) < 10:
            comment = (
                "Approved by the operator in the ContractBridge control plane "
                "after reviewing the verified candidate."
            )
        comment = comment[:1000]
        if c["route"].get("migrationId") == migration["id"] and c["route"]["mode"] == "BRIDGE":
            build_id = c["route"]["buildId"]
            current = self.resources.get("incidents", identifier).get("lifecycle") or {}
            if (current.get("mitigation") or {}).get("status") != "ACTIVE":
                self.record(
                    identifier,
                    "MITIGATED",
                    mitigation={
                        "status": "ACTIVE",
                        "migrationId": migration["id"],
                        "buildId": build_id,
                        "artifactHash": self.workflow.find_build(migration, build_id)["artifactHash"],
                    },
                    message="The temporary bridge is active for the exact approved artifact",
                )
            return {"active": True}
        if migration.get("pendingJob"):
            job = self.workflow.store.get_job(migration["pendingJob"])
            if job["status"] in ("QUEUED", "RUNNING"):
                await self.workflow.execute(job["id"])
            migration = self.workflow.store.get(migration["id"])
        if migration["state"] not in ("APPROVED_FOR_BUILD", "READY_FOR_APPROVAL", "ACTIVE"):
            issues = self.workflow.public(migration)["issues"]
            migration = self.workflow.approve(
                migration["id"], {i["key"]: comment for i in issues}, migration["revision"]
            )
            self.record(
                identifier,
                semanticReview={
                    "actor": "operator",
                    "comment": comment,
                    "candidateHash": candidate.get("artifactHash"),
                    "inputHash": self.workflow.fingerprint(migration),
                },
            )
        if migration["state"] == "APPROVED_FOR_BUILD":
            migration = await self.monitor.run_job(migration, "build")
        if migration["state"] == "READY_FOR_APPROVAL":
            build = migration["builds"][-1]
            # Approval names the actual artifact produced by the passing build.
            self.record(
                identifier,
                "ACTIVATING",
                artifactApproval={
                    "actor": "operator",
                    "comment": comment,
                    "artifactHash": build["artifactHash"],
                    "inputHash": build["inputHash"],
                    "tests": build["tests"],
                    "approvedAt": now(),
                },
            )
            migration = await self.monitor.run_job(
                migration,
                "activate",
                {"buildId": build["id"], "confirmed": True, "artifactHash": build["artifactHash"]},
            )
        if migration["state"] != "ACTIVE":
            raise Conflict("No exact approved runtime artifact is active")
        c = self.resources.get("connections", c["id"])
        await self.monitor.route(c["id"], migration["id"], c["revision"], temporary=True)
        self.record(
            identifier,
            "MITIGATED",
            mitigation={
                "status": "ACTIVE",
                "migrationId": migration["id"],
                "buildId": migration["activeBuildId"],
                "artifactHash": self.workflow.find_build(migration, migration["activeBuildId"])[
                    "artifactHash"
                ],
            },
            message="The temporary bridge is active for the exact approved artifact",
        )
        return {"active": True}

    async def revise(self, identifier):
        incident = self.resources.get("incidents", identifier)
        review = (incident.get("lifecycle") or {}).get("review") or {}
        candidate = incident.get("candidate") or {}
        if (
            review.get("decision") != "reject"
            or review.get("candidateHash") != candidate.get("artifactHash")
        ):
            raise Conflict("Request changes for the current verified candidate before revising")
        await self.monitor.revise(
            identifier,
            review.get("comment") or "",
            candidate["artifactHash"],
        )
        updated = self.resources.get("incidents", identifier)
        if updated["state"] == "READY_FOR_REVIEW" and updated.get("candidate"):
            self.record(
                identifier,
                "CANDIDATE_VERIFIED",
                sharedMappingHash=updated["candidate"]["inputHash"],
            )
            return {"revised": True}
        return {"revised": False}

    async def permanent(self, identifier, manual=False):
        incident, migration, files = await self.monitor.package(identifier)
        # Permanent repair changes the application's integration module, rather
        # than merely adding an unused adapter folder to the repository.
        connection = self.resources.get("connections", incident["connectionId"])
        if not connection.get("client"):
            self.record(
                identifier,
                "CLIENT_TARGET_REQUIRED",
                message="Declare a supported repository client target before preparing a permanent application fix",
            )
            return {"published": False}
        target = validate_client(connection["client"])
        source = (
            files["permanent_client.py"]
            .replace("from bridge_core.", "from " + target.get("importNamespace", "contractbridge") + ".")
            .replace('BUNDLE_NAME = "bundle.json"', "BUNDLE_NAME = " + repr(Path(target["bundlePath"]).name))
        )
        bundle = strict_loads(files["bundle.json"])
        if bundle.get("repairId") != incident["candidate"]["inputHash"]:
            raise Conflict("Permanent and temporary fixes do not share the same verified mapping")
        prefix = "contractbridge-adapters/" + incident["connectionId"] + "/" + identifier
        changes = {prefix + "/" + name: value for name, value in files.items()}
        changes.update({target["modulePath"]: source, target["bundlePath"]: canonical(bundle)})
        out = self.settings.artifacts / "permanent" / identifier
        out.mkdir(parents=True, exist_ok=True)
        (out / "changes.json").write_text(canonical(changes))
        patch_hash = digest(changes)
        permanent = {
            "status": "VERIFIED",
            "mappingHash": bundle["repairId"],
            "patchHash": patch_hash,
            "applicationFiles": [target["modulePath"], target["bundlePath"]],
            "verificationMode": incident["candidate"]["verificationMode"],
            "tests": incident["candidate"]["tests"],
        }
        c = self.resources.get("connections", incident["connectionId"])
        publisher = self.monitor.publisher_for(connection)
        if not publisher.configured:
            self.record(identifier, "GITHUB_NOT_CONFIGURED", permanent=permanent)
            return {"published": False}
        if not manual and (
            c.get("publicationAuthorization") != self.monitor.publication_policy(connection)
            or not c.get("autoPublishDraft")
        ):
            self.record(identifier, "AWAITING_PUBLICATION_AUTHORIZATION", permanent=permanent)
            return {"published": False}
        previous = incident.get("publication") or {}
        if (
            previous.get("url")
            and previous.get("patchHash") == patch_hash
            and previous.get("artifactHash") == incident["candidate"]["artifactHash"]
            and previous.get("repository") == publisher.repository
            and previous.get("base") == publisher.base
        ):
            if (incident.get("lifecycle") or {}).get("phase") != "PR_OPEN":
                self.record(
                    identifier,
                    "PR_OPEN",
                    permanent={**permanent, "status": "PR_OPEN"},
                )
            return {"published": True}
        self.record(identifier, "PUBLISHING_PR", permanent=permanent)
        result = await publisher.publish(
            incident,
            files,
            self.monitor.pr_body(incident, migration)
            + "\n## Permanent application fix\n\nThis PR updates the registered application client itself. The temporary bridge remains until this merged commit is deployed and verified without translation.\n",
            application_files=changes,
            expected_application={target["modulePath"]: baseline_client(self.settings.root, target)},
            application_paths=[target["modulePath"], target["bundlePath"]],
        )
        result.update(patchHash=patch_hash, mappingHash=bundle["repairId"])
        self.resources.update(
            "incidents",
            identifier,
            "PERMANENT_PR_CREATED",
            lambda v: v.update(publication={"status": "CREATED", **result}),
        )
        self.record(
            identifier,
            "PR_OPEN",
            permanent={
                **self.resources.get("incidents", identifier)["lifecycle"]["permanent"],
                "status": "PR_OPEN",
            },
        )
        return {"published": True}

    async def native(self, connection_id, operation, payload, revision):
        c = self.resources.get("connections", connection_id)
        incident = self.resources.get("incidents", c.get("repairIncidentId") or c["openIncidentId"])
        if revision != (incident.get("candidate") or {}).get("inputHash"):
            raise Conflict("The application client revision does not match this verified repair")
        m = self.workflow.store.get(incident["migrationId"])
        target = Contract.select(m["specs"]["new"], m["plan"]["targetOperation"]["path"])
        if operation != target.path:
            raise Conflict("Native operation does not match the verified provider contract")
        validate_payload(target.request, payload)
        raw, status, _ = await self.monitor.exchange("POST", c["upstreamBaseUrl"] + target.path, payload)
        value = strict_loads(raw)
        if status != target.status:
            raise Conflict("Native provider response has an unexpected status")
        validate_payload(target.response, value)
        return value, status

    async def verify(self, identifier):
        incident = self.resources.get("incidents", identifier)
        life = incident.get("lifecycle") or {}
        merge, deployment = life.get("merge") or {}, life.get("deployment") or {}
        connection = self.resources.get("connections", incident["connectionId"])
        expected_environment = (connection.get("application") or {}).get(
            "environment", self.settings.deployment_environment
        )
        if (
            not merge.get("sha")
            or deployment.get("sha") != merge["sha"]
            or deployment.get("state") != "success"
            or deployment.get("environment") != expected_environment
        ):
            return {"ready": False, "retired": False}
        mitigation = life.get("mitigation") or {}
        if mitigation.get("status") == "RETIRED":
            return {"ready": True, "retired": True}
        if mitigation.get("status") == "RETIRING":
            return await self._finish_retirement(identifier, incident, mitigation)
        if mitigation.get("status") != "ACTIVE":
            return {"ready": True, "retired": False}
        if (
            not self.settings.allow_synthetic_probes
            or connection.get("fixtureSource") != "synthetic"
            or not 2 <= self.settings.verification_samples <= 10
            or self.settings.verification_interval < 0
        ):
            self.record(
                identifier,
                "VERIFICATION_REQUIRED",
                message="Synthetic bypass probes are not authorized for this deployment",
            )
            return {"ready": True, "retired": False}
        await self.monitor.assert_current(self.workflow.store.get(incident["migrationId"]))
        application = connection.get("application") or {}
        if (
            application.get("baseUrl") not in self.settings.verification_origins
            or application.get("credentialEnv") not in self.settings.verification_credentials
        ):
            raise Conflict("Application verification is not configured for this integration")
        import os

        application_token = os.getenv(application["credentialEnv"], "")
        if len(application_token) < 24:
            raise Conflict("Application verification credential is unavailable")
        self.record(identifier, "VERIFYING")
        checks = []
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=20,
                follow_redirects=False,
                trust_env=False,
                headers={"Authorization": "Bearer " + application_token},
            ) as client:

                async def call(method, path):
                    r = await client.request(method, application["baseUrl"].rstrip("/") + path)
                    if r.status_code != 200 or len(r.content) > 16384:
                        raise Conflict("The deployed application verification endpoint failed")
                    return strict_loads(r.content)

                for sample in range(self.settings.verification_samples):
                    release = await call("GET", application.get("releasePath", "/release"))
                    if (
                        release.get("commit") != merge["sha"]
                        or release.get("repairHash") != life["sharedMappingHash"]
                        or release.get("mode") != "native"
                    ):
                        raise Conflict("The actual application is not the repaired deployed commit")
                    probe = await call("POST", application.get("probePath", "/probe"))
                    if (
                        probe.get("commit") != merge["sha"]
                        or probe.get("repairHash") != life["sharedMappingHash"]
                        or probe.get("translation") != "bypassed"
                        or probe.get("status") != "CONFIRMED"
                    ):
                        raise Conflict("The repaired application did not pass without runtime translation")
                    checks.append({"commit": merge["sha"], "translation": "bypassed", "checkedAt": now()})
                    if sample + 1 < self.settings.verification_samples:
                        await asyncio.sleep(self.settings.verification_interval)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            self.record(
                identifier,
                "VERIFY_FAILED",
                verification={"passed": False, "samples": checks, "errorCategory": type(exc).__name__},
            )
            return {"ready": True, "retired": False}
        # Re-read after the observation window: stale/failed deployments and
        # changed routes cannot authorize removal of another active repair.
        current = self.resources.get("incidents", identifier)
        if current["lifecycle"].get("deployment") != deployment or current["lifecycle"].get("merge") != merge:
            raise Conflict("Release evidence changed during verification")
        m = self.workflow.store.get(incident["migrationId"])
        await self.monitor.assert_current(m)

        # Persist the exact successful observation before changing routing. A
        # retry after a worker/process crash can then finish stop/finalization
        # without replaying the synthetic operation.
        self.record(
            identifier,
            "RETIRING",
            verification={"passed": True, "samples": checks, "commit": merge["sha"]},
            mitigation={**mitigation, "status": "RETIRING", "retiringAt": now()},
        )

        return await self._finish_retirement(
            identifier,
            self.resources.get("incidents", identifier),
            self.resources.get("incidents", identifier)["lifecycle"]["mitigation"],
        )

    async def _finish_retirement(self, identifier, incident, mitigation):
        c = self.resources.get("connections", incident["connectionId"])

        def retire(v):
            if v["route"].get("buildId") == mitigation["buildId"] and v["route"].get("mode") == "BRIDGE":
                v.update(
                    route={"mode": "DIRECT"},
                    openIncidentId=None,
                    repairIncidentId=identifier,
                    generation=v["generation"] + 1,
                )
                return
            if v["route"].get("mode") == "DIRECT" and v.get("repairIncidentId") == identifier:
                return
            else:
                raise Conflict("The active temporary bridge changed during verification")

        self.resources.update("connections", c["id"], "TEMPORARY_TRANSLATION_RETIRED", retire, c["revision"])
        await self.workflow.runner.stop(mitigation["buildId"])
        current = self.resources.get("incidents", identifier)
        verification = current["lifecycle"].get("verification") or {}
        self.record(
            identifier,
            "RESOLVED",
            verification=verification,
            mitigation={**mitigation, "status": "RETIRED", "retiredAt": now()},
        )
        self.monitor.stage(
            identifier,
            "RESOLVED",
            message="The repaired application release passed bypass checks; the temporary bridge was retired",
        )
        return {"ready": True, "retired": True}

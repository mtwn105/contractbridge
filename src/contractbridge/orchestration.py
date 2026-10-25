"""Temporal owns long waits; side effects stay in idempotent API activities."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy, WorkflowIDReusePolicy

TASK_QUEUE = "contractbridge-repairs"


@workflow.defn(name="ContractBridgeDualRepair")
class RepairWorkflow:
    def __init__(self):
        self.changed = True
        self.review_decision = None

    @workflow.signal
    def wake(self):
        self.changed = True

    @workflow.signal
    def review(self, value: dict):
        self.review_decision = value

    @workflow.run
    async def run(self, identifier: str):
        async def phase(name):
            return await workflow.execute_activity(
                "contractbridge.phase",
                args=[identifier, name],
                start_to_close_timeout=timedelta(minutes=12),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=3), maximum_attempts=3),
            )

        result = await phase("investigate")
        if not result.get("supported"):
            return "UNSUPPORTED"
        # No runtime change happens until an operator approves the verified
        # candidate. A rejection feeds the review comment back into the model.
        while True:
            await workflow.wait_condition(lambda: self.review_decision is not None)
            decision = self.review_decision
            self.review_decision = None
            if (decision or {}).get("decision") == "approve":
                break
            await phase("revise")
        # Both derive from the same immutable, tested mapping. The temporary
        # mitigation is attempted before publication, never after merge alone.
        mitigation = await phase("mitigate")
        await phase("permanent")
        if not mitigation.get("active"):
            # The permanent repair can still be prepared without an authorized
            # runtime change. There is then no temporary bridge to retire.
            return "PERMANENT_PREPARED_WITHOUT_RUNTIME_MITIGATION"
        loops = 0
        while True:
            self.changed = False
            result = await phase("verify")
            if result.get("retired"):
                return "RESOLVED"
            try:
                await workflow.wait_condition(lambda: self.changed, timeout=timedelta(seconds=30))
            except TimeoutError:
                pass
            loops += 1
            if loops >= 1000:
                workflow.continue_as_new(identifier)


async def start_repair(settings, identifier):
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await client.start_workflow(
        "ContractBridgeDualRepair",
        identifier,
        id="contractbridge-repair-" + identifier,
        task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


async def wake_repair(settings, identifier):
    if settings.temporal_address:
        client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
        await client.get_workflow_handle("contractbridge-repair-" + identifier).signal("wake")


async def signal_review(settings, identifier, decision):
    if not settings.temporal_address:
        return False
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await client.get_workflow_handle("contractbridge-repair-" + identifier).signal(
        "review", {"decision": decision}
    )
    return True


def phase_activity(settings, api_url):
    @activity.defn(name="contractbridge.phase")
    async def execute(identifier: str, phase: str):
        async def heartbeat():
            while True:
                activity.heartbeat({"incident": identifier, "phase": phase})
                await asyncio.sleep(5)

        beats = asyncio.create_task(heartbeat())
        try:
            async with httpx.AsyncClient(timeout=700, trust_env=False, follow_redirects=False) as client:
                response = await client.post(
                    api_url.rstrip("/") + "/api/v1/dual/" + identifier + "/step",
                    json={"phase": phase},
                    headers={"Authorization": "Bearer " + settings.operator_token},
                )
                if response.status_code != 200:
                    raise RuntimeError("Repair phase failed (HTTP " + str(response.status_code) + ")")
                return response.json()
        finally:
            beats.cancel()
            try:
                await beats
            except asyncio.CancelledError:
                pass

    return execute


async def run_worker():
    import os

    from temporalio.worker import UnsandboxedWorkflowRunner, Worker

    from .config import Settings

    settings = Settings.environment()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    # Workflow code contains no I/O. Unsandboxed execution avoids importing the
    # HTTP transport in Temporal's restricted importer; activities own all I/O.
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[RepairWorkflow],
        activities=[phase_activity(settings, os.getenv("CONTROL_PLANE_URL", "http://control-plane:8080"))],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run_worker())

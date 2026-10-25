import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

from contractbridge.analysis import structural_changes
from contractbridge.api import create_app
from contractbridge.config import Settings
from contractbridge.generation import Generator
from contractbridge.models import Plan
from contractbridge.storage import Store
from contractbridge.util import digest, sha256_file

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-operator-token-with-at-least-32-characters"


class TestDiff:
    __test__ = False
    async def analyze(self, old, new):
        return {"engine": "test-double-not-oasdiff", "version": "test", "raw": [], "changes": structural_changes(old, new)}


class TestPlanner:
    __test__ = False
    def __init__(self, plan):
        self.plan = plan
    async def propose(self, *_args):
        return Plan.parse(self.plan), {"provider": "test-fixture", "liveInference": False}


class InProcessTestRunner:
    """Test-only implementation. Production only constructs SandboxRunner."""
    def __init__(self, root):
        self.root = root
    async def build(self, identifier, source_hash):
        source = self.root / "builds" / identifier / "source"
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short"], cwd=source,
                                env={**os.environ, "PYTHONPATH": str(source)}, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Generated project tests failed: " + result.stdout[-2000:])
        output = source.parent / "output"
        output.mkdir()
        artifact = output / "adapter.zip"
        with zipfile.ZipFile(artifact, "w") as archive:
            for p in sorted(source.rglob("*")):
                if p.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in p.relative_to(source).parts):
                    archive.write(p, p.relative_to(source))
        count = len(json.loads((source / "bundle.json").read_text())["fixtures"]) + 4
        return {"passed": True, "sourceHash": source_hash, "artifactHash": sha256_file(artifact), "tests": count,
                "verificationMode": "test-only-in-process", "pythonVersion": sys.version.split()[0]}
    async def activate(self, identifier, artifact_hash):
        return {"passed": True, "inspectToken": "private-runtime-token", "hostPort": 18080, "verificationMode": "test-only-no-container"}
    async def stop(self, _identifier):
        return {"passed": True}


@pytest.fixture
async def api(tmp_path, bundle):
    settings = Settings(ROOT, tmp_path / "artifacts", "sqlite:///" + str(tmp_path / "db.sqlite"), TOKEN)
    app = create_app(settings, diff=TestDiff(), planner=TestPlanner(bundle["plan"]), runner=InProcessTestRunner(settings.artifacts))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control", headers={"Authorization": "Bearer " + TOKEN}) as client:
        yield client, app, settings


async def seed(client, bundle):
    m = (await client.post("/api/v1/migrations", json={"title": "Orders migration"})).json()
    for label in ("old", "new"):
        response = await client.post(f'/api/v1/migrations/{m["id"]}/specs/{label}', json={"content": json.dumps(bundle[label + "Spec"])}, headers={"If-Match": str(m["revision"])})
        assert response.status_code == 200, response.text
        m = response.json()
    m = (await client.post(f'/api/v1/migrations/{m["id"]}/fixtures', json={"source": "synthetic", "fixtures": bundle["fixtures"]}, headers={"If-Match": str(m["revision"])})).json()
    return m


async def queued(client, m, action, payload=None):
    response = await client.post(f'/api/v1/migrations/{m["id"]}/{action}', json=payload or {}, headers={"If-Match": str(m["revision"])})
    assert response.status_code == 202, response.text
    job = (await client.get(response.headers["location"])).json()
    assert job["status"] == "PASSED", job
    return (await client.get(f'/api/v1/migrations/{m["id"]}')).json()


async def ready(client, bundle):
    m = await seed(client, bundle)
    m = await queued(client, m, "analyze")
    m = await queued(client, m, "proposals")
    assert m["state"] == "NEEDS_REVIEW"
    decisions = {i["key"]: "Confirmed against synthetic provider documentation and golden fixtures" for i in m["issues"]}
    response = await client.post(f'/api/v1/migrations/{m["id"]}/plan/approve', json={"decisions": decisions}, headers={"If-Match": str(m["revision"])})
    assert response.status_code == 200, response.text
    return await queued(client, response.json(), "builds")


async def test_auth_health_and_optimistic_concurrency(api, bundle):
    client, _, _ = api
    assert (await client.get("/api/v1/health", headers={"Authorization": ""})).status_code == 200
    assert (await client.get("/api/v1/migrations", headers={"Authorization": ""})).status_code == 401
    m = await seed(client, bundle)
    response = await client.post(f'/api/v1/migrations/{m["id"]}/specs/old', json={"content": json.dumps(bundle["oldSpec"])}, headers={"If-Match": "0"})
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_full_workflow_generated_project_and_artifact_activation(api, bundle):
    client, _, _ = api
    m = await ready(client, bundle)
    assert m["state"] == "READY_FOR_APPROVAL"
    build = m["builds"][-1]
    assert build["tests"] == 8
    download = await client.get(f'/api/v1/migrations/{m["id"]}/exports/{build["id"]}')
    assert download.status_code == 200 and download.headers["x-artifact-sha256"] == build["artifactHash"]
    response = await client.post(f'/api/v1/migrations/{m["id"]}/builds/{build["id"]}/activate',
        json={"confirmed": True, "artifactHash": build["artifactHash"]}, headers={"If-Match": str(m["revision"])})
    assert response.status_code == 202
    m = (await client.get(f'/api/v1/migrations/{m["id"]}')).json()
    assert m["state"] == "ACTIVE"
    assert "private-runtime-token" not in json.dumps(m)
    assert (await client.get(f'/api/v1/migrations/{m["id"]}/audit')).json()[-1]["action"] == "ACTIVATE_COMPLETED"


async def test_unapproved_build_and_unreviewed_mapping_blocked(api, bundle):
    client, _, _ = api
    m = await seed(client, bundle)
    m = await queued(client, m, "analyze")
    m = await queued(client, m, "proposals")
    assert (await client.post(f'/api/v1/migrations/{m["id"]}/builds', json={}, headers={"If-Match": str(m["revision"])})).status_code == 409
    assert (await client.post(f'/api/v1/migrations/{m["id"]}/plan/approve', json={"decisions": {}}, headers={"If-Match": str(m["revision"])})).status_code == 422


async def test_modified_artifact_and_stale_plan_cannot_activate(api, bundle):
    client, _, settings = api
    m = await ready(client, bundle)
    b = m["builds"][-1]
    artifact = settings.artifacts / "builds" / b["id"] / "output/adapter.zip"
    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    response = await client.post(f'/api/v1/migrations/{m["id"]}/builds/{b["id"]}/activate', json={"confirmed": True, "artifactHash": b["artifactHash"]}, headers={"If-Match": str(m["revision"])})
    assert response.status_code == 409
    response = await client.put(f'/api/v1/migrations/{m["id"]}/plan', json=m["plan"], headers={"If-Match": str(m["revision"])})
    assert response.json()["approvalHash"] is None
    assert response.json()["state"] == "NEEDS_REVIEW"


def test_deterministic_generation_and_python_syntax(core, tmp_path):
    generator = Generator(ROOT)
    plan, old, new, fixtures = core
    a = generator.generate(tmp_path / "a", plan, old, new, fixtures)
    b = generator.generate(tmp_path / "b", plan, old, new, fixtures)
    assert a == b
    for p in (tmp_path / "a").rglob("*.py"):
        compile(p.read_text(), str(p), "exec")
    assert digest(a["manifest"]) == a["sourceHash"]


def test_restart_marks_jobs_interrupted_and_unlocks(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "store.db"))
    m = store.create("Interrupted")
    store.job({"id": "job", "migrationId": m["id"], "status": "RUNNING"})
    store.mutate(m["id"], "QUEUED", lambda value: value.update(pendingJob="job"))
    store.recover()
    assert store.get_job("job")["status"] == "FAILED"
    assert store.get(m["id"])["pendingJob"] is None

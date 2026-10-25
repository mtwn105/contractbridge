"""Trusted Docker supervisor. The API and generated workloads have no Docker socket."""
import json
import os
import secrets
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from uuid import UUID

from contractbridge.util import canonical, digest, inside, sha256_file

ROOT = Path(os.getenv("ARTIFACT_ROOT", "/artifacts"))
VOLUME = os.getenv("ARTIFACT_VOLUME", "contractbridge_artifacts")
NETWORK = os.getenv("ADAPTER_NETWORK", "contractbridge_adapters")
SANDBOX = "contractbridge-sandbox:local"
UPSTREAM = os.getenv("DEFAULT_UPSTREAM", "")
APPROVED_UPSTREAMS = set(os.getenv("UPSTREAM_ALLOWLIST", "").split(","))


def docker(*args, timeout=30, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=timeout)


def identifier(value):
    if str(UUID(value)) != value:
        raise ValueError("Invalid identifier")
    return value


def verify_source(source: Path, expected: str):
    manifest = json.loads((source / "source-manifest.json").read_text())
    if digest(manifest) != expected:
        raise ValueError("Source manifest checksum mismatch")
    for name, checksum in manifest.items():
        path = inside(source, name)
        if path.is_symlink() or sha256_file(path) != checksum:
            raise ValueError("Generated source changed after freezing")


def mount(subpath, target, readonly=True):
    return f"type=volume,src={VOLUME},dst={target},volume-subpath={subpath}" + (",readonly" if readonly else "")


def build(job):
    build_id = identifier(job["buildId"])
    folder = ROOT / "builds" / build_id
    source = folder / "source"
    verify_source(source, job["sourceHash"])
    output = folder / "output"
    output.mkdir(exist_ok=True)
    os.chown(output, 10001, 10001)
    name = "cb-build-" + build_id
    try:
        docker("create", "--name", name, "--network", "none", "--read-only", "--user", "10001:10001",
               "--cpus", "2", "--memory", "512m", "--memory-swap", "512m", "--pids-limit", "64",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--label", "contractbridge.role=verification",
               "--tmpfs", "/work:rw,size=128m,uid=10001,gid=10001", "--tmpfs", "/tmp:rw,size=64m,uid=10001,gid=10001",
               "--mount", mount(f"builds/{build_id}/source", "/input"),
               "--mount", mount(f"builds/{build_id}/output", "/output", False), SANDBOX)
        result = docker("start", "--attach", name, timeout=180, check=False)
        inspected = json.loads(docker("inspect", name).stdout)[0]
        status = inspected["State"]["ExitCode"]
        if result.returncode or status:
            raise RuntimeError("Isolated compilation or tests failed; inspect the per-build verification report")
        xml = ET.parse(output / "tests.xml").getroot()
        suites = list(xml.iter("testsuite"))
        tests = sum(int(s.get("tests", 0)) for s in suites)
        failures = sum(int(s.get(k, 0)) for s in suites for k in ("failures", "errors", "skipped"))
        if tests < 1 or failures:
            raise RuntimeError("No complete passing test report was produced")
        verify_source(source, job["sourceHash"])
        host = inspected["HostConfig"]
        sandbox = {"network": host["NetworkMode"], "readOnlyRoot": host["ReadonlyRootfs"],
                   "user": inspected["Config"]["User"], "memoryBytes": host["Memory"], "nanoCpus": host["NanoCpus"],
                   "pidsLimit": host["PidsLimit"], "dockerSocketMounted": any(m["Destination"] == "/var/run/docker.sock" for m in inspected["Mounts"])}
        if sandbox != {"network": "none", "readOnlyRoot": True, "user": "10001:10001", "memoryBytes": 536870912,
                       "nanoCpus": 2000000000, "pidsLimit": 64, "dockerSocketMounted": False}:
            raise RuntimeError("Observed sandbox isolation differs from required policy")
        details = json.loads((output / "stages.json").read_text())
        report = {"passed": True, "sourceHash": job["sourceHash"], "tests": tests,
                  "verificationMode": "isolated-docker", "sandboxImageId": json.loads(docker("image", "inspect", SANDBOX).stdout)[0]["Id"],
                  **details, "sandbox": sandbox, "cases": [{"name": c.get("name"), "passed": True} for c in xml.iter("testcase")]}
        artifact = output / "adapter.zip"
        with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    entry = zipfile.ZipInfo(str(path.relative_to(source)), (1980, 1, 1, 0, 0, 0))
                    archive.writestr(entry, path.read_bytes())
            archive.writestr("verification.json", canonical(report))
        if artifact.stat().st_size > 10_000_000:
            raise RuntimeError("Artifact exceeds export size limit")
        report["artifactHash"] = sha256_file(artifact)
        (output / "verification.json").write_text(canonical(report))
        return report
    finally:
        docker("rm", "--force", name, check=False)


def activate(job):
    upstream = job.get("upstream", UPSTREAM)
    if upstream not in APPROVED_UPSTREAMS:
        raise ValueError("Runtime upstream is not approved by the supervisor")
    build_id = identifier(job["buildId"])
    artifact = ROOT / "builds" / build_id / "output/adapter.zip"
    if sha256_file(artifact) != job["artifactHash"]:
        raise RuntimeError("Artifact changed after verification")
    runtime = ROOT / "runtime" / build_id
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True)
    with zipfile.ZipFile(artifact) as archive:
        for info in archive.infolist():
            inside(runtime, info.filename)
            if info.file_size > 2_000_000 or info.external_attr >> 16 & 0o170000 == 0o120000:
                raise RuntimeError("Unsupported artifact entry")
        archive.extractall(runtime)
    source_manifest = json.loads((runtime / "source-manifest.json").read_text())
    for name, checksum in source_manifest.items():
        if sha256_file(inside(runtime, name)) != checksum:
            raise RuntimeError("Artifact source manifest mismatch")
    name = "cb-proxy-" + build_id
    docker("rm", "--force", name, check=False)
    token = secrets.token_urlsafe(32)
    docker("run", "--detach", "--name", name, "--network", NETWORK, "--read-only", "--user", "10001:10001",
           "--cpus", "1", "--memory", "256m", "--pids-limit", "64", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--tmpfs", "/tmp:rw,size=32m,uid=10001,gid=10001",
           "--mount", mount(f"runtime/{build_id}", "/adapter"), "--workdir", "/adapter",
           "--env", "UPSTREAM_BASE_URL=" + upstream, "--env", "BRIDGE_ALLOWED_UPSTREAMS=" + upstream,
           "--env", "BRIDGE_INSPECT_TOKEN=" + token, "--label", "contractbridge.role=approved-proxy", SANDBOX,
           "python", "-m", "uvicorn", "adapter:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log")
    for _ in range(30):
        health = docker("exec", name, "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=1)", check=False)
        if health.returncode == 0:
            info = json.loads(docker("inspect", name).stdout)[0]
            return {"passed": True, "inspectToken": token, "hostPort": 18080, "baseUrl": f"http://localhost:18080/{build_id}",
                    "runtimeImageId": info["Image"]}
        time.sleep(0.5)
    docker("rm", "--force", name, check=False)
    raise RuntimeError("Approved adapter failed its runtime health check")


def process(path):
    job = json.loads(path.read_text())
    if identifier(job["id"]) != path.stem:
        raise ValueError("Queue identifier mismatch")
    allowed = {"id", "action", "buildId", "sourceHash", "artifactHash", "upstream"}
    if set(job) - allowed:
        raise ValueError("Unknown supervisor request fields")
    match job["action"]:
        case "build":
            return build(job)
        case "activate":
            return activate(job)
        case "stop":
            docker("rm", "--force", "cb-proxy-" + identifier(job["buildId"]), check=False)
            return {"passed": True}
        case _:
            raise ValueError("Unknown supervisor action")


def main():
    queue = ROOT / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    while True:
        for path in sorted(queue.glob("*.json")):
            result = path.with_suffix(".result")
            if result.exists():
                continue
            try:
                value = process(path)
            except Exception:
                value = {"passed": False, "error": "Sandbox action failed; confirm Docker images, resources, and per-build reports"}
            pending = result.with_suffix(".tmp")
            pending.write_text(canonical(value))
            pending.replace(result)
        time.sleep(0.5)


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from .util import canonical, inside


class SandboxRunner:
    """File protocol to a separate trusted worker. No Docker socket in the API."""

    def __init__(self, root: Path, timeout=300):
        self.root, self.timeout = root.resolve(), timeout
        (self.root / "queue").mkdir(parents=True, exist_ok=True)

    async def request(self, action: str, build_id: str, **arguments) -> dict:
        identifier = str(uuid4())
        job = {"id": identifier, "action": action, "buildId": build_id, **arguments}
        path = inside(self.root, f"queue/{identifier}.json")
        pending = path.with_suffix(".tmp")
        pending.write_text(canonical(job))
        pending.replace(path)
        result = path.with_suffix(".result")
        for _ in range(self.timeout * 2):
            if result.exists():
                value = json.loads(result.read_text())
                if not value.get("passed"):
                    raise RuntimeError(value.get("error", "Sandbox verification failed"))
                return value
            await asyncio.sleep(0.5)
        raise RuntimeError("Sandbox worker unavailable or timed out; no verification result was accepted")

    async def build(self, build_id: str, source_hash: str) -> dict:
        return await self.request("build", build_id, sourceHash=source_hash)

    async def activate(self, build_id: str, artifact_hash: str, upstream: str | None = None) -> dict:
        return await self.request(
            "activate", build_id, artifactHash=artifact_hash, **({"upstream": upstream} if upstream else {})
        )

    async def stop(self, build_id: str) -> dict:
        return await self.request("stop", build_id)

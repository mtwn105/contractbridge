"""Publish only a checksummed, sandbox-verified adapter to a draft GitHub PR."""

from __future__ import annotations

import base64
import difflib
import io
import re
import zipfile
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx

from .storage import Conflict
from .util import digest, sha256_file, strict_loads


def artifact_files(path, checksum):
    if not path.exists() or path.stat().st_size > 10_000_000 or sha256_file(path) != checksum:
        raise Conflict("Verified candidate artifact is missing or changed")
    files = {}
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        if len(archive.infolist()) > 100:
            raise Conflict("Artifact contains too many files")
        total = 0
        for info in archive.infolist():
            name = PurePosixPath(info.filename)
            total += info.file_size
            if (
                name.is_absolute()
                or ".." in name.parts
                or info.is_dir()
                or info.file_size > 1_000_000
                or total > 5_000_000
            ):
                raise Conflict("Unsupported candidate artifact entry")
            if info.external_attr >> 16 & 0o170000 == 0o120000 or str(name) in files:
                raise Conflict("Artifact symlinks and duplicate paths are unsupported")
            files[str(name)] = archive.read(info).decode("utf-8")
    return files


def repair_patch(files, prefix):
    output = []
    for name, text in sorted(files.items()):
        for line in difflib.unified_diff(
            [], text.splitlines(keepends=True), fromfile="/dev/null", tofile="b/" + prefix + "/" + name
        ):
            output.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(output)


class GitHubPublisher:
    def __init__(self, repository="", token="", base="main", transport=None):
        self.repository, self.token, self.base, self.transport = repository, token, base, transport
        if repository and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("GITHUB_REPOSITORY must be owner/repository")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}", base) or ".." in base or "//" in base:
            raise ValueError("Unsupported GitHub base branch")

    @property
    def configured(self):
        return bool(self.repository and self.token)

    async def publish(
        self, incident, files, body, application_files=None, expected_application=None, application_paths=None
    ):
        if not self.configured:
            raise Conflict(
                "Configure GITHUB_REPOSITORY and GITHUB_TOKEN on the server; patch download is available"
            )
        branch = "contractbridge/repair/" + incident["id"] + "/" + incident["candidate"]["artifactHash"][:12]
        owner = self.repository.split("/")[0]
        prefix = "contractbridge-adapters/" + incident["connectionId"] + "/" + incident["id"]
        marker = "ContractBridge artifact " + incident["candidate"]["artifactHash"]
        if application_files is not None:
            allowed = set(application_paths or [])
            for path in allowed:
                parsed = PurePosixPath(path)
                if (
                    parsed.is_absolute()
                    or ".." in parsed.parts
                    or any(p.startswith(".") for p in parsed.parts)
                ):
                    raise Conflict("Permanent repair target must be a repository-relative file")
            if not allowed or len(allowed) > 10:
                raise Conflict("Permanent repair has no registered application target")
            if not allowed <= set(application_files) or any(
                not (p in allowed or p.startswith(prefix + "/")) for p in application_files
            ):
                raise Conflict("Permanent repair attempted to modify an unsupported application path")
            patch_hash = digest(application_files)
            branch += "-application-" + patch_hash[:12]
            marker += "\nApplication patch " + patch_hash
            tree_files = application_files
        else:
            tree_files = {prefix + "/" + name: content for name, content in files.items()}
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=self.transport,
            timeout=30,
            follow_redirects=False,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        ) as client:

            async def call(method, path, data=None, allow_missing=False):
                async with client.stream(method, "/repos/" + self.repository + path, json=data) as r:
                    raw = b""
                    async for part in r.aiter_bytes():
                        raw += part
                        if len(raw) > 5_000_000:
                            raise RuntimeError("GitHub response exceeds limit")
                    if allow_missing and r.status_code == 404:
                        return None
                    if r.status_code >= 400:
                        raise RuntimeError(
                            f"GitHub request failed (HTTP {r.status_code}); no merge or deployment occurred"
                        )
                    return strict_loads(raw)

            # Deterministic branch + commit marker make retries recover a remote
            # success whose response was lost, without overwriting another branch.
            existing = await call("GET", "/git/ref/heads/" + quote(branch, safe=""), allow_missing=True)
            if existing:
                head_sha = existing["object"]["sha"]
                commit = await call("GET", "/git/commits/" + existing["object"]["sha"])
                if commit.get("message") != marker:
                    raise Conflict("Repair branch exists with different contents")
            else:
                base = await call("GET", "/git/ref/heads/" + quote(self.base, safe=""))
                parent = await call("GET", "/git/commits/" + base["object"]["sha"])
                if application_files is not None:
                    if not expected_application:
                        raise Conflict("Permanent publication requires the expected application baseline")
                    for name in allowed:
                        remote = await call(
                            "GET",
                            "/contents/" + quote(name, safe="/") + "?ref=" + base["object"]["sha"],
                            allow_missing=True,
                        )
                        if name in expected_application:
                            expected = expected_application[name]
                            if (
                                remote is None
                                or remote.get("encoding") != "base64"
                                or base64.b64decode(remote.get("content", "")).decode("utf-8") != expected
                            ):
                                raise Conflict(
                                    "The repository application changed; do not overwrite an unverified baseline"
                                )
                        elif remote is not None:
                            raise Conflict(
                                "The permanent repair bundle path already exists on the base branch"
                            )
                tree = await call(
                    "POST",
                    "/git/trees",
                    {
                        "base_tree": parent["tree"]["sha"],
                        "tree": [
                            {"path": name, "mode": "100644", "type": "blob", "content": content}
                            for name, content in sorted(tree_files.items())
                        ],
                    },
                )
                commit = await call(
                    "POST",
                    "/git/commits",
                    {"message": marker, "tree": tree["sha"], "parents": [base["object"]["sha"]]},
                )
                head_sha = commit["sha"]
                await call("POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": commit["sha"]})
            pulls = await call("GET", "/pulls?state=all&head=" + quote(owner + ":" + branch, safe=""))
            if pulls:
                pr = pulls[0]
                if not pr.get("draft") or pr.get("base", {}).get("ref") != self.base:
                    raise Conflict("Existing repair PR no longer has the expected draft/base state")
            else:
                pr = await call(
                    "POST",
                    "/pulls",
                    {
                        "title": "Repair API contract drift: " + incident["id"][:8],
                        "head": branch,
                        "base": self.base,
                        "body": body,
                        "draft": True,
                    },
                )
            url = pr.get("html_url", "")
            if not url.startswith("https://github.com/" + self.repository + "/pull/"):
                raise RuntimeError("GitHub returned an unexpected PR URL")
            return {
                "url": url,
                "number": pr["number"],
                "branch": branch,
                "headSha": head_sha,
                "repository": self.repository,
                "draft": True,
                "base": self.base,
                "artifactHash": incident["candidate"]["artifactHash"],
            }

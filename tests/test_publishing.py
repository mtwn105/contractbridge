import base64
import json
import subprocess
from urllib.parse import unquote

import httpx
import pytest

from contractbridge.publishing import GitHubPublisher, repair_patch
from contractbridge.storage import Conflict


async def test_github_transport_protocol_uses_draft_additive_tree_and_idempotent_branch():
    calls, published = [], {"branch": False, "pr": None}
    checksum = "d" * 64
    incident = {
        "id": "11111111-1111-4111-8111-111111111111",
        "connectionId": "22222222-2222-4222-8222-222222222222",
        "candidate": {"artifactHash": checksum},
    }
    branch = "contractbridge/repair/" + incident["id"] + "/" + checksum[:12]
    prefix = "/repos/acme/adapter"

    def handle(request):
        path = unquote(request.url.path.removeprefix(prefix))
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, path, payload))
        assert request.headers["authorization"] == "Bearer private-test-token"
        assert request.headers["x-github-api-version"] == "2026-03-10"
        if path == "/git/ref/heads/" + branch:
            return (
                httpx.Response(200, json={"object": {"sha": "candidate"}})
                if published["branch"]
                else httpx.Response(404)
            )
        if path == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "base"}})
        if path == "/git/commits/base":
            return httpx.Response(200, json={"tree": {"sha": "base-tree"}})
        if path == "/git/trees":
            assert payload["base_tree"] == "base-tree"  # existing repository files survive
            assert all(
                f["path"].startswith("contractbridge-adapters/" + incident["connectionId"] + "/")
                for f in payload["tree"]
            )
            return httpx.Response(201, json={"sha": "new-tree"})
        if path == "/git/commits" and request.method == "POST":
            assert payload["parents"] == ["base"]
            return httpx.Response(201, json={"sha": "candidate"})
        if path == "/git/refs":
            assert payload == {"ref": "refs/heads/" + branch, "sha": "candidate"}
            published["branch"] = True
            return httpx.Response(201, json={})
        if path == "/git/commits/candidate":
            return httpx.Response(200, json={"message": "ContractBridge artifact " + checksum})
        if path == "/pulls" and request.method == "GET":
            return httpx.Response(200, json=[published["pr"]] if published["pr"] else [])
        if path == "/pulls" and request.method == "POST":
            assert payload["draft"] is True and payload["base"] == "main"
            published["pr"] = {
                "number": 12,
                "html_url": "https://github.com/acme/adapter/pull/12",
                "draft": True,
                "base": {"ref": "main"},
            }
            return httpx.Response(201, json=published["pr"])
        raise AssertionError((request.method, path))

    publisher = GitHubPublisher("acme/adapter", "private-test-token", transport=httpx.MockTransport(handle))
    first = await publisher.publish(incident, {"adapter.py": "pass\n"}, "Pending semantic review")
    second = await publisher.publish(incident, {"adapter.py": "pass\n"}, "Pending semantic review")
    assert first["url"] == second["url"] and first["draft"]
    assert len([c for c in calls if c[:2] == ("POST", "/pulls")]) == 1
    assert not any(c[0] == "PATCH" or "/merge" in c[1] for c in calls)


async def test_unconfigured_publisher_never_claims_a_pr():
    with pytest.raises(Conflict):
        await GitHubPublisher().publish({}, {}, "")


def test_downloaded_patch_applies_and_preserves_no_final_newline(tmp_path):
    patch = repair_patch(
        {"adapter.py": 'print("hello")', "bundle.json": '{"plan":"data"}'}, "repairs/candidate"
    )
    result = subprocess.run(["git", "apply", "-"], input=patch, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "repairs/candidate/adapter.py").read_text() == 'print("hello")'
    assert (tmp_path / "repairs/candidate/bundle.json").read_text() == '{"plan":"data"}'


async def test_changed_candidate_uses_a_new_branch_instead_of_returning_an_old_pr():
    branches = []

    def handle(request):
        if "/git/ref/heads/contractbridge" in unquote(request.url.path):
            branches.append(unquote(request.url.path))
            return httpx.Response(404)
        return httpx.Response(403)

    publisher = GitHubPublisher("acme/adapter", "test-token", transport=httpx.MockTransport(handle))
    for checksum in ["a" * 64, "b" * 64]:
        incident = {
            "id": "11111111-1111-4111-8111-111111111111",
            "connectionId": "22222222-2222-4222-8222-222222222222",
            "candidate": {"artifactHash": checksum},
        }
        with pytest.raises(RuntimeError):
            await publisher.publish(incident, {"adapter.py": "pass"}, "Pending review")
    assert len(branches) == 2 and branches[0] != branches[1]


async def test_permanent_publication_refuses_to_overwrite_existing_unverified_bundle():
    incident = {
        "id": "11111111-1111-4111-8111-111111111111",
        "connectionId": "22222222-2222-4222-8222-222222222222",
        "candidate": {"artifactHash": "f" * 64},
    }
    module_path = "src/connectors/provider.py"
    bundle_path = "src/connectors/provider.json"
    expected_module = "# trusted registered baseline\n"
    writes = []

    def handle(request):
        path = unquote(request.url.path.removeprefix("/repos/acme/application"))
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            writes.append((request.method, path))
        if path.startswith("/git/ref/heads/contractbridge"):
            return httpx.Response(404)
        if path == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "base"}})
        if path == "/git/commits/base":
            return httpx.Response(200, json={"tree": {"sha": "base-tree"}})
        if path == "/contents/" + module_path:
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.b64encode(expected_module.encode()).decode(),
                },
            )
        if path == "/contents/" + bundle_path:
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.b64encode(b'{"owned":"by-application"}').decode(),
                },
            )
        raise AssertionError((request.method, path, request.url.query))

    publisher = GitHubPublisher("acme/application", "test-only-token", transport=httpx.MockTransport(handle))
    with pytest.raises(Conflict, match="bundle path already exists"):
        await publisher.publish(
            incident,
            {"adapter.py": "pass\n"},
            "Verified permanent repair",
            application_files={
                module_path: "# repaired client\n",
                bundle_path: '{"repair":"verified"}',
            },
            expected_application={module_path: expected_module},
            application_paths=[module_path, bundle_path],
        )
    assert writes == []

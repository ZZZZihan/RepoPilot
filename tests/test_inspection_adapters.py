from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path

import httpx
import pytest

from repopilot.adapters.filesystem import FixedRootRepositoryInspector
from repopilot.adapters.github import GitHubRepositoryInspector
from repopilot.errors import (
    InspectionLimitExceededError,
    RepositoryAccessError,
    RepositoryNotFoundError,
    RepositoryRateLimitedError,
    RepositoryTimeoutError,
    RepositoryUpstreamError,
)
from repopilot.inspection import InspectionLimits, RepositorySnapshot, SelectedEntry
from repopilot.models import GitHubRepositoryInput

_REPOSITORY = GitHubRepositoryInput(url="https://github.com/acme/tiny")
_TREE_SHA = "f" * 40


def _inspect(
    handler: httpx.MockTransport,
    *,
    limits: InspectionLimits | None = None,
) -> RepositorySnapshot:
    inspector = GitHubRepositoryInspector(
        limits=limits or InspectionLimits(),
        transport=handler,
    )
    return asyncio.run(inspector.inspect(_REPOSITORY))


def _tree_entry(
    path: str,
    payload: bytes,
    *,
    sha: str = "a" * 40,
    size: int | bool | None = None,
) -> dict[str, object]:
    return {
        "path": path,
        "type": "blob",
        "size": len(payload) if size is None else size,
        "sha": sha,
    }


def _tree_response(
    entries: list[dict[str, object]],
    *,
    padding: str = "",
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "sha": _TREE_SHA,
            "truncated": False,
            "tree": entries,
            "padding": padding,
        },
    )


def _github_transport(
    *,
    metadata: httpx.Response | None = None,
    tree: httpx.Response | None = None,
    blobs: dict[str, httpx.Response] | None = None,
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    metadata_response = (
        metadata if metadata is not None else httpx.Response(200, json={"default_branch": "main"})
    )
    tree_response = tree if tree is not None else _tree_response([])
    blob_responses = blobs if blobs is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.url.path == "/repos/acme/tiny":
            return metadata_response
        if request.url.path == "/repos/acme/tiny/git/trees/main":
            return tree_response
        prefix = "/repos/acme/tiny/git/blobs/"
        if request.url.path.startswith(prefix):
            sha = request.url.path.removeprefix(prefix)
            if sha in blob_responses:
                return blob_responses[sha]
        raise AssertionError(f"unexpected request: {request.url}")

    return httpx.MockTransport(handler)


def test_github_adapter_reads_only_selected_bounded_blobs() -> None:
    files = {
        "README.md": b"# Tiny\n",
        "pyproject.toml": b'[project]\nname = "tiny"\n',
        "tests/test_core.py": b"def test_core():\n    assert True\n",
        "src/tiny/core.py": b"def core():\n    return True\n",
        "docs/conf.py": b"project = 'Tiny'\n",
        "bench.py": b"def benchmark():\n    return True\n",
    }
    blob_shas = {
        path: hashlib.sha1(payload, usedforsecurity=False).hexdigest()
        for path, payload in files.items()
    }
    requests: list[httpx.Request] = []
    snapshot = _inspect(
        _github_transport(
            tree=_tree_response(
                [_tree_entry(path, payload, sha=blob_shas[path]) for path, payload in files.items()]
            ),
            blobs={
                blob_shas[path]: httpx.Response(
                    200,
                    json={
                        "encoding": "base64",
                        "content": base64.b64encode(payload).decode("ascii"),
                    },
                )
                for path, payload in files.items()
            },
            requests=requests,
        )
    )

    assert snapshot.repository.ref == "main"
    assert snapshot.repository.tree_sha == _TREE_SHA
    excluded_support_files = {"bench.py", "docs/conf.py"}
    assert {document.path for document in snapshot.documents} == set(files) - excluded_support_files
    assert all(request.url.host == "api.github.com" for request in requests)
    tree_request = next(request for request in requests if "/git/trees/" in request.url.path)
    assert tree_request.url.params["recursive"] == "1"
    assert sum("/git/blobs/" in request.url.path for request in requests) == 4
    assert snapshot.selection_truncated is False


@pytest.mark.parametrize(
    ("status_code", "headers", "error_type", "message"),
    [
        (302, {"Location": "https://example.test"}, RepositoryUpstreamError, "HTTP 302"),
        (401, {}, RepositoryAccessError, "rejected access"),
        (403, {}, RepositoryAccessError, "rejected access"),
        (403, {"Retry-After": "60"}, RepositoryRateLimitedError, "rate limit"),
        (403, {"X-RateLimit-Remaining": "0"}, RepositoryRateLimitedError, "rate limit"),
        (404, {}, RepositoryNotFoundError, "not found"),
        (429, {}, RepositoryRateLimitedError, "rate limit"),
    ],
)
def test_github_adapter_maps_non_success_statuses(
    status_code: int,
    headers: dict[str, str],
    error_type: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error_type, match=message):
        _inspect(_github_transport(metadata=httpx.Response(status_code, headers=headers)))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"{", "invalid JSON"),
        (b"[]", "unexpected JSON value"),
    ],
)
def test_github_adapter_rejects_invalid_json(body: bytes, message: str) -> None:
    with pytest.raises(RepositoryUpstreamError, match=message):
        _inspect(_github_transport(metadata=httpx.Response(200, content=body)))


@pytest.mark.parametrize("oversized_phase", ["metadata", "tree", "blob"])
def test_github_adapter_bounds_every_response(oversized_phase: str) -> None:
    payload = b"x" * 400 if oversized_phase == "blob" else b"x"
    blob_sha = "a" * 40
    metadata = httpx.Response(
        200,
        json={
            "default_branch": "main",
            "padding": "x" * 1_024 if oversized_phase == "metadata" else "",
        },
    )
    tree = _tree_response(
        [_tree_entry("pyproject.toml", payload, sha=blob_sha)],
        padding="x" * 1_024 if oversized_phase == "tree" else "",
    )
    blob = httpx.Response(
        200,
        json={
            "encoding": "base64",
            "content": base64.b64encode(payload).decode("ascii"),
        },
    )
    limits = InspectionLimits(
        max_file_bytes=1_024,
        max_total_bytes=1_024,
        max_response_bytes=512,
    )
    with pytest.raises(InspectionLimitExceededError, match="response exceeded"):
        _inspect(
            _github_transport(metadata=metadata, tree=tree, blobs={blob_sha: blob}),
            limits=limits,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "../pyproject.toml"),
        ("size", None),
        ("size", True),
        ("size", -1),
        ("sha", "not-an-object-id"),
    ],
)
def test_github_adapter_fails_closed_on_malformed_blob_entry(field: str, value: object) -> None:
    payload = b"[project]\n"
    entry = _tree_entry("pyproject.toml", payload)
    entry[field] = value

    with pytest.raises(RepositoryUpstreamError, match="malformed blob tree entry"):
        _inspect(_github_transport(tree=_tree_response([entry])))


@pytest.mark.parametrize(
    ("tree_size", "blob_content", "message"),
    [
        (1, "%%%", "invalid base64"),
        (2, base64.b64encode(b"x").decode("ascii"), "did not match"),
    ],
)
def test_github_adapter_rejects_invalid_blob_payloads(
    tree_size: int, blob_content: str, message: str
) -> None:
    blob_sha = "a" * 40
    with pytest.raises(RepositoryUpstreamError, match=message):
        _inspect(
            _github_transport(
                tree=_tree_response(
                    [_tree_entry("pyproject.toml", b"x", sha=blob_sha, size=tree_size)]
                ),
                blobs={
                    blob_sha: httpx.Response(
                        200,
                        json={"encoding": "base64", "content": blob_content},
                    )
                },
            )
        )


def test_github_adapter_omits_non_utf8_blob_and_marks_selection_truncated() -> None:
    files = {
        "README.md": ("a" * 40, b"\xff"),
        "pyproject.toml": ("b" * 40, b"[project]\n"),
    }
    snapshot = _inspect(
        _github_transport(
            tree=_tree_response(
                [_tree_entry(path, payload, sha=sha) for path, (sha, payload) in files.items()]
            ),
            blobs={
                sha: httpx.Response(
                    200,
                    json={
                        "encoding": "base64",
                        "content": base64.b64encode(payload).decode("ascii"),
                    },
                )
                for sha, payload in files.values()
            },
        )
    )

    assert [document.path for document in snapshot.documents] == ["pyproject.toml"]
    assert snapshot.selection_truncated is True


def test_github_adapter_rejects_truncated_tree() -> None:
    tree = httpx.Response(
        200,
        json={"sha": _TREE_SHA, "truncated": True, "tree": []},
    )

    with pytest.raises(InspectionLimitExceededError, match="truncated"):
        _inspect(_github_transport(tree=tree))


@pytest.mark.parametrize(
    ("field_present", "value"),
    [
        (False, None),
        (True, None),
        (True, "false"),
        (True, 0),
        (True, 1),
        (True, []),
    ],
)
def test_github_adapter_fails_closed_on_malformed_tree_truncation_flag(
    field_present: bool, value: object
) -> None:
    payload: dict[str, object] = {"sha": _TREE_SHA, "tree": []}
    if field_present:
        payload["truncated"] = value
    tree = httpx.Response(200, json=payload)

    with pytest.raises(RepositoryUpstreamError, match="malformed tree truncation flag"):
        _inspect(_github_transport(tree=tree))


def test_github_adapter_stops_at_tree_entry_bound() -> None:
    entries = [
        _tree_entry(f"src/module_{index}.py", b"x", sha=f"{index + 1:040x}") for index in range(3)
    ]
    limits = InspectionLimits(max_tree_entries=2, max_selected_files=2)

    with pytest.raises(InspectionLimitExceededError, match="more than 2 files"):
        _inspect(_github_transport(tree=_tree_response(entries)), limits=limits)


def test_github_adapter_enforces_file_byte_limit_independently() -> None:
    blob_sha = "a" * 40
    limits = InspectionLimits(max_file_bytes=4, max_total_bytes=8)
    transport = _github_transport(
        tree=_tree_response([_tree_entry("pyproject.toml", b"xxxx", sha=blob_sha)]),
        blobs={
            blob_sha: httpx.Response(
                200,
                json={"encoding": "base64", "content": base64.b64encode(b"xxxxx").decode()},
            )
        },
    )

    with pytest.raises(InspectionLimitExceededError, match="file byte limit"):
        _inspect(transport, limits=limits)


def test_github_adapter_enforces_total_byte_budget_independently() -> None:
    files = {
        "README.md": ("a" * 40, b"read"),
        "pyproject.toml": ("b" * 40, b"conf"),
    }
    requests: list[httpx.Request] = []
    snapshot = _inspect(
        _github_transport(
            tree=_tree_response(
                [_tree_entry(path, payload, sha=sha) for path, (sha, payload) in files.items()]
            ),
            blobs={
                sha: httpx.Response(
                    200,
                    json={"encoding": "base64", "content": base64.b64encode(payload).decode()},
                )
                for sha, payload in files.values()
            },
            requests=requests,
        ),
        limits=InspectionLimits(max_file_bytes=4, max_total_bytes=4),
    )

    assert [document.path for document in snapshot.documents] == ["README.md"]
    assert [request.url.path for request in requests if "/git/blobs/" in request.url.path] == [
        f"/repos/acme/tiny/git/blobs/{'a' * 40}"
    ]
    assert snapshot.selection_truncated is True


def test_github_adapter_maps_httpx_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(RepositoryTimeoutError, match="timed out"):
        _inspect(httpx.MockTransport(handler))


def test_github_adapter_does_not_redirect_bearer_token_off_host() -> None:
    requests: list[tuple[str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.host, request.headers.get("authorization")))
        return httpx.Response(302, headers={"Location": "https://attacker.example/repository"})

    inspector = GitHubRepositoryInspector(
        limits=InspectionLimits(),
        token="test-only-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RepositoryUpstreamError, match="HTTP 302"):
        asyncio.run(inspector.inspect(_REPOSITORY))

    assert requests == [("api.github.com", "Bearer test-only-token")]


def test_github_adapter_deadline_cancels_and_drains_bounded_blob_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        active = 0
        peak_active = 0
        cancelled = 0
        never_release = asyncio.Event()
        files = {
            "README.md": "1" * 40,
            "pyproject.toml": "2" * 40,
            "requirements.txt": "3" * 40,
            "setup.py": "4" * 40,
            "src/a.py": "5" * 40,
            "tests/test_a.py": "6" * 40,
        }

        async def fetch_document(
            _client: httpx.AsyncClient,
            _owner: str,
            _name: str,
            _selected: SelectedEntry,
        ) -> None:
            nonlocal active, peak_active, cancelled
            active += 1
            peak_active = max(peak_active, active)
            try:
                await never_release.wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise
            finally:
                active -= 1

        inspector = GitHubRepositoryInspector(
            limits=InspectionLimits(
                max_selected_files=6,
                inspection_timeout_seconds=0.2,
            ),
            transport=_github_transport(
                tree=_tree_response(
                    [_tree_entry(path, b"x", sha=sha) for path, sha in files.items()]
                )
            ),
        )
        monkeypatch.setattr(inspector, "_fetch_document", fetch_document)
        with pytest.raises(RepositoryTimeoutError, match="deadline"):
            await inspector.inspect(_REPOSITORY)

        assert peak_active == 4
        assert active == 0
        assert cancelled == 4

    asyncio.run(scenario())


def test_github_adapter_blob_failure_cancels_and_drains_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        blocker_started = asyncio.Event()
        blocker_cancelled = asyncio.Event()
        bad_sha = "a" * 40
        blocking_sha = "b" * 40

        async def fetch_document(
            _client: httpx.AsyncClient,
            _owner: str,
            _name: str,
            selected: SelectedEntry,
        ) -> None:
            if selected.entry.blob_sha == bad_sha:
                await blocker_started.wait()
                raise RepositoryUpstreamError("simulated blob failure")
            if selected.entry.blob_sha == blocking_sha:
                blocker_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    blocker_cancelled.set()
                    raise
            raise AssertionError(f"unexpected blob: {selected.entry.blob_sha}")

        inspector = GitHubRepositoryInspector(
            limits=InspectionLimits(),
            transport=_github_transport(
                tree=_tree_response(
                    [
                        _tree_entry("README.md", b"x", sha=bad_sha),
                        _tree_entry("pyproject.toml", b"x", sha=blocking_sha),
                    ]
                )
            ),
        )
        monkeypatch.setattr(inspector, "_fetch_document", fetch_document)
        with pytest.raises(RepositoryUpstreamError, match="simulated blob failure"):
            await inspector.inspect(_REPOSITORY)

        assert blocker_cancelled.is_set()

    asyncio.run(scenario())


def test_fixed_root_adapter_stops_at_tree_entry_bound(
    fixture_repository_root: Path,
) -> None:
    limits = InspectionLimits(
        max_tree_entries=3,
        max_selected_files=3,
        max_file_bytes=64 * 1024,
        max_total_bytes=192 * 1024,
    )
    inspector = FixedRootRepositoryInspector(
        root=fixture_repository_root,
        owner="acme",
        name="tiny-python",
        limits=limits,
    )

    with pytest.raises(InspectionLimitExceededError, match="more than 3 files"):
        asyncio.run(
            inspector.inspect(
                GitHubRepositoryInput(url="https://github.com/acme/tiny-python", ref="main")
            )
        )

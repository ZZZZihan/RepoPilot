from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import os
import stat
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
from repopilot.inspection import (
    InspectedDocument,
    InspectionLimits,
    RepositorySnapshot,
    SelectedEntry,
)
from repopilot.models import EvidenceCategory, GitHubRepositoryInput, InspectedRepository

_REPOSITORY = GitHubRepositoryInput(url="https://github.com/acme/tiny")
_TREE_SHA = "f" * 40


def _git_blob_oid(payload: bytes, *, algorithm: str = "sha1") -> str:
    object_payload = f"blob {len(payload)}\0".encode("ascii") + payload
    if algorithm == "sha1":
        return hashlib.sha1(  # noqa: S324 - Git object identity is SHA-1.
            object_payload,
            usedforsecurity=False,
        ).hexdigest()
    if algorithm == "sha256":
        return hashlib.sha256(object_payload).hexdigest()
    raise ValueError(f"unsupported Git object algorithm: {algorithm}")


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
    mode: str = "100644",
) -> dict[str, object]:
    return {
        "path": path,
        "type": "blob",
        "size": len(payload) if size is None else size,
        "sha": sha,
        "mode": mode,
    }


def _tree_response(
    entries: list[dict[str, object]],
    *,
    padding: str = "",
    tree_sha: str = _TREE_SHA,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "sha": tree_sha,
            "truncated": False,
            "tree": entries,
            "padding": padding,
        },
    )


def _inspected_document(
    *,
    path: str = "src/example.py",
    category: EvidenceCategory = EvidenceCategory.SOURCE,
    content: str = "VALUE = 'π'\n",
    size: int | None = None,
    sha256: str | None = None,
) -> InspectedDocument:
    payload = content.encode("utf-8")
    return InspectedDocument(
        path=path,
        category=category,
        size=len(payload) if size is None else size,
        sha256=hashlib.sha256(payload).hexdigest() if sha256 is None else sha256,
        content=content,
    )


def _repository_snapshot(
    *,
    all_paths: tuple[str, ...] | None = None,
    documents: tuple[InspectedDocument, ...] | None = None,
    limits: InspectionLimits | None = None,
) -> RepositorySnapshot:
    inspected_documents = documents if documents is not None else (_inspected_document(),)
    repository_paths = (
        all_paths
        if all_paths is not None
        else tuple(document.path for document in inspected_documents)
    )
    return RepositorySnapshot(
        repository=InspectedRepository(
            url="https://github.com/acme/tiny",
            owner="acme",
            name="tiny",
            ref="main",
            tree_sha=_TREE_SHA,
        ),
        all_paths=repository_paths,
        documents=inspected_documents,
        selection_truncated=False,
        limits=limits or InspectionLimits(),
    )


def test_inspected_document_binds_utf8_byte_size_and_digest_to_content() -> None:
    document = _inspected_document(content="π\n")

    assert document.size == len("π\n".encode())
    assert document.sha256 == hashlib.sha256("π\n".encode()).hexdigest()

    with pytest.raises(ValueError, match="UTF-8 byte length"):
        _inspected_document(content="π\n", size=len("π\n"))
    with pytest.raises(ValueError, match="sha256 must match"):
        _inspected_document(sha256="0" * 64)


def test_inspected_document_requires_safe_path_and_canonical_category() -> None:
    with pytest.raises(ValueError, match="safe repository path"):
        _inspected_document(path="../src/example.py")
    with pytest.raises(ValueError, match="canonical path classification"):
        _inspected_document(category=EvidenceCategory.TEST)


@pytest.mark.parametrize(
    ("all_paths", "message"),
    [
        ((), "at least one repository path"),
        (("../src/example.py",), "safe repository paths"),
        (("src/example.py", "src/example.py"), "all_paths must be unique"),
    ],
)
def test_repository_snapshot_requires_safe_unique_nonempty_all_paths(
    all_paths: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _repository_snapshot(all_paths=all_paths)


def test_repository_snapshot_requires_unique_documents_with_tree_membership() -> None:
    document = _inspected_document()
    with pytest.raises(ValueError, match="at least one inspected document"):
        _repository_snapshot(all_paths=(document.path,), documents=())
    with pytest.raises(ValueError, match="document paths must be unique"):
        _repository_snapshot(
            all_paths=(document.path,),
            documents=(document, document),
        )

    foreign_document = _inspected_document(path="src/foreign.py")
    with pytest.raises(ValueError, match="document paths must belong to all_paths"):
        _repository_snapshot(
            all_paths=(document.path,),
            documents=(foreign_document,),
        )


def test_repository_snapshot_revalidates_document_integrity() -> None:
    forged_document = _inspected_document()
    object.__setattr__(forged_document, "sha256", "0" * 64)

    with pytest.raises(ValueError, match="sha256 must match"):
        _repository_snapshot(documents=(forged_document,))


def test_repository_snapshot_preserves_distinct_case_variant_paths() -> None:
    uppercase = _inspected_document(path="src/Example.py")
    lowercase = _inspected_document(path="src/example.py")
    all_paths = (lowercase.path, uppercase.path)

    snapshot = _repository_snapshot(
        all_paths=all_paths,
        documents=(lowercase, uppercase),
    )

    assert snapshot.all_paths == all_paths


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
    blob_shas = {path: _git_blob_oid(payload) for path, payload in files.items()}
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
                        "sha": blob_shas[path],
                        "size": len(payload),
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
    assert all(request.headers["accept-encoding"] == "identity" for request in requests)
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


def test_github_adapter_rejects_encoded_responses_before_reading_content() -> None:
    with pytest.raises(RepositoryUpstreamError, match="content encoding"):
        _inspect(
            _github_transport(
                metadata=httpx.Response(
                    200,
                    headers={"Content-Encoding": "gzip"},
                    content=gzip.compress(b'{"default_branch":"main"}'),
                )
            )
        )


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
            "sha": blob_sha,
            "size": len(payload),
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
        ("sha", "a" * 41),
        ("sha", "a" * 63),
        ("sha", "A" * 40),
    ],
)
def test_github_adapter_fails_closed_on_malformed_blob_entry(field: str, value: object) -> None:
    payload = b"[project]\n"
    entry = _tree_entry("pyproject.toml", payload)
    entry[field] = value

    with pytest.raises(RepositoryUpstreamError, match="malformed blob tree entry"):
        _inspect(_github_transport(tree=_tree_response([entry])))


@pytest.mark.parametrize("mode", [None, "", "100600", "120000", "160000"])
def test_github_adapter_rejects_non_regular_blob_modes(mode: object) -> None:
    payload = b"[project]\n"
    entry = _tree_entry("pyproject.toml", payload)
    entry["mode"] = mode

    with pytest.raises(RepositoryUpstreamError, match="non-regular or malformed"):
        _inspect(_github_transport(tree=_tree_response([entry])))


def test_github_adapter_rejects_duplicate_blob_paths() -> None:
    payload = b"[project]\n"
    entry = _tree_entry("pyproject.toml", payload)

    with pytest.raises(RepositoryUpstreamError, match="duplicate blob tree path"):
        _inspect(_github_transport(tree=_tree_response([entry, dict(entry)])))


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
                        json={
                            "sha": blob_sha,
                            "size": tree_size,
                            "encoding": "base64",
                            "content": blob_content,
                        },
                    )
                },
            )
        )


@pytest.mark.parametrize("response_sha", [None, "not-an-object-id", "b" * 40])
def test_github_adapter_binds_blob_response_to_requested_object(
    response_sha: str | None,
) -> None:
    payload = b"[project]\n"
    blob_sha = _git_blob_oid(payload)
    response = {
        "size": len(payload),
        "encoding": "base64",
        "content": base64.b64encode(payload).decode("ascii"),
    }
    if response_sha is not None:
        response["sha"] = response_sha

    with pytest.raises(RepositoryUpstreamError, match="requested object identifier"):
        _inspect(
            _github_transport(
                tree=_tree_response([_tree_entry("pyproject.toml", payload, sha=blob_sha)]),
                blobs={blob_sha: httpx.Response(200, json=response)},
            )
        )


@pytest.mark.parametrize("response_size", [None, True, -1, 0, 10_000])
def test_github_adapter_binds_blob_response_size_to_tree_entry(
    response_size: object,
) -> None:
    payload = b"[project]\n"
    blob_sha = _git_blob_oid(payload)
    response: dict[str, object] = {
        "sha": blob_sha,
        "encoding": "base64",
        "content": base64.b64encode(payload).decode("ascii"),
    }
    if response_size is not None:
        response["size"] = response_size

    with pytest.raises(RepositoryUpstreamError, match="tree entry size"):
        _inspect(
            _github_transport(
                tree=_tree_response([_tree_entry("pyproject.toml", payload, sha=blob_sha)]),
                blobs={blob_sha: httpx.Response(200, json=response)},
            )
        )


def test_github_adapter_recomputes_blob_object_identity_from_content() -> None:
    payload = b"[project]\n"
    claimed_blob_sha = _git_blob_oid(b"[project]\r")

    with pytest.raises(RepositoryUpstreamError, match="blob content did not match"):
        _inspect(
            _github_transport(
                tree=_tree_response([_tree_entry("pyproject.toml", payload, sha=claimed_blob_sha)]),
                blobs={
                    claimed_blob_sha: httpx.Response(
                        200,
                        json={
                            "sha": claimed_blob_sha,
                            "size": len(payload),
                            "encoding": "base64",
                            "content": base64.b64encode(payload).decode("ascii"),
                        },
                    )
                },
            )
        )


def test_github_adapter_accepts_a_wrapped_one_mebibyte_blob_within_limits() -> None:
    payload = b"x" * (1024 * 1024)
    blob_sha = _git_blob_oid(payload)
    encoded = base64.b64encode(payload).decode("ascii")
    github_wrapped_content = "\n".join(
        encoded[index : index + 60] for index in range(0, len(encoded), 60)
    )

    snapshot = _inspect(
        _github_transport(
            tree=_tree_response([_tree_entry("pyproject.toml", payload, sha=blob_sha)]),
            blobs={
                blob_sha: httpx.Response(
                    200,
                    json={
                        "sha": blob_sha,
                        "size": len(payload),
                        "encoding": "base64",
                        "content": github_wrapped_content,
                    },
                )
            },
        ),
        limits=InspectionLimits(
            max_file_bytes=len(payload),
            max_total_bytes=len(payload),
            max_response_bytes=2 * 1024 * 1024,
        ),
    )

    assert snapshot.documents[0].size == len(payload)


def test_github_adapter_accepts_sha256_blob_identity_and_executable_mode() -> None:
    payload = b"[project]\n"
    blob_sha = _git_blob_oid(payload, algorithm="sha256")

    snapshot = _inspect(
        _github_transport(
            tree=_tree_response(
                [
                    _tree_entry(
                        "pyproject.toml",
                        payload,
                        sha=blob_sha,
                        mode="100755",
                    )
                ],
                tree_sha="f" * 64,
            ),
            blobs={
                blob_sha: httpx.Response(
                    200,
                    json={
                        "sha": blob_sha,
                        "size": len(payload),
                        "encoding": "base64",
                        "content": base64.b64encode(payload).decode("ascii"),
                    },
                )
            },
        )
    )

    assert snapshot.documents[0].sha256 == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    ("tree_sha", "blob_algorithm"),
    [("f" * 40, "sha256"), ("f" * 64, "sha1")],
)
def test_github_adapter_rejects_mixed_repository_object_formats(
    tree_sha: str,
    blob_algorithm: str,
) -> None:
    payload = b"[project]\n"
    blob_sha = _git_blob_oid(payload, algorithm=blob_algorithm)

    with pytest.raises(RepositoryUpstreamError, match="malformed blob tree entry"):
        _inspect(
            _github_transport(
                tree=_tree_response(
                    [_tree_entry("pyproject.toml", payload, sha=blob_sha)],
                    tree_sha=tree_sha,
                )
            )
        )


def test_github_adapter_omits_non_utf8_blob_and_marks_selection_truncated() -> None:
    files = {
        "README.md": (_git_blob_oid(b"\xff"), b"\xff"),
        "pyproject.toml": (_git_blob_oid(b"[project]\n"), b"[project]\n"),
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
                        "sha": sha,
                        "size": len(payload),
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


@pytest.mark.parametrize("tree_sha", ["a" * 41, "a" * 63, "A" * 40])
def test_github_adapter_requires_an_exact_lowercase_tree_identifier(
    tree_sha: str,
) -> None:
    tree = httpx.Response(
        200,
        json={"sha": tree_sha, "truncated": False, "tree": []},
    )

    with pytest.raises(RepositoryUpstreamError, match="invalid tree identifier"):
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
                json={
                    "sha": blob_sha,
                    "size": 4,
                    "encoding": "base64",
                    "content": base64.b64encode(b"xxxxx").decode(),
                },
            )
        },
    )

    with pytest.raises(InspectionLimitExceededError, match="file byte limit"):
        _inspect(transport, limits=limits)


def test_github_adapter_enforces_total_byte_budget_independently() -> None:
    files = {
        "README.md": (_git_blob_oid(b"read"), b"read"),
        "pyproject.toml": (_git_blob_oid(b"conf"), b"conf"),
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
                    json={
                        "sha": sha,
                        "size": len(payload),
                        "encoding": "base64",
                        "content": base64.b64encode(payload).decode(),
                    },
                )
                for sha, payload in files.values()
            },
            requests=requests,
        ),
        limits=InspectionLimits(max_file_bytes=4, max_total_bytes=4),
    )

    assert [document.path for document in snapshot.documents] == ["README.md"]
    assert [request.url.path for request in requests if "/git/blobs/" in request.url.path] == [
        f"/repos/acme/tiny/git/blobs/{_git_blob_oid(b'read')}"
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


def _fixed_root_snapshot(
    root: Path,
    *,
    limits: InspectionLimits | None = None,
) -> RepositorySnapshot:
    inspector = FixedRootRepositoryInspector(
        root=root,
        owner="acme",
        name="fixture",
        limits=limits or InspectionLimits(),
    )
    return asyncio.run(
        inspector.inspect(GitHubRepositoryInput(url="https://github.com/acme/fixture", ref="main"))
    )


def test_fixed_root_tree_identity_hashes_unselected_file_content(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    source = root / "src" / "hidden.py"
    source.parent.mkdir(parents=True)
    (root / "README.md").write_text("# Fixture\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    source.write_bytes(b"A")
    limits = InspectionLimits(max_selected_files=1)

    before = _fixed_root_snapshot(root, limits=limits)
    source.write_bytes(b"B")
    after = _fixed_root_snapshot(root, limits=limits)

    assert [document.path for document in before.documents] == ["README.md"]
    assert before.repository.tree_sha != after.repository.tree_sha


@pytest.mark.skipif(os.name != "posix", reason="Git-like executable mode is POSIX-only")
def test_fixed_root_tree_identity_hashes_regular_file_executable_mode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "fixture"
    source = root / "src" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    source.chmod(stat.S_IRUSR | stat.S_IWUSR)

    before = _fixed_root_snapshot(root)
    source.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    after = _fixed_root_snapshot(root)

    assert before.repository.tree_sha != after.repository.tree_sha


@pytest.mark.skipif(os.name != "posix", reason="Git-like executable mode is POSIX-only")
@pytest.mark.parametrize("non_owner_execute", [stat.S_IXGRP, stat.S_IXOTH])
def test_fixed_root_git_mode_ignores_non_owner_execute_bits(
    tmp_path: Path,
    non_owner_execute: int,
) -> None:
    root = tmp_path / "fixture"
    source = root / "src" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    base_mode = stat.S_IRUSR | stat.S_IWUSR
    source.chmod(base_mode)
    before = _fixed_root_snapshot(root)

    source.chmod(base_mode | non_owner_execute)
    after = _fixed_root_snapshot(root)

    assert before.repository.tree_sha == after.repository.tree_sha


def test_fixed_root_tree_identity_uses_unambiguous_entry_framing(
    tmp_path: Path,
) -> None:
    payload_a = b"A"
    payload_b = b"B"
    digest_a = hashlib.sha256(payload_a).hexdigest()
    one_entry_root = tmp_path / "one-entry"
    two_entry_root = tmp_path / "two-entry"
    one_entry_root.mkdir()
    two_entry_root.mkdir()

    (one_entry_root / f"a.py1{digest_a}b.py").write_bytes(payload_b)
    (two_entry_root / "a.py").write_bytes(payload_a)
    (two_entry_root / "b.py").write_bytes(payload_b)

    one_entry = _fixed_root_snapshot(one_entry_root)
    two_entries = _fixed_root_snapshot(two_entry_root)

    assert one_entry.repository.tree_sha != two_entries.repository.tree_sha


@pytest.mark.skipif(os.name != "posix", reason="O_NOFOLLOW coverage is POSIX-only")
def test_fixed_root_rejects_a_selected_file_swapped_to_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "fixture"
    root.mkdir()
    selected = root / "pyproject.toml"
    selected.write_text("[project]\n", encoding="utf-8")
    outside = tmp_path / "outside.toml"
    outside.write_text("[project]\nname='outside'\n", encoding="utf-8")
    inspector = FixedRootRepositoryInspector(
        root=root,
        owner="acme",
        name="fixture",
        limits=InspectionLimits(),
    )
    original_read = inspector._read_stable_file

    def swap_before_read(
        fixture_file: object,
        *,
        capture_payload: bool,
    ) -> tuple[bytes, bytes | None]:
        if fixture_file.path == "pyproject.toml":
            selected.unlink()
            selected.symlink_to(outside)
        return original_read(fixture_file, capture_payload=capture_payload)  # type: ignore[arg-type]

    monkeypatch.setattr(inspector, "_read_stable_file", swap_before_read)

    with pytest.raises(RepositoryUpstreamError, match="snapshot file was opened"):
        asyncio.run(
            inspector.inspect(
                GitHubRepositoryInput(
                    url="https://github.com/acme/fixture",
                    ref="main",
                )
            )
        )


def test_fixed_root_rejects_same_size_content_changed_and_restored_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "fixture"
    root.mkdir()
    selected = root / "pyproject.toml"
    original_payload = b"[project]\n"
    selected.write_bytes(original_payload)
    inspector = FixedRootRepositoryInspector(
        root=root,
        owner="acme",
        name="fixture",
        limits=InspectionLimits(),
    )
    original_read = inspector._read_stable_file
    mutation_completed = False

    def mutate_and_restore_after_read(
        fixture_file: object,
        *,
        capture_payload: bool,
    ) -> tuple[bytes, bytes | None]:
        nonlocal mutation_completed
        result = original_read(fixture_file, capture_payload=capture_payload)  # type: ignore[arg-type]
        if fixture_file.path == "pyproject.toml" and not mutation_completed:
            mutation_completed = True
            selected.write_bytes(b"X" * len(original_payload))
            selected.write_bytes(original_payload)
        return result

    monkeypatch.setattr(inspector, "_read_stable_file", mutate_and_restore_after_read)

    with pytest.raises(RepositoryUpstreamError, match="content snapshot"):
        asyncio.run(
            inspector.inspect(
                GitHubRepositoryInput(
                    url="https://github.com/acme/fixture",
                    ref="main",
                )
            )
        )

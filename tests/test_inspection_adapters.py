from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path

import httpx
import pytest

from repopilot.adapters.filesystem import FixedRootRepositoryInspector
from repopilot.adapters.github import GitHubRepositoryInspector
from repopilot.errors import InspectionLimitExceededError
from repopilot.inspection import InspectionLimits
from repopilot.models import GitHubRepositoryInput


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
    requested_blob_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        if request.url.path == "/repos/acme/tiny":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path == "/repos/acme/tiny/git/trees/main":
            assert request.url.params["recursive"] == "1"
            return httpx.Response(
                200,
                json={
                    "sha": "f" * 40,
                    "truncated": False,
                    "tree": [
                        {
                            "path": path,
                            "type": "blob",
                            "size": len(payload),
                            "sha": blob_shas[path],
                        }
                        for path, payload in files.items()
                    ],
                },
            )
        prefix = "/repos/acme/tiny/git/blobs/"
        if request.url.path.startswith(prefix):
            requested_blob_paths.append(request.url.path)
            sha = request.url.path.removeprefix(prefix)
            path = next(path for path, candidate_sha in blob_shas.items() if candidate_sha == sha)
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.b64encode(files[path]).decode("ascii"),
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    inspector = GitHubRepositoryInspector(
        limits=InspectionLimits(), transport=httpx.MockTransport(handler)
    )
    snapshot = asyncio.run(
        inspector.inspect(GitHubRepositoryInput(url="https://github.com/acme/tiny"))
    )

    assert snapshot.repository.ref == "main"
    assert snapshot.repository.tree_sha == "f" * 40
    excluded_support_files = {"bench.py", "docs/conf.py"}
    assert {document.path for document in snapshot.documents} == set(files) - excluded_support_files
    assert len(requested_blob_paths) == len(files) - len(excluded_support_files)


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

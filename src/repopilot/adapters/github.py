"""Bounded, read-only GitHub REST adapter; it never clones or executes repositories."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
from typing import Any
from urllib.parse import quote

import httpx

from repopilot.errors import (
    InspectionLimitExceededError,
    RepositoryAccessError,
    RepositoryNotFoundError,
    RepositoryRateLimitedError,
    RepositoryTimeoutError,
    RepositoryUpstreamError,
    UnsupportedRepositoryError,
)
from repopilot.inspection import (
    InspectedDocument,
    InspectionLimits,
    RepositorySnapshot,
    SelectedEntry,
    TreeEntry,
    has_python_footprint,
    is_safe_repository_path,
    select_entries,
)
from repopilot.models import GitHubRepositoryInput, InspectedRepository

_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$")


class GitHubRepositoryInspector:
    """Inspect selected GitHub blobs under explicit count, byte, host, and time limits."""

    def __init__(
        self,
        *,
        limits: InspectionLimits,
        token: str | None = None,
        api_version: str = "2026-03-10",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._limits = limits
        self._token = token
        self._api_version = api_version
        self._transport = transport

    async def inspect(self, repository: GitHubRepositoryInput) -> RepositorySnapshot:
        try:
            async with asyncio.timeout(self._limits.inspection_timeout_seconds):
                return await self._inspect(repository)
        except TimeoutError as exc:
            raise RepositoryTimeoutError("GitHub inspection deadline was exceeded") from exc

    async def _inspect(self, repository: GitHubRepositoryInput) -> RepositorySnapshot:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "RepoPilot/0.1.0",
            "X-GitHub-Api-Version": self._api_version,
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"

        timeout = httpx.Timeout(self._limits.request_timeout_seconds)
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            owner = repository.owner
            name = repository.name
            metadata = await self._get_json(
                client,
                f"/repos/{owner}/{name}",
                max_bytes=min(64 * 1024, self._limits.max_response_bytes),
            )
            default_branch = metadata.get("default_branch")
            if not isinstance(default_branch, str) or not default_branch:
                raise RepositoryUpstreamError("GitHub did not return a usable default branch")
            requested_ref = repository.ref or default_branch

            tree = await self._get_json(
                client,
                f"/repos/{owner}/{name}/git/trees/{quote(requested_ref, safe='')}",
                params={"recursive": "1"},
                max_bytes=self._limits.max_response_bytes,
            )
            if tree.get("truncated") is True:
                raise InspectionLimitExceededError(
                    "GitHub truncated the recursive tree; this slice supports only "
                    "small repositories"
                )

            raw_tree = tree.get("tree")
            tree_sha = tree.get("sha")
            if not isinstance(raw_tree, list) or not isinstance(tree_sha, str):
                raise RepositoryUpstreamError("GitHub returned an invalid repository tree")
            if not _GIT_OBJECT_ID.fullmatch(tree_sha.lower()):
                raise RepositoryUpstreamError("GitHub returned an invalid tree identifier")

            entries = self._parse_entries(raw_tree)
            if len(entries) > self._limits.max_tree_entries:
                raise InspectionLimitExceededError(
                    f"repository has more than {self._limits.max_tree_entries} files"
                )
            if not entries:
                raise UnsupportedRepositoryError("repository has no inspectable files")
            if not has_python_footprint(entries):
                raise UnsupportedRepositoryError(
                    "repository does not appear to be a Python repository"
                )

            selection = select_entries(entries, self._limits)
            semaphore = asyncio.Semaphore(4)

            async def fetch(selected: SelectedEntry) -> InspectedDocument | None:
                async with semaphore:
                    return await self._fetch_document(client, owner, name, selected)

            tasks = [asyncio.create_task(fetch(item)) for item in selection.entries]
            try:
                fetched = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            documents = tuple(document for document in fetched if document is not None)
            if not documents:
                raise UnsupportedRepositoryError(
                    "repository has no bounded UTF-8 README, Python, or test "
                    "configuration to inspect"
                )
            if sum(document.size for document in documents) > self._limits.max_total_bytes:
                raise InspectionLimitExceededError(
                    "selected repository content exceeded the byte budget"
                )

            inspected_repository = InspectedRepository(
                url=repository.url,
                owner=owner,
                name=name,
                ref=requested_ref,
                tree_sha=tree_sha.lower(),
            )
            return RepositorySnapshot(
                repository=inspected_repository,
                all_paths=tuple(entry.path for entry in entries),
                documents=documents,
                selection_truncated=selection.truncated or len(documents) != len(selection.entries),
                limits=self._limits,
            )

    def _parse_entries(self, raw_tree: list[Any]) -> list[TreeEntry]:
        entries: list[TreeEntry] = []
        for item in raw_tree:
            if not isinstance(item, dict):
                raise RepositoryUpstreamError("GitHub returned a malformed tree entry")
            entry_type = item.get("type")
            if entry_type not in {"blob", "tree", "commit"}:
                raise RepositoryUpstreamError("GitHub returned a malformed tree entry")
            if entry_type != "blob":
                continue
            path = item.get("path")
            size = item.get("size")
            blob_sha = item.get("sha")
            if not isinstance(path, str) or not is_safe_repository_path(path):
                raise RepositoryUpstreamError("GitHub returned a malformed blob tree entry")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise RepositoryUpstreamError("GitHub returned a malformed blob tree entry")
            if not isinstance(blob_sha, str) or not _GIT_OBJECT_ID.fullmatch(blob_sha.lower()):
                raise RepositoryUpstreamError("GitHub returned a malformed blob tree entry")
            entries.append(TreeEntry(path=path, size=size, blob_sha=blob_sha.lower()))
            if len(entries) > self._limits.max_tree_entries:
                break
        entries.sort(key=lambda entry: entry.path.lower())
        return entries

    async def _fetch_document(
        self,
        client: httpx.AsyncClient,
        owner: str,
        name: str,
        selected: SelectedEntry,
    ) -> InspectedDocument | None:
        blob_sha = selected.entry.blob_sha
        if blob_sha is None:
            raise RepositoryUpstreamError("GitHub tree entry did not include a blob identifier")
        max_blob_response = min(
            (self._limits.max_file_bytes * 4 // 3) + 16 * 1024,
            self._limits.max_response_bytes,
        )
        blob = await self._get_json(
            client,
            f"/repos/{owner}/{name}/git/blobs/{blob_sha}",
            max_bytes=max_blob_response,
        )
        if blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
            raise RepositoryUpstreamError("GitHub returned an unsupported blob encoding")
        try:
            compact_content = "".join(blob["content"].split())
            payload = base64.b64decode(compact_content, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RepositoryUpstreamError("GitHub returned invalid base64 blob content") from exc
        if len(payload) > self._limits.max_file_bytes:
            raise InspectionLimitExceededError(
                "GitHub blob exceeded the configured file byte limit"
            )
        if selected.entry.size != len(payload):
            raise RepositoryUpstreamError("GitHub blob payload did not match its tree entry size")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return InspectedDocument(
            path=selected.entry.path,
            category=selected.category,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            content=content,
        )

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        max_bytes: int,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            async with client.stream("GET", path, params=params) as response:
                self._raise_for_status(response)
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > max_bytes:
                        raise InspectionLimitExceededError(
                            "GitHub response exceeded the configured byte limit"
                        )
        except httpx.TimeoutException as exc:
            raise RepositoryTimeoutError("GitHub inspection timed out") from exc
        except httpx.RequestError as exc:
            raise RepositoryUpstreamError("GitHub could not be reached") from exc

        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RepositoryUpstreamError("GitHub returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise RepositoryUpstreamError("GitHub returned an unexpected JSON value")
        return decoded

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        if response.status_code == 404:
            raise RepositoryNotFoundError("repository or requested ref was not found")
        rate_limit_remaining = response.headers.get("x-ratelimit-remaining", "").strip()
        has_retry_after = response.headers.get("retry-after") is not None
        if response.status_code == 429 or (
            response.status_code == 403 and (rate_limit_remaining == "0" or has_retry_after)
        ):
            raise RepositoryRateLimitedError("GitHub API rate limit was reached")
        if response.status_code in {401, 403}:
            raise RepositoryAccessError("GitHub rejected access to the repository")
        raise RepositoryUpstreamError(f"GitHub returned HTTP {response.status_code}")

"""Fixed-root repository adapter for fixtures and local contract tests.

The HTTP application never selects this adapter from user input. A caller must inject
both its root and expected GitHub identity, so it cannot browse arbitrary local paths.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from repopilot.errors import (
    InspectionLimitExceededError,
    RepositoryNotFoundError,
    UnsupportedRepositoryError,
)
from repopilot.inspection import (
    InspectedDocument,
    InspectionLimits,
    RepositorySnapshot,
    TreeEntry,
    has_python_footprint,
    is_safe_repository_path,
    select_entries,
)
from repopilot.models import GitHubRepositoryInput, InspectedRepository


class FixedRootRepositoryInspector:
    """Inspect one preconfigured local repository identity under production-like bounds."""

    def __init__(
        self,
        *,
        root: Path,
        owner: str,
        name: str,
        limits: InspectionLimits,
        default_ref: str = "main",
    ) -> None:
        self._root = root.resolve(strict=True)
        if not self._root.is_dir():
            raise ValueError("fixture repository root must be a directory")
        self._owner = owner
        self._name = name
        self._limits = limits
        self._default_ref = default_ref

    async def inspect(self, repository: GitHubRepositoryInput) -> RepositorySnapshot:
        return await asyncio.to_thread(self._inspect_sync, repository)

    def _inspect_sync(self, repository: GitHubRepositoryInput) -> RepositorySnapshot:
        if repository.owner != self._owner or repository.name != self._name:
            raise RepositoryNotFoundError(
                "repository is not available in the fixed fixture adapter"
            )
        if repository.ref is not None and repository.ref != self._default_ref:
            raise RepositoryNotFoundError(
                "requested ref is not available in the fixed fixture adapter"
            )

        entries: list[TreeEntry] = []
        for path in self._root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(self._root).as_posix()
            if ".git" in path.relative_to(self._root).parts or not is_safe_repository_path(
                relative
            ):
                continue
            size = path.stat().st_size
            entries.append(TreeEntry(path=relative, size=size))
            if len(entries) > self._limits.max_tree_entries:
                raise InspectionLimitExceededError(
                    f"repository has more than {self._limits.max_tree_entries} files"
                )
        entries.sort(key=lambda entry: entry.path.lower())

        if not entries:
            raise UnsupportedRepositoryError("repository has no inspectable files")
        if not has_python_footprint(entries):
            raise UnsupportedRepositoryError("repository does not appear to be a Python repository")

        selection = select_entries(entries, self._limits)
        documents: list[InspectedDocument] = []
        snapshot_hash = hashlib.sha256()
        for entry in entries:
            snapshot_hash.update(entry.path.encode("utf-8"))
            snapshot_hash.update(str(entry.size).encode("ascii"))

        skipped_non_text = False
        total_bytes = 0
        for selected in selection.entries:
            file_path = self._root / selected.entry.path
            resolved = file_path.resolve(strict=True)
            if not resolved.is_relative_to(self._root):
                skipped_non_text = True
                continue
            payload = resolved.read_bytes()
            if len(payload) > self._limits.max_file_bytes:
                raise InspectionLimitExceededError(
                    "fixture file exceeded the configured byte limit"
                )
            total_bytes += len(payload)
            if total_bytes > self._limits.max_total_bytes:
                raise InspectionLimitExceededError(
                    "fixture content exceeded the configured byte budget"
                )
            try:
                content = payload.decode("utf-8")
            except UnicodeDecodeError:
                skipped_non_text = True
                continue
            digest = hashlib.sha256(payload).hexdigest()
            snapshot_hash.update(selected.entry.path.encode("utf-8"))
            snapshot_hash.update(digest.encode("ascii"))
            documents.append(
                InspectedDocument(
                    path=selected.entry.path,
                    category=selected.category,
                    size=len(payload),
                    sha256=digest,
                    content=content,
                )
            )

        if not documents:
            raise UnsupportedRepositoryError(
                "repository has no bounded UTF-8 README, Python, or test configuration to inspect"
            )

        inspected_repository = InspectedRepository(
            url=repository.url,
            owner=self._owner,
            name=self._name,
            ref=self._default_ref,
            tree_sha=snapshot_hash.hexdigest(),
        )
        return RepositorySnapshot(
            repository=inspected_repository,
            all_paths=tuple(entry.path for entry in entries),
            documents=tuple(documents),
            selection_truncated=selection.truncated or skipped_non_text,
            limits=self._limits,
        )

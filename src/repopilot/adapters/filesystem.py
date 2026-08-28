"""Fixed-root repository adapter for fixtures and local contract tests.

The HTTP application never selects this adapter from user input. A caller must inject
both its root and expected GitHub identity, so it cannot browse arbitrary local paths.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import struct
from dataclasses import dataclass
from pathlib import Path

from repopilot.errors import (
    InspectionLimitExceededError,
    RepositoryNotFoundError,
    RepositoryUpstreamError,
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

_SNAPSHOT_DOMAIN = b"RepoPilot.FixedRootSnapshot\x00v2\x00"
_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class _FixtureFile:
    """Metadata frozen before a fixture file is opened for snapshotting."""

    path: str
    absolute_path: Path
    size: int
    device: int
    inode: int
    stat_mode: int
    modified_ns: int
    changed_ns: int
    git_mode: bytes


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

        fixture_files: list[_FixtureFile] = []
        directory_paths: list[str] = []
        opaque_paths: list[str] = []
        tree_entry_count = 0
        skipped_unsafe = False
        for path in self._root.rglob("*"):
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise RepositoryUpstreamError(
                    "fixture repository changed while its file list was inspected"
                ) from exc
            relative = path.relative_to(self._root).as_posix()
            if ".git" in path.relative_to(self._root).parts or not is_safe_repository_path(
                relative
            ):
                if ".git" not in path.relative_to(self._root).parts:
                    skipped_unsafe = True
                continue
            tree_entry_count += 1
            if tree_entry_count > self._limits.max_tree_entries:
                raise InspectionLimitExceededError(
                    f"repository has more than {self._limits.max_tree_entries} tree entries"
                )
            if stat.S_ISDIR(metadata.st_mode):
                directory_paths.append(relative)
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                opaque_paths.append(relative)
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise RepositoryUpstreamError(
                    "fixture repository changed while its paths were resolved"
                ) from exc
            if resolved != path or not resolved.is_relative_to(self._root):
                skipped_unsafe = True
                opaque_paths.append(relative)
                continue
            fixture_files.append(
                _FixtureFile(
                    path=relative,
                    absolute_path=path,
                    size=metadata.st_size,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    stat_mode=metadata.st_mode,
                    modified_ns=metadata.st_mtime_ns,
                    changed_ns=metadata.st_ctime_ns,
                    git_mode=b"100755" if metadata.st_mode & stat.S_IXUSR else b"100644",
                )
            )
        fixture_files.sort(key=lambda item: (item.path.casefold(), item.path))
        directory_paths.sort(key=lambda path: (path.casefold(), path))
        opaque_paths.sort(key=lambda path: (path.casefold(), path))
        entries = [TreeEntry(path=item.path, size=item.size) for item in fixture_files]

        if not entries:
            raise UnsupportedRepositoryError("repository has no inspectable files")
        if not has_python_footprint(entries):
            raise UnsupportedRepositoryError("repository does not appear to be a Python repository")

        selection = select_entries(entries, self._limits)
        selected_by_path = {item.entry.path: item for item in selection.entries}
        documents: list[InspectedDocument] = []
        snapshot_hash = hashlib.sha256()
        snapshot_hash.update(_SNAPSHOT_DOMAIN)
        for claim_kind, paths in (
            (b"D", directory_paths),
            (b"O", opaque_paths),
        ):
            snapshot_hash.update(claim_kind)
            snapshot_hash.update(struct.pack(">Q", len(paths)))
            for claimed_path in paths:
                encoded_claim = claimed_path.encode("utf-8")
                snapshot_hash.update(struct.pack(">I", len(encoded_claim)))
                snapshot_hash.update(encoded_claim)
        snapshot_hash.update(struct.pack(">Q", len(fixture_files)))

        skipped_non_text = skipped_unsafe
        total_bytes = 0
        for fixture_file in fixture_files:
            selected = selected_by_path.get(fixture_file.path)
            digest, payload = self._read_stable_file(
                fixture_file,
                capture_payload=selected is not None,
            )
            encoded_path = fixture_file.path.encode("utf-8")
            snapshot_hash.update(struct.pack(">I", len(encoded_path)))
            snapshot_hash.update(encoded_path)
            snapshot_hash.update(fixture_file.git_mode)
            snapshot_hash.update(struct.pack(">Q", fixture_file.size))
            snapshot_hash.update(digest)

            if selected is None:
                continue
            if payload is None:
                raise AssertionError("selected fixture payload was not captured")
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
            digest_hex = digest.hex()
            documents.append(
                InspectedDocument(
                    path=selected.entry.path,
                    category=selected.category,
                    size=len(payload),
                    sha256=digest_hex,
                    content=content,
                )
            )

        # Revalidate every original pathname after all bytes have been read. This
        # catches an early file that changes while a later entry is being hashed.
        for fixture_file in fixture_files:
            try:
                final_path = fixture_file.absolute_path.lstat()
            except OSError as exc:
                raise RepositoryUpstreamError(
                    "fixture repository changed after its content snapshot was captured"
                ) from exc
            self._assert_same_file(fixture_file, final_path)

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
            directory_paths=tuple(directory_paths),
            opaque_paths=tuple(opaque_paths),
        )

    def _read_stable_file(
        self,
        fixture_file: _FixtureFile,
        *,
        capture_payload: bool,
    ) -> tuple[bytes, bytes | None]:
        """Hash one regular file and reject a path or inode that changes mid-read."""

        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(fixture_file.absolute_path, flags)
        except OSError as exc:
            raise RepositoryUpstreamError(
                "fixture repository changed while a snapshot file was opened"
            ) from exc

        digest = hashlib.sha256()
        payload_parts: list[bytes] | None = [] if capture_payload else None
        try:
            opened = os.fstat(descriptor)
            self._assert_same_file(fixture_file, opened)
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                while chunk := stream.read(_READ_CHUNK_BYTES):
                    digest.update(chunk)
                    if payload_parts is not None:
                        payload_parts.append(chunk)
                finished = os.fstat(stream.fileno())
            self._assert_same_file(fixture_file, finished)
            final_path = fixture_file.absolute_path.lstat()
            self._assert_same_file(fixture_file, final_path)
        except OSError as exc:
            raise RepositoryUpstreamError(
                "fixture repository changed while a snapshot file was read"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        payload = b"".join(payload_parts) if payload_parts is not None else None
        if payload is not None and len(payload) != fixture_file.size:
            raise RepositoryUpstreamError(
                "fixture repository changed while a snapshot payload was captured"
            )
        return digest.digest(), payload

    @staticmethod
    def _assert_same_file(fixture_file: _FixtureFile, observed: os.stat_result) -> None:
        identity = (observed.st_dev, observed.st_ino)
        expected_identity = (fixture_file.device, fixture_file.inode)
        if (
            identity != expected_identity
            or observed.st_size != fixture_file.size
            or observed.st_mode != fixture_file.stat_mode
            or observed.st_mtime_ns != fixture_file.modified_ns
            or observed.st_ctime_ns != fixture_file.changed_ns
            or not stat.S_ISREG(observed.st_mode)
        ):
            raise RepositoryUpstreamError(
                "fixture repository changed while its content snapshot was captured"
            )

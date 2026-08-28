"""The bounded repository-inspection seam shared by production and test adapters."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from repopilot.models import (
    MAX_PLAN_EVIDENCE_ITEMS,
    EvidenceCategory,
    GitHubRepositoryInput,
    InspectedRepository,
    classify_evidence_path,
    validate_repository_path,
)


@dataclass(frozen=True, slots=True)
class InspectionLimits:
    max_tree_entries: int = 2_000
    max_selected_files: int = 32
    max_file_bytes: int = 64 * 1024
    max_total_bytes: int = 384 * 1024
    max_response_bytes: int = 2 * 1024 * 1024
    request_timeout_seconds: float = 10.0
    inspection_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        numeric_values = (
            self.max_tree_entries,
            self.max_selected_files,
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_response_bytes,
            self.request_timeout_seconds,
            self.inspection_timeout_seconds,
        )
        if any(value <= 0 for value in numeric_values):
            raise ValueError("all inspection limits must be positive")
        if self.max_selected_files > MAX_PLAN_EVIDENCE_ITEMS:
            raise ValueError(
                "max_selected_files cannot exceed "
                f"{MAX_PLAN_EVIDENCE_ITEMS}, the implementation-plan evidence limit"
            )
        if self.max_selected_files > self.max_tree_entries:
            raise ValueError("max_selected_files cannot exceed max_tree_entries")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("max_file_bytes cannot exceed max_total_bytes")


@dataclass(frozen=True, slots=True)
class TreeEntry:
    path: str
    size: int | None
    blob_sha: str | None = None


@dataclass(frozen=True, slots=True)
class SelectedEntry:
    entry: TreeEntry
    category: EvidenceCategory


@dataclass(frozen=True, slots=True)
class SelectionResult:
    entries: tuple[SelectedEntry, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class InspectedDocument:
    path: str
    category: EvidenceCategory
    size: int
    sha256: str
    content: str

    def __post_init__(self) -> None:
        self._validate_integrity()

    def _validate_integrity(self) -> None:
        if not isinstance(self.path, str):
            raise ValueError("inspected document path must be a safe repository path")
        try:
            validate_repository_path(self.path)
        except ValueError as exc:
            raise ValueError("inspected document path must be a safe repository path") from exc

        expected_category = classify_evidence_path(self.path)
        if expected_category is None or self.category is not expected_category:
            raise ValueError(
                "inspected document category must match its canonical path classification"
            )

        if not isinstance(self.content, str):
            raise ValueError("inspected document content must be UTF-8 text")
        try:
            payload = self.content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("inspected document content must be UTF-8 text") from exc

        if (
            not isinstance(self.size, int)
            or isinstance(self.size, bool)
            or self.size != len(payload)
        ):
            raise ValueError("inspected document size must match its UTF-8 byte length")

        expected_sha256 = hashlib.sha256(payload).hexdigest()
        if not isinstance(self.sha256, str) or not hmac.compare_digest(
            self.sha256, expected_sha256
        ):
            raise ValueError("inspected document sha256 must match its UTF-8 content")


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    repository: InspectedRepository
    all_paths: tuple[str, ...]
    documents: tuple[InspectedDocument, ...]
    selection_truncated: bool
    limits: InspectionLimits
    directory_paths: tuple[str, ...] = ()
    opaque_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.repository, InspectedRepository):
            raise ValueError("repository snapshot requires an inspected repository")
        InspectedRepository.model_validate(self.repository)

        if not isinstance(self.limits, InspectionLimits):
            raise ValueError("repository snapshot requires inspection limits")
        self.limits.__post_init__()
        if not isinstance(self.selection_truncated, bool):
            raise ValueError("repository snapshot selection_truncated must be a boolean")
        if not isinstance(self.all_paths, tuple):
            raise ValueError("repository snapshot all_paths must be an immutable tuple")
        if not self.all_paths:
            raise ValueError("repository snapshot must contain at least one repository path")

        for path in self.all_paths:
            if not isinstance(path, str):
                raise ValueError("repository snapshot all_paths must contain safe repository paths")
            try:
                validate_repository_path(path)
            except ValueError as exc:
                raise ValueError(
                    "repository snapshot all_paths must contain safe repository paths"
                ) from exc
        if len(self.all_paths) != len(set(self.all_paths)):
            raise ValueError("repository snapshot all_paths must be unique")

        for field_name, paths in (
            ("directory_paths", self.directory_paths),
            ("opaque_paths", self.opaque_paths),
        ):
            if not isinstance(paths, tuple):
                raise ValueError(f"repository snapshot {field_name} must be an immutable tuple")
            for path in paths:
                if not isinstance(path, str):
                    raise ValueError(
                        f"repository snapshot {field_name} must contain safe repository paths"
                    )
                try:
                    validate_repository_path(path)
                except ValueError as exc:
                    raise ValueError(
                        f"repository snapshot {field_name} must contain safe repository paths"
                    ) from exc
            if len(paths) != len(set(paths)):
                raise ValueError(f"repository snapshot {field_name} must be unique")

        claimed_paths = (*self.all_paths, *self.directory_paths, *self.opaque_paths)
        if len(claimed_paths) != len(set(claimed_paths)):
            raise ValueError("repository snapshot path kinds must be disjoint")
        if len(claimed_paths) > self.limits.max_tree_entries:
            raise ValueError("repository snapshot paths exceed the inspection tree limit")

        if not isinstance(self.documents, tuple):
            raise ValueError("repository snapshot documents must be an immutable tuple")
        if not self.documents:
            raise ValueError("repository snapshot must contain at least one inspected document")
        for document in self.documents:
            if not isinstance(document, InspectedDocument):
                raise ValueError("repository snapshot documents must contain inspected documents")
            document._validate_integrity()

        document_paths = tuple(document.path for document in self.documents)
        if len(document_paths) != len(set(document_paths)):
            raise ValueError("repository snapshot document paths must be unique")
        all_path_set = set(self.all_paths)
        if any(path not in all_path_set for path in document_paths):
            raise ValueError("repository snapshot document paths must belong to all_paths")
        if len(self.documents) > self.limits.max_selected_files:
            raise ValueError("repository snapshot documents exceed the selected-file limit")
        if any(document.size > self.limits.max_file_bytes for document in self.documents):
            raise ValueError("repository snapshot document exceeds the file-byte limit")
        if sum(document.size for document in self.documents) > self.limits.max_total_bytes:
            raise ValueError("repository snapshot documents exceed the total-byte limit")


class RepositoryInspector(Protocol):
    """Inspect one immutable repository snapshot without cloning or executing it."""

    async def inspect(self, repository: GitHubRepositoryInput) -> RepositorySnapshot: ...


_CATEGORY_PRIORITY = {
    EvidenceCategory.README: 0,
    EvidenceCategory.PROJECT_CONFIG: 1,
    EvidenceCategory.TEST_CONFIG: 2,
    EvidenceCategory.TEST: 3,
    EvidenceCategory.SOURCE: 4,
}


def is_safe_repository_path(path: str) -> bool:
    try:
        validate_repository_path(path)
    except ValueError:
        return False
    return True


def classify_path(path: str) -> EvidenceCategory | None:
    """Classify only files that can provide useful Python implementation evidence."""

    return classify_evidence_path(path)


def has_python_footprint(entries: list[TreeEntry]) -> bool:
    for entry in entries:
        category = classify_path(entry.path)
        if category is EvidenceCategory.PROJECT_CONFIG or (
            entry.path.lower().endswith(".py") and category is not None
        ):
            return True
    return False


def select_entries(entries: list[TreeEntry], limits: InspectionLimits) -> SelectionResult:
    """Select useful evidence deterministically while reserving a hard byte budget."""

    candidates: list[SelectedEntry] = []
    truncated = False
    for entry in entries:
        if not is_safe_repository_path(entry.path):
            truncated = True
            continue
        category = classify_path(entry.path)
        if category is None:
            continue
        if entry.size is not None and entry.size > limits.max_file_bytes:
            truncated = True
            continue
        candidates.append(SelectedEntry(entry=entry, category=category))

    candidates.sort(
        key=lambda selected: (
            _CATEGORY_PRIORITY[selected.category],
            len(PurePosixPath(selected.entry.path).parts),
            selected.entry.path.casefold(),
            selected.entry.path,
        )
    )

    selected_entries: list[SelectedEntry] = []
    reserved_bytes = 0
    for candidate in candidates:
        reservation = candidate.entry.size
        if reservation is None:
            reservation = limits.max_file_bytes
        if len(selected_entries) >= limits.max_selected_files:
            truncated = True
            break
        if reserved_bytes + reservation > limits.max_total_bytes:
            truncated = True
            continue
        selected_entries.append(candidate)
        reserved_bytes += reservation

    if len(selected_entries) < len(candidates):
        truncated = True
    return SelectionResult(entries=tuple(selected_entries), truncated=truncated)

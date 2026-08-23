"""The bounded repository-inspection seam shared by production and test adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from repopilot.models import (
    MAX_PLAN_EVIDENCE_ITEMS,
    EvidenceCategory,
    GitHubRepositoryInput,
    InspectedRepository,
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


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    repository: InspectedRepository
    all_paths: tuple[str, ...]
    documents: tuple[InspectedDocument, ...]
    selection_truncated: bool
    limits: InspectionLimits


class RepositoryInspector(Protocol):
    """Inspect one immutable repository snapshot without cloning or executing it."""

    async def inspect(self, repository: GitHubRepositoryInput) -> RepositorySnapshot: ...


_PROJECT_CONFIG_NAMES = {
    ".python-version",
    "pipfile",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements-test.txt",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
}
_TEST_CONFIG_NAMES = {"conftest.py", "noxfile.py", "pytest.ini", "tox.ini"}
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

    pure = PurePosixPath(path)
    parts = tuple(part.lower() for part in pure.parts)
    name = pure.name.lower()

    if len(parts) == 1 and (name == "readme" or name.startswith("readme.")):
        return EvidenceCategory.README
    if len(parts) == 1 and (name in _PROJECT_CONFIG_NAMES or name.startswith("requirements-")):
        return EvidenceCategory.PROJECT_CONFIG
    if name in _TEST_CONFIG_NAMES:
        return EvidenceCategory.TEST_CONFIG
    if (
        len(parts) >= 3
        and parts[0:2] == (".github", "workflows")
        and pure.suffix.lower()
        in {
            ".yml",
            ".yaml",
        }
    ):
        return EvidenceCategory.TEST_CONFIG
    if pure.suffix.lower() != ".py":
        return None
    if parts[0] in {"doc", "docs"} or name.startswith(("bench", "benchmark")):
        return None
    if "tests" in parts[:-1] or "test" in parts[:-1] or name.startswith("test_"):
        return EvidenceCategory.TEST
    return EvidenceCategory.SOURCE


def has_python_footprint(entries: list[TreeEntry]) -> bool:
    for entry in entries:
        name = PurePosixPath(entry.path).name.lower()
        category = classify_path(entry.path)
        if name in _PROJECT_CONFIG_NAMES or (
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
            selected.entry.path.lower(),
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

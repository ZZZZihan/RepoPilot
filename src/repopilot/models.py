"""Validated request, evidence, plan, and transition contracts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationInfo,
    field_validator,
    model_validator,
)

_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_TREE_SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_EVIDENCE_ID_PATTERN = re.compile(r"^E[1-9][0-9]*$")
_RFC3339_DATETIME_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$"
)
MAX_PLAN_EVIDENCE_ITEMS = 64
ISSUE_TITLE_MAX_LENGTH = 200
ISSUE_BODY_MAX_LENGTH = 20_000


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        revalidate_instances="always",
    )


def normalize_utc_datetime(value: datetime, *, field_name: str) -> datetime:
    """Require an unambiguous instant and serialize it consistently in UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def validate_datetime_input(
    value: object,
    *,
    field_name: str,
    mode: str,
) -> object:
    """Reject timestamp coercion while retaining strict JSON ISO datetime input."""

    if mode == "json":
        if not isinstance(value, str) or _RFC3339_DATETIME_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{field_name} must be an RFC 3339 datetime string")
        normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        try:
            datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an RFC 3339 datetime string") from exc
        return value
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    return value


def parse_github_repository_url(value: str) -> tuple[str, str, str]:
    """Return a canonical URL and owner/repository coordinates.

    Only github.com HTTPS repository URLs are accepted. This intentionally prevents
    callers from turning the inspection adapter into an arbitrary HTTP client.
    """

    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("repository URL is malformed") from exc

    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "github.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("repository URL must be an HTTPS github.com repository URL")

    segments = parsed.path.strip("/").split("/")
    if len(segments) != 2 or not all(segments):
        raise ValueError("repository URL must have the form https://github.com/OWNER/REPO")

    owner, name = segments
    if name.endswith(".git"):
        name = name[:-4]
    if not _SLUG_PATTERN.fullmatch(owner) or not _SLUG_PATTERN.fullmatch(name):
        raise ValueError("repository owner and name contain unsupported characters")

    return f"https://github.com/{owner}/{name}", owner, name


def parse_github_issue_url(value: str) -> tuple[str, str, int, str]:
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("issue URL is malformed") from exc

    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "github.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("issue URL must be an HTTPS github.com issue URL")

    segments = parsed.path.strip("/").split("/")
    if len(segments) != 4 or segments[2] != "issues":
        raise ValueError("issue URL must have the form https://github.com/OWNER/REPO/issues/NUMBER")
    owner, name, _, number_text = segments
    if not _SLUG_PATTERN.fullmatch(owner) or not _SLUG_PATTERN.fullmatch(name):
        raise ValueError("issue owner and repository contain unsupported characters")
    try:
        number = int(number_text)
    except ValueError as exc:
        raise ValueError("issue URL must end with a positive issue number") from exc
    if number < 1:
        raise ValueError("issue URL must end with a positive issue number")

    return owner, name, number, f"https://github.com/{owner}/{name}/issues/{number}"


def validate_repository_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 500
        or value != value.strip()
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("repository path is empty, too long, or unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("repository path must be normalized and relative")
    if any(part.casefold() == ".git" for part in path.parts):
        raise ValueError("repository path must not address Git administrative data")
    if str(path) != value:
        raise ValueError("repository path must be normalized and relative")
    return value


def validate_github_ref(value: str | None, *, required: bool = False) -> str | None:
    """Validate the one GitHub ref grammar shared by input and inspected identities."""

    if value is None:
        if required:
            raise ValueError("inspected repository ref is required")
        return None
    if (
        not _REF_PATTERN.fullmatch(value)
        or ".." in value
        or "//" in value
        or value.endswith(("/", ".", ".lock"))
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("repository ref contains unsupported characters")
    return value


class GitHubRepositoryInput(StrictModel):
    url: str = Field(min_length=1, max_length=512)
    ref: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        canonical, _, _ = parse_github_repository_url(value)
        return canonical

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str | None) -> str | None:
        return validate_github_ref(value)

    @property
    def owner(self) -> str:
        return parse_github_repository_url(self.url)[1]

    @property
    def name(self) -> str:
        return parse_github_repository_url(self.url)[2]


class IssueInput(StrictModel):
    title: str = Field(min_length=1, max_length=ISSUE_TITLE_MAX_LENGTH)
    body: str = Field(default="", max_length=ISSUE_BODY_MAX_LENGTH)
    number: StrictInt | None = Field(default=None, ge=1)
    url: str | None = Field(default=None, min_length=1, max_length=600)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if not any(not character.isspace() for character in value):
            raise ValueError("issue title must contain visible text")
        return value

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return parse_github_issue_url(value)[3]

    @model_validator(mode="after")
    def validate_number_matches_url(self) -> Self:
        if self.url is None:
            return self
        _, _, url_number, _ = parse_github_issue_url(self.url)
        if self.number is not None and self.number != url_number:
            raise ValueError("issue number does not match issue URL")
        if self.number is None:
            object.__setattr__(self, "number", url_number)
        return self


class CreatePlanRequest(StrictModel):
    repository: GitHubRepositoryInput
    issue: IssueInput


class PlanStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"


class EvidenceCategory(StrEnum):
    README = "readme"
    PROJECT_CONFIG = "project_config"
    TEST_CONFIG = "test_config"
    TEST = "test"
    SOURCE = "source"


VerificationTool = Literal["pytest", "ruff", "mypy"]


class VerificationDeclarationKind(StrEnum):
    COMMAND = "command"
    CONFIGURATION = "configuration"


class VerificationDeclaration(StrictModel):
    tool: VerificationTool
    kind: VerificationDeclarationKind
    arguments: list[str] = Field(default_factory=list, max_length=12)
    line_start: StrictInt = Field(ge=1)
    line_end: StrictInt = Field(ge=1)

    @field_validator("arguments")
    @classmethod
    def validate_m0_arguments(cls, value: list[str]) -> list[str]:
        if value:
            raise ValueError("M0 verification declarations do not support arguments")
        return value

    @model_validator(mode="after")
    def validate_line_range(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("verification declaration line_end must not precede line_start")
        return self


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


def classify_evidence_path(path: str) -> EvidenceCategory | None:
    """Return the one canonical M0 evidence category for a repository path."""

    pure = PurePosixPath(path)
    parts = tuple(part.casefold() for part in pure.parts)
    name = pure.name.casefold()

    if len(parts) == 1 and (name == "readme" or name.startswith("readme.")):
        return EvidenceCategory.README
    if len(parts) == 1 and (name in _PROJECT_CONFIG_NAMES or name.startswith("requirements-")):
        return EvidenceCategory.PROJECT_CONFIG
    if name in _TEST_CONFIG_NAMES:
        return EvidenceCategory.TEST_CONFIG
    if (
        len(pure.parts) == 3
        and pure.parts[0:2] == (".github", "workflows")
        and pure.suffix in {".yml", ".yaml"}
    ):
        return EvidenceCategory.TEST_CONFIG
    if pure.suffix.casefold() != ".py":
        return None
    if parts[0] in {"doc", "docs"} or name.startswith(("bench", "benchmark")):
        return None
    if "tests" in parts[:-1] or "test" in parts[:-1] or name.startswith("test_"):
        return EvidenceCategory.TEST
    return EvidenceCategory.SOURCE


class InspectedRepository(StrictModel):
    url: str = Field(min_length=1, max_length=512)
    owner: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    ref: str = Field(min_length=1, max_length=255)
    tree_sha: str

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        canonical, _, _ = parse_github_repository_url(value)
        return canonical

    @field_validator("owner", "name", mode="before")
    @classmethod
    def validate_raw_coordinate(cls, value: object, info: ValidationInfo) -> str:
        if not isinstance(value, str) or _SLUG_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"inspected repository {info.field_name} contains unsupported characters"
            )
        return value

    @field_validator("ref", mode="before")
    @classmethod
    def validate_ref(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("inspected repository ref must be a string")
        validated = validate_github_ref(value, required=True)
        if validated is None:  # pragma: no cover - required=True closes this branch
            raise ValueError("inspected repository ref is required")
        return validated

    @field_validator("tree_sha", mode="before")
    @classmethod
    def validate_tree_sha(cls, value: object) -> str:
        if not isinstance(value, str) or _TREE_SHA_PATTERN.fullmatch(value) is None:
            raise ValueError("tree_sha must be a lowercase hexadecimal content identifier")
        return value

    @model_validator(mode="after")
    def validate_repository_identity(self) -> Self:
        _, expected_owner, expected_name = parse_github_repository_url(self.url)
        if self.owner != expected_owner or self.name != expected_name:
            raise ValueError("inspected repository owner/name must match its canonical URL")
        return self


class InspectionSummary(StrictModel):
    files_seen: StrictInt = Field(ge=1)
    documents_read: StrictInt = Field(ge=1)
    selection_truncated: StrictBool
    max_tree_entries: StrictInt = Field(ge=1)
    max_selected_files: StrictInt = Field(ge=1)
    max_file_bytes: StrictInt = Field(ge=1)
    max_total_bytes: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def validate_bounded_counts(self) -> Self:
        if self.documents_read > self.files_seen:
            raise ValueError("documents_read cannot exceed files_seen")
        if self.files_seen > self.max_tree_entries:
            raise ValueError("files_seen cannot exceed max_tree_entries")
        if self.documents_read > self.max_selected_files:
            raise ValueError("documents_read cannot exceed max_selected_files")
        if self.max_selected_files > self.max_tree_entries:
            raise ValueError("max_selected_files cannot exceed max_tree_entries")
        if self.max_selected_files > MAX_PLAN_EVIDENCE_ITEMS:
            raise ValueError("max_selected_files exceeds the implementation-plan evidence limit")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("max_file_bytes cannot exceed max_total_bytes")
        return self


class EvidenceItem(StrictModel):
    id: str
    path: str
    category: EvidenceCategory
    line_start: StrictInt = Field(ge=1)
    line_end: StrictInt = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation: str = Field(min_length=1, max_length=400)
    declared_tools: list[VerificationDeclaration] = Field(default_factory=list, max_length=3)

    @field_validator("id", mode="before")
    @classmethod
    def validate_id(cls, value: object) -> str:
        if not isinstance(value, str) or not _EVIDENCE_ID_PATTERN.fullmatch(value):
            raise ValueError("evidence ID must use the E<number> form")
        return value

    @field_validator("path", mode="before")
    @classmethod
    def validate_path(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("repository path must be a string")
        return validate_repository_path(value)

    @field_validator("sha256", mode="before")
    @classmethod
    def validate_sha256_identity(cls, value: object) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("sha256 must be an exact lowercase hexadecimal digest")
        return value

    @model_validator(mode="after")
    def validate_line_range(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("evidence line_end must not precede line_start")
        expected_category = classify_evidence_path(self.path)
        if expected_category is not self.category:
            raise ValueError(
                "evidence category does not match the canonical repository-path classification"
            )
        if self.declared_tools and self.category not in {
            EvidenceCategory.README,
            EvidenceCategory.PROJECT_CONFIG,
            EvidenceCategory.TEST_CONFIG,
        }:
            raise ValueError(
                "verification declarations require README, project config, or test config evidence"
            )
        declaration_tools = [declaration.tool for declaration in self.declared_tools]
        if len(declaration_tools) != len(set(declaration_tools)):
            raise ValueError("evidence verification declarations must be unique by tool")
        if any(
            declaration.line_start < self.line_start or declaration.line_end > self.line_end
            for declaration in self.declared_tools
        ):
            raise ValueError(
                "verification declaration line range must be inside the evidence line window"
            )
        return self


class FileAction(StrEnum):
    INSPECT = "inspect"
    MODIFY = "modify"
    CREATE = "create"
    VERIFY = "verify"


class FileReference(StrictModel):
    path: str
    action: FileAction
    exists: StrictBool
    reason: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)

    @field_validator("path", mode="before")
    @classmethod
    def validate_path(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("repository path must be a string")
        return validate_repository_path(value)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def validate_raw_evidence_ids(cls, value: object) -> object:
        if isinstance(value, list | tuple) and any(
            not isinstance(item, str) or _EVIDENCE_ID_PATTERN.fullmatch(item) is None
            for item in value
        ):
            raise ValueError("file reference contains an invalid evidence ID")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("file reference evidence IDs must be unique")
        if not all(_EVIDENCE_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("file reference contains an invalid evidence ID")
        return value

    @model_validator(mode="after")
    def validate_action_matches_existence(self) -> Self:
        if self.action is FileAction.CREATE:
            if self.exists:
                raise ValueError("a create reference must identify a path that does not exist")
        elif not self.exists:
            raise ValueError("inspect, modify, and verify references must identify existing paths")
        return self


class StepKind(StrEnum):
    ANALYSIS = "analysis"
    IMPLEMENTATION = "implementation"
    TEST = "test"
    VERIFICATION = "verification"


class PlanStep(StrictModel):
    sequence: StrictInt = Field(ge=1)
    kind: StepKind
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1_500)
    file_references: list[FileReference] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_reference_actions(self) -> Self:
        allowed_actions = {
            StepKind.ANALYSIS: {FileAction.INSPECT},
            StepKind.IMPLEMENTATION: {FileAction.CREATE, FileAction.MODIFY},
            StepKind.TEST: {FileAction.CREATE, FileAction.MODIFY},
            StepKind.VERIFICATION: {FileAction.VERIFY},
        }
        invalid_actions = {
            reference.action
            for reference in self.file_references
            if reference.action not in allowed_actions[self.kind]
        }
        if invalid_actions:
            raise ValueError(
                f"{self.kind.value} step contains incompatible file-reference actions: "
                f"{sorted(action.value for action in invalid_actions)}"
            )
        return self


class VerificationIntent(StrictModel):
    tool: VerificationTool
    arguments: list[str] = Field(max_length=12)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    executed: Literal[False] = False

    @field_validator("executed", mode="before")
    @classmethod
    def validate_executed_literal(cls, value: object) -> object:
        if value is not False:
            raise ValueError("executed must be the boolean false")
        return value

    @field_validator("arguments")
    @classmethod
    def validate_m0_arguments(cls, value: list[str]) -> list[str]:
        if value:
            raise ValueError("M0 verification intents do not support arguments")
        return value

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def validate_raw_evidence_ids(cls, value: object) -> object:
        if isinstance(value, list | tuple) and any(
            not isinstance(item, str) or _EVIDENCE_ID_PATTERN.fullmatch(item) is None
            for item in value
        ):
            raise ValueError("verification intent contains an invalid evidence ID")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("verification intent evidence IDs must be unique")
        if not all(_EVIDENCE_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("verification intent contains an invalid evidence ID")
        return value


class ApprovalRecord(StrictModel):
    approved_by: str = Field(min_length=1, max_length=100)
    approved_at: datetime
    from_version: StrictInt = Field(ge=1)

    @field_validator("approved_by")
    @classmethod
    def normalize_approved_by(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("approved_by must contain visible text")
        return normalized

    @field_validator("approved_at", mode="before")
    @classmethod
    def validate_approved_at_input(cls, value: object, info: ValidationInfo) -> object:
        return validate_datetime_input(
            value,
            field_name="approved_at",
            mode=info.mode,
        )

    @field_validator("approved_at")
    @classmethod
    def normalize_approved_at(cls, value: datetime) -> datetime:
        return normalize_utc_datetime(value, field_name="approved_at")


class ImplementationPlan(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "x-repopilot-semantic-constraints": {
                "version": "1.0",
                "enforced_by": "pydantic-runtime",
                "constraints": [
                    {
                        "id": "repository-identity",
                        "description": (
                            "The inspected GitHub URL is canonical and agrees with owner/name; "
                            "the resolved ref uses the accepted GitHub ref grammar."
                        ),
                    },
                    {
                        "id": "evidence-graph",
                        "description": (
                            "Evidence IDs are unique and every file or verification reference "
                            "resolves to compatible observed evidence."
                        ),
                    },
                    {
                        "id": "step-sequence-and-actions",
                        "description": (
                            "Step sequences are contiguous from one and each step kind uses only "
                            "compatible file actions without contradictory create references."
                        ),
                    },
                    {
                        "id": "verification-declarations-and-readiness",
                        "description": (
                            "Verification intents require exact structured declarations; ready "
                            "requires an evidence-backed pytest intent."
                        ),
                    },
                    {
                        "id": "plan-state-and-approval",
                        "description": (
                            "Proposed plans are version one without approval; approved plans are "
                            "exactly version two from version one with UTC ordered timestamps."
                        ),
                    },
                ],
            }
        }
    )

    schema_version: Literal["1.0"] = "1.0"
    plan_id: UUID
    status: PlanStatus
    version: StrictInt = Field(ge=1)
    repository: InspectedRepository
    issue: IssueInput
    summary: str = Field(min_length=1, max_length=1_500)
    inspection: InspectionSummary
    evidence: list[EvidenceItem] = Field(
        min_length=1,
        max_length=MAX_PLAN_EVIDENCE_ITEMS,
    )
    steps: list[PlanStep] = Field(min_length=1, max_length=20)
    verification_intents: list[VerificationIntent] = Field(max_length=8)
    verification_readiness: Literal["ready", "needs_human_input"] = "needs_human_input"
    assumptions: list[str] = Field(max_length=20)
    risks: list[str] = Field(max_length=20)
    out_of_scope: list[str] = Field(min_length=1, max_length=20)
    created_at: datetime
    approval: ApprovalRecord | None = None

    @field_validator("plan_id", mode="before")
    @classmethod
    def validate_plan_id_identity(cls, value: object) -> UUID | str:
        if isinstance(value, UUID):
            return value
        if not isinstance(value, str):
            raise ValueError("plan_id must be a canonical UUID string or UUID instance")
        try:
            parsed = UUID(value)
        except ValueError as exc:
            raise ValueError("plan_id must be a canonical UUID string or UUID instance") from exc
        if str(parsed) != value:
            raise ValueError("plan_id must be a canonical UUID string or UUID instance")
        return value

    @model_validator(mode="before")
    @classmethod
    def derive_verification_readiness(cls, value: object) -> object:
        if not isinstance(value, dict) or "verification_readiness" in value:
            return value
        raw_intents = value.get("verification_intents")
        if not isinstance(raw_intents, list | tuple):
            raw_intents = ()
        has_pytest_intent = any(
            (isinstance(intent, VerificationIntent) and intent.tool == "pytest")
            or (isinstance(intent, dict) and intent.get("tool") == "pytest")
            for intent in raw_intents
        )
        derived = dict(value)
        derived["verification_readiness"] = "ready" if has_pytest_intent else "needs_human_input"
        return derived

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at_input(cls, value: object, info: ValidationInfo) -> object:
        return validate_datetime_input(
            value,
            field_name="created_at",
            mode=info.mode,
        )

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return normalize_utc_datetime(value, field_name="created_at")

    @model_validator(mode="after")
    def validate_graph_and_state(self) -> Self:
        evidence_ids = [item.id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence IDs must be unique")
        evidence_by_id = {item.id: item for item in self.evidence}
        known_evidence = set(evidence_by_id)
        observed_paths = {item.path for item in self.evidence}
        if self.inspection.documents_read != len(self.evidence):
            raise ValueError("inspection documents_read must equal the evidence item count")

        expected_sequences = list(range(1, len(self.steps) + 1))
        if [step.sequence for step in self.steps] != expected_sequences:
            raise ValueError("plan step sequence must be contiguous and start at 1")

        for step in self.steps:
            for reference in step.file_references:
                unknown = set(reference.evidence_ids) - known_evidence
                if unknown:
                    raise ValueError(
                        f"file reference {reference.path!r} cites unknown evidence: "
                        f"{sorted(unknown)}"
                    )
                cited_evidence = [evidence_by_id[item] for item in reference.evidence_ids]
                if reference.exists and not any(
                    item.path == reference.path for item in cited_evidence
                ):
                    raise ValueError(
                        f"existing file reference {reference.path!r} must cite same-path evidence"
                    )
                if not reference.exists and reference.path in observed_paths:
                    raise ValueError(
                        f"create reference {reference.path!r} conflicts with observed evidence"
                    )

        references_by_path: dict[str, list[FileReference]] = {}
        for step in self.steps:
            for reference in step.file_references:
                references_by_path.setdefault(reference.path, []).append(reference)
        for path, references in references_by_path.items():
            create_count = sum(reference.action is FileAction.CREATE for reference in references)
            if create_count > 1:
                raise ValueError(f"path {path!r} is created more than once")
            if create_count == 1 and len(references) > 1:
                raise ValueError(
                    f"created path {path!r} has contradictory snapshot-based references"
                )

        allowed_intent_categories = {
            EvidenceCategory.README,
            EvidenceCategory.PROJECT_CONFIG,
            EvidenceCategory.TEST_CONFIG,
        }
        for intent in self.verification_intents:
            unknown = set(intent.evidence_ids) - known_evidence
            if unknown:
                raise ValueError(f"verification intent cites unknown evidence: {sorted(unknown)}")
            disallowed = {
                evidence_by_id[item].category
                for item in intent.evidence_ids
                if evidence_by_id[item].category not in allowed_intent_categories
            }
            if disallowed:
                raise ValueError(
                    "verification intent evidence must come from README, project config, "
                    "or test config; found "
                    f"{sorted(category.value for category in disallowed)}"
                )
            allowed_declaration_kinds = (
                {
                    VerificationDeclarationKind.COMMAND,
                    VerificationDeclarationKind.CONFIGURATION,
                }
                if intent.tool == "pytest"
                else {VerificationDeclarationKind.COMMAND}
            )
            unsupported_evidence = [
                evidence_id
                for evidence_id in intent.evidence_ids
                if not any(
                    declaration.tool == intent.tool
                    and declaration.arguments == intent.arguments
                    and declaration.kind in allowed_declaration_kinds
                    for declaration in evidence_by_id[evidence_id].declared_tools
                )
            ]
            if unsupported_evidence:
                raise ValueError(
                    f"verification intent {intent.tool!r} cites evidence without an exact "
                    "supported tool declaration: "
                    f"{unsupported_evidence}"
                )

        if self.verification_readiness == "ready" and not any(
            intent.tool == "pytest" for intent in self.verification_intents
        ):
            raise ValueError(
                "ready verification requires at least 1 item: an evidence-backed pytest intent"
            )

        if self.status is PlanStatus.PROPOSED:
            if self.version != 1 or self.approval is not None:
                raise ValueError("a proposed plan must be version 1 with no approval record")
        elif self.status is PlanStatus.APPROVED:
            if self.version != 2 or self.approval is None:
                raise ValueError("an approved plan must be version 2 with an approval record")
            if self.approval.approved_at < self.created_at:
                raise ValueError("approval cannot predate plan creation")
            if self.approval.from_version != 1:
                raise ValueError("approval from_version must be 1")
        return self


class ApprovePlanRequest(StrictModel):
    approved_by: str = Field(min_length=1, max_length=100)
    expected_version: StrictInt = Field(ge=1)

    @field_validator("approved_by")
    @classmethod
    def normalize_approved_by(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("approved_by must contain visible text")
        return normalized


class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"

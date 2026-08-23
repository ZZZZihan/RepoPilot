"""Validated request, evidence, plan, and transition contracts."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_TREE_SHA_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_EVIDENCE_ID_PATTERN = re.compile(r"^E[1-9][0-9]*$")
MAX_PLAN_EVIDENCE_ITEMS = 64


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


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
    if not value or len(value) > 500 or "\\" in value or "\x00" in value:
        raise ValueError("repository path is empty, too long, or unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("repository path must be normalized and relative")
    if str(path) != value:
        raise ValueError("repository path must be normalized and relative")
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
        if value is None:
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

    @property
    def owner(self) -> str:
        return parse_github_repository_url(self.url)[1]

    @property
    def name(self) -> str:
        return parse_github_repository_url(self.url)[2]


class IssueInput(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=20_000)
    number: int | None = Field(default=None, ge=1)
    url: str | None = Field(default=None, min_length=1, max_length=600)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("issue title must contain visible text")
        return normalized

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


class InspectedRepository(StrictModel):
    url: str
    owner: str
    name: str
    ref: str
    tree_sha: str

    @field_validator("tree_sha")
    @classmethod
    def validate_tree_sha(cls, value: str) -> str:
        if not _TREE_SHA_PATTERN.fullmatch(value):
            raise ValueError("tree_sha must be a lowercase hexadecimal content identifier")
        return value


class InspectionSummary(StrictModel):
    files_seen: int = Field(ge=1)
    documents_read: int = Field(ge=1)
    selection_truncated: bool
    max_tree_entries: int = Field(ge=1)
    max_selected_files: int = Field(ge=1)
    max_file_bytes: int = Field(ge=1)
    max_total_bytes: int = Field(ge=1)


class EvidenceItem(StrictModel):
    id: str
    path: str
    category: EvidenceCategory
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation: str = Field(min_length=1, max_length=400)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _EVIDENCE_ID_PATTERN.fullmatch(value):
            raise ValueError("evidence ID must use the E<number> form")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_repository_path(value)

    @model_validator(mode="after")
    def validate_line_range(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("evidence line_end must not precede line_start")
        return self


class FileAction(StrEnum):
    INSPECT = "inspect"
    MODIFY = "modify"
    CREATE = "create"
    VERIFY = "verify"


class FileReference(StrictModel):
    path: str
    action: FileAction
    exists: bool
    reason: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_repository_path(value)

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("file reference evidence IDs must be unique")
        if not all(_EVIDENCE_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("file reference contains an invalid evidence ID")
        return value


class StepKind(StrEnum):
    ANALYSIS = "analysis"
    IMPLEMENTATION = "implementation"
    TEST = "test"
    VERIFICATION = "verification"


class PlanStep(StrictModel):
    sequence: int = Field(ge=1)
    kind: StepKind
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1_500)
    file_references: list[FileReference] = Field(min_length=1, max_length=12)


class VerificationIntent(StrictModel):
    tool: Literal["pytest", "ruff", "mypy"]
    arguments: list[str] = Field(max_length=12)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    executed: Literal[False] = False


class ApprovalRecord(StrictModel):
    approved_by: str = Field(min_length=1, max_length=100)
    approved_at: datetime
    from_version: int = Field(ge=1)

    @field_validator("approved_by")
    @classmethod
    def normalize_approved_by(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("approved_by must contain visible text")
        return normalized


class ImplementationPlan(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: UUID
    status: PlanStatus
    version: int = Field(ge=1)
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
    assumptions: list[str] = Field(max_length=20)
    risks: list[str] = Field(max_length=20)
    out_of_scope: list[str] = Field(min_length=1, max_length=20)
    created_at: datetime
    approval: ApprovalRecord | None = None

    @model_validator(mode="after")
    def validate_graph_and_state(self) -> Self:
        evidence_ids = [item.id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence IDs must be unique")
        known_evidence = set(evidence_ids)

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
        for intent in self.verification_intents:
            unknown = set(intent.evidence_ids) - known_evidence
            if unknown:
                raise ValueError(f"verification intent cites unknown evidence: {sorted(unknown)}")

        if self.status is PlanStatus.PROPOSED:
            if self.version != 1 or self.approval is not None:
                raise ValueError("a proposed plan must be version 1 with no approval record")
        elif self.status is PlanStatus.APPROVED:
            if self.version < 2 or self.approval is None:
                raise ValueError("an approved plan must have an approval record and version >= 2")
            if self.approval.approved_at < self.created_at:
                raise ValueError("approval cannot predate plan creation")
            if self.approval.from_version != self.version - 1:
                raise ValueError("approval from_version must be the immediately preceding version")
        return self


class ApprovePlanRequest(StrictModel):
    approved_by: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)

    @field_validator("approved_by")
    @classmethod
    def normalize_approved_by(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("approved_by must contain visible text")
        return normalized


class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"

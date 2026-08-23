"""Typed interfaces reserved for later stages; this slice provides no adapters for them."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from repopilot.models import ImplementationPlan, PlanStatus, StrictModel, validate_repository_path


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ApprovedPlanExecutionRequest(StrictModel):
    """A future executor receives an approved, immutable plan—not a shell string."""

    plan: ImplementationPlan

    @model_validator(mode="after")
    def require_approved_plan(self) -> Self:
        if self.plan.status is not PlanStatus.APPROVED:
            raise ValueError("execution requires an approved plan")
        return self


class VerificationResult(StrictModel):
    tool: str = Field(min_length=1, max_length=100)
    passed: bool
    summary: str = Field(min_length=1, max_length=2_000)


class ExecutionReport(StrictModel):
    plan_id: UUID
    plan_version: int = Field(ge=2)
    repository_tree_sha: str
    status: ExecutionStatus
    changed_paths: list[str] = Field(max_length=200)
    verification_results: list[VerificationResult] = Field(max_length=50)
    isolated_workspace_ref: str = Field(min_length=1, max_length=500)

    @field_validator("changed_paths")
    @classmethod
    def validate_changed_paths(cls, value: list[str]) -> list[str]:
        return [validate_repository_path(path) for path in value]


class ApprovedPlanExecutor(Protocol):
    """Future seam for policy-controlled isolated implementation and verification."""

    async def execute(self, request: ApprovedPlanExecutionRequest) -> ExecutionReport: ...


class PullRequestPublicationRequest(StrictModel):
    plan: ImplementationPlan
    execution: ExecutionReport
    title: str = Field(min_length=1, max_length=240)
    body: str = Field(max_length=20_000)

    @model_validator(mode="after")
    def validate_lineage(self) -> Self:
        if self.plan.status is not PlanStatus.APPROVED:
            raise ValueError("pull request publication requires an approved plan")
        if self.execution.status is not ExecutionStatus.SUCCEEDED:
            raise ValueError("pull request publication requires successful execution")
        if (
            self.execution.plan_id != self.plan.plan_id
            or self.execution.plan_version != self.plan.version
            or self.execution.repository_tree_sha != self.plan.repository.tree_sha
        ):
            raise ValueError("plan and execution lineage do not match")
        return self


class PullRequestPublication(StrictModel):
    plan_id: UUID
    pull_request_url: str = Field(min_length=1, max_length=1_000)


class PullRequestPublisher(Protocol):
    """Future seam for publishing an already verified isolated change."""

    async def publish(self, request: PullRequestPublicationRequest) -> PullRequestPublication: ...

"""Typed interfaces reserved for later stages; this slice provides no adapters for them."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, StrictBool, field_validator, model_validator

from repopilot.models import ImplementationPlan, StrictModel, validate_repository_path


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ApprovedPlanExecutionRequest(StrictModel):
    """A future executor receives an approved, immutable plan—not a shell string."""

    plan: ImplementationPlan

    @model_validator(mode="after")
    def reject_planning_only_plan(self) -> Self:
        raise ValueError(
            "the planning-only ImplementationPlan is not execution authority; "
            "a distinct future execution-sealed plan type is required"
        )


class VerificationResult(StrictModel):
    tool: Literal["pytest", "ruff", "mypy"]
    passed: StrictBool
    summary: str = Field(min_length=1, max_length=2_000)


class ExecutionReport(StrictModel):
    plan_id: UUID
    plan_version: int = Field(ge=2)
    repository_tree_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    status: ExecutionStatus
    changed_paths: list[str] = Field(max_length=200)
    verification_results: list[VerificationResult] = Field(max_length=50)
    isolated_workspace_ref: str = Field(min_length=1, max_length=500)

    @field_validator("changed_paths")
    @classmethod
    def validate_changed_paths(cls, value: list[str]) -> list[str]:
        return [validate_repository_path(path) for path in value]

    @model_validator(mode="after")
    def validate_successful_verification(self) -> Self:
        if self.status is not ExecutionStatus.SUCCEEDED:
            return self

        if (
            not self.changed_paths
            or not self.verification_results
            or not all(result.passed for result in self.verification_results)
        ):
            raise ValueError(
                "successful execution requires changed paths and non-empty passing verification"
            )

        if len(self.changed_paths) != len(set(self.changed_paths)):
            raise ValueError("successful execution changed paths must be unique")

        verification_tools = [result.tool for result in self.verification_results]
        if len(verification_tools) != len(set(verification_tools)):
            raise ValueError("successful execution verification tools must be unique")
        if "pytest" not in verification_tools:
            raise ValueError("successful execution requires pytest verification")

        return self


class ApprovedPlanExecutor(Protocol):
    """Future seam for policy-controlled isolated implementation and verification."""

    async def execute(self, request: ApprovedPlanExecutionRequest) -> ExecutionReport: ...


class PullRequestPublicationRequest(StrictModel):
    plan: ImplementationPlan
    execution: ExecutionReport
    title: str = Field(min_length=1, max_length=240)
    body: str = Field(max_length=20_000)

    @model_validator(mode="after")
    def reject_planning_only_plan(self) -> Self:
        raise ValueError(
            "the planning-only ImplementationPlan is not publication authority; "
            "a distinct future execution-sealed plan type is required"
        )


class PullRequestPublication(StrictModel):
    plan_id: UUID
    pull_request_url: str = Field(min_length=1, max_length=1_000)


class PullRequestPublisher(Protocol):
    """Future seam for publishing an already verified isolated change."""

    async def publish(self, request: PullRequestPublicationRequest) -> PullRequestPublication: ...

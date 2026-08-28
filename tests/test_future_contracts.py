from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from repopilot.future import (
    ApprovedPlanExecutionRequest,
    ExecutionReport,
    ExecutionStatus,
    PullRequestPublicationRequest,
    VerificationResult,
)
from repopilot.models import (
    ApprovalRecord,
    GitHubRepositoryInput,
    ImplementationPlan,
    IssueInput,
    PlanStatus,
)
from repopilot.planning import PlanBuilder


def successful_execution_report() -> ExecutionReport:
    return ExecutionReport(
        plan_id="00000000-0000-0000-0000-000000000001",
        plan_version=2,
        repository_tree_sha="a" * 40,
        status=ExecutionStatus.SUCCEEDED,
        changed_paths=["src/example.py"],
        verification_results=[
            VerificationResult(tool="pytest", passed=True, summary="suite passed")
        ],
        isolated_workspace_ref="refs/heads/codex/example",
    )


def test_current_plan_is_never_execution_or_publication_authority(fixture_inspector) -> None:
    snapshot = asyncio.run(
        fixture_inspector.inspect(
            GitHubRepositoryInput(url="https://github.com/acme/tiny-python", ref="main")
        )
    )
    proposed = PlanBuilder().build(
        snapshot,
        IssueInput(
            number=17,
            title="Give divide() an explicit zero-divisor error",
            body="Add regression coverage for the exact exception.",
        ),
    )
    approved_payload = proposed.model_dump(mode="python")
    approved_payload.update(
        {
            "status": PlanStatus.APPROVED,
            "version": 2,
            "approval": ApprovalRecord(
                approved_by="Reviewer",
                approved_at=datetime.now(UTC),
                from_version=1,
            ),
        }
    )
    approved_v1 = ImplementationPlan.model_validate(approved_payload)

    forged_schema = approved_v1.model_copy(update={"schema_version": "2.0"})
    for planning_only_plan, expected_error in (
        (proposed, "planning-only ImplementationPlan"),
        (approved_v1, "planning-only ImplementationPlan"),
        (forged_schema, "schema_version"),
    ):
        with pytest.raises(ValidationError, match=expected_error):
            ApprovedPlanExecutionRequest(plan=planning_only_plan)

        execution = ExecutionReport(
            plan_id=planning_only_plan.plan_id,
            plan_version=max(2, planning_only_plan.version),
            repository_tree_sha=planning_only_plan.repository.tree_sha,
            status=ExecutionStatus.SUCCEEDED,
            changed_paths=["src/tinycalc/calculator.py"],
            verification_results=[
                VerificationResult(tool="pytest", passed=True, summary="suite passed")
            ],
            isolated_workspace_ref="refs/heads/codex/example",
        )
        with pytest.raises(ValidationError, match=expected_error):
            PullRequestPublicationRequest(
                plan=planning_only_plan,
                execution=execution,
                title="Implement the approved change",
                body="Verified in an isolated workspace.",
            )


@pytest.mark.parametrize(
    ("changed_paths", "verification_results"),
    (
        ([], [VerificationResult(tool="pytest", passed=True, summary="suite passed")]),
        (["src/example.py"], []),
        (
            ["src/example.py"],
            [VerificationResult(tool="pytest", passed=False, summary="suite failed")],
        ),
    ),
)
def test_successful_execution_requires_passing_verification(
    changed_paths: list[str],
    verification_results: list[VerificationResult],
) -> None:
    with pytest.raises(ValidationError, match="non-empty passing verification"):
        ExecutionReport(
            plan_id="00000000-0000-0000-0000-000000000001",
            plan_version=2,
            repository_tree_sha="a" * 40,
            status=ExecutionStatus.SUCCEEDED,
            changed_paths=changed_paths,
            verification_results=verification_results,
            isolated_workspace_ref="refs/heads/codex/example",
        )


def test_execution_report_revalidates_model_copy_at_the_boundary() -> None:
    report = successful_execution_report()
    forged = report.model_copy(update={"changed_paths": ["src/example.py", "src/example.py"]})

    with pytest.raises(ValidationError, match="changed paths must be unique"):
        ExecutionReport.model_validate(forged.model_dump(mode="python"))


def test_verification_result_rejects_unsupported_tool() -> None:
    with pytest.raises(ValidationError, match="tool"):
        VerificationResult.model_validate(
            {"tool": "coverage", "passed": True, "summary": "suite passed"}
        )


@pytest.mark.parametrize("passed", [1, 0, "true", "false"])
def test_verification_result_rejects_coerced_booleans(passed: object) -> None:
    with pytest.raises(ValidationError, match="passed"):
        VerificationResult.model_validate(
            {"tool": "pytest", "passed": passed, "summary": "suite passed"}
        )


def test_successful_execution_rejects_duplicate_verification_tools() -> None:
    with pytest.raises(ValidationError, match="verification tools must be unique"):
        ExecutionReport(
            plan_id="00000000-0000-0000-0000-000000000001",
            plan_version=2,
            repository_tree_sha="a" * 40,
            status=ExecutionStatus.SUCCEEDED,
            changed_paths=["src/example.py"],
            verification_results=[
                VerificationResult(tool="pytest", passed=True, summary="unit tests passed"),
                VerificationResult(tool="pytest", passed=True, summary="integration tests passed"),
            ],
            isolated_workspace_ref="refs/heads/codex/example",
        )


def test_successful_execution_requires_pytest_verification() -> None:
    with pytest.raises(ValidationError, match="requires pytest verification"):
        ExecutionReport(
            plan_id="00000000-0000-0000-0000-000000000001",
            plan_version=2,
            repository_tree_sha="a" * 40,
            status=ExecutionStatus.SUCCEEDED,
            changed_paths=["src/example.py"],
            verification_results=[
                VerificationResult(tool="ruff", passed=True, summary="lint passed"),
                VerificationResult(tool="mypy", passed=True, summary="type check passed"),
            ],
            isolated_workspace_ref="refs/heads/codex/example",
        )


@pytest.mark.parametrize("repository_tree_sha", ["a" * 40, "b" * 64])
def test_execution_report_accepts_exact_git_object_id_lengths(
    repository_tree_sha: str,
) -> None:
    report = ExecutionReport(
        plan_id="00000000-0000-0000-0000-000000000001",
        plan_version=2,
        repository_tree_sha=repository_tree_sha,
        status=ExecutionStatus.FAILED,
        changed_paths=[],
        verification_results=[],
        isolated_workspace_ref="refs/heads/codex/example",
    )

    assert report.repository_tree_sha == repository_tree_sha


@pytest.mark.parametrize(
    "repository_tree_sha",
    ["a" * 39, "a" * 41, "a" * 63, "a" * 65, "A" * 40],
)
def test_execution_report_rejects_noncanonical_git_object_ids(
    repository_tree_sha: str,
) -> None:
    with pytest.raises(ValidationError, match="repository_tree_sha"):
        ExecutionReport(
            plan_id="00000000-0000-0000-0000-000000000001",
            plan_version=2,
            repository_tree_sha=repository_tree_sha,
            status=ExecutionStatus.FAILED,
            changed_paths=[],
            verification_results=[],
            isolated_workspace_ref="refs/heads/codex/example",
        )

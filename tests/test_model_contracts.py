from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from repopilot.adapters.filesystem import FixedRootRepositoryInspector
from repopilot.models import (
    ApprovePlanRequest,
    EvidenceCategory,
    EvidenceItem,
    FileAction,
    FileReference,
    GitHubRepositoryInput,
    ImplementationPlan,
    InspectedRepository,
    IssueInput,
    PlanStep,
    StepKind,
    VerificationDeclaration,
    VerificationDeclarationKind,
    classify_evidence_path,
)


def test_only_direct_canonical_github_workflow_paths_are_test_config() -> None:
    assert classify_evidence_path(".github/workflows/ci.yml") is EvidenceCategory.TEST_CONFIG
    assert classify_evidence_path(".github/workflows/archive/ci.yml") is None
    assert classify_evidence_path(".GITHUB/workflows/ci.yml") is None
    assert classify_evidence_path(".github/workflows/ci.YML") is None


def _plan_payload(fixture_inspector: FixedRootRepositoryInspector) -> dict[str, object]:
    snapshot = asyncio.run(
        fixture_inspector.inspect(
            GitHubRepositoryInput(url="https://github.com/acme/tiny-python", ref="main")
        )
    )
    evidence: list[dict[str, object]] = []
    evidence_ids_by_path: dict[str, str] = {}
    for index, document in enumerate(snapshot.documents, start=1):
        evidence_id = f"E{index}"
        evidence_ids_by_path[document.path] = evidence_id
        line_end = max(1, len(document.content.splitlines()))
        declarations: list[dict[str, object]] = []
        if document.path == "pyproject.toml":
            declarations.append(
                {
                    "tool": "pytest",
                    "kind": "configuration",
                    "arguments": [],
                    "line_start": 6,
                    "line_end": 6,
                }
            )
        evidence.append(
            {
                "id": evidence_id,
                "path": document.path,
                "category": document.category,
                "line_start": 1,
                "line_end": line_end,
                "sha256": document.sha256,
                "observation": "Observed in the immutable repository snapshot.",
                "declared_tools": declarations,
            }
        )

    source_path = "src/tinycalc/calculator.py"
    test_path = "tests/test_calculator.py"
    verification_path = "pyproject.toml"
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "plan_id": UUID("00000000-0000-4000-8000-000000000017"),
        "status": "proposed",
        "version": 1,
        "repository": snapshot.repository.model_dump(mode="python"),
        "issue": {
            "number": 17,
            "title": "Give divide() an explicit zero-divisor error",
            "body": "Add regression coverage for the exact exception.",
            "url": None,
        },
        "summary": "Add an explicit zero-divisor error with regression coverage.",
        "inspection": {
            "files_seen": len(snapshot.all_paths),
            "documents_read": len(snapshot.documents),
            "selection_truncated": snapshot.selection_truncated,
            "max_tree_entries": snapshot.limits.max_tree_entries,
            "max_selected_files": snapshot.limits.max_selected_files,
            "max_file_bytes": snapshot.limits.max_file_bytes,
            "max_total_bytes": snapshot.limits.max_total_bytes,
        },
        "evidence": evidence,
        "steps": [
            {
                "sequence": 1,
                "kind": "analysis",
                "title": "Confirm current behavior",
                "description": "Read the existing implementation before proposing a change.",
                "file_references": [
                    {
                        "path": source_path,
                        "action": "inspect",
                        "exists": True,
                        "reason": "Observed implementation path.",
                        "evidence_ids": [evidence_ids_by_path[source_path]],
                    }
                ],
            },
            {
                "sequence": 2,
                "kind": "implementation",
                "title": "Implement the change",
                "description": "Change only the observed implementation path.",
                "file_references": [
                    {
                        "path": source_path,
                        "action": "modify",
                        "exists": True,
                        "reason": "Observed implementation path.",
                        "evidence_ids": [evidence_ids_by_path[source_path]],
                    }
                ],
            },
            {
                "sequence": 3,
                "kind": "test",
                "title": "Add regression coverage",
                "description": "Exercise the requested behavior in the observed test path.",
                "file_references": [
                    {
                        "path": test_path,
                        "action": "modify",
                        "exists": True,
                        "reason": "Observed test path.",
                        "evidence_ids": [evidence_ids_by_path[test_path]],
                    }
                ],
            },
            {
                "sequence": 4,
                "kind": "verification",
                "title": "Record verification intent",
                "description": "Record but do not execute the declared test runner.",
                "file_references": [
                    {
                        "path": verification_path,
                        "action": "verify",
                        "exists": True,
                        "reason": "Observed test-runner configuration.",
                        "evidence_ids": [evidence_ids_by_path[verification_path]],
                    }
                ],
            },
        ],
        "verification_intents": [
            {
                "tool": "pytest",
                "arguments": [],
                "evidence_ids": [evidence_ids_by_path[verification_path]],
                "executed": False,
            }
        ],
        "verification_readiness": "ready",
        "assumptions": [],
        "risks": [],
        "out_of_scope": ["Executing the implementation plan"],
        "created_at": datetime(2026, 8, 27, tzinfo=UTC),
        "approval": None,
    }
    return ImplementationPlan.model_validate(payload).model_dump(mode="python")


def _plan(fixture_inspector: FixedRootRepositoryInspector) -> ImplementationPlan:
    return ImplementationPlan.model_validate(_plan_payload(fixture_inspector))


@pytest.mark.parametrize("value", (True, 1.0, "1"))
def test_plan_version_rejects_coercible_non_integers(
    fixture_inspector: FixedRootRepositoryInspector,
    value: object,
) -> None:
    payload = _plan_payload(fixture_inspector)
    payload["version"] = value

    with pytest.raises(ValidationError, match="version"):
        ImplementationPlan.model_validate(payload)


@pytest.mark.parametrize("value", (True, 1.0, "1"))
def test_approval_from_version_rejects_coercible_non_integers(
    fixture_inspector: FixedRootRepositoryInspector,
    value: object,
) -> None:
    payload = _plan_payload(fixture_inspector)
    payload.update(
        {
            "status": "approved",
            "version": 2,
            "approval": {
                "approved_by": "Reviewer",
                "approved_at": datetime(2026, 8, 27, 1, tzinfo=UTC),
                "from_version": value,
            },
        }
    )

    with pytest.raises(ValidationError, match="from_version"):
        ImplementationPlan.model_validate(payload)


@pytest.mark.parametrize("value", (True, 1.0, "1"))
def test_request_identity_integers_reject_coercible_non_integers(value: object) -> None:
    with pytest.raises(ValidationError, match="expected_version"):
        ApprovePlanRequest(approved_by="Reviewer", expected_version=value)  # type: ignore[arg-type]

    with pytest.raises(ValidationError, match="number"):
        IssueInput(number=value, title="Strict issue identity", body="")  # type: ignore[arg-type]


def test_issue_title_preserves_internal_whitespace() -> None:
    issue = IssueInput(
        number=1,
        title="Update `src/package/double  space.py`",
        body="",
    )

    assert issue.title == "Update `src/package/double  space.py`"


@pytest.mark.parametrize("tree_sha", ["a" * 40, "b" * 64])
def test_inspected_repository_accepts_exact_git_object_id_lengths(tree_sha: str) -> None:
    repository = InspectedRepository(
        url="https://github.com/acme/tiny",
        owner="acme",
        name="tiny",
        ref="main",
        tree_sha=tree_sha,
    )

    assert repository.tree_sha == tree_sha


@pytest.mark.parametrize("tree_sha", ["a" * 39, "a" * 41, "a" * 63, "a" * 65, "A" * 40])
def test_inspected_repository_rejects_noncanonical_git_object_ids(tree_sha: str) -> None:
    with pytest.raises(ValidationError, match="tree_sha"):
        InspectedRepository(
            url="https://github.com/acme/tiny",
            owner="acme",
            name="tiny",
            ref="main",
            tree_sha=tree_sha,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("owner", " acme"),
        ("name", "tiny "),
        ("ref", " main"),
        ("tree_sha", "a" * 40 + " "),
        ("owner", b"acme"),
        ("name", b"tiny"),
        ("ref", b"main"),
        ("tree_sha", b"a" * 40),
    ),
)
def test_inspected_repository_rejects_padded_or_coercible_identity_fields(
    field: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "url": "https://github.com/acme/tiny",
        "owner": "acme",
        "name": "tiny",
        "ref": "main",
        "tree_sha": "a" * 40,
    }
    payload[field] = value

    with pytest.raises(ValidationError, match=field):
        InspectedRepository.model_validate(payload)


def test_inspected_repository_normalizes_url_and_requires_matching_coordinates() -> None:
    repository = InspectedRepository(
        url=" https://github.com/acme/tiny.git ",
        owner="acme",
        name="tiny",
        ref="refs/heads/main",
        tree_sha="a" * 40,
    )

    assert repository.url == "https://github.com/acme/tiny"

    for coordinates in ({"owner": "other", "name": "tiny"}, {"owner": "acme", "name": "other"}):
        with pytest.raises(ValidationError, match="owner/name must match"):
            InspectedRepository(
                url="https://github.com/acme/tiny",
                ref="main",
                tree_sha="a" * 40,
                **coordinates,
            )


@pytest.mark.parametrize("ref", ["", "../main", "refs//heads/main", "refs/heads/main.lock"])
def test_inspected_repository_reuses_the_input_ref_contract(ref: str) -> None:
    with pytest.raises(ValidationError, match="ref"):
        InspectedRepository(
            url="https://github.com/acme/tiny",
            owner="acme",
            name="tiny",
            ref=ref,
            tree_sha="a" * 40,
        )


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        "src/.git/config",
        "src/.GIT/config",
        "nested/.Git/objects/aa",
    ],
)
def test_file_reference_rejects_git_administrative_component_at_any_depth(path: str) -> None:
    with pytest.raises(ValidationError, match="Git administrative data"):
        FileReference(
            path=path,
            action=FileAction.CREATE,
            exists=False,
            reason="contract test",
            evidence_ids=["E1"],
        )


def test_model_validate_revalidates_a_top_level_model_copy(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    plan = _plan(fixture_inspector)
    forged = plan.model_copy(update={"version": 2})

    with pytest.raises(ValidationError, match="proposed plan must be version 1"):
        ImplementationPlan.model_validate(forged)


def test_model_validate_revalidates_nested_model_copies(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    plan = _plan(fixture_inspector)
    forged_reference = plan.steps[1].file_references[0].model_copy(update={"exists": False})
    forged_step = plan.steps[1].model_copy(update={"file_references": [forged_reference]})
    forged_steps = list(plan.steps)
    forged_steps[1] = forged_step
    forged_plan = plan.model_copy(update={"steps": forged_steps})

    with pytest.raises(ValidationError, match="must identify existing paths"):
        ImplementationPlan.model_validate(forged_plan)


@pytest.mark.parametrize("version", [3, 99])
def test_approved_plan_is_exactly_version_two(
    fixture_inspector: FixedRootRepositoryInspector,
    version: int,
) -> None:
    payload = _plan_payload(fixture_inspector)
    payload.update(
        {
            "status": "approved",
            "version": version,
            "approval": {
                "approved_by": "Reviewer",
                "approved_at": datetime(2026, 8, 27, 1, tzinfo=UTC),
                "from_version": version - 1,
            },
        }
    )

    with pytest.raises(ValidationError, match="must be version 2"):
        ImplementationPlan.model_validate(payload)


def test_approved_plan_is_only_the_version_one_to_two_transition(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    payload.update(
        {
            "status": "approved",
            "version": 2,
            "approval": {
                "approved_by": "Reviewer",
                "approved_at": datetime(2026, 8, 27, 1, tzinfo=UTC),
                "from_version": 2,
            },
        }
    )

    with pytest.raises(ValidationError, match="from_version must be 1"):
        ImplementationPlan.model_validate(payload)


def test_plan_timestamps_require_timezone_and_normalize_to_utc(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    payload["created_at"] = datetime(2026, 8, 27)

    with pytest.raises(ValidationError, match="created_at must be timezone-aware"):
        ImplementationPlan.model_validate(payload)

    payload["created_at"] = datetime(
        2026,
        8,
        27,
        8,
        tzinfo=timezone(timedelta(hours=8)),
    )
    plan = ImplementationPlan.model_validate(payload)

    assert plan.created_at == datetime(2026, 8, 27, tzinfo=UTC)
    assert plan.created_at.tzinfo is UTC


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    (
        ("2026-08-27T00:00:00Z", datetime(2026, 8, 27, tzinfo=UTC)),
        (
            "2026-08-27t00:00:00.125z",
            datetime(2026, 8, 27, microsecond=125_000, tzinfo=UTC),
        ),
        ("2026-08-27T08:00:00+08:00", datetime(2026, 8, 27, tzinfo=UTC)),
    ),
)
def test_plan_json_timestamp_accepts_complete_rfc3339_and_normalizes_to_utc(
    fixture_inspector: FixedRootRepositoryInspector,
    timestamp: str,
    expected: datetime,
) -> None:
    payload = _plan(fixture_inspector).model_dump(mode="json")
    payload["created_at"] = timestamp

    plan = ImplementationPlan.model_validate_json(json.dumps(payload))

    assert plan.created_at == expected
    assert plan.created_at.tzinfo is UTC


@pytest.mark.parametrize("field", ("created_at", "approved_at"))
@pytest.mark.parametrize(
    "timestamp",
    (
        "2026-08-27 00:00:00Z",
        "2026-08-27_00:00:00Z",
        "2026-08-27T00:00Z",
        "2026-08-27T00:00:00+0000",
        "2026-08-27T00:00:00",
    ),
)
def test_plan_json_timestamps_reject_non_rfc3339_lexical_forms(
    fixture_inspector: FixedRootRepositoryInspector,
    field: str,
    timestamp: str,
) -> None:
    payload = _plan(fixture_inspector).model_dump(mode="json")
    if field == "created_at":
        payload["created_at"] = timestamp
    else:
        payload.update(
            {
                "status": "approved",
                "version": 2,
                "approval": {
                    "approved_by": "Reviewer",
                    "approved_at": timestamp,
                    "from_version": 1,
                },
            }
        )

    with pytest.raises(ValidationError, match=f"{field}.*RFC 3339"):
        ImplementationPlan.model_validate_json(json.dumps(payload))


def test_approval_timestamp_requires_timezone_and_normalizes_to_utc(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    payload.update(
        {
            "status": "approved",
            "version": 2,
            "approval": {
                "approved_by": "Reviewer",
                "approved_at": datetime(2026, 8, 27, 1),
                "from_version": 1,
            },
        }
    )

    with pytest.raises(ValidationError, match="approved_at must be timezone-aware"):
        ImplementationPlan.model_validate(payload)

    payload["approval"]["approved_at"] = datetime(  # type: ignore[index]
        2026,
        8,
        27,
        9,
        tzinfo=timezone(timedelta(hours=8)),
    )
    plan = ImplementationPlan.model_validate(payload)

    assert plan.approval is not None
    assert plan.approval.approved_at == datetime(2026, 8, 27, 1, tzinfo=UTC)
    assert plan.approval.approved_at.tzinfo is UTC


def test_plan_id_accepts_a_uuid_instance_and_canonical_json_string(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    plan_from_uuid = ImplementationPlan.model_validate(payload)
    json_payload = plan_from_uuid.model_dump(mode="json")

    plan_from_json = ImplementationPlan.model_validate_json(json.dumps(json_payload))

    assert plan_from_uuid.plan_id == UUID("00000000-0000-4000-8000-000000000017")
    assert plan_from_json.plan_id == plan_from_uuid.plan_id


@pytest.mark.parametrize(
    "plan_id",
    (
        "00000000000040008000000000000017",
        "{00000000-0000-4000-8000-000000000017}",
        "urn:uuid:00000000-0000-4000-8000-000000000017",
        "00000000-0000-4000-8000-000000000017 ",
        "00000000-0000-4000-8000-0000000000AA",
        17,
    ),
)
def test_plan_id_rejects_noncanonical_or_coercible_inputs(
    fixture_inspector: FixedRootRepositoryInspector,
    plan_id: object,
) -> None:
    payload = _plan(fixture_inspector).model_dump(mode="json")
    payload["plan_id"] = plan_id

    with pytest.raises(ValidationError, match="canonical UUID"):
        ImplementationPlan.model_validate_json(json.dumps(payload))


def test_plan_schema_advertises_runtime_semantic_constraints() -> None:
    semantic_constraints = ImplementationPlan.model_json_schema()[
        "x-repopilot-semantic-constraints"
    ]

    assert semantic_constraints["version"] == "1.0"
    assert semantic_constraints["enforced_by"] == "pydantic-runtime"
    assert {item["id"] for item in semantic_constraints["constraints"]} >= {
        "evidence-graph",
        "step-sequence-and-actions",
        "verification-declarations-and-readiness",
        "plan-state-and-approval",
    }


@pytest.mark.parametrize(
    "update",
    [
        {"category": EvidenceCategory.README},
        {"path": "README.md"},
    ],
)
def test_model_validate_rejects_forged_evidence_category_or_path(
    fixture_inspector: FixedRootRepositoryInspector,
    update: dict[str, object],
) -> None:
    plan = _plan(fixture_inspector)
    evidence_index = next(
        index
        for index, item in enumerate(plan.evidence)
        if item.path == "src/tinycalc/calculator.py"
    )
    forged_evidence = plan.evidence[evidence_index].model_copy(update=update)
    evidence = list(plan.evidence)
    evidence[evidence_index] = forged_evidence
    forged_plan = plan.model_copy(update={"evidence": evidence})

    with pytest.raises(ValidationError, match="category does not match"):
        ImplementationPlan.model_validate(forged_plan)


def test_evidence_declarations_default_to_empty() -> None:
    evidence = EvidenceItem(
        id="E1",
        path="README.md",
        category=EvidenceCategory.README,
        line_start=1,
        line_end=1,
        sha256="a" * 64,
        observation="Repository overview.",
    )

    assert evidence.declared_tools == []


def test_verification_declaration_rejects_reversed_line_range() -> None:
    with pytest.raises(ValidationError, match="must not precede"):
        VerificationDeclaration(
            tool="pytest",
            kind=VerificationDeclarationKind.COMMAND,
            arguments=[],
            line_start=4,
            line_end=3,
        )


def test_evidence_rejects_declaration_outside_its_line_window() -> None:
    declaration = VerificationDeclaration(
        tool="pytest",
        kind=VerificationDeclarationKind.COMMAND,
        arguments=[],
        line_start=4,
        line_end=4,
    )

    with pytest.raises(ValidationError, match="inside the evidence line window"):
        EvidenceItem(
            id="E1",
            path="README.md",
            category=EvidenceCategory.README,
            line_start=1,
            line_end=3,
            sha256="a" * 64,
            observation="Repository overview.",
            declared_tools=[declaration],
        )


def test_evidence_rejects_duplicate_verification_declarations() -> None:
    declaration = VerificationDeclaration(
        tool="pytest",
        kind=VerificationDeclarationKind.COMMAND,
        arguments=[],
        line_start=2,
        line_end=2,
    )

    with pytest.raises(ValidationError, match="declarations must be unique"):
        EvidenceItem(
            id="E1",
            path="README.md",
            category=EvidenceCategory.README,
            line_start=1,
            line_end=3,
            sha256="a" * 64,
            observation="Repository overview.",
            declared_tools=[declaration, declaration],
        )


def test_evidence_rejects_multiple_declarations_for_the_same_tool() -> None:
    declarations = [
        VerificationDeclaration(
            tool="pytest",
            kind=VerificationDeclarationKind.COMMAND,
            arguments=[],
            line_start=1,
            line_end=1,
        ),
        VerificationDeclaration(
            tool="pytest",
            kind=VerificationDeclarationKind.CONFIGURATION,
            arguments=[],
            line_start=2,
            line_end=3,
        ),
    ]

    with pytest.raises(ValidationError, match="declarations must be unique by tool"):
        EvidenceItem(
            id="E1",
            path="README.md",
            category=EvidenceCategory.README,
            line_start=1,
            line_end=3,
            sha256="a" * 64,
            observation="Repository overview.",
            declared_tools=declarations,
        )


def test_evidence_accepts_one_declaration_for_each_supported_tool() -> None:
    declarations = [
        VerificationDeclaration(
            tool=tool,
            kind=VerificationDeclarationKind.COMMAND,
            arguments=[],
            line_start=line_number,
            line_end=line_number,
        )
        for line_number, tool in enumerate(("pytest", "ruff", "mypy"), start=1)
    ]

    evidence = EvidenceItem(
        id="E1",
        path="README.md",
        category=EvidenceCategory.README,
        line_start=1,
        line_end=3,
        sha256="a" * 64,
        observation="Repository overview.",
        declared_tools=declarations,
    )

    assert [declaration.tool for declaration in evidence.declared_tools] == [
        "pytest",
        "ruff",
        "mypy",
    ]


def test_source_evidence_cannot_claim_verification_declarations() -> None:
    declaration = VerificationDeclaration(
        tool="pytest",
        kind=VerificationDeclarationKind.COMMAND,
        arguments=[],
        line_start=1,
        line_end=1,
    )

    with pytest.raises(ValidationError, match="require README, project config, or test config"):
        EvidenceItem(
            id="E1",
            path="src/example.py",
            category=EvidenceCategory.SOURCE,
            line_start=1,
            line_end=1,
            sha256="a" * 64,
            observation="Source code.",
            declared_tools=[declaration],
        )


def test_m0_verification_declaration_rejects_arguments() -> None:
    with pytest.raises(ValidationError, match="declarations do not support arguments"):
        VerificationDeclaration(
            tool="pytest",
            kind=VerificationDeclarationKind.COMMAND,
            arguments=["-q"],
            line_start=1,
            line_end=1,
        )


@pytest.mark.parametrize(
    ("action", "exists"),
    [
        (FileAction.CREATE, True),
        (FileAction.INSPECT, False),
        (FileAction.MODIFY, False),
        (FileAction.VERIFY, False),
    ],
)
def test_file_reference_action_must_match_path_existence(
    action: FileAction,
    exists: bool,
) -> None:
    with pytest.raises(ValidationError, match="must identify"):
        FileReference(
            path="src/example.py",
            action=action,
            exists=exists,
            reason="contract test",
            evidence_ids=["E1"],
        )


@pytest.mark.parametrize(
    ("kind", "action"),
    [
        (StepKind.ANALYSIS, FileAction.MODIFY),
        (StepKind.IMPLEMENTATION, FileAction.INSPECT),
        (StepKind.TEST, FileAction.VERIFY),
        (StepKind.VERIFICATION, FileAction.MODIFY),
    ],
)
def test_plan_step_rejects_incompatible_reference_actions(
    kind: StepKind,
    action: FileAction,
) -> None:
    reference = FileReference(
        path="src/example.py",
        action=action,
        exists=True,
        reason="contract test",
        evidence_ids=["E1"],
    )

    with pytest.raises(ValidationError, match="incompatible file-reference actions"):
        PlanStep(
            sequence=1,
            kind=kind,
            title="Check the contract",
            description="Exercise the incompatible action boundary.",
            file_references=[reference],
        )


def test_existing_reference_requires_same_path_evidence(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    payload["steps"][0]["file_references"][0]["path"] = "src/tinycalc/other.py"  # type: ignore[index]

    with pytest.raises(ValidationError, match="must cite same-path evidence"):
        ImplementationPlan.model_validate(payload)


def test_create_reference_conflicts_with_any_observed_same_path_evidence(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    reference = payload["steps"][1]["file_references"][0]  # type: ignore[index]
    reference["action"] = FileAction.CREATE
    reference["exists"] = False

    with pytest.raises(ValidationError, match="conflicts with observed evidence"):
        ImplementationPlan.model_validate(payload)


def test_duplicate_create_references_for_one_path_are_rejected(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    plan = _plan(fixture_inspector)
    create_reference = FileReference(
        path="src/tinycalc/new_module.py",
        action=FileAction.CREATE,
        exists=False,
        reason="Create one new module exactly once.",
        evidence_ids=plan.steps[1].file_references[0].evidence_ids,
    )
    steps = list(plan.steps)
    steps[1] = steps[1].model_copy(update={"file_references": [create_reference]})
    steps[2] = steps[2].model_copy(update={"file_references": [create_reference.model_copy()]})
    forged_plan = plan.model_copy(update={"steps": steps})

    with pytest.raises(ValidationError, match="is created more than once"):
        ImplementationPlan.model_validate(forged_plan)


def test_verification_intent_evidence_is_limited_to_declaration_categories(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    source_evidence_id = next(
        item["id"]
        for item in payload["evidence"]
        if item["category"] == "source"  # type: ignore[union-attr]
    )
    payload["verification_intents"][0]["evidence_ids"] = [source_evidence_id]  # type: ignore[index]

    with pytest.raises(ValidationError, match="README, project config, or test config"):
        ImplementationPlan.model_validate(payload)


def test_observation_text_cannot_forge_a_verification_declaration(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    evidence_id = payload["verification_intents"][0]["evidence_ids"][0]  # type: ignore[index]
    evidence = next(
        item
        for item in payload["evidence"]
        if item["id"] == evidence_id  # type: ignore[index,union-attr]
    )
    evidence["observation"] = "Run pytest to verify this repository."
    evidence["declared_tools"] = []

    with pytest.raises(ValidationError, match="without an exact supported tool declaration"):
        ImplementationPlan.model_validate(payload)


def test_forged_declared_tool_cannot_support_a_different_intent(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    evidence_id = payload["verification_intents"][0]["evidence_ids"][0]  # type: ignore[index]
    evidence = next(
        item
        for item in payload["evidence"]
        if item["id"] == evidence_id  # type: ignore[index,union-attr]
    )
    evidence["declared_tools"][0]["tool"] = "ruff"

    with pytest.raises(ValidationError, match="without an exact supported tool declaration"):
        ImplementationPlan.model_validate(payload)


def test_every_intent_evidence_id_must_declare_the_exact_tool(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    readme_id = next(
        item["id"]
        for item in payload["evidence"]
        if item["category"] == "readme"  # type: ignore[union-attr]
    )
    payload["verification_intents"][0]["evidence_ids"].append(readme_id)  # type: ignore[index,union-attr]

    with pytest.raises(ValidationError, match="without an exact supported tool declaration"):
        ImplementationPlan.model_validate(payload)


def test_m0_verification_intent_rejects_arguments(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    payload["verification_intents"][0]["arguments"] = ["-q"]  # type: ignore[index]

    with pytest.raises(ValidationError, match="intents do not support arguments"):
        ImplementationPlan.model_validate(payload)


@pytest.mark.parametrize("tool", ["ruff", "mypy"])
def test_non_pytest_configuration_does_not_authorize_an_intent(
    fixture_inspector: FixedRootRepositoryInspector,
    tool: str,
) -> None:
    payload = _plan_payload(fixture_inspector)
    intent = payload["verification_intents"][0]  # type: ignore[index]
    intent["tool"] = tool
    payload["verification_readiness"] = "needs_human_input"
    evidence_id = intent["evidence_ids"][0]
    evidence = next(
        item
        for item in payload["evidence"]
        if item["id"] == evidence_id  # type: ignore[index,union-attr]
    )
    evidence["declared_tools"][0]["tool"] = tool

    with pytest.raises(ValidationError, match="without an exact supported tool declaration"):
        ImplementationPlan.model_validate(payload)


@pytest.mark.parametrize("tool", ["ruff", "mypy"])
def test_non_pytest_command_declaration_authorizes_an_advisory_intent(
    fixture_inspector: FixedRootRepositoryInspector,
    tool: str,
) -> None:
    payload = _plan_payload(fixture_inspector)
    intent = payload["verification_intents"][0]  # type: ignore[index]
    intent["tool"] = tool
    payload["verification_readiness"] = "needs_human_input"
    evidence_id = intent["evidence_ids"][0]
    evidence = next(
        item
        for item in payload["evidence"]
        if item["id"] == evidence_id  # type: ignore[index,union-attr]
    )
    evidence["declared_tools"][0]["tool"] = tool
    evidence["declared_tools"][0]["kind"] = "command"

    plan = ImplementationPlan.model_validate(payload)

    assert plan.verification_intents[0].tool == tool
    assert plan.verification_readiness == "needs_human_input"


def test_verification_intents_may_be_empty_when_human_input_is_needed(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    payload["verification_intents"] = []
    payload["verification_readiness"] = "needs_human_input"

    plan = ImplementationPlan.model_validate(payload)

    assert plan.verification_intents == []
    assert plan.verification_readiness == "needs_human_input"


def test_human_input_readiness_may_retain_advisory_intents(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    payload["verification_readiness"] = "needs_human_input"

    plan = ImplementationPlan.model_validate(payload)

    assert plan.verification_intents
    assert plan.verification_readiness == "needs_human_input"


def test_ready_requires_an_evidence_backed_pytest_intent(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    payload["verification_intents"] = []
    payload["verification_readiness"] = "ready"

    with pytest.raises(ValidationError, match="at least 1 item"):
        ImplementationPlan.model_validate(payload)


def test_ready_rejects_non_pytest_intents_even_with_allowed_evidence(
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    payload = _plan_payload(fixture_inspector)
    payload["verification_intents"][0]["tool"] = "ruff"  # type: ignore[index]
    payload["verification_readiness"] = "ready"

    with pytest.raises(ValidationError, match="without an exact supported tool declaration"):
        ImplementationPlan.model_validate(payload)


@pytest.mark.parametrize(
    ("keep_pytest_intent", "expected_readiness"),
    [(True, "ready"), (False, "needs_human_input")],
)
def test_missing_readiness_is_derived_for_builder_compatibility(
    fixture_inspector: FixedRootRepositoryInspector,
    keep_pytest_intent: bool,
    expected_readiness: str,
) -> None:
    payload = _plan_payload(fixture_inspector)
    payload.pop("verification_readiness")
    if not keep_pytest_intent:
        payload["verification_intents"] = []

    plan = ImplementationPlan.model_validate(payload)

    assert plan.verification_readiness == expected_readiness

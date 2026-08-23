from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from repopilot.errors import InspectionLimitExceededError
from repopilot.inspection import (
    InspectedDocument,
    InspectionLimits,
    RepositorySnapshot,
)
from repopilot.models import (
    EvidenceCategory,
    FileAction,
    ImplementationPlan,
    InspectedRepository,
    IssueInput,
    PlanStep,
    StepKind,
)
from repopilot.planning import PlanBuilder


def _document(path: str, category: EvidenceCategory, content: str) -> InspectedDocument:
    payload = content.encode("utf-8")
    return InspectedDocument(
        path=path,
        category=category,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        content=content,
    )


def _semantic_snapshot() -> RepositorySnapshot:
    documents = (
        _document(
            "README.md",
            EvidenceCategory.README,
            """# Arithmetic

`divide()` returns a quotient and `add()` returns a sum.
Zero divisors currently use Python's normal exception behavior.
""",
        ),
        _document(
            "pyproject.toml",
            EvidenceCategory.PROJECT_CONFIG,
            """[project]
name = "arithmetic"
""",
        ),
        _document(
            "tests/test_calculator.py",
            EvidenceCategory.TEST,
            """from arithmetic import divide

def test_unrelated_contract() -> None:
    assert True



def test_divide_returns_quotient() -> None:
    assert divide(8, 2) == 4
""",
        ),
        _document(
            "tests/test_adder.py",
            EvidenceCategory.TEST,
            """from arithmetic import add

def test_add_returns_sum() -> None:
    assert add(2, 3) == 5
""",
        ),
        _document(
            "tests/test_noise.py",
            EvidenceCategory.TEST,
            """def test_error_message_words() -> None:
    # update clear error regression test behavior
    assert True
""",
        ),
        _document(
            "src/arithmetic/calculator.py",
            EvidenceCategory.SOURCE,
            """def unrelated() -> bool:
    return True


# The requested symbol is deliberately far from the first definition.


def divide(dividend: float, divisor: float) -> float:
    return dividend / divisor
""",
        ),
        _document(
            "src/arithmetic/adder.py",
            EvidenceCategory.SOURCE,
            """def add(left: float, right: float) -> float:
    return left + right
""",
        ),
        _document(
            "src/arithmetic/noisy.py",
            EvidenceCategory.SOURCE,
            """# divide divisor zero ValueError calculator quotient
def summarize() -> str:
    return "lexical distractor"
""",
        ),
        _document(
            "src/arithmetic/unrelated.py",
            EvidenceCategory.SOURCE,
            """# update clear error regression test behavior
def untouched() -> bool:
    return True
""",
        ),
    )
    return RepositorySnapshot(
        repository=InspectedRepository(
            url="https://github.com/acme/arithmetic",
            owner="acme",
            name="arithmetic",
            ref="main",
            tree_sha="a" * 64,
        ),
        all_paths=tuple(document.path for document in documents),
        documents=documents,
        selection_truncated=False,
        limits=InspectionLimits(),
    )


def _divide_issue() -> IssueInput:
    return IssueInput(
        number=17,
        title="Give divide() an explicit zero-divisor error",
        body=(
            'In calculator.py, make divide() raise ValueError("divisor must not be zero") '
            "when divisor is zero. Preserve non-zero quotients and add a regression test "
            "asserting the exact exception type and message."
        ),
    )


def _paths_for(step: PlanStep) -> list[str]:
    return [reference.path for reference in step.file_references]


def _evidence_snippet(plan: ImplementationPlan, snapshot: RepositorySnapshot, path: str) -> str:
    evidence = next(item for item in plan.evidence if item.path == path)
    document = next(item for item in snapshot.documents if item.path == path)
    lines = document.content.splitlines()
    return "\n".join(lines[evidence.line_start - 1 : evidence.line_end])


def test_plan_builder_prefers_explicit_target_and_semantic_evidence() -> None:
    snapshot = _semantic_snapshot()
    issue = _divide_issue()

    plan = PlanBuilder().build(snapshot, issue)
    steps = {step.kind: step for step in plan.steps}

    assert _paths_for(steps[StepKind.ANALYSIS]) == [
        "src/arithmetic/calculator.py",
        "tests/test_calculator.py",
        "README.md",
    ]
    assert _paths_for(steps[StepKind.IMPLEMENTATION]) == ["src/arithmetic/calculator.py"]
    assert _paths_for(steps[StepKind.TEST]) == ["tests/test_calculator.py"]
    assert 'ValueError("divisor must not be zero")' in steps[StepKind.IMPLEMENTATION].description
    assert 'ValueError("divisor must not be zero")' in steps[StepKind.TEST].description

    source_snippet = _evidence_snippet(plan, snapshot, "src/arithmetic/calculator.py")
    test_snippet = _evidence_snippet(plan, snapshot, "tests/test_calculator.py")
    assert "def divide" in source_snippet
    assert "def unrelated" not in source_snippet
    assert "test_divide_returns_quotient" in test_snippet
    assert "test_unrelated_contract" not in test_snippet

    ranked_sources = PlanBuilder._rank_documents(
        snapshot.documents,
        f"{issue.title}\n{issue.body}".casefold(),
        EvidenceCategory.SOURCE,
    )
    ranked_paths = [document.path for document in ranked_sources]
    assert ranked_paths[0] == "src/arithmetic/calculator.py"
    assert "src/arithmetic/noisy.py" in ranked_paths
    assert "src/arithmetic/unrelated.py" not in ranked_paths


def test_issue_mutation_changes_the_selected_source_and_test() -> None:
    snapshot = _semantic_snapshot()
    builder = PlanBuilder()
    divide_plan = builder.build(snapshot, _divide_issue())
    add_plan = builder.build(
        snapshot,
        IssueInput(
            number=18,
            title="Clarify add() behavior for negative inputs",
            body=(
                "In adder.py, update add() while preserving the public sum contract and add "
                "focused regression coverage."
            ),
        ),
    )

    divide_steps = {step.kind: step for step in divide_plan.steps}
    add_steps = {step.kind: step for step in add_plan.steps}
    assert _paths_for(divide_steps[StepKind.IMPLEMENTATION]) == ["src/arithmetic/calculator.py"]
    assert _paths_for(add_steps[StepKind.IMPLEMENTATION]) == ["src/arithmetic/adder.py"]
    assert _paths_for(divide_steps[StepKind.TEST]) == ["tests/test_calculator.py"]
    assert _paths_for(add_steps[StepKind.TEST]) == ["tests/test_adder.py"]


def test_symbol_definition_outweighs_comment_keyword_stuffing_without_a_file_hint() -> None:
    snapshot = _semantic_snapshot()
    issue = IssueInput(
        number=19,
        title="Handle zero divisors in divide()",
        body=(
            "Raise ValueError when the divisor is zero, preserve the quotient, and add focused "
            "regression coverage."
        ),
    )

    plan = PlanBuilder().build(snapshot, issue)
    steps = {step.kind: step for step in plan.steps}
    ranked_sources = PlanBuilder._rank_documents(
        snapshot.documents,
        f"{issue.title}\n{issue.body}".casefold(),
        EvidenceCategory.SOURCE,
    )

    assert "calculator.py" not in f"{issue.title}\n{issue.body}"
    assert [document.path for document in ranked_sources[:2]] == [
        "src/arithmetic/calculator.py",
        "src/arithmetic/noisy.py",
    ]
    assert _paths_for(steps[StepKind.IMPLEMENTATION]) == ["src/arithmetic/calculator.py"]
    assert _paths_for(steps[StepKind.TEST]) == ["tests/test_calculator.py"]


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("Improve this", "Make it better."),
        ("改进这个功能", "让它更可靠。"),
    ],
)
def test_low_signal_issue_falls_back_to_observed_files_with_explicit_risk(
    title: str, body: str
) -> None:
    plan = PlanBuilder().build(
        _semantic_snapshot(),
        IssueInput(number=20, title=title, body=body),
    )
    steps = {step.kind: step for step in plan.steps}
    implementation_reference = steps[StepKind.IMPLEMENTATION].file_references[0]
    test_reference = steps[StepKind.TEST].file_references[0]

    assert implementation_reference.path == "src/arithmetic/adder.py"
    assert implementation_reference.action is FileAction.MODIFY
    assert implementation_reference.exists is True
    assert "Low-confidence deterministic fallback" in implementation_reference.reason
    assert test_reference.path == "tests/test_adder.py"
    assert test_reference.action is FileAction.MODIFY
    assert test_reference.exists is True
    assert "Low-confidence deterministic fallback" in test_reference.reason
    assert any(
        "Low-confidence source selection" in risk and "src/arithmetic/adder.py" in risk
        for risk in plan.risks
    )
    assert any(
        "Low-confidence test selection" in risk and "tests/test_adder.py" in risk
        for risk in plan.risks
    )


def test_inferred_paths_are_only_used_when_source_and_test_categories_are_absent() -> None:
    snapshot = _semantic_snapshot()
    documents = tuple(
        document
        for document in snapshot.documents
        if document.category not in {EvidenceCategory.SOURCE, EvidenceCategory.TEST}
    )
    snapshot_without_code = replace(
        snapshot,
        documents=documents,
        all_paths=tuple(document.path for document in documents),
    )

    plan = PlanBuilder().build(
        snapshot_without_code,
        IssueInput(number=21, title="Improve this", body="Make it better."),
    )
    steps = {step.kind: step for step in plan.steps}
    implementation_reference = steps[StepKind.IMPLEMENTATION].file_references[0]
    test_reference = steps[StepKind.TEST].file_references[0]

    assert implementation_reference.path == "src/arithmetic/feature.py"
    assert implementation_reference.action is FileAction.CREATE
    assert implementation_reference.exists is False
    assert "No Python source file was observed" in implementation_reference.reason
    assert test_reference.path == "tests/test_arithmetic.py"
    assert test_reference.action is FileAction.CREATE
    assert test_reference.exists is False
    assert "No test file was observed" in test_reference.reason
    assert not any("Low-confidence" in risk for risk in plan.risks)


def test_uninspected_tree_source_and_test_fail_closed_instead_of_creating_paths() -> None:
    snapshot = _semantic_snapshot()
    readme = next(
        document for document in snapshot.documents if document.category is EvidenceCategory.README
    )
    limited_snapshot = replace(
        snapshot,
        documents=(readme,),
        selection_truncated=True,
        limits=replace(snapshot.limits, max_selected_files=1),
    )

    with pytest.raises(
        InspectionLimitExceededError,
        match="increase inspection limits or narrow the repository",
    ):
        PlanBuilder().build(
            limited_snapshot,
            IssueInput(number=22, title="Improve this", body="Make it better."),
        )

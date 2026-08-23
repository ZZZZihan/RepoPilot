from __future__ import annotations

import hashlib

from repopilot.inspection import (
    InspectedDocument,
    InspectionLimits,
    RepositorySnapshot,
)
from repopilot.models import (
    EvidenceCategory,
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

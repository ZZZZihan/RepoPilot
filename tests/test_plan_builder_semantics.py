from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from time import perf_counter

import pytest

from repopilot.errors import (
    AmbiguousIssuePathError,
    ConflictingIssuePathError,
    InspectionLimitExceededError,
)
from repopilot.inspection import (
    InspectedDocument,
    InspectionLimits,
    RepositorySnapshot,
)
from repopilot.models import (
    ISSUE_BODY_MAX_LENGTH,
    ISSUE_TITLE_MAX_LENGTH,
    EvidenceCategory,
    FileAction,
    ImplementationPlan,
    InspectedRepository,
    IssueInput,
    PlanStep,
    StepKind,
)
from repopilot.planning import (
    _CJK_PATH_ACTION_PREFIXES,
    _CJK_PATH_LOCATION_PREFIXES,
    _URL_LABEL_NAMES,
    PlanBuilder,
    _ProtectedSpanInventory,
)


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
        _document("pytest.ini", EvidenceCategory.TEST_CONFIG, "[pytest]\n"),
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
    assert [intent.tool for intent in plan.verification_intents] == ["pytest"]
    pytest_evidence = next(item for item in plan.evidence if item.path == "pytest.ini")
    assert plan.verification_intents[0].evidence_ids == [pytest_evidence.id]
    assert plan.verification_readiness == "ready"
    assert not any("conventional inference" in risk for risk in plan.risks)
    assert not any("Low-confidence" in risk for risk in plan.risks)


@pytest.mark.parametrize(
    ("repository_name", "expected_path"),
    [
        ("123-project", "src/repopilot_123_project/feature.py"),
        ("class", "src/repopilot_class/feature.py"),
    ],
)
def test_inferred_source_package_is_a_valid_python_identifier(
    repository_name: str,
    expected_path: str,
) -> None:
    snapshot = _semantic_snapshot()
    documents = tuple(
        document
        for document in snapshot.documents
        if document.category not in {EvidenceCategory.SOURCE, EvidenceCategory.TEST}
    )
    snapshot_without_code = replace(
        snapshot,
        repository=InspectedRepository(
            url=f"https://github.com/acme/{repository_name}",
            owner="acme",
            name=repository_name,
            ref="main",
            tree_sha="b" * 64,
        ),
        documents=documents,
        all_paths=tuple(document.path for document in documents),
    )

    plan = PlanBuilder().build(
        snapshot_without_code,
        IssueInput(number=47, title="Improve this", body="Make it better."),
    )
    implementation = next(step for step in plan.steps if step.kind is StepKind.IMPLEMENTATION)

    assert implementation.file_references[0].path == expected_path


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


def test_explicit_absent_source_and_test_paths_create_exact_references() -> None:
    plan = PlanBuilder().build(
        _semantic_snapshot(),
        IssueInput(
            number=23,
            title="Add a dedicated division error module",
            body=(
                "Create `src/arithmetic/division_error.py`. Add coverage in "
                "[tests/test_division_error.py](https://example.com/ignored.py)."
            ),
        ),
    )
    steps = {step.kind: step for step in plan.steps}
    implementation_reference = steps[StepKind.IMPLEMENTATION].file_references[0]
    test_reference = steps[StepKind.TEST].file_references[0]

    assert implementation_reference.path == "src/arithmetic/division_error.py"
    assert implementation_reference.action is FileAction.CREATE
    assert implementation_reference.exists is False
    assert test_reference.path == "tests/test_division_error.py"
    assert test_reference.action is FileAction.CREATE
    assert test_reference.exists is False
    assert not any("Low-confidence" in risk for risk in plan.risks)


def test_explicit_create_rejects_existing_file_ancestor() -> None:
    with pytest.raises(ConflictingIssuePathError, match="already occupies"):
        PlanBuilder().build(
            _semantic_snapshot(),
            IssueInput(
                number=48,
                title="Create `src/arithmetic/calculator.py/child.py`.",
                body="Add the nested implementation.",
            ),
        )


@pytest.mark.parametrize(
    ("directory_paths", "opaque_paths", "target"),
    [
        (("src/arithmetic/new.py",), (), "src/arithmetic/new.py"),
        ((), ("src/arithmetic/new.py",), "src/arithmetic/new.py"),
        ((), ("src/vendor",), "src/vendor/child.py"),
    ],
)
def test_explicit_create_rejects_non_regular_namespace_claims(
    directory_paths: tuple[str, ...],
    opaque_paths: tuple[str, ...],
    target: str,
) -> None:
    snapshot = replace(
        _semantic_snapshot(),
        directory_paths=directory_paths,
        opaque_paths=opaque_paths,
    )

    with pytest.raises(ConflictingIssuePathError, match="already occupies"):
        PlanBuilder().build(
            snapshot,
            IssueInput(
                number=50,
                title=f"Create `{target}`.",
                body="Add the implementation.",
            ),
        )


def test_inferred_create_rejects_non_regular_namespace_claim() -> None:
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
        opaque_paths=("src",),
    )

    with pytest.raises(ConflictingIssuePathError, match="already occupies"):
        PlanBuilder().build(
            snapshot_without_code,
            IssueInput(number=51, title="Improve this", body="Make it better."),
        )


def test_explicit_existing_path_preserves_internal_whitespace() -> None:
    document = _document(
        "src/arithmetic/double  space.py",
        EvidenceCategory.SOURCE,
        "VALUE = 1\n",
    )
    snapshot = _semantic_snapshot()
    snapshot = replace(
        snapshot,
        all_paths=(*snapshot.all_paths, document.path),
        documents=(*snapshot.documents, document),
    )

    plan = PlanBuilder().build(
        snapshot,
        IssueInput(
            number=49,
            title="Update `src/arithmetic/double  space.py`.",
            body="Preserve the existing behavior.",
        ),
    )
    implementation = next(step for step in plan.steps if step.kind is StepKind.IMPLEMENTATION)

    assert implementation.file_references[0].path == document.path
    assert implementation.file_references[0].action is FileAction.MODIFY


def test_explicit_existing_uninspected_path_fails_with_other_source_evidence() -> None:
    snapshot = _semantic_snapshot()
    limited_snapshot = replace(
        snapshot,
        documents=tuple(
            document
            for document in snapshot.documents
            if document.path != "src/arithmetic/calculator.py"
        ),
        selection_truncated=True,
    )

    with pytest.raises(InspectionLimitExceededError) as raised:
        PlanBuilder().build(
            limited_snapshot,
            IssueInput(
                number=24,
                title="Update `src/arithmetic/calculator.py`.",
                body="Preserve the existing regression-test layout.",
            ),
        )

    assert "src/arithmetic/calculator.py" in str(raised.value)
    assert "increase inspection limits or narrow the repository" in str(raised.value)


def test_unique_case_insensitive_tree_paths_are_canonicalized_consistently() -> None:
    plan = PlanBuilder().build(
        _semantic_snapshot(),
        IssueInput(
            number=25,
            title="Update `SRC/ARITHMETIC/CALCULATOR.PY`.",
            body="Keep coverage in `TESTS/TEST_CALCULATOR.PY`.",
        ),
    )
    steps = {step.kind: step for step in plan.steps}

    assert _paths_for(steps[StepKind.IMPLEMENTATION]) == ["src/arithmetic/calculator.py"]
    assert _paths_for(steps[StepKind.TEST]) == ["tests/test_calculator.py"]
    assert steps[StepKind.IMPLEMENTATION].file_references[0].action is FileAction.MODIFY
    assert steps[StepKind.TEST].file_references[0].action is FileAction.MODIFY


def test_ambiguous_case_insensitive_tree_path_fails_closed() -> None:
    snapshot = _semantic_snapshot()
    case_variant = _document(
        "src/Arithmetic/Calculator.py",
        EvidenceCategory.SOURCE,
        "def divide(dividend: float, divisor: float) -> float:\n    return dividend / divisor\n",
    )
    ambiguous_snapshot = replace(
        snapshot,
        all_paths=(*snapshot.all_paths, case_variant.path),
        documents=(*snapshot.documents, case_variant),
    )

    with pytest.raises(InspectionLimitExceededError) as raised:
        PlanBuilder().build(
            ambiguous_snapshot,
            IssueInput(
                number=26,
                title="Update `SRC/ARITHMETIC/CALCULATOR.PY`.",
                body="Preserve the existing regression tests.",
            ),
        )

    assert "matches multiple repository paths" in str(raised.value)
    assert "use the exact tree path" in str(raised.value)


def test_explicit_path_parser_rejects_unsafe_or_suffix_salvage() -> None:
    assert PlanBuilder._explicit_file_references(
        " ".join(
            (
                "/src/absolute.py",
                "../src/traversal.py",
                "https://example.com/src/url.py",
                "https://example.com/?file=src/query.py",
                'https://example.com/?file="src/quoted_query.py"',
                "https://example.com/#tests/test_fragment.py",
                "mailto:review@example.com?subject=src/mail_subject.py",
                "idea://open?file=src/unknown_scheme.py",
                "//example.com/?file=src/protocol_relative.py",
                "www.example.com/?file=src/www_query.py",
                "github.com/acme/demo/blob/main/src/pkg/github_blob.py",
                "example.com/#src/pkg/bare_fragment.py",
                'example.xn--p1ai/?file="src/punycode_query.py"',
                '例子.测试/?file="src/idn_query.py"',
                '[::1]/?file="src/ipv6_query.py"',
                '2130706433/?file="src/integer_host_query.py"',
                'intranet/?file="src/single_label_query.py"',
                'intranet/docs?file="src/single_label_path_query.py"',
                '2130706433/docs?file="src/integer_path_query.py"',
                'xn--p1ai/docs?file="src/punycode_path_query.py"',
                'user@intranet/docs?file="src/userinfo_path_query.py"',
                "src/backup.py.bak",
                "src/backup.py_bak",
                "src/backup.py)junk",
                "src/backup.py?raw=1",
                "(src/backup.py).bak",
                '"src/backup.py".bak',
                "src/backup.py备份",
                "src/backup.py旧版",
                "src/backup.py副本",
                "`src/wrapped_backup.py`备份",
                '"src/wrapped_old.py"旧版',
                "“src/wrapped_copy.py”副本",
                "(src/parenthesized_backup.py)备份",
                "`src/punctuated_backup.py`.备份",
                "src/unicode.py.π",
                "src/quoted_suffix.py'junk",
                "`src/safe.py`.",
                '"src/double_quoted.py",',
                "'tests/test_single_quoted.py';",
                "Path:src/colon_label.py.",
                "[tests/test_safe.py](//example.com/?p=src/pkg/markdown_bad.py)",
                "![diagram](src/pkg/image_destination_bad.py)",
            )
        )
    ) == (
        "src/safe.py",
        "src/double_quoted.py",
        "tests/test_single_quoted.py",
        "src/colon_label.py",
        "tests/test_safe.py",
    )


def test_url_query_or_fragment_paths_cannot_create_issue_targets() -> None:
    snapshot = _semantic_snapshot()
    issue = IssueInput(
        number=27,
        title="Review linked context",
        body=(
            "See www.example.com/?file=src/arithmetic/unrelated_new.py, "
            "//example.com/#tests/test_unrelated_new.py, and "
            "[the guide](github.com/acme/demo/blob/main/src/arithmetic/other_new.py). "
            "![diagram](src/arithmetic/image_destination_new.py) "
            'URL=https://example.com/?file="src/arithmetic/labeled_new.py"; '
            'See(https://example.com/?file="tests/test_parenthesized_new.py"). '
            '参见https://example.com/?file="src/arithmetic/cjk_new.py"。'
        ),
    )

    assert PlanBuilder._explicit_file_references(f"{issue.title}\n{issue.body}") == ()
    plan = PlanBuilder().build(snapshot, issue)
    steps = {step.kind: step for step in plan.steps}

    assert steps[StepKind.IMPLEMENTATION].file_references[0].action is FileAction.MODIFY
    assert steps[StepKind.IMPLEMENTATION].file_references[0].exists is True
    assert steps[StepKind.IMPLEMENTATION].file_references[0].path != (
        "src/arithmetic/unrelated_new.py"
    )
    assert steps[StepKind.TEST].file_references[0].action is FileAction.MODIFY
    assert steps[StepKind.TEST].file_references[0].exists is True
    assert steps[StepKind.TEST].file_references[0].path != "tests/test_unrelated_new.py"


def test_unicode_paths_and_quoted_root_paths_are_target_eligible() -> None:
    unicode_plan = PlanBuilder().build(
        _semantic_snapshot(),
        IssueInput(
            number=28,
            title="创建“src/功能/零处理.py”。",
            body="并在【tests/test_零处理.py】中添加回归覆盖。",
        ),
    )
    unicode_steps = {step.kind: step for step in unicode_plan.steps}
    assert _paths_for(unicode_steps[StepKind.IMPLEMENTATION]) == ["src/功能/零处理.py"]
    assert unicode_steps[StepKind.IMPLEMENTATION].file_references[0].action is FileAction.CREATE
    assert _paths_for(unicode_steps[StepKind.TEST]) == ["tests/test_零处理.py"]
    assert unicode_steps[StepKind.TEST].file_references[0].action is FileAction.CREATE

    root_plan = PlanBuilder().build(
        _semantic_snapshot(),
        IssueInput(
            number=29,
            title='Create "new_module.py".',
            body="Add coverage in 'test_new_module.py'.",
        ),
    )
    root_steps = {step.kind: step for step in root_plan.steps}
    assert _paths_for(root_steps[StepKind.IMPLEMENTATION]) == ["new_module.py"]
    assert root_steps[StepKind.IMPLEMENTATION].file_references[0].action is FileAction.CREATE
    assert _paths_for(root_steps[StepKind.TEST]) == ["test_new_module.py"]
    assert root_steps[StepKind.TEST].file_references[0].action is FileAction.CREATE


def test_quoted_existing_root_path_without_evidence_fails_closed() -> None:
    snapshot = _semantic_snapshot()
    limited_snapshot = replace(
        snapshot,
        all_paths=(*snapshot.all_paths, "root_module.py"),
        selection_truncated=True,
    )

    with pytest.raises(InspectionLimitExceededError) as raised:
        PlanBuilder().build(
            limited_snapshot,
            IssueInput(
                number=30,
                title='Update "root_module.py".',
                body="Preserve the existing tests.",
            ),
        )

    assert "root_module.py" in str(raised.value)
    assert "increase inspection limits or narrow the repository" in str(raised.value)


def test_wrapped_prose_is_not_salvaged_as_a_repository_path() -> None:
    assert (
        PlanBuilder._explicit_file_references(
            " ".join(
                (
                    "Please investigate (see src/pkg/current.py).",
                    'Please use "the existing src/pkg/current.py".',
                    "[see src/pkg/current.py](https://example.com/context)",
                    "请检查（请修改 src/pkg/current.py）。",
                )
            )
        )
        == ()
    )


def test_bare_cjk_boundaries_preserve_exact_source_and_test_targets() -> None:
    issue = IssueInput(
        number=31,
        title="创建 src/功能/新增.py，并保持兼容。",
        body="添加 tests/test_新增.py中的回归覆盖。",
    )
    assert PlanBuilder._explicit_file_references(f"{issue.title}\n{issue.body}") == (
        "src/功能/新增.py",
        "tests/test_新增.py",
    )

    plan = PlanBuilder().build(_semantic_snapshot(), issue)
    steps = {step.kind: step for step in plan.steps}
    assert _paths_for(steps[StepKind.IMPLEMENTATION]) == ["src/功能/新增.py"]
    assert steps[StepKind.IMPLEMENTATION].file_references[0].action is FileAction.CREATE
    assert _paths_for(steps[StepKind.TEST]) == ["tests/test_新增.py"]
    assert steps[StepKind.TEST].file_references[0].action is FileAction.CREATE

    delimited_issue = IssueInput(
        number=37,
        title="请修改 src/arithmetic/calculator.py。",
        body="更新 tests/test_calculator.py 中的回归覆盖。",
    )
    assert PlanBuilder._explicit_file_references(
        f"{delimited_issue.title}\n{delimited_issue.body}"
    ) == ("src/arithmetic/calculator.py", "tests/test_calculator.py")
    delimited_plan = PlanBuilder().build(_semantic_snapshot(), delimited_issue)
    delimited_steps = {step.kind: step for step in delimited_plan.steps}
    assert _paths_for(delimited_steps[StepKind.IMPLEMENTATION]) == ["src/arithmetic/calculator.py"]
    assert delimited_steps[StepKind.IMPLEMENTATION].file_references[0].action is (FileAction.MODIFY)


def test_cjk_location_wrappers_drive_exact_root_target_selection() -> None:
    issue = IssueInput(
        number=44,
        title='在 "new_module.py" 中创建实现',
        body="并在 `test_new_module.py` 中新增测试。",
    )

    plan = PlanBuilder().build(_semantic_snapshot(), issue)
    steps = {step.kind: step for step in plan.steps}

    implementation = steps[StepKind.IMPLEMENTATION].file_references[0]
    assert (implementation.path, implementation.action, implementation.exists) == (
        "new_module.py",
        FileAction.CREATE,
        False,
    )
    test_reference = steps[StepKind.TEST].file_references[0]
    assert (test_reference.path, test_reference.action, test_reference.exists) == (
        "test_new_module.py",
        FileAction.CREATE,
        False,
    )


def test_existing_spaced_root_test_name_uses_its_inspected_category() -> None:
    snapshot = _semantic_snapshot()
    source_document = _document(
        "test new.py",
        EvidenceCategory.SOURCE,
        "def value() -> int:\n    return 1\n",
    )
    snapshot = replace(
        snapshot,
        all_paths=(*snapshot.all_paths, source_document.path),
        documents=(*snapshot.documents, source_document),
    )

    existing_plan = PlanBuilder().build(
        snapshot,
        IssueInput(
            number=45,
            title='Modify:"test new.py".',
            body="Preserve its behavior.",
        ),
    )
    existing_steps = {step.kind: step for step in existing_plan.steps}
    existing_reference = existing_steps[StepKind.IMPLEMENTATION].file_references[0]
    assert (
        existing_reference.path,
        existing_reference.action,
        existing_reference.exists,
    ) == ("test new.py", FileAction.MODIFY, True)

    absent_plan = PlanBuilder().build(
        _semantic_snapshot(),
        IssueInput(
            number=46,
            title='Test:"test new.py".',
            body="Add coverage.",
        ),
    )
    absent_steps = {step.kind: step for step in absent_plan.steps}
    absent_reference = absent_steps[StepKind.TEST].file_references[0]
    assert (
        absent_reference.path,
        absent_reference.action,
        absent_reference.exists,
    ) == ("test new.py", FileAction.CREATE, False)


def test_repeated_root_reference_merges_target_eligibility() -> None:
    issue = IssueInput(
        number=32,
        title="new_module.py must be added",
        body='Create "new_module.py".',
    )
    parsed = PlanBuilder._parse_file_references(f"{issue.title}\n{issue.body}")
    assert len(parsed) == 1
    assert parsed[0].path == "new_module.py"
    assert parsed[0].target_eligible is True

    plan = PlanBuilder().build(_semantic_snapshot(), issue)
    implementation = next(step for step in plan.steps if step.kind is StepKind.IMPLEMENTATION)
    assert _paths_for(implementation) == ["new_module.py"]
    assert implementation.file_references[0].action is FileAction.CREATE

    colon_issue = IssueInput(
        number=35,
        title='Create:"new spaced module.py".',
        body='Test:"test_new spaced module.py".',
    )
    colon_plan = PlanBuilder().build(_semantic_snapshot(), colon_issue)
    colon_steps = {step.kind: step for step in colon_plan.steps}
    assert _paths_for(colon_steps[StepKind.IMPLEMENTATION]) == ["new spaced module.py"]
    assert _paths_for(colon_steps[StepKind.TEST]) == ["test_new spaced module.py"]


def test_every_explicit_path_is_audited_before_one_target_per_category_is_selected() -> None:
    snapshot = _semantic_snapshot()
    unread_snapshot = replace(
        snapshot,
        documents=tuple(
            document
            for document in snapshot.documents
            if document.path != "src/arithmetic/calculator.py"
        ),
        selection_truncated=True,
    )

    with pytest.raises(InspectionLimitExceededError) as missing:
        PlanBuilder().build(
            unread_snapshot,
            IssueInput(
                number=33,
                title="Create `src/arithmetic/new.py`.",
                body="Then update `src/arithmetic/calculator.py`.",
            ),
        )
    assert "src/arithmetic/calculator.py" in str(missing.value)

    case_variant = _document(
        "src/Arithmetic/Calculator.py",
        EvidenceCategory.SOURCE,
        "def divide(dividend: float, divisor: float) -> float:\n    return dividend / divisor\n",
    )
    ambiguous_snapshot = replace(
        snapshot,
        all_paths=(*snapshot.all_paths, case_variant.path),
        documents=(*snapshot.documents, case_variant),
    )
    with pytest.raises(InspectionLimitExceededError, match="matches multiple repository paths"):
        PlanBuilder().build(
            ambiguous_snapshot,
            IssueInput(
                number=34,
                title="Create `src/arithmetic/new.py`.",
                body="Then update `SRC/ARITHMETIC/CALCULATOR.PY`.",
            ),
        )

    audited_plan = PlanBuilder().build(
        snapshot,
        IssueInput(
            number=36,
            title="Create `src/arithmetic/new.py`.",
            body="Then update `src/arithmetic/calculator.py`.",
        ),
    )
    assert any("deferred multi-file scope" in risk for risk in audited_plan.risks)


def test_markdown_destinations_and_reference_ids_are_opaque_balanced_spans() -> None:
    issue_text = "\n".join(
        (
            "[](src/pkg/empty_destination.py)",
            "![diagram](src/pkg/image_destination.py)",
            "[see [nested]](src/pkg/nested_destination.py)",
            '[guide](https://example.com/wiki/Foo_(bar)?file="src/pkg/quoted_leak.py")',
            "[guide](https://example.com/wiki/Foo_(bar)?file=`src/pkg/tick_leak.py`)",
            "[guide][src/pkg/reference_id.py]",
            "![alt][src/pkg/image_reference_id.py]",
            "[guide]: src/pkg/reference_definition.py",
            '[broken](https://example.com/?file="src/pkg/malformed_leak.py"',
            "[src/pkg/real.py](https://example.com/wiki/Foo_(bar))",
        )
    )

    assert PlanBuilder._explicit_file_references(issue_text) == ("src/pkg/real.py",)


def test_markdown_reference_definitions_only_hide_complete_definition_grammar() -> None:
    valid_definitions = "\n".join(
        (
            '[inline]: https://example.com/?file="src/pkg/inline_leak.py" "title"',
            "[angle]: <https://example.com/src/pkg/angle_leak.py>",
            "[multiline]:",
            '  https://example.com/?file="src/pkg/multiline_leak.py"',
            '  "continued title"',
        )
    )
    assert PlanBuilder._explicit_file_references(valid_definitions) == ()

    invalid_definition = '[guide]:\nCreate "src/pkg/real.py".'
    assert PlanBuilder._explicit_file_references(invalid_definition) == ("src/pkg/real.py",)


def test_markdown_labels_are_complete_wrappers_for_spaced_paths() -> None:
    issue = IssueInput(
        number=38,
        title="Add dedicated modules",
        body=(
            "Create [new module.py](https://example.com/source)\n"
            "Test [test new module.py][coverage-guide]"
        ),
    )

    assert PlanBuilder._explicit_file_references(f"{issue.title}\n{issue.body}") == (
        "new module.py",
        "test new module.py",
    )
    plan = PlanBuilder().build(_semantic_snapshot(), issue)
    steps = {step.kind: step for step in plan.steps}
    assert _paths_for(steps[StepKind.IMPLEMENTATION]) == ["new module.py"]
    assert steps[StepKind.IMPLEMENTATION].file_references[0].action is FileAction.CREATE
    assert _paths_for(steps[StepKind.TEST]) == ["test new module.py"]
    assert steps[StepKind.TEST].file_references[0].action is FileAction.CREATE

    snapshot = _semantic_snapshot()
    unread_snapshot = replace(
        snapshot,
        all_paths=(*snapshot.all_paths, "new module.py"),
        selection_truncated=True,
    )
    with pytest.raises(InspectionLimitExceededError, match="new module.py"):
        PlanBuilder().build(unread_snapshot, issue)


@pytest.mark.parametrize(
    "separator", ("\n", "\r", "\r\n", "\v", "\f", "\x85", "\u2028", "\u2029", "\u00a0")
)
@pytest.mark.parametrize("opener,closer", (('"', '"'), ("`", "`"), ("（", "）")))
def test_complete_wrappers_cannot_join_path_text_across_control_whitespace(
    separator: str,
    opener: str,
    closer: str,
) -> None:
    issue_text = f"Create {opener}new.py{separator}do not create test_new.py{closer}"

    assert PlanBuilder._explicit_file_references(issue_text) == ()


@pytest.mark.parametrize(
    "separator", ("\n", "\r", "\r\n", "\v", "\f", "\x85", "\u2028", "\u2029", "\u00a0")
)
def test_markdown_labels_cannot_join_path_text_across_control_whitespace(
    separator: str,
) -> None:
    issue_text = f"Create [new.py{separator}then test_new.py](context)"

    assert PlanBuilder._explicit_file_references(issue_text) == ()


@pytest.mark.parametrize(
    "path",
    (
        "src/pkg/what?.py",
        "hash#name.py",
        "example.com/src/pkg/a.py",
        "localhost/src/pkg/a.py",
        "127.0.0.1/src/pkg/a.py",
        "a.py",
        "review.py",
        "inspect.py",
        "an.py",
        "the.py",
        "use/module.py",
        "current/pkg/a.py",
        "check/pkg/a.py",
    ),
)
def test_complete_wrappers_preserve_safe_url_like_git_paths_and_three_state_resolution(
    path: str,
) -> None:
    issue = IssueInput(number=39, title=f'Update "{path}".', body="Preserve behavior.")
    assert PlanBuilder._explicit_file_references(f"{issue.title}\n{issue.body}") == (path,)

    absent_plan = PlanBuilder().build(_semantic_snapshot(), issue)
    implementation = next(
        step for step in absent_plan.steps if step.kind is StepKind.IMPLEMENTATION
    )
    assert _paths_for(implementation) == [path]
    assert implementation.file_references[0].action is FileAction.CREATE
    assert implementation.file_references[0].exists is False

    unread_snapshot = replace(
        _semantic_snapshot(),
        all_paths=(*_semantic_snapshot().all_paths, path),
        selection_truncated=True,
    )
    with pytest.raises(InspectionLimitExceededError, match=re.escape(path)):
        PlanBuilder().build(unread_snapshot, issue)


def test_url_query_values_with_whitespace_never_become_targets() -> None:
    issue_text = " ".join(
        (
            'URL = https://example.com/?file= "src/pkg/spaced.py"',
            'URL = https://example.com/?file = "tests/test_more_spaced.py"',
            'URL:https://example.com/#path="src/pkg/same_token.py"',
            'https://example.com/update:"src/pkg/action_suffix.py"',
            'https://example.com/?next=update:"src/pkg/query_action_suffix.py"',
            'idea://open/update:"tests/test_uri_action_suffix.py"',
            'URL=https://example.com/modify:"src/pkg/labeled_action_suffix.py"',
            "https://example.com/路径：“src/pkg/cjk_action_suffix.py”",
        )
    )
    assert PlanBuilder._explicit_file_references(issue_text) == ()

    plan = PlanBuilder().build(
        _semantic_snapshot(),
        IssueInput(number=41, title="Review linked context", body=issue_text),
    )
    steps = {step.kind: step for step in plan.steps}
    assert steps[StepKind.IMPLEMENTATION].file_references[0].action is FileAction.MODIFY
    assert steps[StepKind.IMPLEMENTATION].file_references[0].exists is True
    assert steps[StepKind.TEST].file_references[0].action is FileAction.MODIFY
    assert steps[StepKind.TEST].file_references[0].exists is True


@pytest.mark.parametrize(
    "issue_text",
    (
        "https://example.com/?redirect=[src/pkg/leak.py](context)",
        "https://example.com/#anchor=[tests/test_leak.py][guide]",
        "idea://open?target=[src/pkg/idea_leak.py](x)",
        "www.example.com/?x=[src/pkg/www_leak.py](y)",
        "https://e.example/?file= Path:src/pkg/labeled_leak.py",
        "https://e.example/?file = Create:src/pkg/create_leak.py",
        "https://e.example/?file= ( Path:src/pkg/grouped_label_leak.py )",
        "URL = Path:src/pkg/url_label_leak.py",
        "参见URL = Path:src/pkg/cjk_url_label_leak.py。",
        "URL: Create:tests/test_url_label_leak.py",
        '参考URL: "src/pkg/prefixed_colon_leak.py"',
        "参见URL： Path:src/pkg/fullwidth_colon_leak.py",
        "seeURL: [src/pkg/markdown_colon_leak.py](context)",
        'URI: "src/pkg/uri_value_leak.py"',
        'URI = "src/pkg/separate_uri_value_leak.py"',
        'href= "src/pkg/href_value_leak.py"',
        'link="src/pkg/link_value_leak.py"',
        '<a href = "src/pkg/html_href_value_leak.py" >',
        '<img src = "src/pkg/html_src_value_leak.py" >',
        "网址：“src/pkg/cjk_website_leak.py”",
        "网址 = Path:src/pkg/cjk_website_label_leak.py",
        "网址：[src/pkg/cjk_website_markdown_leak.py](context)",
        "URL = Add`src/pkg/action_wrapper_leak.py`",
        "URI = Update:[src/pkg/action_markdown_leak.py](context)",
        'href = Path: "src/pkg/spaced_action_leak.py"',
        "link = Create【src/pkg/cjk_wrapper_leak.py】",
        "网址 = 在[src/pkg/location_markdown_leak.py](context)中修改",
        "https://e.example/?file=( `src/pkg/wrapped_group_leak.py` )",
        "https://e.example/?file=( src/pkg/bare_group_leak.py )",
    ),
)
def test_url_context_is_shared_by_every_explicit_reference_syntax(
    issue_text: str,
) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == ()


@pytest.mark.parametrize("url_label", ("URL", "URI", "href", "link", "src", "网址"))
@pytest.mark.parametrize("delimiter", ("=", "："))
@pytest.mark.parametrize("action", ("修改", "创建", "更新", "新增", "测试"))
@pytest.mark.parametrize(
    "value_template",
    (
        "{action}: src/pkg/compact_cjk_leak.py",
        "{action}`src/pkg/compact_cjk_wrapper_leak.py`",
        "{action}:[src/pkg/compact_cjk_markdown_leak.py](context)",
    ),
)
def test_compact_url_labels_keep_cjk_action_values_opaque(
    url_label: str,
    delimiter: str,
    action: str,
    value_template: str,
) -> None:
    issue_text = url_label + delimiter + value_template.format(action=action)
    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize(
    "url_label",
    ("URL", "URI", "href", "link", "src", "网址", "参见URL"),
)
@pytest.mark.parametrize("delimiter", ("=", ":", "：", " = ", " : ", " ： "))
@pytest.mark.parametrize(
    "url_value",
    (
        "在 src/pkg/location_leak.py 中修改",
        "在 [src/pkg/location_markdown_leak.py](context) 中修改",
        "请在【src/pkg/location_wrapper_leak.py】中测试",
    ),
)
def test_url_labels_keep_spaced_cjk_location_values_opaque(
    url_label: str,
    delimiter: str,
    url_value: str,
) -> None:
    assert PlanBuilder._parse_file_references(url_label + delimiter + url_value) == ()


@pytest.mark.parametrize(
    ("issue_text", "expected_path"),
    (
        ("URL=URL Path:src/pkg/real.py", "src/pkg/real.py"),
        ("src/路径.py", "src/路径.py"),
        ("linker=修改: src/pkg/real.py", "src/pkg/real.py"),
        ("路径:src/pkg/real.py", "src/pkg/real.py"),
        ("修改:src/pkg/real.py", "src/pkg/real.py"),
        ("URL=URL→Path:src/pkg/real.py", "src/pkg/real.py"),
    ),
)
def test_url_label_prefix_does_not_consume_an_unrelated_repository_clause(
    issue_text: str,
    expected_path: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)
    assert [(item.path, item.target_eligible) for item in parsed] == [(expected_path, True)]


@pytest.mark.parametrize(
    "issue_text",
    (
        'Path: "src/pkg/real.py"',
        "Create: `src/pkg/real.py`",
        "File:\u00a0[src/pkg/real.py](context)",
        "Update:\u202f【src/pkg/real.py】",
        "Path: src/pkg/real.py",
        "Create:\tsrc/pkg/real.py",
        "Modify:\u3000src/pkg/real.py",
    ),
)
def test_spaced_path_action_labels_take_precedence_over_generic_uri_schemes(
    issue_text: str,
) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == ("src/pkg/real.py",)


@pytest.mark.parametrize(
    "url_token",
    (
        "https://example.com/?files=README.md,src/pkg/leak.py",
        "https://example.com/path,src/pkg/leak.py",
        "//example.com/?files=x,src/pkg/leak.py",
        "www.example.com/?files=x,src/pkg/leak.py",
        "data:text/plain,src/pkg/leak.py",
        "mailto:user@example.com?body=x,src/pkg/leak.py",
        "idea://open?files=x;src/pkg/leak.py",
        "https://example.test/?q=a.py。bk/b.py;Update:src/pkg/leak.py",
        "mailto:user@example.test?body=a.py。bk/b.py,Create:src/pkg/leak.py",
    ),
)
def test_ascii_uri_delimiters_remain_inside_the_opaque_url_token(url_token: str) -> None:
    assert PlanBuilder._explicit_file_references(url_token) == ()


@pytest.mark.parametrize(
    "issue_text",
    (
        'https://e.example/?files="src/pkg/quoted.py",tests/test_tail.py',
        "https://e.example/?files=`src/pkg/tick.py`,tests/test_tail.py",
        "https://e.example/?redirect=[src/pkg/markdown.py](context),tests/test_tail.py",
        "https://e.example/?redirect=![src/pkg/image.py](context),tests/test_tail.py",
        "https://e.example/?target=(src/pkg/grouped.py),tests/test_tail.py",
        'data:text/plain,"src/pkg/data.py",tests/test_tail.py',
    ),
)
def test_url_protection_merges_over_opaque_nested_constructs(issue_text: str) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == ()


@pytest.mark.parametrize(
    "issue_text",
    (
        'URL="src/pkg/leak.py";Update:"src/pkg/real.py"',
        "URL=`src/pkg/leak.py`,Path:src/pkg/real.py",
        'URL=(https://e.example/x(y));Update:"src/pkg/real.py"',
        'URL=[https://e.example/x[y]],Update:"src/pkg/real.py"',
        'URL={https://e.example/x{y}};Update:"src/pkg/real.py"',
        "URL=（https://e.example/x（y））；Update:“src/pkg/real.py”",
        "URL=“https://e.example/?q=x”；Create:src/pkg/real.py",
        "URL=‘https://e.example/?q=x’,Update:src/pkg/real.py",
        "URL=「https://e.example/?q=x」;Path:src/pkg/real.py",
        "URL=『https://e.example/?q=x』，Modify:src/pkg/real.py",
    ),
)
def test_completed_structured_url_values_end_before_the_next_explicit_clause(
    issue_text: str,
) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == ("src/pkg/real.py",)


@pytest.mark.parametrize(
    "issue_text",
    (
        "URL=“https://e.example/?q=x;Create:src/pkg/hidden.py”",
        "URL=‘https://e.example/?q=x,Update:src/pkg/hidden.py’",
        "URL=「https://e.example/?q=x;Path:src/pkg/hidden.py」",
        "URL=『https://e.example/?q=x,Modify:src/pkg/hidden.py』",
        "URL=“https://e.example/?q=x;Create:src/pkg/hidden.py",
        "URL=‘https://e.example/?q=x,Update:src/pkg/hidden.py",
        "URL=「https://e.example/?q=x;Path:src/pkg/hidden.py",
        "URL=『https://e.example/?q=x,Modify:src/pkg/hidden.py",
    ),
)
def test_asymmetric_url_wrappers_keep_internal_or_unclosed_clauses_opaque(
    issue_text: str,
) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == ()


def test_path_action_markdown_does_not_hide_following_bare_targets() -> None:
    assert PlanBuilder._explicit_file_references(
        "Update:[src/pkg/a.py](context),tests/test_a.py"
    ) == ("src/pkg/a.py", "tests/test_a.py")
    assert PlanBuilder._explicit_file_references(
        "Update:[src/pkg/a.py](context);tests/test_a.py"
    ) == ("src/pkg/a.py", "tests/test_a.py")
    assert PlanBuilder._explicit_file_references(
        "Update:[src/pkg/a.py](context),https://e.example/?files=x,tests/test_tail.py"
    ) == ("src/pkg/a.py",)


def test_cjk_sentence_boundaries_reopen_real_targets_after_opaque_urls() -> None:
    issue_text = " ".join(
        (
            "参见https://example.com/docs，然后修改“src/pkg/a.py”。",
            '参见https://example.com/?file="src/pkg/leak.py"，并修改“tests/test_a.py”。',
            "URL=https://example.com/docs；修改【src/pkg/b.py】。",
            "参见https://example.com/docs。然后修改 tests/test_b.py。",
            (
                "更新[src/pkg/c.py](context)，参见URL = Path:src/pkg/labeled_leak.py；"
                "再更新【tests/test_c.py】。"
            ),
        )
    )
    assert PlanBuilder._explicit_file_references(issue_text) == (
        "src/pkg/a.py",
        "tests/test_a.py",
        "src/pkg/b.py",
        "tests/test_b.py",
        "src/pkg/c.py",
        "tests/test_c.py",
    )

    ungrouped_values = " ".join(
        (
            "URL = src/context。然后修改 tests/test_d.py。",
            "href = /docs，然后修改 src/pkg/d.py。",
        )
    )
    assert PlanBuilder._explicit_file_references(ungrouped_values) == (
        "tests/test_d.py",
        "src/pkg/d.py",
    )


@pytest.mark.parametrize("dot", ("。", "．", "｡"))
def test_idna_dots_distinguish_real_authorities_from_cjk_action_boundaries(
    dot: str,
) -> None:
    assert (
        PlanBuilder._explicit_file_references(f"https://例子{dot}中国/src/pkg/idna_leak.py") == ()
    )
    assert PlanBuilder._explicit_file_references(f"例子{dot}中国/src/pkg/idna_leak.py") == ()
    assert PlanBuilder._explicit_file_references(f"完成{dot}更新 src/pkg/real.py") == (
        "src/pkg/real.py",
    )
    assert PlanBuilder._explicit_file_references(
        f"更新 src/pkg/a.py{dot}然后修改 tests/test_a.py{dot}"
    ) == ("src/pkg/a.py", "tests/test_a.py")
    for explicit_uri in (
        f"https://例子{dot}更新src/pkg/idna_leak.py",
        f"idea://例子{dot}更新src/pkg/idna_leak.py",
        f"//例子{dot}更新src/pkg/idna_leak.py",
        f"www.例子{dot}更新src/pkg/idna_leak.py",
        f"URL=例子{dot}更新src/pkg/idna_leak.py",
    ):
        assert PlanBuilder._explicit_file_references(explicit_uri) == ()


def test_cjk_locative_prefixes_and_semantic_dashes_preserve_exact_paths() -> None:
    assert PlanBuilder._explicit_file_references(
        "在 src/pkg/a.py 中修改，并将 tests/test_a.py 中的覆盖更新。"
    ) == ("src/pkg/a.py", "tests/test_a.py")
    assert PlanBuilder._explicit_file_references(
        "更新 `src/pkg/a.py`—然后修改 tests/test_a.py。"
    ) == ("src/pkg/a.py", "tests/test_a.py")
    assert PlanBuilder._explicit_file_references(
        "src/pkg/a.py–tests/test_a.py→docs/guide.md|README.md"
    ) == ("src/pkg/a.py", "tests/test_a.py", "docs/guide.md", "README.md")
    assert PlanBuilder._explicit_file_references("更新 src/pkg/foo—bar.py。") == (
        "src/pkg/foo—bar.py",
    )
    assert PlanBuilder._explicit_file_references("在库/src/pkg/a.py") == ("在库/src/pkg/a.py",)


@pytest.mark.parametrize(
    "derived_suffix",
    (
        "．bak",
        "..bak",
        "。bak",
        "．ＢＡＫ",
        ".old",
        ".orig",
        ".save",
        ".tmp",
        ".swp",
        ".rej",
        ".disabled",
        ".dist",
        ".example",
        ".sample",
    ),
)
def test_wrapped_references_reject_backup_and_derived_copy_suffixes(
    derived_suffix: str,
) -> None:
    assert PlanBuilder._explicit_file_references(f'"src/pkg/a.py"{derived_suffix}') == ()


def test_url_false_targets_do_not_bypass_three_state_resolution() -> None:
    snapshot = _semantic_snapshot()
    leak_path = "src/pkg/leak.py"
    unread_snapshot = replace(
        snapshot,
        all_paths=(*snapshot.all_paths, leak_path),
        selection_truncated=True,
    )
    issue = IssueInput(
        number=42,
        title="Review linked context",
        body=f"https://example.com/?files=README.md,{leak_path}",
    )

    plan = PlanBuilder().build(unread_snapshot, issue)
    planned_paths = {reference.path for step in plan.steps for reference in step.file_references}
    assert leak_path not in planned_paths

    real_target = "tests/test_good.py"
    target_unread_snapshot = replace(
        snapshot,
        all_paths=(*snapshot.all_paths, real_target),
        selection_truncated=True,
    )
    with pytest.raises(InspectionLimitExceededError, match=re.escape(real_target)):
        PlanBuilder().build(
            target_unread_snapshot,
            IssueInput(
                number=43,
                title="Update linked regression target",
                body=f"参见https://example.com/docs。然后修改 {real_target}。",
            ),
        )


def test_unicode_boundaries_and_bare_lists_preserve_each_complete_target() -> None:
    issue_text = " ".join(
        (
            "在“src/pkg/a.py”增加校验，并让【tests/test_a.py】覆盖它。",
            'Update "src/pkg/b.py"—then update "tests/test_b.py".',
            "更新 src/pkg/c.py、tests/test_c.py。",
            "src/pkg/d.py,tests/test_d.py;src/pkg/e.py；tests/test_e.py",
        )
    )
    assert PlanBuilder._explicit_file_references(issue_text) == (
        "src/pkg/a.py",
        "tests/test_a.py",
        "src/pkg/b.py",
        "tests/test_b.py",
        "src/pkg/c.py",
        "tests/test_c.py",
        "src/pkg/d.py",
        "tests/test_d.py",
        "src/pkg/e.py",
        "tests/test_e.py",
    )

    plan = PlanBuilder().build(
        _semantic_snapshot(),
        IssueInput(number=40, title="新增实现和测试", body="更新 src/pkg/c.py、tests/test_c.py。"),
    )
    steps = {step.kind: step for step in plan.steps}
    assert _paths_for(steps[StepKind.IMPLEMENTATION]) == ["src/pkg/c.py"]
    assert _paths_for(steps[StepKind.TEST]) == ["tests/test_c.py"]


def test_wrapped_and_bare_prose_are_not_repository_targets() -> None:
    issue_text = " ".join(
        (
            'File: "look at src/pkg/current.py".',
            "Create (inspect tests/test_current.py).",
            "文件：“查看 src/pkg/current.py”。",
            "创建（参见 tests/test_current.py）。",
            "Use/src/pkg/current.py",
            "查看/src/pkg/current.py",
        )
    )
    assert PlanBuilder._explicit_file_references(issue_text) == ()


def test_wrapper_scan_stays_bounded_for_many_unmatched_openers() -> None:
    started = perf_counter()
    assert PlanBuilder._explicit_file_references("(" * 19_000 + ")") == ()
    assert perf_counter() - started < 1.0


def test_wrapper_scan_stays_bounded_for_deeply_nested_complete_wrappers() -> None:
    issue_text = "(" * 9_998 + "a.py" + ")" * 9_998

    started = perf_counter()
    assert PlanBuilder._explicit_file_references(issue_text) == ("a.py",)
    assert perf_counter() - started < 1.0


def test_reference_scan_stays_bounded_for_dense_complete_wrappers_and_suffixes() -> None:
    dense_wrappers = '"a.py"' * 3_333
    started = perf_counter()
    assert PlanBuilder._explicit_file_references(dense_wrappers) == ("a.py",)
    assert perf_counter() - started < 1.0

    dense_suffixes = ("a.py中" * 4_000)[:19_000]
    started = perf_counter()
    assert PlanBuilder._explicit_file_references(dense_suffixes) == ()
    assert perf_counter() - started < 1.0

    escaped_markdown_destination = "[src/pkg/a.py](" + "\\" * 19_000 + ")"
    started = perf_counter()
    assert PlanBuilder._explicit_file_references(escaped_markdown_destination) == ("src/pkg/a.py",)
    assert perf_counter() - started < 1.0

    invalid_wrappers = ('"a.py"x' * 4_000)[:19_900]
    started = perf_counter()
    assert PlanBuilder._explicit_file_references(invalid_wrappers) == ()
    assert perf_counter() - started < 1.0


def test_dense_separated_wrappers_do_not_copy_the_accumulated_prefix() -> None:
    issue_text = ("mention （new.py）； " * 40_000)[:640_000]

    started = perf_counter()
    assert PlanBuilder._explicit_file_references(issue_text) == ("new.py",)
    assert perf_counter() - started < 2.0


def test_dense_explicit_uri_ascii_separators_do_not_rescan_the_prefix() -> None:
    issue_text = ("https://e.test/?q=x," + "a," * 400_000)[:640_000]

    started = perf_counter()
    assert PlanBuilder._explicit_file_references(issue_text) == ()
    assert perf_counter() - started < 2.0


def test_dense_invalid_idna_clauses_do_not_rescan_the_remaining_token() -> None:
    unit = "例子。测试/a.py。bk/b.py；Update:src/real.py→"
    issue_text = (unit * 20_000)[:640_000]

    started = perf_counter()
    assert PlanBuilder._explicit_file_references(issue_text) == ()
    assert perf_counter() - started < 4.0


def test_dense_unclosed_structured_url_values_are_scanned_once() -> None:
    issue_text = ("URL = ( " * 80_000)[:640_000]

    started = perf_counter()
    assert PlanBuilder._explicit_file_references(issue_text) == ()
    assert perf_counter() - started < 2.0


def test_dense_completed_structured_url_values_stop_at_each_clause_seam() -> None:
    issue_text = ('URL="opaque"→' * 10_000)[:128_000]

    started = perf_counter()
    assert PlanBuilder._explicit_file_references(issue_text) == ()
    assert perf_counter() - started < 2.0


def test_many_token_completed_url_values_reuse_precomputed_line_boundaries() -> None:
    issue_text = ('URL = "opaque"→ ' * 40_000)[:640_000]

    started = perf_counter()
    assert PlanBuilder._explicit_file_references(issue_text) == ()
    assert perf_counter() - started < 2.0


def test_many_token_markdown_url_values_scan_only_their_local_destinations() -> None:
    issue_text = ("URL = [opaque](context)→ " * 30_000)[:640_000]

    started = perf_counter()
    assert PlanBuilder._explicit_file_references(issue_text) == ()
    assert perf_counter() - started < 2.0


@pytest.mark.parametrize(
    "issue_text",
    (
        ("a." * 1_249) + "1/",
        (("a." * 9_999) + "1/")[:20_000],
    ),
)
def test_authority_regexes_skip_delimiters_beyond_the_bounded_probe(
    issue_text: str,
) -> None:
    started = perf_counter()
    assert PlanBuilder._explicit_file_references(issue_text) == ()
    assert perf_counter() - started < 1.0


@pytest.mark.parametrize(
    "authority",
    (
        "www." + ("a." * 1_100) + "com",
        "user@" + ("a." * 1_100) + "com",
        ("a" * 2_100) + "@example.com",
        ("a" * 2_100) + ".example",
        "a" * 2_100,
    ),
)
@pytest.mark.parametrize(
    "wrapped_reference",
    (
        '"src/pkg/leak.py"',
        "(src/pkg/leak.py)",
        "[src/pkg/leak.py](context)",
    ),
)
def test_truncated_url_context_probe_keeps_later_wrappers_opaque(
    authority: str,
    wrapped_reference: str,
) -> None:
    issue_text = f"{authority}/{wrapped_reference}"
    assert len(authority) > 2_048

    started = perf_counter()
    assert PlanBuilder._explicit_file_references(issue_text) == ()
    assert perf_counter() - started < 1.0


def test_cjk_idna_dot_action_scan_stays_bounded_at_the_issue_size_limit() -> None:
    issue_text = ("例子。更新a.py。" * 2_000)[:20_000]
    started = perf_counter()
    assert PlanBuilder._explicit_file_references(issue_text) == ("更新a.py",)
    assert perf_counter() - started < 1.0


@pytest.mark.parametrize("dot", ("。", "．", "｡"))
@pytest.mark.parametrize("url_prefix", ("https://", "//", "www.", ""))
def test_idna_url_spans_are_opaque_before_wrapper_parsing(
    dot: str,
    url_prefix: str,
) -> None:
    issue_text = f"{url_prefix}例子{dot}测试/x/(src/pkg/leak.py)"
    assert PlanBuilder._explicit_file_references(issue_text) == ()


@pytest.mark.parametrize("dot", ("。", "．", "｡"))
def test_idna_sentence_boundaries_still_reopen_cjk_action_paths(dot: str) -> None:
    assert PlanBuilder._explicit_file_references(f"完成{dot}更新 src/pkg/real.py") == (
        "src/pkg/real.py",
    )


@pytest.mark.parametrize(
    "issue_text",
    (
        "<img src =\n tests/test_leak.py >",
        '<a href=\n\n"src/pkg/leak.py" >',
        "<img src =\r\n tests/test_leak.py >",
        '<a HREF =\r\n\r\n"src/pkg/leak.py">',
        "<img src=\n\n'tests/test_leak.py'>",
    ),
)
def test_multiline_html_href_and_src_values_are_opaque(issue_text: str) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == ()


@pytest.mark.parametrize(
    "issue_text",
    (
        "<a href=\nsrc/pkg/leak.py",
        "<img src =\ntests/test_leak.py",
        '<a HREF=\n"src/pkg/leak.py',
    ),
)
def test_incomplete_html_href_and_src_values_fail_closed(issue_text: str) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == ()


@pytest.mark.parametrize(
    "issue_text",
    (
        "<a href=x tests/test_leak.py",
        "<img src=x Create:src/pkg/leak.py",
        "<a href=x Path:src/pkg/leak.py",
        "<a href=x [src/pkg/leak.py](context)",
        '<a href="x" tests/test_leak.py',
    ),
)
def test_incomplete_html_url_tags_are_opaque_through_their_structural_end(
    issue_text: str,
) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == ()


def test_paths_after_complete_html_url_tags_remain_visible() -> None:
    assert PlanBuilder._explicit_file_references("<a href=x> Create src/pkg/real.py") == (
        "src/pkg/real.py",
    )


def test_only_exact_html_href_and_src_attributes_are_opaque() -> None:
    assert PlanBuilder._explicit_file_references('href=\n"src/pkg/outside.py"') == (
        "src/pkg/outside.py",
    )
    assert PlanBuilder._explicit_file_references('<a data-path=\n"src/pkg/ordinary.py">') == (
        "src/pkg/ordinary.py",
    )
    assert PlanBuilder._explicit_file_references("<a data-path=\nsrc/pkg/ordinary.py") == (
        "src/pkg/ordinary.py",
    )


@pytest.mark.parametrize(
    ("issue_text", "expected_path"),
    (
        ("在 new.py 中修改。", "new.py"),
        ("Add coverage in test_new.py", "test_new.py"),
    ),
)
def test_root_path_actions_preserve_target_eligibility(
    issue_text: str,
    expected_path: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)
    assert [(item.path, item.target_eligible) for item in parsed] == [(expected_path, True)]


@pytest.mark.parametrize(
    "filename",
    (
        "创建new.py",
        "请修改new.py",
        "并创建test_new.py",
        "更新_v2.md",
        "测试2.md",
        "测试test.py",
        "添加README.md",
        "新增-api.py",
        "创建foo.py",
        "实现v2.py",
        "请修改src/arithmetic/calculator.py",
        "更新tests/test_calculator.py",
        "路径src/pkg.py",
        "请文件src/pkg.py",
        "并路径src/pkg.py",
        "请修改功能/模块.py",
        "创建功能.py",
        "并请修改src/pkg.py",
        "请检查new.py",
    ),
)
def test_attached_cjk_action_prefixes_preserve_ambiguous_path_identity(
    filename: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(f"{filename}。")

    assert [(item.path, item.target_eligible) for item in parsed] == [(filename, False)]


@pytest.mark.parametrize(
    ("issue_text", "preserved_path", "preserved_suffix", "separator_example"),
    (
        ("在new.py中修改", "在new.py", "中修改", "在 new.py 中修改"),
        ("在src/pkg.py中修改", "在src/pkg.py", "中修改", "在 src/pkg.py 中修改"),
        (
            "请在tests/test_pkg.py里新增",
            "请在tests/test_pkg.py",
            "里新增",
            "请在 tests/test_pkg.py 里新增",
        ),
        ("并在src/pkg.py内更新", "并在src/pkg.py", "内更新", "并在 src/pkg.py 内更新"),
        ("请于src/pkg.py中修改", "请于src/pkg.py", "中修改", "请于 src/pkg.py 中修改"),
        ("请将src/pkg.py中修改", "请将src/pkg.py", "中修改", "请将 src/pkg.py 中修改"),
        ("在功能/模块.py中修改", "在功能/模块.py", "中修改", "在 功能/模块.py 中修改"),
        (
            "并请在功能/模块.py内更新",
            "并请在功能/模块.py",
            "内更新",
            "并请在 功能/模块.py 内更新",
        ),
        (
            "在new.py中修改错误处理",
            "在new.py",
            "中修改错误处理",
            "在 new.py 中修改错误处理",
        ),
        (
            "请于new.py里新增回归测试",
            "请于new.py",
            "里新增回归测试",
            "请于 new.py 里新增回归测试",
        ),
        (
            "并请将new.py内检查边界条件",
            "并请将new.py",
            "内检查边界条件",
            "并请将 new.py 内检查边界条件",
        ),
        (
            "在new.py中修改helper.py逻辑",
            "在new.py",
            "中修改helper.py逻辑",
            "在 new.py 中修改helper.py逻辑",
        ),
        (
            "在new.py中更新v2.1行为",
            "在new.py",
            "中更新v2.1行为",
            "在 new.py 中更新v2.1行为",
        ),
    ),
)
def test_attached_cjk_location_prefixes_preserve_ambiguous_path_identity(
    issue_text: str,
    preserved_path: str,
    preserved_suffix: str,
    separator_example: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert [(item.path, item.target_eligible) for item in parsed] == [(preserved_path, False)]
    suffix_span = parsed[0].ambiguous_cjk_suffix_span
    assert suffix_span is not None
    assert issue_text[slice(*suffix_span)] == preserved_suffix
    with pytest.raises(
        AmbiguousIssuePathError,
        match=re.escape(preserved_path),
    ) as raised:
        PlanBuilder().build(
            _semantic_snapshot(),
            IssueInput(number=64, title=issue_text, body="Preserve behavior."),
        )
    assert repr(separator_example) in str(raised.value)
    prefix = parsed[0].ambiguous_cjk_prefix
    assert prefix is not None
    operand = preserved_path[len(prefix) :]
    explicit = PlanBuilder._parse_file_references(separator_example)
    assert [(item.path, item.target_eligible) for item in explicit] == [(operand, True)]


def test_attached_cjk_location_without_suffix_never_invents_an_action() -> None:
    issue_text = "在new.py"
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert parsed[0].ambiguous_cjk_suffix_span is None
    with pytest.raises(AmbiguousIssuePathError) as raised:
        PlanBuilder().build(
            _semantic_snapshot(),
            IssueInput(number=67, title=issue_text, body="Preserve behavior."),
        )

    message = str(raised.value)
    assert repr("@new.py") in message
    assert repr("路径:在new.py") in message
    assert "中修改" not in message


def test_attached_cjk_location_suggestion_preserves_the_complete_same_line_suffix() -> None:
    issue_text = "在new.py中修改错误处理，并检查README.md"
    parsed = PlanBuilder._parse_file_references(issue_text)

    ambiguous = next(item for item in parsed if item.path == "在new.py")
    suffix_span = ambiguous.ambiguous_cjk_suffix_span
    assert suffix_span is not None
    assert issue_text[slice(*suffix_span)] == "中修改错误处理，并检查README.md"
    with pytest.raises(AmbiguousIssuePathError) as raised:
        PlanBuilder().build(
            _semantic_snapshot(),
            IssueInput(number=68, title=issue_text, body="Preserve behavior."),
        )

    message = str(raised.value)
    assert repr("在 new.py 中修改错误处理，并检查README.md") in message


def test_attached_cjk_location_suffix_preserves_a_spaced_dotted_operand() -> None:
    issue_text = "在new.py中修改 helper.py 的逻辑"
    parsed = PlanBuilder._parse_file_references(issue_text)
    ambiguous = next(item for item in parsed if item.path == "在new.py")
    suffix_span = ambiguous.ambiguous_cjk_suffix_span

    assert suffix_span is not None
    assert issue_text[slice(*suffix_span)] == "中修改 helper.py 的逻辑"
    with pytest.raises(AmbiguousIssuePathError) as raised:
        PlanBuilder().build(
            _semantic_snapshot(),
            IssueInput(number=69, title=issue_text, body="Preserve behavior."),
        )
    separated_example = "在 new.py 中修改 helper.py 的逻辑"
    assert repr(separated_example) in str(raised.value)
    assert ("new.py", True) in [
        (item.path, item.target_eligible)
        for item in PlanBuilder._parse_file_references(separated_example)
    ]


def test_attached_cjk_location_suffix_is_not_truncated_at_the_context_probe_limit() -> None:
    suffix = "中修改" + "边" * 254
    issue_text = "在new.py" + suffix
    parsed = PlanBuilder._parse_file_references(issue_text)
    suffix_span = parsed[0].ambiguous_cjk_suffix_span

    assert suffix_span is not None
    assert issue_text[slice(*suffix_span)] == suffix
    with pytest.raises(AmbiguousIssuePathError) as raised:
        PlanBuilder().build(
            _semantic_snapshot(),
            IssueInput(number=70, title="Fix", body=issue_text),
        )
    separated_example = "在 new.py " + suffix
    assert repr(separated_example) in str(raised.value)
    assert ("new.py", True) in [
        (item.path, item.target_eligible)
        for item in PlanBuilder._parse_file_references(separated_example)
    ]


@pytest.mark.parametrize(
    ("issue_text", "preserved_suffix"),
    (
        ("在new.py中修改a.py。", "中修改a.py。"),
        ("在new.py中修改a.py，随后检查README.md", "中修改a.py，随后检查README.md"),
        ("在new.py中修改src/a.py", "中修改src/a.py"),
        ("在new.py中修改v2.1/helper.py逻辑", "中修改v2.1/helper.py逻辑"),
        ("在new.py中修改a.py?", "中修改a.py?"),
        ("在new.py中修改a.py#next", "中修改a.py#next"),
        ("在new.py中修改a.py？", "中修改a.py？"),
        ("在new.py中修改a.py！", "中修改a.py！"),
    ),
)
def test_attached_cjk_location_prefers_the_first_complete_path_endpoint(
    issue_text: str,
    preserved_suffix: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert parsed[0].path == "在new.py"
    assert parsed[0].target_eligible is False
    assert parsed[0].ambiguous_cjk_prefix == "在"
    suffix_span = parsed[0].ambiguous_cjk_suffix_span
    assert suffix_span is not None
    assert issue_text[slice(*suffix_span)] == preserved_suffix
    with pytest.raises(AmbiguousIssuePathError) as raised:
        PlanBuilder().build(
            _semantic_snapshot(),
            IssueInput(number=71, title=issue_text, body="Preserve behavior."),
        )
    separated_example = "在 new.py " + preserved_suffix
    assert repr(separated_example) in str(raised.value)
    assert ("new.py", True) in [
        (item.path, item.target_eligible)
        for item in PlanBuilder._parse_file_references(separated_example)
    ]


def test_attached_cjk_action_cannot_be_erased_by_a_later_url_shaped_path() -> None:
    issue_text = "修改new.py并更新src/a.py"
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert parsed[0].path == "修改new.py"
    assert parsed[0].target_eligible is False
    assert parsed[0].ambiguous_cjk_prefix == "修改"
    with pytest.raises(AmbiguousIssuePathError) as raised:
        PlanBuilder().build(
            _semantic_snapshot(),
            IssueInput(number=72, title=issue_text, body="Preserve behavior."),
        )
    assert repr("修改 new.py") in str(raised.value)


@pytest.mark.parametrize(
    ("issue_text", "expected_path", "expected_suffix"),
    (
        ("修改pkg.py/module.py", "修改pkg.py/module.py", ""),
        ("修改foo.py修改/bar.py", "修改foo.py修改/bar.py", ""),
        ("在foo.py修改/bar.py中更新", "在foo.py修改/bar.py", "中更新"),
        ("修改foo.py错误/bar.py", "修改foo.py错误/bar.py", ""),
        ("修改a.py错b.py", "修改a.py错b.py", ""),
    ),
)
def test_attached_cjk_reference_does_not_stop_inside_a_path_operand(
    issue_text: str,
    expected_path: str,
    expected_suffix: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert [(item.path, item.target_eligible) for item in parsed] == [(expected_path, False)]
    suffix_span = parsed[0].ambiguous_cjk_suffix_span
    assert suffix_span is not None
    assert issue_text[slice(*suffix_span)] == expected_suffix
    prefix = parsed[0].ambiguous_cjk_prefix
    assert prefix is not None
    operand = expected_path[len(prefix) :]
    compact = "@" + operand + expected_suffix
    assert [
        (item.path, item.target_eligible) for item in PlanBuilder._parse_file_references(compact)
    ] == [(operand, True)]


@pytest.mark.parametrize(
    "issue_text",
    (
        "修改foo.pyx",
        "修改foo.pyjunk",
        "修改foo.py.old",
        "修改foo.py.tsx",
        "修改foo.py/bar.txt",
        "修改foo.py错误a.pyx",
        "修改foo.py错误a.mdbackup",
        "修改foo.py．old",
        "修改foo.py｡old",
        "修改foo.py。bak",
        "并于b.py里新增\\备份",
        "创建README.mdbackup",
    ),
)
def test_attached_cjk_action_does_not_salvage_an_incomplete_path(
    issue_text: str,
) -> None:
    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize(
    ("field", "limit"),
    (("title", ISSUE_TITLE_MAX_LENGTH), ("body", ISSUE_BODY_MAX_LENGTH)),
)
def test_ambiguity_guidance_uses_a_length_preserving_label_at_field_limits(
    field: str,
    limit: int,
) -> None:
    prefix = "在new.py中修改"
    issue_text = prefix + "边" * (limit - len(prefix))
    compact_example = "@new.py中修改" + "边" * (limit - len(prefix))
    issue = (
        IssueInput(number=73, title=issue_text, body="Preserve behavior.")
        if field == "title"
        else IssueInput(number=73, title="Fix", body=issue_text)
    )

    with pytest.raises(AmbiguousIssuePathError) as raised:
        PlanBuilder().build(_semantic_snapshot(), issue)

    assert len(issue_text) == limit
    assert len(compact_example) == limit
    assert repr(compact_example) in str(raised.value)
    replay = PlanBuilder._parse_file_references(compact_example)
    assert [(item.path, item.target_eligible) for item in replay] == [("new.py", True)]


@pytest.mark.parametrize(
    ("field", "limit"),
    (("title", ISSUE_TITLE_MAX_LENGTH), ("body", ISSUE_BODY_MAX_LENGTH)),
)
@pytest.mark.parametrize("punctuation", ("?", "!", "#"))
def test_length_preserving_compact_guidance_replays_before_a_url_like_action_suffix(
    field: str,
    limit: int,
    punctuation: str,
) -> None:
    prefix = f"在new.py中修改a.py{punctuation}随后更新"
    padding = "边" * (limit - len(prefix))
    issue_text = prefix + padding
    compact_example = "@new.py" + issue_text[len("在new.py") :]
    issue = (
        IssueInput(number=731, title=issue_text, body="Preserve behavior.")
        if field == "title"
        else IssueInput(number=731, title="Fix", body=issue_text)
    )

    with pytest.raises(AmbiguousIssuePathError) as raised:
        PlanBuilder().build(_semantic_snapshot(), issue)

    assert len(compact_example) == limit
    assert repr(compact_example) in str(raised.value)
    assert [
        (item.path, item.target_eligible)
        for item in PlanBuilder._parse_file_references(compact_example)
    ] == [("new.py", True)]


def test_compact_action_suffix_keeps_url_like_markdown_text_opaque() -> None:
    parsed = PlanBuilder._parse_file_references(
        "@new.py中修改a.py?redirect=[tests/leak.py](https://docs.example/context)"
    )
    assert [(item.path, item.target_eligible) for item in parsed] == [("new.py", True)]


@pytest.mark.parametrize("separator", ("→", "|", "—", "–"))
@pytest.mark.parametrize(
    "right_clause",
    (
        "Path:c.py",
        "Add`c.py`",
        "Update:[c.py](context)",
        "在[c.py](context)中修改",
    ),
)
def test_compact_fragment_action_suffix_does_not_hide_a_valid_right_clause(
    separator: str,
    right_clause: str,
) -> None:
    parsed = PlanBuilder._parse_file_references("@a.py中修改b.py#x" + separator + right_clause)
    assert [(item.path, item.target_eligible) for item in parsed] == [
        ("a.py", True),
        ("c.py", True),
    ]


def test_compact_fragment_action_suffix_keeps_markdown_text_opaque() -> None:
    parsed = PlanBuilder._parse_file_references("@a.py中修改b.py#[x.py](context)")
    assert [(item.path, item.target_eligible) for item in parsed] == [("a.py", True)]


def test_compact_path_label_stays_opaque_inside_an_explicit_url_value() -> None:
    assert PlanBuilder._parse_file_references("URL = @new.py中修改") == ()


@pytest.mark.parametrize(
    ("issue_text", "expected_path"),
    (
        ("@pkg.py/module.py中修改", "pkg.py/module.py"),
        ("@new.py中修改a.py", "new.py"),
        ("@new.py并更新src/a.py", "new.py"),
        ("@pkg.py/module.py错误处理", "pkg.py/module.py"),
        ("@foo.py.other.py", "foo.py.other.py"),
        ("@foo.py修改/bar.py中更新", "foo.py修改/bar.py"),
        ("@foo.py错误/bar.py", "foo.py错误/bar.py"),
        ("@foo.py错误a.py", "foo.py错误a.py"),
        ("@foo.py错误bar.txt", "foo.py"),
    ),
)
def test_compact_path_label_uses_the_grammar_boundary_not_the_first_suffix(
    issue_text: str,
    expected_path: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)
    assert [(item.path, item.target_eligible) for item in parsed] == [(expected_path, True)]


@pytest.mark.parametrize(
    "issue_text",
    ("@foo.py.old", "@foo.py备份", "@foo.py副本", "@foo.py旧版"),
)
def test_compact_path_label_does_not_downgrade_an_invalid_suffix(
    issue_text: str,
) -> None:
    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize(
    "issue_text",
    (
        "@foo.py错误/../bar.py",
        "@foo.py错误/.git/bar.py",
        "@foo.py错误//bar.py",
        "@foo.py错误/bar.txt",
        "@foo.py修改/../bar.py",
        "@foo.py中修改/bar.txt",
        "@foo.py错误\\bar.py",
        "@/x.toml",
    ),
)
def test_compact_path_label_never_revives_a_rejected_continuation(
    issue_text: str,
) -> None:
    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize("padding_length", (499, 500, 501))
@pytest.mark.parametrize(
    "continuation",
    ("/../bar.py", "/.git/bar.py", "//bar.py", "/bar.txt", "\\bar.py"),
)
def test_distant_invalid_continuation_never_authorizes_a_short_endpoint(
    padding_length: int,
    continuation: str,
) -> None:
    suffix = "错" * padding_length + continuation

    assert PlanBuilder._parse_file_references("@foo.py" + suffix) == ()
    assert PlanBuilder._parse_file_references("修改foo.py" + suffix) == ()


def test_long_generic_cjk_suffix_without_a_path_continuation_keeps_its_endpoint() -> None:
    suffix = "错" * 501

    assert [
        (item.path, item.target_eligible)
        for item in PlanBuilder._parse_file_references("@foo.py" + suffix)
    ] == [("foo.py", True)]


@pytest.mark.parametrize(
    "issue_text",
    ("修改/foo.py", "再请修改/x.toml", "修改../foo.py", "修改.git/foo.py"),
)
def test_action_shaped_path_with_an_invalid_stripped_operand_is_not_a_target_or_ambiguity(
    issue_text: str,
) -> None:
    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize(
    "issue_text",
    (
        "@foo.py．old",
        "@foo.py｡old",
        "@foo.py。bak",
        "@foo.py．ＢＡＫ",
    ),
)
def test_compact_path_label_rejects_unicode_derived_copy_suffixes(
    issue_text: str,
) -> None:
    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize(
    "issue_text",
    ("foo.py．old", "foo.py｡old", "foo.py。bak", "修改foo.py．ＢＡＫ"),
)
def test_bare_and_attached_paths_reject_unicode_derived_copy_suffixes(
    issue_text: str,
) -> None:
    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize(
    "issue_text",
    (
        "@foo.py．old/bar.py",
        "@foo.py｡ＯＬＤ/bar.py",
        "@foo.py。backup/bar.py",
        "@foo.py．copy/bar.py",
        "@foo.py。ＢＡＫ/bar.py",
        "@foo.py｡备份/bar.py",
        "@foo.py。副本/bar.py",
        "@foo.py｡bk/bar.py",
        "@foo.py．diff/bar.py",
        "@foo.py。patch/bar.py",
        "@foo.py.old/bar.py",
        "@foo.py.OLD/bar.py",
        "@foo.py.BaK/bar.py",
        "@foo.py_old/bar.py",
        "@foo.py＿ＢＡＫ/bar.py",
        "@foo.pybackup/bar.py",
        "@foo.pycopy/bar.py",
        "src/foo.py．old/bar.py",
        "src/foo.py.old/bar.py",
        "修改src/foo.py．old/bar.py",
        "修改src/foo.py_backup/bar.py",
        "在src/foo.py中修改。old/bar.py",
        "Path:src/foo.py｡old/bar.py",
        '"src/foo.py"．old/bar.py',
        '"src/foo.py".old/bar.py',
        "`src/foo.py`．old/bar.py",
        "(src/foo.py)．old/bar.py",
        "[src/foo.py]．old/bar.py",
        "https://example.test/?q=src/foo.py．old/bar.py",
    ),
)
def test_invalid_unicode_suffix_cannot_escape_a_same_token_reference_envelope(
    issue_text: str,
) -> None:
    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize(
    "issue_text",
    (
        'URL="src/foo.py.old/bar.py";Update:"src/pkg/real.py"',
        'URL="src/foo.py．ＯＬＤ/bar.py";Update:"src/pkg/real.py"',
        '@foo.py.old/bar.py;Update:"src/pkg/real.py"',
        "src/foo.py．old/bar.py；然后修改“src/pkg/real.py”",
        "URL=`src/foo.py．old/bar.py`,Path:src/pkg/real.py",
        "URL={src/foo.py.old/bar.py};Create:src/pkg/real.py",
        "src/foo.py.old/bar.py;src/pkg/real.py",
        "URL=src/foo.py。bk/bar.py→Path:src/pkg/real.py",
        'URL=src/foo.py。bk/bar.py|Update:"src/pkg/real.py"',
        "URL=src/foo.py。bk/bar.py—然后修改“src/pkg/real.py”",
        "URL=src/foo.py。bk/bar.py–Create:src/pkg/real.py",
        "URL=src/foo.py。bk/bar.py→src/pkg/real.py",
        *(
            f'URL=src/foo.py{dot}{suffix}/bar.py;Update:"src/pkg/real.py"'
            for dot in ("。", "｡", "．")
            for suffix in ("bk", "diff", "patch")
        ),
    ),
)
def test_invalid_clause_does_not_poison_a_following_explicit_clause(issue_text: str) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == ("src/pkg/real.py",)


@pytest.mark.parametrize(
    ("issue_text", "expected"),
    (
        ('URL="a.py。bk/b.py;src/pkg/hidden.py"', ()),
        (
            "URL=(a.py。bk/b.py;src/pkg/hidden.py);Update:src/pkg/real.py",
            ("src/pkg/real.py",),
        ),
        (r"URL=a.py。bk/b.py\;Update:src/pkg/real.py", ()),
        (r"URL=a.py。bk/b.py\\;Update:src/pkg/real.py", ("src/pkg/real.py",)),
        ("URL=(a.py。bk/b.py;Update:src/pkg/real.py", ()),
        ('URL="a.py。bk/b.py;Update:src/pkg/real.py', ()),
        ("URL=a。b/(；Update:src/pkg/real.py", ()),
        ('URL=a。b/"；Update:src/pkg/real.py', ()),
    ),
)
def test_idna_url_clause_split_respects_wrappers_escapes_and_unbalanced_input(
    issue_text: str,
    expected: tuple[str, ...],
) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == expected


@pytest.mark.parametrize(
    "issue_text",
    (
        "https://e.test/?q=x→Path:src/pkg/real.py",
        'mailto:user@e.test?body=x|Update:"src/pkg/real.py"',
        "idea://open?x—然后修改“src/pkg/real.py”",
        "//e.test/?q=x–Create:src/pkg/real.py",
    ),
)
def test_explicit_uri_hard_semantic_boundary_reopens_issue_clause(issue_text: str) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == ("src/pkg/real.py",)


@pytest.mark.parametrize(
    "issue_text",
    (
        "URL = https://e.test/?q=x→Path:src/pkg/real.py",
        "URI : mailto:user@e.test?body=x|Add`src/pkg/real.py`",
        'href ： "https://e.test/?q=x"—Update:[src/pkg/real.py](context)',
        "link = “https://e.test/?q=x”–Create【src/pkg/real.py】",
        "网址 ： ‘https://e.test/?q=x’→在[src/pkg/real.py](context)中修改",
        "参见URL = 「https://e.test/?q=x」|请在`src/pkg/real.py`中测试",
        "URL = Path: src/pkg/hidden.py→Update:src/pkg/real.py",
        'URI : Add "src/pkg/hidden.py"|Create:`src/pkg/real.py`',
        "网址 ： 修改 src/pkg/hidden.py—在[src/pkg/real.py](context)中修改",
        "URL=修改: src/pkg/hidden.py→Path:src/pkg/real.py",
        "URI：创建: src/pkg/hidden.py|Add`src/pkg/real.py`",
        "网址=更新:[src/pkg/hidden.py](context)—请在`src/pkg/real.py`中测试",
    ),
)
def test_separate_url_value_hard_semantic_boundary_reopens_issue_clause(
    issue_text: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)
    assert [(item.path, item.target_eligible) for item in parsed] == [("src/pkg/real.py", True)]


@pytest.mark.parametrize(
    ("opener", "closer"),
    (
        ("`", "`"),
        ('"', '"'),
        ("'", "'"),
        ("“", "”"),
        ("‘", "’"),
        ("「", "」"),
        ("『", "』"),
        ("(", ")"),
        ("（", "）"),
        ("[", "]"),
        ("【", "】"),
        ("{", "}"),
    ),
)
def test_spaced_structured_url_value_reopens_only_after_its_closer(
    opener: str,
    closer: str,
) -> None:
    issue_text = (
        f"URL = {opener}opaque src/pkg/leak.py→Path:src/pkg/internal.py{closer}"
        "→Path:src/pkg/real.py"
    )

    parsed = PlanBuilder._parse_file_references(issue_text)

    assert [(item.path, item.target_eligible) for item in parsed] == [("src/pkg/real.py", True)]


@pytest.mark.parametrize(
    ("opener", "closer"),
    (
        ("`", "`"),
        ('"', '"'),
        ("'", "'"),
        ("“", "”"),
        ("‘", "’"),
        ("「", "」"),
        ("『", "』"),
        ("(", ")"),
        ("（", "）"),
        ("[", "]"),
        ("【", "】"),
        ("{", "}"),
    ),
)
def test_completed_structured_url_value_needs_a_clause_separator(
    opener: str,
    closer: str,
) -> None:
    issue_text = f'URL = {opener}opaque src/pkg/hidden.py{closer} Path: "src/pkg/leak.py"'

    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize(
    "template",
    (
        "URL=URL=Add {operand}",
        "URL= URL= Add {operand}",
        "URL=URL=URL=Add {operand}",
        "网址 = URL : Create {operand}",
        "网址=URL=请在 {operand} 中修改",
    ),
)
@pytest.mark.parametrize(
    ("opener", "closer"),
    (
        ("`", "`"),
        ('"', '"'),
        ("'", "'"),
        ("“", "”"),
        ("‘", "’"),
        ("「", "」"),
        ("『", "』"),
        ("(", ")"),
        ("（", "）"),
        ("[", "]"),
        ("【", "】"),
        ("{", "}"),
    ),
)
@pytest.mark.parametrize(
    ("tail", "expected"),
    (
        ("", ()),
        (' Path: "src/pkg/outside.py"', ()),
        ("→Path:src/pkg/outside.py", ("src/pkg/outside.py",)),
        (",Path:src/pkg/outside.py", ("src/pkg/outside.py",)),
        (";Path:src/pkg/outside.py", ("src/pkg/outside.py",)),
        ("，Path:src/pkg/outside.py", ("src/pkg/outside.py",)),
        ("；Path:src/pkg/outside.py", ("src/pkg/outside.py",)),
        ("\\→Path:src/pkg/outside.py", ()),
    ),
)
def test_nested_url_label_action_values_respect_only_real_clause_seams(
    template: str,
    opener: str,
    closer: str,
    tail: str,
    expected: tuple[str, ...],
) -> None:
    operand = f"{opener}src/pkg/internal.py{closer}"
    issue_text = template.format(operand=operand) + tail

    assert PlanBuilder._explicit_file_references(issue_text) == expected


@pytest.mark.parametrize(
    "inner_label",
    tuple(
        dict.fromkeys(
            (
                *(
                    prefix + base
                    for prefix in (
                        "",
                        "参见",
                        "查看",
                        "请参见",
                        "请查看",
                        "参考",
                        "链接",
                        "see",
                        "use",
                    )
                    for base in ("url", "uri", "link", "href", "src")
                ),
                *(
                    prefix + base
                    for prefix in ("", "参见", "查看", "请参见", "请查看", "参考")
                    for base in ("链接", "网址", "URL地址", "链接地址")
                ),
            )
        )
    ),
)
@pytest.mark.parametrize(
    "operand",
    ('"src/pkg/internal.py"', "[src/pkg/internal.py](context)"),
)
@pytest.mark.parametrize(
    ("tail", "expected"),
    (
        ("", ()),
        (" Path:src/pkg/outside.py", ()),
        ("→Path:src/pkg/outside.py", ("src/pkg/outside.py",)),
    ),
)
def test_nested_url_chain_uses_every_label_accepted_by_the_canonical_grammar(
    inner_label: str,
    operand: str,
    tail: str,
    expected: tuple[str, ...],
) -> None:
    issue_text = f"URL={inner_label}=Add {operand}{tail}"

    assert PlanBuilder._explicit_file_references(issue_text) == expected


@pytest.mark.parametrize(
    ("operand", "tail", "expected"),
    (
        ('"opaque src/pkg/internal.py"', ' Path: "src/pkg/outside.py"', ()),
        (
            '"opaque src/pkg/internal.py"',
            "→Path:src/pkg/outside.py",
            ("src/pkg/outside.py",),
        ),
        (
            '"opaque src/pkg/internal.py"',
            ",Path:src/pkg/outside.py",
            ("src/pkg/outside.py",),
        ),
        (
            '"opaque src/pkg/internal.py"',
            ";Path:src/pkg/outside.py",
            ("src/pkg/outside.py",),
        ),
        ('"opaque src/pkg/internal.py', "→Path:src/pkg/outside.py", ()),
        ("[src/pkg/internal.py](context)", " Path:src/pkg/outside.py", ()),
        (
            "[src/pkg/internal.py](context)",
            "→Path:src/pkg/outside.py",
            ("src/pkg/outside.py",),
        ),
        ("[src/pkg/internal.py][guide]", " Path:src/pkg/outside.py", ()),
        (
            "[src/pkg/internal.py][guide]",
            "|Path:src/pkg/outside.py",
            ("src/pkg/outside.py",),
        ),
    ),
)
def test_nested_url_label_structured_values_keep_the_same_line_opaque(
    operand: str,
    tail: str,
    expected: tuple[str, ...],
) -> None:
    assert PlanBuilder._explicit_file_references(f"URL=URL={operand}{tail}") == expected


@pytest.mark.parametrize("separator", (",", ";"))
def test_nested_url_labels_do_not_split_ascii_delimiters_inside_a_real_uri(
    separator: str,
) -> None:
    issue_text = f"URL=URL:https://e.test/?q=src/pkg/internal.py{separator}Path:src/pkg/leak.py"

    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize("line_break", ("\n", "\r", "\r\n"))
def test_url_value_opacity_ends_at_every_physical_line_break(line_break: str) -> None:
    assert PlanBuilder._explicit_file_references(
        f'URL = "opaque src/pkg/internal.py{line_break}Path:src/pkg/outside.py'
    ) == ("src/pkg/outside.py",)
    assert PlanBuilder._explicit_file_references(f'URL={line_break}"src/pkg/outside.py"') == (
        "src/pkg/outside.py",
    )


@pytest.mark.parametrize(
    "issue_text",
    (
        "URL = (opaque})→Path:src/pkg/leak.py",
        "URL = [opaque)]→Path:src/pkg/leak.py",
        "URL = {opaque]}→Path:src/pkg/leak.py",
        "URL = （opaque】）→Path:src/pkg/leak.py",
        "URL = 【opaque）】→Path:src/pkg/leak.py",
        "URL=URL=(opaque})→Path:src/pkg/leak.py",
    ),
)
def test_mismatched_structured_url_wrappers_fail_closed_to_line_end(
    issue_text: str,
) -> None:
    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize("line_break", ("\n", "\r", "\r\n"))
def test_markdown_definitions_and_malformed_destinations_share_physical_lines(
    line_break: str,
) -> None:
    definition = f"intro{line_break}[src/pkg/leak.py]: https://e.test/context"
    malformed = f"[opaque](unclosed{line_break}Path:src/pkg/outside.py"

    assert PlanBuilder._parse_file_references(definition) == ()
    assert PlanBuilder._explicit_file_references(malformed) == ("src/pkg/outside.py",)


@pytest.mark.parametrize("separator", ("→", "|", "—", "–"))
@pytest.mark.parametrize(
    "prefix",
    ('URL = "opaque"', 'URL=URL="opaque"', "URL = [opaque](context)"),
)
def test_structured_url_value_hard_seams_reopen_bare_repository_paths(
    prefix: str,
    separator: str,
) -> None:
    issue_text = f"{prefix}{separator}src/pkg/outside.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("src/pkg/outside.py",)


@pytest.mark.parametrize(
    "authority_path",
    ("example.com/src/pkg/leak.py", "例子.中国/src/pkg/leak.py"),
)
def test_structured_url_seam_does_not_authorize_a_second_url_as_a_path(
    authority_path: str,
) -> None:
    issue_text = f'URL = "opaque"→{authority_path}'

    assert PlanBuilder._parse_file_references(authority_path) == ()
    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize("path", ("new.py", "src/pkg/a.py"))
def test_structured_url_seam_preserves_attached_cjk_path_ambiguity(path: str) -> None:
    issue_text = f'URL = "opaque"→在{path}中修改'

    parsed = PlanBuilder._parse_file_references(issue_text)

    assert [(item.path, item.target_eligible) for item in parsed] == [(f"在{path}", False)]
    assert parsed[0].ambiguous_cjk_prefix == "在"
    assert parsed[0].ambiguous_cjk_suffix_span is not None
    assert issue_text[slice(*parsed[0].ambiguous_cjk_suffix_span)] == "中修改"


@pytest.mark.parametrize("spacing", ("", " "))
def test_structured_url_seam_emits_one_canonical_bare_path(spacing: str) -> None:
    parsed = PlanBuilder._parse_file_references(f'URL = "opaque"{spacing}→src/pkg/a.py')

    assert [(item.path, item.target_eligible) for item in parsed] == [("src/pkg/a.py", True)]


@pytest.mark.parametrize("separator", ("→", "|", "—", "–"))
@pytest.mark.parametrize("spacing", ("", " "))
@pytest.mark.parametrize(
    "url_value",
    ('URL="opaque"', "URL=[opaque](context)", "URL=https://e.test/x"),
)
def test_structured_url_seam_defers_compact_operands_to_the_compact_parser(
    url_value: str,
    spacing: str,
    separator: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(
        f"{url_value}{separator}{spacing}@src/post.py中修改"
    )

    assert tuple((item.path, item.target_eligible) for item in parsed) == (("src/post.py", True),)


@pytest.mark.parametrize("spacing", ("", " "))
def test_structured_url_seam_does_not_emit_a_separator_prefixed_root_path(
    spacing: str,
) -> None:
    assert PlanBuilder._parse_file_references(f'URL = "opaque"{spacing}→new.py') == ()


@pytest.mark.parametrize(
    ("clauses", "expected"),
    (
        (
            "src/a.py→Path:src/b.py",
            (("src/a.py", True), ("src/b.py", True)),
        ),
        ("new.py→src/b.py", (("src/b.py", True),)),
        (
            "example.com/src/leak.py→Path:src/b.py",
            (("src/b.py", True),),
        ),
    ),
)
def test_structured_url_recovery_preserves_every_later_same_token_clause(
    clauses: str,
    expected: tuple[tuple[str, bool], ...],
) -> None:
    parsed = PlanBuilder._parse_file_references(f'URL="opaque"→{clauses}')

    assert tuple((item.path, item.target_eligible) for item in parsed) == expected


@pytest.mark.parametrize(
    ("issue_text", "expected"),
    (
        ('URL = "opaque"→ Path:src/a.py', (("src/a.py", True),)),
        (
            'URL = "opaque"→ [src/a.py](context)',
            (("src/a.py", True),),
        ),
        ('URL = "opaque"→Add`new.py`', (("new.py", True),)),
        ('URL = "opaque"→ Add`new.py`', (("new.py", True),)),
    ),
)
def test_proven_url_seam_bounds_every_following_reference_parser(
    issue_text: str,
    expected: tuple[tuple[str, bool], ...],
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert tuple((item.path, item.target_eligible) for item in parsed) == expected


@pytest.mark.parametrize(
    ("issue_text", "expected"),
    (
        ('URL="opaque"→URL = Path:src/leak.py', ()),
        ('URL=""→x/y.py→URL=""', (("x/y.py", True),)),
        (
            'URL="x"→Path:src/a.py→URL="y"→Path:src/b.py',
            (("src/a.py", True), ("src/b.py", True)),
        ),
        ('URL=x→URL="x" Path:a/b.py', ()),
        ('URL=x→URL="x a/b.py"→Path:c/d.py', (("c/d.py", True),)),
        (
            'Path:pre/a.py→URL="i a/b.py"→URL="j c/d.py"→Path:post/e.py',
            (("pre/a.py", True), ("post/e.py", True)),
        ),
        ('URL="opaque"→URL = URI = Path:src/leak.py', ()),
        (
            'URL="opaque"→URL = Path:src/leak.py→Path:src/real.py',
            (("src/real.py", True),),
        ),
        (
            'URL="prose URL = Path:src/internal.py"→Path:src/real.py',
            (("src/real.py", True),),
        ),
    ),
)
def test_new_url_labels_reset_only_their_own_clause(
    issue_text: str,
    expected: tuple[tuple[str, bool], ...],
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert tuple((item.path, item.target_eligible) for item in parsed) == expected


def test_every_canonical_url_label_uses_its_complete_longest_name() -> None:
    for label in _URL_LABEL_NAMES:
        cjk_value = f'URL="outer"→{label} = 在[src/leak.py](context)中修改→Path:src/real.py'
        spaced_value = f'URL="outer"→{label}:"opaque src/leak.py"→Path:src/real.py'
        compact_value = f'URL="outer"→{label} = @src/leak.py中修改→Path:src/real.py'

        assert PlanBuilder._explicit_file_references(cjk_value) == ("src/real.py",)
        assert PlanBuilder._explicit_file_references(spaced_value) == ("src/real.py",)
        assert PlanBuilder._explicit_file_references(compact_value) == ("src/real.py",)


def test_every_spaced_canonical_url_label_reopens_at_proven_list_seams() -> None:
    for label in _URL_LABEL_NAMES:
        for separator in ",;，、；":
            issue_text = f"{label} = x{separator}Path:src/real.py"

            assert PlanBuilder._explicit_file_references(issue_text) == ("src/real.py",)


@pytest.mark.parametrize(
    ("opener", "closer"),
    (
        ("`", "`"),
        ('"', '"'),
        ("'", "'"),
        ("“", "”"),
        ("‘", "’"),
        ("「", "」"),
        ("『", "』"),
        ("(", ")"),
        ("（", "）"),
        ("[", "]"),
        ("【", "】"),
        ("{", "}"),
    ),
)
@pytest.mark.parametrize(
    "url_clause",
    (
        "URL=x",
        "URL=Path:src/leak.py",
        "prose URL=x",
        "prose URL = Path:src/leak.py",
    ),
)
def test_complete_url_assignment_wrappers_preserve_neighboring_targets(
    opener: str,
    closer: str,
    url_clause: str,
) -> None:
    envelope = f"{opener}{url_clause}{closer}"

    assert PlanBuilder._parse_file_references(envelope) == ()
    assert PlanBuilder._explicit_file_references(f"Path:a/b.py→{envelope}→Path:c/d.py") == (
        "a/b.py",
        "c/d.py",
    )


@pytest.mark.parametrize(
    "opener",
    ("`", '"', "'", "“", "‘", "「", "『", "(", "（", "[", "【", "{"),
)
@pytest.mark.parametrize("url_clause", ("URL=x", "prose URL = x"))
def test_unclosed_outer_url_assignment_wrappers_keep_internal_targets_opaque(
    opener: str,
    url_clause: str,
) -> None:
    issue_text = f"Path:a/b.py→{opener}{url_clause}→Path:c/d.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("a/b.py",)


def test_unclosed_image_url_assignment_preserves_only_the_left_target() -> None:
    issue_text = "Path:a/b.py→![prose URL=x→Path:c/d.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("a/b.py",)


@pytest.mark.parametrize(
    "prose",
    ("don't", "it's prose", "users'"),
)
def test_word_apostrophes_do_not_open_a_url_assignment_wrapper(prose: str) -> None:
    issue_text = f"Path:a/b.py→{prose} URL=x→Path:c/d.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("c/d.py",)


def test_real_single_quote_still_wraps_a_url_assignment() -> None:
    issue_text = "Path:a/b.py→'prose URL=x'→Path:c/d.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("a/b.py", "c/d.py")


@pytest.mark.parametrize(
    "prose",
    ("don't", "it's prose", "users'", "don’t", "it’s prose", "users’"),
)
def test_word_apostrophes_after_a_complete_url_value_do_not_hide_a_hard_seam(
    prose: str,
) -> None:
    issue_text = f'URL="x" {prose}→Path:c/d.py'

    assert PlanBuilder._explicit_file_references(issue_text) == ("c/d.py",)


@pytest.mark.parametrize(
    ("opener", "apostrophe", "closer"),
    (("'", "'", "'"), ("‘", "’", "’")),
)
@pytest.mark.parametrize("closed", (False, True))
def test_contraction_apostrophe_does_not_close_an_active_url_quote(
    opener: str,
    apostrophe: str,
    closer: str,
    closed: bool,
) -> None:
    suffix = f"{closer}→Path:b.py" if closed else ""
    issue_text = f"Path:a.py→{opener}don{apostrophe}t URL=x→Path:hidden.py{suffix}"

    assert PlanBuilder._explicit_file_references(issue_text) == (
        ("a.py", "b.py") if closed else ("a.py",)
    )


@pytest.mark.parametrize(
    "elision",
    (
        "'tis",
        "'twas",
        "'cause",
        "'em",
        "'90s",
        "'round",
        "'til",
        "'nother",
        "'n",
        "'bout",
        "'cept",
        "'gainst",
        "'neath",
        "'ere",
        "'scuse",
        "'sup",
        "'kay",
    ),
)
def test_leading_english_elisions_do_not_open_an_unclosed_quote(elision: str) -> None:
    issue_text = f"Path:a.py→{elision} URL=x→Path:c.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("c.py",)


@pytest.mark.parametrize(
    "elision",
    (
        "tis",
        "twas",
        "cause",
        "em",
        "90s",
        "round",
        "til",
        "nother",
        "n",
        "bout",
        "cept",
        "gainst",
        "neath",
        "ere",
        "scuse",
        "sup",
        "kay",
    ),
)
def test_complete_single_quote_takes_precedence_over_leading_elision(elision: str) -> None:
    issue_text = f"Path:a.py→'{elision} URL=x' Path:hidden.py→Path:c.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("a.py", "c.py")


@pytest.mark.parametrize("apostrophe", ("'", "’"))
def test_rock_n_roll_elisions_do_not_hide_a_proven_url_seam(apostrophe: str) -> None:
    issue_text = f'URL="x" rock {apostrophe}n{apostrophe} roll→Path:c/d.py'

    assert PlanBuilder._explicit_file_references(issue_text) == ("c/d.py",)


@pytest.mark.parametrize(
    "quoted_prose",
    (
        "'users' URL=x' tail",
        "'rock 'n' roll URL=x' tail",
        "‘users’ URL=x’ tail",
        "‘rock ’n’ roll URL=x’ tail",
    ),
)
def test_completed_possessive_or_elision_quotes_do_not_extend_url_opacity(
    quoted_prose: str,
) -> None:
    issue_text = f"Path:a.py→{quoted_prose}→Path:c.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("a.py", "c.py")


@pytest.mark.parametrize("opener", ("'", "‘"))
def test_complete_leading_elision_url_quote_hides_only_its_own_tail(opener: str) -> None:
    closer = "'" if opener == "'" else "’"
    issue_text = f"Path:a.py→{opener}90s URL=x,prose{closer} Path:hidden.py→Path:c.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("a.py", "c.py")


@pytest.mark.parametrize("action", ("Update", "修改", "请在"))
def test_action_adjacent_single_quote_recovers_a_markdown_url_label(
    action: str,
) -> None:
    issue_text = f"Path:a.py→{action}'[src/real.py](URL=x)→Path:hidden.py"

    assert PlanBuilder._explicit_file_references(issue_text) == (
        "a.py",
        "src/real.py",
    )


@pytest.mark.parametrize(
    "issue_text",
    (
        "('tis @before.py) (URL=users')",
        "(‘tis @before.py) (URL=users’)",
        "('tis @before.py)\n(URL=users')",
        "(‘tis @before.py)\r\n(URL=users’)",
    ),
)
def test_single_quote_roles_do_not_pair_across_independent_wrappers(
    issue_text: str,
) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == ("before.py",)


@pytest.mark.parametrize(
    "issue_text",
    (
        "'x' @a.py URL=z users'→@b.py",
        "‘x’ @a.py URL=z users’→@b.py",
    ),
)
def test_completed_quote_does_not_pair_with_a_later_possessive_url_tail(
    issue_text: str,
) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == ("a.py", "b.py")


@pytest.mark.parametrize("false_suffix", ("notes.pybak", "notes.py_backup"))
def test_quote_completion_requires_a_real_file_reference(false_suffix: str) -> None:
    issue_text = f"Path:left.py→'x' {false_suffix} URL=z users'→@b.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("left.py", "b.py")


@pytest.mark.parametrize("false_label", ("ſeeURL", "URİ", "URı", "LİNK", "LıNK", "ſrc"))
def test_url_labels_use_ascii_case_folding_only(false_label: str) -> None:
    issue_text = f'Path:a.py→"prose {false_label}=x→Path:c.py'

    assert PlanBuilder._explicit_file_references(issue_text) == ("c.py",)


@pytest.mark.parametrize(
    ("issue_text", "expected"),
    (
        ("Path:a/b.py→([URL=x]→Path:c/d.py", ("a/b.py",)),
        ("Path:a/b.py→(URL=x]→Path:c/d.py", ("a/b.py",)),
        (r'Path:a/b.py→"URL=x\"→Path:c/d.py', ("a/b.py",)),
        (
            r'Path:a/b.py→"URL=x\\"→Path:c/d.py',
            ("a/b.py", "c/d.py"),
        ),
        ("Path:a/b.py→[URL=x](unclosed→Path:c/d.py", ("a/b.py",)),
        ("Path:a/b.py→[URL=x][unclosed→Path:c/d.py", ("a/b.py",)),
    ),
)
def test_outer_url_assignment_wrapper_topology_controls_reopening(
    issue_text: str,
    expected: tuple[str, ...],
) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == expected


@pytest.mark.parametrize("line_break", ("\n", "\r", "\r\n"))
def test_unclosed_outer_url_assignment_opacity_ends_at_physical_line(
    line_break: str,
) -> None:
    issue_text = f'Path:a/b.py→"prose URL = x→Path:hidden.py{line_break}Path:c/d.py'

    assert PlanBuilder._explicit_file_references(issue_text) == ("a/b.py", "c/d.py")


@pytest.mark.parametrize(
    "envelope",
    (
        "[URL=Path:src/leak.py](context)",
        "[URL=Path:src/leak.py][guide]",
        "![URL=Path:src/leak.py](context)",
    ),
)
def test_complete_url_assignment_markdown_preserves_neighboring_targets(
    envelope: str,
) -> None:
    assert PlanBuilder._parse_file_references(envelope) == ()
    assert PlanBuilder._explicit_file_references(f"Path:a/b.py→{envelope}→Path:c/d.py") == (
        "a/b.py",
        "c/d.py",
    )


def test_url_assignment_in_markdown_destination_does_not_hide_the_path_label() -> None:
    assert PlanBuilder._explicit_file_references("[src/real.py](URL=https://e.test/context)") == (
        "src/real.py",
    )


@pytest.mark.parametrize(
    ("outer_opener", "outer_closer"),
    (
        ('"', '"'),
        ("'", "'"),
        ("(", ")"),
        ("（", "）"),
        ("【", "】"),
        ("{", "}"),
        ("「", "」"),
        ("『", "』"),
    ),
)
@pytest.mark.parametrize("destination", ("(URL=x)", "[URL=x]"))
def test_ordinary_outer_wrapper_does_not_hide_a_markdown_path_label(
    outer_opener: str,
    outer_closer: str,
    destination: str,
) -> None:
    issue_text = f"Path:a.py→{outer_opener}[src/real.py]{destination}{outer_closer}→Path:b.py"

    assert PlanBuilder._explicit_file_references(issue_text) == (
        "a.py",
        "src/real.py",
        "b.py",
    )


@pytest.mark.parametrize("action", ("Update:", "修改:", "请在"))
@pytest.mark.parametrize("destination", ("(context)", "[guide]"))
def test_explicit_action_outer_preserves_an_ordinary_markdown_path_label(
    action: str,
    destination: str,
) -> None:
    issue_text = f'{action}"[src/real.py]{destination}"'

    assert PlanBuilder._explicit_file_references(issue_text) == ("src/real.py",)


@pytest.mark.parametrize("outer_closer", (")", ""))
def test_ordinary_outer_wrapper_preserves_all_peer_markdown_path_labels(
    outer_closer: str,
) -> None:
    issue_text = "([src/a.py](URL=x) [src/b.py](context)" + outer_closer

    assert PlanBuilder._explicit_file_references(issue_text) == (
        "src/a.py",
        "src/b.py",
    )


def test_ordinary_outer_wrapper_never_promotes_a_peer_image_label() -> None:
    issue_text = "([src/a.py](URL=x) ![src/leak.py](context))"

    assert PlanBuilder._explicit_file_references(issue_text) == ("src/a.py",)


def test_raw_uri_owner_never_promotes_outer_markdown_label_holes() -> None:
    issue_text = "https://e.test/([src/a.py](URL=x) [src/leak.py](context))"

    assert PlanBuilder._explicit_file_references(issue_text) == ()


@pytest.mark.parametrize(
    ("suffix", "expected"),
    (("", ()), ("→Path:b.py", ("b.py",))),
)
def test_idna_dot_uri_owner_never_promotes_outer_markdown_label_holes(
    suffix: str,
    expected: tuple[str, ...],
) -> None:
    issue_text = f"example。com/([src/leak.py](URL=x)){suffix}"

    assert PlanBuilder._explicit_file_references(issue_text) == expected


def test_uri_inside_an_outer_wrapper_still_owns_its_markdown_label() -> None:
    issue_text = '"https://e.test/([src/leak.py](URL=x))"'

    assert PlanBuilder._explicit_file_references(issue_text) == ()


@pytest.mark.parametrize("uri_joiner", ("/", "&"))
def test_raw_uri_owner_carries_across_tight_peer_markdown_references(
    uri_joiner: str,
) -> None:
    issue_text = f"https://e.test/a{uri_joiner}[src/a.py](URL=x)[src/leak.py](context)"

    assert PlanBuilder._explicit_file_references(issue_text) == ()


def test_raw_uri_owner_inside_an_outer_wrapper_carries_across_peer_labels() -> None:
    issue_text = '"https://e.test/a/([src/a.py](URL=x) [src/leak.py](context))"'

    assert PlanBuilder._explicit_file_references(issue_text) == ()


def test_completed_quote_proof_cannot_promote_a_path_owned_by_a_raw_uri() -> None:
    issue_text = "https://e.test/'x' @leak.py URL=z users'→@b.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("b.py",)


def test_completed_quote_proof_cannot_promote_a_labeled_url_operand() -> None:
    issue_text = "URL='x' @leak.py URL=z users'→@b.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("b.py",)


@pytest.mark.parametrize("hard_seam", ("→", "|", "—", "–"))
def test_hard_seam_resets_raw_uri_ownership_before_quote_proof(
    hard_seam: str,
) -> None:
    issue_text = f"https://e.test/a{hard_seam}'x' @a.py URL=z users'→@b.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("a.py", "b.py")


@pytest.mark.parametrize("separator", (" ", "\n", "→"))
def test_a_real_clause_boundary_ends_raw_uri_markdown_ownership(
    separator: str,
) -> None:
    issue_text = f"https://e.test/a/[src/a.py](URL=x){separator}[src/real.py](context)"

    assert PlanBuilder._explicit_file_references(issue_text) == ("src/real.py",)


def test_ascii_uri_data_cannot_forge_an_action_before_a_complete_outer_hole() -> None:
    issue_text = 'https://e.test/a,Add "[src/leak.py](URL=x)"→Path:b.py'

    assert PlanBuilder._explicit_file_references(issue_text) == ("b.py",)


def test_completed_outer_hole_without_a_proven_seam_keeps_its_tail_opaque() -> None:
    issue_text = 'Path:left.py→"[src/real.py](URL=x) Path:inside.py" Path:hidden.py'

    assert PlanBuilder._explicit_file_references(issue_text) == (
        "left.py",
        "src/real.py",
    )


@pytest.mark.parametrize("slash_count", (1, 2, 3, 4))
@pytest.mark.parametrize("line_break", ("\n", "\r\n"))
def test_recoverable_outer_markdown_label_terminates_at_a_physical_line(
    slash_count: int,
    line_break: str,
) -> None:
    escape_run = "\\" * slash_count
    issue_text = f'Path:left.py→"[src/real.py](URL=x){escape_run}{line_break}Path:right.py'

    assert PlanBuilder._explicit_file_references(issue_text) == (
        "left.py",
        "src/real.py",
        "right.py",
    )


@pytest.mark.parametrize(
    "markdown_reference",
    (
        "[src/leak.py](URL=x)garbage",
        "[src/leak.py][URL=x]garbage",
    ),
)
def test_recoverable_outer_label_still_rejects_markdown_trailing_garbage(
    markdown_reference: str,
) -> None:
    issue_text = f'Path:left.py→"{markdown_reference}"→Path:right.py'

    assert PlanBuilder._explicit_file_references(issue_text) == (
        "left.py",
        "right.py",
    )


@pytest.mark.parametrize("slash_count", (2, 4))
@pytest.mark.parametrize("closed", (False, True))
def test_even_escape_run_before_active_outer_preserves_the_preceding_seam(
    slash_count: int,
    closed: bool,
) -> None:
    escape_run = "\\" * slash_count
    closer = '"' if closed else ""
    issue_text = f'Path:a.py→{escape_run}"[src/real.py](URL=x){closer}→Path:b.py'
    expected = ("a.py", "src/real.py", "b.py") if closed else ("a.py", "src/real.py")

    assert PlanBuilder._explicit_file_references(issue_text) == expected


@pytest.mark.parametrize("separator", (",", ";"))
def test_spaced_list_action_recovers_an_unclosed_outer_markdown_label(
    separator: str,
) -> None:
    issue_text = f'Path:a.py{separator}Update: "[src/real.py](URL=x) prose→Path:hidden.py'

    assert PlanBuilder._explicit_file_references(issue_text) == (
        "a.py",
        "src/real.py",
    )


@pytest.mark.parametrize("separator", (",", ";"))
def test_raw_uri_ascii_data_cannot_forge_a_spaced_action_outer_hole(
    separator: str,
) -> None:
    issue_text = f'https://e.test/a{separator}Update: "[src/leak.py](URL=x) prose→Path:hidden.py'

    assert PlanBuilder._explicit_file_references(issue_text) == ()


@pytest.mark.parametrize(
    ("outer_opener", "outer_closer"),
    (
        ('"', '"'),
        ("'", "'"),
        ("(", ")"),
        ("（", "）"),
        ("【", "】"),
        ("{", "}"),
        ("「", "」"),
        ("『", "』"),
    ),
)
@pytest.mark.parametrize("separator", (",", ";", "，", "、", "；"))
@pytest.mark.parametrize("destination", ("(URL = x)", "(prose URL=x)"))
def test_outer_markdown_wrapper_propagates_a_proven_list_seam_across_tokens(
    outer_opener: str,
    outer_closer: str,
    separator: str,
    destination: str,
) -> None:
    issue_text = f"{outer_opener}[src/real.py]{destination}{outer_closer}{separator}Path:b.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("src/real.py", "b.py")


def test_nested_markdown_destination_exposes_only_the_outer_path_label() -> None:
    issue_text = "[src/outer.py]([src/inner.py](URL=x))"

    assert PlanBuilder._explicit_file_references(issue_text) == ("src/outer.py",)


@pytest.mark.parametrize("separator", (",", ";"))
@pytest.mark.parametrize(
    "destination_template",
    ("( {label}=x)", "(prose {label}=x)", "[prose {label}=x]"),
)
def test_ascii_list_seam_preserves_markdown_label_before_a_spaced_url_destination(
    separator: str,
    destination_template: str,
) -> None:
    for label in _URL_LABEL_NAMES:
        destination = destination_template.format(label=label)
        issue_text = f"Path:a/b.py{separator}[src/real.py]{destination}{separator}Path:c/d.py"

        assert PlanBuilder._explicit_file_references(issue_text) == (
            "a/b.py",
            "src/real.py",
            "c/d.py",
        )


@pytest.mark.parametrize("separator", (",", ";"))
def test_ascii_list_seam_never_promotes_markdown_image_alt_text(separator: str) -> None:
    issue_text = f"Path:a/b.py{separator}![src/leak.py](prose URL=x)"

    assert PlanBuilder._explicit_file_references(issue_text) == ("a/b.py",)


@pytest.mark.parametrize("separator", (",", ";"))
def test_ascii_uri_data_does_not_become_a_proven_repository_clause_seam(
    separator: str,
) -> None:
    issue_text = f"Path:a.py{separator}URL=https://e.test/a{separator}b{separator}Path:c.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("a.py",)


@pytest.mark.parametrize("envelope", ('"URL=x"', "(URL=x)", "[URL=x](context)"))
def test_complete_outer_url_value_without_a_proven_seam_keeps_the_tail_opaque(
    envelope: str,
) -> None:
    issue_text = f"Path:a.py→{envelope} Path:c.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("a.py",)


def test_markdown_destination_boundary_does_not_hide_following_plain_target() -> None:
    issue_text = "[src/real.py](URL=x) Path:c/d.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("src/real.py", "c/d.py")


@pytest.mark.parametrize("destination_opener", ("(", "["))
def test_unclosed_url_assignment_markdown_destination_preserves_the_path_label(
    destination_opener: str,
) -> None:
    issue_text = f"Path:a/b.py→[src/real.py]{destination_opener}URL=x→Path:c/d.py"

    assert PlanBuilder._explicit_file_references(issue_text) == (
        "a/b.py",
        "src/real.py",
    )


@pytest.mark.parametrize("destination_opener", ("(", "["))
@pytest.mark.parametrize("line_break", ("\n", "\r", "\r\n"))
def test_unclosed_url_assignment_markdown_destination_ends_at_physical_line(
    destination_opener: str,
    line_break: str,
) -> None:
    issue_text = (
        f"Path:a/b.py→[src/real.py]{destination_opener}URL=x→Path:hidden.py{line_break}Path:c/d.py"
    )

    assert PlanBuilder._explicit_file_references(issue_text) == (
        "a/b.py",
        "src/real.py",
        "c/d.py",
    )


@pytest.mark.parametrize("destination_opener", ("(", "["))
@pytest.mark.parametrize("action", ("Update:", "Add", "修改", "请在"))
def test_unclosed_url_markdown_destination_preserves_a_bounded_action_clause(
    destination_opener: str,
    action: str,
) -> None:
    issue_text = f"Path:a.py→{action}[src/real.py]{destination_opener}URL=x→Path:hidden.py"

    assert PlanBuilder._explicit_file_references(issue_text) == (
        "a.py",
        "src/real.py",
    )


def test_unclosed_image_url_assignment_with_action_preserves_only_the_left_target() -> None:
    issue_text = "Path:a.py→Update:![prose URL=x→Path:hidden.py"

    assert PlanBuilder._explicit_file_references(issue_text) == ("a.py",)


@pytest.mark.parametrize(
    "issue_text",
    (
        "URL=[src/leak.py](URL=x)",
        "URL = [src/leak.py](prose URL=x)",
        "https://e.test/[src/leak.py](URL=x)",
        "URL=https://e.test/[src/leak.py](URL=x)",
        "Path:a.py→URL=[src/leak.py](URL=x)→Path:b.py",
    ),
)
def test_nested_url_context_cannot_recover_a_markdown_destination_label(
    issue_text: str,
) -> None:
    assert "src/leak.py" not in PlanBuilder._explicit_file_references(issue_text)


@pytest.mark.parametrize(
    "issue_text",
    (
        "[src/real.py](URL=x)garbage",
        "[src/real.py][URL=x]garbage",
    ),
)
def test_url_assignment_markdown_destination_still_requires_termination(
    issue_text: str,
) -> None:
    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize("separator", ("→", "|", "—", "–"))
@pytest.mark.parametrize("spacing", (" ", "  ", "\t"))
def test_proven_url_seam_discards_inline_spacing_before_action_markdown(
    separator: str,
    spacing: str,
) -> None:
    issue_text = f'URL = "opaque"{separator}{spacing}Update:[src/a.py](context)'

    assert PlanBuilder._explicit_file_references(issue_text) == ("src/a.py",)


@pytest.mark.parametrize(
    "unit",
    (
        'URL = "opaque"→src/pkg/a.py ',
        "URL = [opaque](context)→src/pkg/a.py ",
    ),
)
def test_many_recovered_url_seams_do_not_reorder_the_protected_span_inventory(
    unit: str,
) -> None:
    issue_text = (unit * 30_000)[:640_000]

    started = perf_counter()
    assert PlanBuilder._explicit_file_references(issue_text) == ("src/pkg/a.py",)
    assert perf_counter() - started < 5.0


def test_parser_phases_never_insert_before_future_protected_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_add = _ProtectedSpanInventory.add
    displaced_intervals = 0

    def tracked_add(
        inventory: _ProtectedSpanInventory,
        span: tuple[int, int],
    ) -> None:
        nonlocal displaced_intervals
        layer = inventory._layers[-1]
        if layer and span[0] < layer[-1][1]:
            insertion_index = next(
                (index for index, existing in enumerate(layer) if existing[0] >= span[0]),
                len(layer),
            )
            displaced_intervals += max(0, len(layer) - insertion_index - 1)
        original_add(inventory, span)

    monkeypatch.setattr(_ProtectedSpanInventory, "add", tracked_add)
    issue_text = (
        "URL = [opaque](context)→Path:src/a.py→[src/b.py](context) "
        '<a href="https://e.test/x">x</a> '
    ) * 1_000

    assert PlanBuilder._explicit_file_references(issue_text) == ("src/a.py", "src/b.py")
    assert displaced_intervals == 0


def test_interleaved_markdown_definitions_keep_protected_spans_monotonic() -> None:
    unit = "[src/a.py](context)\n[id]: https://e.test/context\n"
    issue_text = (unit * 20_000)[:320_000]

    markdown_references, protected_spans, malformed_references = PlanBuilder._markdown_references(
        issue_text
    )

    assert tuple(reference.span for reference in markdown_references) == tuple(
        sorted(reference.span for reference in markdown_references)
    )
    assert protected_spans == tuple(sorted(protected_spans))
    assert malformed_references == ()
    started = perf_counter()
    assert PlanBuilder._explicit_file_references(issue_text) == ("src/a.py",)
    assert perf_counter() - started < 5.0


@pytest.mark.parametrize(
    ("destination", "separator"),
    (("(context)", "→"), ("[guide]", "|")),
)
def test_spaced_markdown_url_value_reopens_after_its_complete_destination(
    destination: str,
    separator: str,
) -> None:
    issue_text = (
        f'URL=[ https://e.test/?q=src/pkg/leak.py ]{destination}{separator}Path: "src/pkg/real.py"'
    )

    parsed = PlanBuilder._parse_file_references(issue_text)

    assert [(item.path, item.target_eligible) for item in parsed] == [("src/pkg/real.py", True)]


@pytest.mark.parametrize("destination", ("(context)", "[guide]"))
@pytest.mark.parametrize("spacing", (" ", "\t", " please "))
def test_completed_markdown_url_value_needs_a_clause_separator(
    destination: str,
    spacing: str,
) -> None:
    issue_text = f'URL=[ opaque src/pkg/hidden.py ]{destination}{spacing}Path: "src/pkg/leak.py"'

    assert PlanBuilder._parse_file_references(issue_text) == ()


def test_completed_url_value_can_reopen_after_prose_and_a_hard_seam() -> None:
    parsed = PlanBuilder._parse_file_references(
        'URL = "opaque x" trailing prose →Path: "src/pkg/real.py"'
    )

    assert [(item.path, item.target_eligible) for item in parsed] == [("src/pkg/real.py", True)]


@pytest.mark.parametrize(
    "opener",
    ("`", '"', "'", "“", "‘", "「", "『", "(", "（", "[", "【", "{"),
)
def test_unclosed_spaced_structured_url_value_is_opaque_to_line_end(opener: str) -> None:
    issue_text = f"URL = {opener}opaque src/pkg/leak.py→Path:src/pkg/real.py"

    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize(
    ("left", "operand", "suffix"),
    (
        ("URL = https://e.test/?q=x→Path:", '"src/pkg/real.py"', ""),
        ("URI : mailto:user@e.test?body=x|Path:", "[src/pkg/real.py](context)", ""),
        ("href = idea://open?file=x—Path:", "`src/pkg/real.py`", ""),
        ("网址 ： //e.test/?q=x–请在", "“src/pkg/real.py”", " 中修改"),
        (
            "参见URL = https://e.test/?q=x→请在",
            "[src/pkg/real.py](context)",
            " 中测试",
        ),
        ("URL=https://e.test/?q=x|于", "`src/pkg/real.py`", " 里更新"),
        ("URL=https://e.test/?q=x—将", "（src/pkg/real.py）", " 内修改"),
    ),
)
def test_url_tail_action_reopens_an_operand_in_the_next_whitespace_token(
    left: str,
    operand: str,
    suffix: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(f"{left} {operand}{suffix}")

    assert [(item.path, item.target_eligible) for item in parsed] == [("src/pkg/real.py", True)]


@pytest.mark.parametrize(
    "issue_text",
    (
        'URL=( https://e.test/?q=x )Path: "src/pkg/leak.py"',
        'URL=“ https://e.test/?q=x ”Path: "src/pkg/leak.py"',
        "URL=[ https://e.test/?q=x ]Add: [src/pkg/leak.py](context)",
        r'URL=( https://e.test/?q=x )\→Path: "src/pkg/leak.py"',
        'URL=[ https://e.test/?q=x ](context)Path: "src/pkg/leak.py"',
        'URL=[ https://e.test/?q=x ](context→Path: "src/pkg/leak.py"',
        'URL=[ https://e.test/?q=x ][guide→Path: "src/pkg/leak.py"',
        'URL = "https://e.test/?q=x→Path: src/pkg/leak.py"',
        "URL = “https://e.test/?q=x→请在 [src/pkg/leak.py](context) 中修改”",
        'URL = https://e.test/?q=x→Path: "src/pkg/leak.py',
    ),
)
def test_url_tail_actions_require_a_proven_external_boundary(issue_text: str) -> None:
    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize(
    ("issue_text", "expected"),
    (
        (
            'URL = "opaque \\" still src/pkg/leak.py→Path:src/pkg/internal.py"'
            "→Path:src/pkg/real.py",
            (("src/pkg/real.py", True),),
        ),
        (
            "URL = (opaque [src/pkg/leak.py→Path:src/pkg/internal.py] more)→Path:src/pkg/real.py",
            (("src/pkg/real.py", True),),
        ),
        ('URL = "opaque \\" still src/pkg/leak.py→Path:src/pkg/real.py', ()),
        ("URL = (opaque [src/pkg/leak.py]→Path:src/pkg/real.py", ()),
        (
            'URL = "opaque src/pkg/leak.py→Path:src/pkg/internal.py\nPath:src/pkg/real.py',
            (("src/pkg/real.py", True),),
        ),
        ('URL =\n"src/pkg/real.py"', (("src/pkg/real.py", True),)),
    ),
)
def test_spaced_url_wrapper_escape_nesting_and_newline_boundaries(
    issue_text: str,
    expected: tuple[tuple[str, bool], ...],
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert tuple((item.path, item.target_eligible) for item in parsed) == expected


@pytest.mark.parametrize(
    "issue_text",
    (
        "URL=https://e.test/?q=x→Path:\nnew.py",
        "URL=https://e.test/?q=x→请在\nnew.py 中修改",
    ),
)
def test_url_tail_action_authorization_never_crosses_a_newline(issue_text: str) -> None:
    assert not any(
        item.path == "new.py" and item.target_eligible
        for item in PlanBuilder._parse_file_references(issue_text)
    )


@pytest.mark.parametrize(
    "issue_text",
    (
        'URL = "https://e.test/?q=x→Path:src/pkg/hidden.py"',
        "URI : ‘mailto:user@e.test?body=x|Add`src/pkg/hidden.py`’",
        "网址 ： 「https://e.test/?q=x—Update:[src/pkg/hidden.py](context)」",
    ),
)
def test_separate_url_value_keeps_internal_semantic_seams_opaque(issue_text: str) -> None:
    assert PlanBuilder._parse_file_references(issue_text) == ()


@pytest.mark.parametrize(
    ("opener", "closer"),
    (
        ("`", "`"),
        ('"', '"'),
        ("'", "'"),
        ("“", "”"),
        ("‘", "’"),
        ("「", "」"),
        ("『", "』"),
        ("(", ")"),
        ("（", "）"),
        ("[", "]"),
        ("【", "】"),
    ),
)
@pytest.mark.parametrize("separator", ("→", "|", "—", "–"))
def test_semantic_boundary_reopens_a_complete_wrapped_reference(
    opener: str,
    closer: str,
    separator: str,
) -> None:
    issue_text = f"URL=a.py。bk/b.py{separator}{opener}src/pkg/real.py{closer}"
    assert PlanBuilder._explicit_file_references(issue_text) == ("src/pkg/real.py",)


@pytest.mark.parametrize(
    "issue_text",
    (
        'https://e.test/?q=x→"src/pkg/real.py"',
        "mailto:user@e.test?body=x|‘src/pkg/real.py’",
        "idea://open?x—[src/pkg/real.py]",
        "//e.test/?q=x–【src/pkg/real.py】",
    ),
)
def test_explicit_uri_hard_semantic_boundary_reopens_a_wrapped_reference(
    issue_text: str,
) -> None:
    assert PlanBuilder._explicit_file_references(issue_text) == ("src/pkg/real.py",)


def test_uri_boundary_keeps_an_attached_root_action_target_eligible() -> None:
    parsed = PlanBuilder._parse_file_references("https://e.test→Add`new.py`")
    assert [(item.path, item.target_eligible) for item in parsed] == [("new.py", True)]


@pytest.mark.parametrize("separator", ("→", "|", "—", "–"))
@pytest.mark.parametrize(
    "right_clause",
    (
        "在[new.py](context)中修改",
        "于[new.py](context)中更新",
        "请在[new.py](context)中测试",
    ),
)
def test_uri_boundary_keeps_a_cjk_location_markdown_target_eligible(
    separator: str,
    right_clause: str,
) -> None:
    parsed = PlanBuilder._parse_file_references("https://e.test" + separator + right_clause)
    assert [(item.path, item.target_eligible) for item in parsed] == [("new.py", True)]


@pytest.mark.parametrize("separator", ("→", "|", "—", "–"))
@pytest.mark.parametrize(
    "right_clause",
    (
        "Add`src/alt/deep.py`",
        "Update:[src/alt/deep.py](https://docs.example/context)",
    ),
)
def test_invalid_compact_clause_does_not_hide_a_valid_right_envelope(
    separator: str,
    right_clause: str,
) -> None:
    parsed = PlanBuilder._parse_file_references("@a.py．diff/b.py" + separator + right_clause)
    assert [(item.path, item.target_eligible) for item in parsed] == [("src/alt/deep.py", True)]


@pytest.mark.parametrize("separator", ("→", "|", "—", "–"))
def test_uri_boundary_reopens_a_markdown_label_with_an_opaque_url_destination(
    separator: str,
) -> None:
    issue_text = (
        "https://e.test/?q=x" + separator + "[src/pkg/real.py](https://docs.example/context)"
    )
    assert PlanBuilder._explicit_file_references(issue_text) == ("src/pkg/real.py",)


@pytest.mark.parametrize("separator", ("→", "|", "—", "–"))
@pytest.mark.parametrize(
    "right_clause",
    (
        "Update:[src/pkg/real.py](context)",
        "Path:[src/pkg/real.py](https://docs.example/context)",
        "修改[src/pkg/real.py](context)",
        "然后修改[src/pkg/real.py](context)",
        "请更新[src/pkg/real.py](https://docs.example/context)",
        "在[src/pkg/real.py](context)中修改",
    ),
)
def test_uri_boundary_reopens_an_action_or_location_markdown_reference(
    separator: str,
    right_clause: str,
) -> None:
    issue_text = "https://e.test/?q=x" + separator + right_clause
    assert PlanBuilder._explicit_file_references(issue_text) == ("src/pkg/real.py",)


@pytest.mark.parametrize(
    "left_clause",
    (
        "Path:src/pkg/real.py",
        "src/pkg/real.py",
        '"src/pkg/real.py"',
        "修改:src/pkg/real.py",
        "修改“src/pkg/real.py”",
        "然后修改“src/pkg/real.py”",
        "Add`src/pkg/real.py`",
        'Update"src/pkg/real.py"',
        "File=(src/pkg/real.py)",
        "在“src/pkg/real.py”中修改",
        "请在`src/pkg/real.py`中测试",
        "[src/pkg/real.py](context)",
        "[src/pkg/real.py](https://docs.example/context)",
        "Update:[src/pkg/real.py](https://docs.example/context)",
        "在[src/pkg/real.py](context)中修改",
    ),
)
@pytest.mark.parametrize("separator", ("→", "|", "—", "–"))
@pytest.mark.parametrize(
    "right_uri",
    (
        "https://e.test",
        "mailto:user@e.test?body=tests/leak.py",
        "idea://open?file=tests/leak.py",
        "//e.test/?file=tests/leak.py",
    ),
)
def test_hard_semantic_boundary_preserves_a_left_reference_before_an_opaque_uri(
    left_clause: str,
    separator: str,
    right_uri: str,
) -> None:
    issue_text = left_clause + separator + right_uri
    assert PlanBuilder._explicit_file_references(issue_text) == ("src/pkg/real.py",)


@pytest.mark.parametrize(
    "issue_text",
    (
        "@foo.py?x/bar.py",
        "@foo.py?src/pkg.py",
        "@foo.py?x",
        "src/foo.py?x/bar.py",
        "修改src/foo.py?x/bar.py",
    ),
)
def test_query_continuation_cannot_truncate_a_same_token_reference(
    issue_text: str,
) -> None:
    assert PlanBuilder._parse_file_references(issue_text) == ()


def test_unicode_sentence_separator_preserves_distinct_valid_paths() -> None:
    assert [
        (item.path, item.target_eligible)
        for item in PlanBuilder._parse_file_references("@foo.py．src/pkg.py")
    ] == [("foo.py", True), ("src/pkg.py", True)]
    assert PlanBuilder._explicit_file_references("@foo.py src/pkg.py→tests/test_pkg.py") == (
        "foo.py",
        "src/pkg.py",
        "tests/test_pkg.py",
    )


@pytest.mark.parametrize("separator", ("?", "!", ":", "：", "？", "！"))
def test_compact_reference_reopens_a_canonical_cjk_action_clause(separator: str) -> None:
    assert [
        (item.path, item.target_eligible, item.ambiguous_cjk_prefix)
        for item in PlanBuilder._parse_file_references(f"@foo.py{separator}然后修改src/pkg.py")
    ] == [
        ("foo.py", True, None),
        ("然后修改src/pkg.py", False, "然后修改"),
    ]


def test_attached_continuation_uses_preindexed_boundaries_without_scanning() -> None:
    class CountingText(str):
        item_reads = 0

        def __getitem__(self, key: object) -> str:
            if isinstance(key, slice):
                start, stop, _ = key.indices(len(self))
                if stop - start > 32:
                    raise AssertionError("continuation helper scanned a wide slice")
            else:
                type(self).item_reads += 1
            return super().__getitem__(key)  # type: ignore[arg-type]

    value = CountingText("错" * 19_000 + "/bar.py")
    assert PlanBuilder._has_attached_path_continuation(
        value,
        0,
        line_end=len(value),
        path_separator_positions=(19_000,),
        continuation_boundary_positions=(),
    )
    assert CountingText.item_reads <= 4


def test_invalid_suffix_envelope_scans_each_dense_punctuation_run_once() -> None:
    issue_text = "@foo.py" + "．｡。" * 6_300 + "ＯＬＤ/bar.py"
    started = perf_counter()

    assert PlanBuilder._parse_file_references(issue_text) == ()
    assert perf_counter() - started < 1.0


def test_cjk_clause_reopen_probe_has_a_bounded_slice() -> None:
    class SliceCountingText(str):
        max_slice_width = 0

        def __getitem__(self, key: object) -> str:
            if isinstance(key, slice):
                start, stop, _ = key.indices(len(self))
                type(self).max_slice_width = max(
                    type(self).max_slice_width,
                    stop - start,
                )
            return super().__getitem__(key)  # type: ignore[arg-type]

    unit = "a.py？然后修改src/pkg.py"
    value = SliceCountingText((unit * 1_200)[:20_000])

    assert not PlanBuilder._has_opaque_same_token_reference_continuation(value)
    assert SliceCountingText.max_slice_width <= 501


@pytest.mark.parametrize(
    ("issue_text", "expected_path", "expected_suffix"),
    (
        ("修改a.py错b.py", "a.py错b.py", ""),
        ("修改foo.py错误a.py", "foo.py错误a.py", ""),
        ("修改foo.py错误bar.txt", "foo.py", "错误bar.txt"),
        ("修改b.py错误a.py 后续/.git/", "b.py错误a.py", " 后续/.git/"),
    ),
)
def test_attached_cjk_action_and_compact_replay_choose_the_same_endpoint(
    issue_text: str,
    expected_path: str,
    expected_suffix: str,
) -> None:
    attached = PlanBuilder._parse_file_references(issue_text)

    assert [(item.path, item.target_eligible) for item in attached] == [
        ("修改" + expected_path, False)
    ]
    suffix_span = attached[0].ambiguous_cjk_suffix_span
    assert suffix_span is not None
    assert issue_text[slice(*suffix_span)] == expected_suffix
    compact = PlanBuilder._parse_file_references("@" + expected_path + expected_suffix)
    assert [(item.path, item.target_eligible) for item in compact] == [(expected_path, True)]


def test_semantic_separator_keeps_attached_and_compact_endpoint_selection_identical() -> None:
    issue_text = "路径b.py然后然后.old→b.py"
    attached = PlanBuilder._parse_file_references(issue_text)
    ambiguous = next(item for item in attached if item.ambiguous_cjk_prefix is not None)
    suffix_span = ambiguous.ambiguous_cjk_suffix_span

    assert ambiguous.path == "路径b.py"
    assert suffix_span is not None
    suffix = issue_text[slice(*suffix_span)]
    assert suffix == "然后然后.old→b.py"
    compact = PlanBuilder._parse_file_references("@b.py" + suffix)
    assert ("b.py", True) in [(item.path, item.target_eligible) for item in compact]


def test_compact_label_respects_semantic_boundaries_between_paths() -> None:
    parsed = PlanBuilder._parse_file_references("@foo.py→src/a.py")

    assert [(item.path, item.target_eligible) for item in parsed] == [
        ("foo.py", True),
        ("src/a.py", True),
    ]


def test_compact_label_precedes_url_shape_but_not_explicit_url_context() -> None:
    compact = "@x.toml里新增｡|//处理"

    assert [
        (item.path, item.target_eligible) for item in PlanBuilder._parse_file_references(compact)
    ] == [("x.toml", True)]
    assert PlanBuilder._parse_file_references("URL = " + compact) == ()


def test_location_prefix_stops_at_a_normal_boundary_before_a_later_path() -> None:
    issue_text = "同时请将tests/a.py]然后src/随后检查README.md里新增处理"
    parsed = PlanBuilder._parse_file_references(issue_text)
    ambiguous = next(item for item in parsed if item.ambiguous_cjk_prefix is not None)
    suffix_span = ambiguous.ambiguous_cjk_suffix_span

    assert ambiguous.path == "同时请将tests/a.py"
    assert suffix_span is not None
    suffix = issue_text[slice(*suffix_span)]
    compact = PlanBuilder._parse_file_references("@tests/a.py" + suffix)
    assert ("tests/a.py", True) in [(item.path, item.target_eligible) for item in compact]


@pytest.mark.parametrize(
    ("issue_text", "prefix", "operand", "suffix"),
    (
        ("请将错错误x.toml然后副本\t/b.py", "请将", "错错误x.toml", "然后副本\t/b.py"),
        ('再将.oldb.py"README.md \\', "再将", ".oldb.py", '"README.md \\'),
        ("并在x.toml––README.md", "并在", "x.toml", "––README.md"),
        ("且请将b.py然后a.py处理", "且请将", "b.py然后a.py", "处理"),
    ),
)
def test_compact_guidance_replays_fallback_boundary_cases_exactly(
    issue_text: str,
    prefix: str,
    operand: str,
    suffix: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)
    ambiguous = next(item for item in parsed if item.ambiguous_cjk_prefix is not None)
    suffix_span = ambiguous.ambiguous_cjk_suffix_span

    assert ambiguous.path == prefix + operand
    assert suffix_span is not None
    assert issue_text[slice(*suffix_span)] == suffix
    replay = PlanBuilder._parse_file_references("@" + operand + suffix)
    assert (operand, True) in [(item.path, item.target_eligible) for item in replay]


@pytest.mark.parametrize(
    "issue_text",
    (
        "创建new.py",
        "请修改new.py",
        "并创建test_new.py",
        "更新_v2.py",
        "请修改src/arithmetic/calculator.py",
        "更新tests/test_calculator.py",
        "路径src/pkg.py",
        "请修改功能/模块.py",
        "创建功能.py",
        "并请修改src/pkg.py",
        "请检查new.py",
    ),
)
def test_ambiguous_attached_cjk_root_target_fails_before_fallback(
    issue_text: str,
) -> None:
    with pytest.raises(AmbiguousIssuePathError, match=re.escape(issue_text)) as raised:
        PlanBuilder().build(
            _semantic_snapshot(),
            IssueInput(number=60, title=issue_text, body="Preserve behavior."),
        )

    assert "Add a separator" in str(raised.value)
    assert "路径:" in str(raised.value)


@pytest.mark.parametrize(
    ("issue_text", "expected_path"),
    (
        ("创建 new.py", "new.py"),
        ("请修改 new.py", "new.py"),
        ("并创建 test_new.py", "test_new.py"),
        ("创建:new.py", "new.py"),
        ("请修改:src/pkg.py", "src/pkg.py"),
        ("并创建:src/pkg.py", "src/pkg.py"),
        ("路径 src/pkg.py", "src/pkg.py"),
        ("请文件:src/pkg.py", "src/pkg.py"),
        ("请检查 new.py", "new.py"),
        ("请修改 功能/模块.py", "功能/模块.py"),
        ("创建 功能.py", "功能.py"),
        ("更新 更新_v2.md", "更新_v2.md"),
        ("更新 `更新_v2.md`", "更新_v2.md"),
        ("创建 `创建new.py`", "创建new.py"),
    ),
)
def test_delimited_cjk_root_actions_preserve_exact_operand(
    issue_text: str,
    expected_path: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert [(item.path, item.target_eligible) for item in parsed] == [(expected_path, True)]


def test_every_canonical_cjk_action_prefix_has_one_consistent_identity_contract() -> None:
    for prefix in _CJK_PATH_ACTION_PREFIXES:
        for operand in ("new.py", "功能/模块.py"):
            attached = PlanBuilder._parse_file_references(prefix + operand)
            assert [
                (item.path, item.target_eligible, item.ambiguous_cjk_prefix) for item in attached
            ] == [(prefix + operand, False, prefix)]

            separated_example = prefix + " " + operand
            with pytest.raises(AmbiguousIssuePathError) as raised:
                PlanBuilder().build(
                    _semantic_snapshot(),
                    IssueInput(
                        number=65,
                        title=prefix + operand,
                        body="Preserve behavior.",
                    ),
                )
            assert repr(separated_example) in str(raised.value)
            literal_example = "路径:" + prefix + operand
            assert repr(literal_example) in str(raised.value)
            literal = PlanBuilder._parse_file_references(literal_example)
            assert [(item.path, item.target_eligible) for item in literal] == [
                (prefix + operand, True)
            ]

            for delimiter in (" ", ":", "："):
                explicit = PlanBuilder._parse_file_references(prefix + delimiter + operand)
                assert [(item.path, item.target_eligible) for item in explicit] == [(operand, True)]


def test_every_canonical_cjk_location_prefix_has_one_consistent_identity_contract() -> None:
    for prefix in _CJK_PATH_LOCATION_PREFIXES:
        for operand in ("new.py", "功能/模块.py"):
            issue_text = prefix + operand + "中修改边界条件"
            attached = PlanBuilder._parse_file_references(issue_text)
            assert [
                (
                    item.path,
                    item.target_eligible,
                    item.ambiguous_cjk_prefix,
                )
                for item in attached
            ] == [(prefix + operand, False, prefix)]
            suffix_span = attached[0].ambiguous_cjk_suffix_span
            assert suffix_span is not None
            assert issue_text[slice(*suffix_span)] == "中修改边界条件"

            separated_example = prefix + " " + operand + " 中修改边界条件"
            with pytest.raises(AmbiguousIssuePathError) as raised:
                PlanBuilder().build(
                    _semantic_snapshot(),
                    IssueInput(
                        number=66,
                        title=prefix + operand + "中修改边界条件",
                        body="Preserve behavior.",
                    ),
                )
            assert repr(separated_example) in str(raised.value)
            literal_example = "路径:" + prefix + operand
            assert repr(literal_example) in str(raised.value)
            literal = PlanBuilder._parse_file_references(literal_example)
            assert [(item.path, item.target_eligible) for item in literal] == [
                (prefix + operand, True)
            ]

            explicit = PlanBuilder._parse_file_references(separated_example)
            assert [(item.path, item.target_eligible) for item in explicit] == [(operand, True)]


def test_every_canonical_location_prefix_survives_url_shaped_suffix_punctuation() -> None:
    operand_and_suffixes = (
        ("new.py", "中修改a.py?随后更新"),
        ("new.py", "中修改a.py!随后更新"),
        (
            ".github/workflows/ci.yml",
            "内检查README.md；然后更新v3.2",
        ),
    )
    for prefix in _CJK_PATH_LOCATION_PREFIXES:
        for operand, suffix in operand_and_suffixes:
            issue_text = prefix + operand + suffix
            parsed = PlanBuilder._parse_file_references(issue_text)
            assert [
                (
                    item.path,
                    item.target_eligible,
                    item.ambiguous_cjk_prefix,
                )
                for item in parsed
            ] == [(prefix + operand, False, prefix)]
            suffix_span = parsed[0].ambiguous_cjk_suffix_span
            assert suffix_span is not None
            assert issue_text[slice(*suffix_span)] == suffix
            with pytest.raises(AmbiguousIssuePathError):
                PlanBuilder().build(
                    _semantic_snapshot(),
                    IssueInput(
                        number=72,
                        title=issue_text,
                        body="Preserve behavior.",
                    ),
                )


def test_root_path_action_context_does_not_cross_lines_or_upgrade_mentions() -> None:
    mention = PlanBuilder._parse_file_references("Mention new.py")
    separated = PlanBuilder._parse_file_references("Create\nnew.py")
    assert [(item.path, item.target_eligible) for item in mention] == [("new.py", False)]
    assert [(item.path, item.target_eligible) for item in separated] == [("new.py", False)]


@pytest.mark.parametrize(
    "issue_text",
    (
        "profile new.py",
        "contest new.py",
        "recreate new.py",
        "do not update new.py",
        "never modify new.py",
        "do not create new.py",
        "Create\rnew.py",
        "Create\r\nnew.py",
        "Create\vnew.py",
        "Create\fnew.py",
        "Create\x85new.py",
        "Create\u2028new.py",
        "Create\u2029new.py",
        "do not update" + " " * 250 + "new.py",
        "preupdate" + " " * 250 + "new.py",
        "update" + " " * 250 + "new.py",
    ),
)
def test_non_action_suffixes_negation_and_noncanonical_spacing_do_not_upgrade_root_paths(
    issue_text: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert not any(item.path == "new.py" and item.target_eligible for item in parsed)


@pytest.mark.parametrize("issue_text", ("fıle:new.py", "teſt:new.py", "FİLE:new.py"))
def test_unicode_casefold_cannot_forge_ascii_root_path_labels(issue_text: str) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert not any(item.path == "new.py" and item.target_eligible for item in parsed)


@pytest.mark.parametrize(
    "issue_text",
    (
        "FILE:new.py",
        "TeSt:new.py",
        "Please update new.py",
        "Then create new.py",
        "请修改 new.py",
        "并创建 new.py",
        "update" + " " * 32 + "new.py",
        "x" * 300 + ", update new.py",
    ),
)
def test_exact_affirmative_root_action_grammar_preserves_targets(issue_text: str) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert [(item.path, item.target_eligible) for item in parsed] == [("new.py", True)]


@pytest.mark.parametrize(
    "issue_text",
    (
        "do not Update:new.py",
        "never Create:new.py",
        'Do not create "new.py".',
        "Never update `new.py`.",
    ),
)
def test_root_labels_and_wrappers_cannot_bypass_negative_action_context(
    issue_text: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert not any(item.path == "new.py" and item.target_eligible for item in parsed)


@pytest.mark.parametrize(
    "issue_text",
    (
        "Update:new.py",
        'Please update "new.py".',
        "Then create `new.py`.",
        "Create [new.py](context)",
    ),
)
def test_root_labels_and_wrappers_share_the_affirmative_action_seam(
    issue_text: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert [(item.path, item.target_eligible) for item in parsed] == [("new.py", True)]


@pytest.mark.parametrize(
    "issue_text",
    (
        "不在 new.py 中修改",
        "不要在 new.py 中修改",
        "存在 new.py 中修改",
        "旨在 new.py 中修改",
    ),
)
def test_cjk_location_suffixes_and_negation_do_not_authorize_root_targets(
    issue_text: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert not any(item.path == "new.py" and item.target_eligible for item in parsed)


@pytest.mark.parametrize(
    "issue_text",
    (
        "在 new.py 中修改",
        "请在 new.py 中修改",
        "并在 new.py 中修改",
        "请于 new.py 中修改",
        "请将 new.py 中修改",
        "并请在 new.py 中修改",
    ),
)
def test_exact_cjk_location_context_authorizes_root_targets(issue_text: str) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert [(item.path, item.target_eligible) for item in parsed] == [("new.py", True)]


@pytest.mark.parametrize(
    "issue_text",
    (
        '在 "new.py" 中修改',
        "在 `new.py` 中修改",
        "在（new.py）中修改",
        '请在 "new.py" 里新增',
        "并在 `new.py` 内修改",
        "在 [new.py](context) 中修改",
    ),
)
def test_cjk_location_context_authorizes_complete_wrapped_root_targets(
    issue_text: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert [(item.path, item.target_eligible) for item in parsed] == [("new.py", True)]


@pytest.mark.parametrize(
    "issue_text",
    (
        '不要在 "new.py" 中修改',
        "不在 `new.py` 中修改",
        "存在（new.py）中修改",
        "旨在 [new.py](context) 中修改",
    ),
)
def test_wrappers_do_not_bypass_negative_cjk_location_context(issue_text: str) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert not any(item.path == "new.py" and item.target_eligible for item in parsed)


@pytest.mark.parametrize(
    "issue_text",
    (
        'Mention "new.py" 中修改',
        '在 "new.py" 中提及',
        '在 "new.py"，中修改',
        "在 `new.py`。中修改",
        "在（new.py）；中修改",
        "在 [new.py](context)，中修改",
        '在 "new.py"\n中修改',
    ),
)
def test_wrapped_cjk_location_requires_one_exact_affirmative_clause(
    issue_text: str,
) -> None:
    parsed = PlanBuilder._parse_file_references(issue_text)

    assert [(item.path, item.target_eligible) for item in parsed] == [("new.py", False)]


@pytest.mark.parametrize(
    "issue_text",
    (
        "在 ![new.py](context) 中修改",
        "在 https://example.com/?redirect=[new.py](context) 中修改",
        'URL=https://example.com/?file="new.py" 中修改',
        "在 [guide](https://example.com/?file=new.py) 中修改",
    ),
)
def test_cjk_location_context_cannot_escape_opaque_media_or_url_syntax(
    issue_text: str,
) -> None:
    assert PlanBuilder._parse_file_references(issue_text) == ()


def test_later_wrapped_action_upgrades_one_earlier_root_mention() -> None:
    parsed = PlanBuilder._parse_file_references('Mention new.py. 在 "new.py" 中修改')

    assert [(item.path, item.target_eligible) for item in parsed] == [("new.py", True)]


@pytest.mark.parametrize(
    "filename",
    ("更新日志.md", "测试报告.md", "实现方案.py", "创建者.py", "新增功能.py"),
)
def test_cjk_action_prefixes_do_not_rewrite_real_root_filenames(filename: str) -> None:
    parsed = PlanBuilder._parse_file_references(filename)

    assert [(item.path, item.target_eligible) for item in parsed] == [(filename, False)]


def test_dense_root_path_mentions_are_parsed_in_bounded_time() -> None:
    issue_text = ("mention new.py " * 1_350)[:20_000]

    started = perf_counter()
    parsed = PlanBuilder._parse_file_references(issue_text)
    elapsed = perf_counter() - started

    assert [(item.path, item.target_eligible) for item in parsed] == [("new.py", False)]
    assert elapsed < 1.0


def _snapshot_without_pytest_declaration(readme: str, project_config: str) -> RepositorySnapshot:
    snapshot = _semantic_snapshot()
    documents = tuple(
        _document("README.md", EvidenceCategory.README, readme)
        if document.path == "README.md"
        else _document(
            "pyproject.toml",
            EvidenceCategory.PROJECT_CONFIG,
            project_config,
        )
        if document.path == "pyproject.toml"
        else document
        for document in snapshot.documents
        if document.path != "pytest.ini"
    )
    return replace(
        snapshot,
        documents=documents,
        all_paths=tuple(path for path in snapshot.all_paths if path != "pytest.ini"),
    )


def test_missing_test_runner_needs_human_input_without_inventing_an_intent() -> None:
    snapshot = _snapshot_without_pytest_declaration(
        "This repository has tests but no declared runner.\n",
        '[project]\nname = "arithmetic"\n',
    )
    plan = PlanBuilder().build(
        snapshot,
        IssueInput(number=46, title="Improve this", body="Add regression coverage."),
    )
    assert plan.verification_intents == []
    assert plan.verification_readiness == "needs_human_input"
    assert any("needs human input" in risk for risk in plan.risks)
    verification_step = next(step for step in plan.steps if step.kind is StepKind.VERIFICATION)
    assert "does not invent or execute a command" in verification_step.description


def test_readme_pytest_command_is_evidence_for_ready_verification() -> None:
    snapshot = _snapshot_without_pytest_declaration(
        "Run the suite with `python -m pytest`.\n",
        '[project]\nname = "arithmetic"\n',
    )
    plan = PlanBuilder().build(
        snapshot,
        IssueInput(number=47, title="Improve this", body="Add regression coverage."),
    )
    assert [(intent.tool, intent.arguments) for intent in plan.verification_intents] == [
        ("pytest", [])
    ]
    assert plan.verification_readiness == "ready"
    evidence_by_id = {item.id: item for item in plan.evidence}
    assert evidence_by_id[plan.verification_intents[0].evidence_ids[0]].path == "README.md"


@pytest.mark.parametrize(
    "affirmative_command",
    (
        "pytest",
        "python -m pytest",
        "python3.12 -m pytest",
        "uv run pytest",
        "poetry run pytest",
        "$ python -m pytest",
        "`python -m pytest`",
        "`$ pytest`",
        "`$ python -m pytest`",
        "$ `pytest`",
        "- `python -m pytest`",
        "Run: `$ pytest`.",
        "Run: $ pytest",
        "Run:\npython -m pytest",
        "Run:\n\npython -m pytest",
        "Run:\n`$ pytest`",
        "\npython -m pytest",
        "```bash\npython -m pytest\n```",
        "Run:\n```bash\npython -m pytest\n```",
        "~~~bash\npython -m pytest\n~~~",
        " ```bash\n pytest\n ```",
        "   ```bash\n   pytest\n   ```",
        "Run pytest.",
        "运行 pytest。",
    ),
)
def test_standalone_and_cross_line_affirmative_pytest_commands_remain_ready(
    affirmative_command: str,
) -> None:
    snapshot = _snapshot_without_pytest_declaration(
        affirmative_command,
        '[project]\nname = "arithmetic"\n',
    )
    plan = PlanBuilder().build(
        snapshot,
        IssueInput(number=48, title="Improve this", body="Add regression coverage."),
    )
    assert [(intent.tool, intent.arguments) for intent in plan.verification_intents] == [
        ("pytest", [])
    ]
    assert plan.verification_readiness == "ready"


@pytest.mark.parametrize(
    "negated_command",
    (
        "Do not run python -m pytest here.\n",
        "Don’t run python -m pytest",
        "If you cannot run python -m pytest, contact the maintainer.\n",
        "You shouldn't run python -m pytest in this environment.\n",
        "无需运行 python -m pytest。\n",
        "不运行 python -m pytest",
        "别运行 python -m pytest",
        "不得运行 python -m pytest",
        "不可运行 python -m pytest",
        "避免运行 python -m pytest",
        "pytest is not supported",
        "python -m pytest is unavailable",
        "Avoid python -m pytest",
        "Do not run:\npython -m pytest",
        "不得执行 python -m pytest",
        "不推荐运行 python -m pytest",
        "pytest is disabled",
        "python -m pytest does not work",
        "pytest is not recommended",
        "pytest 不可用",
        "pytest is disabled:\npytest",
        "python -m pytest 不可用：\npython -m pytest",
        "pytest is disabled\npytest",
        "This is not a command:\npytest",
        "Broken command:\npytest",
        "Do not run:\n- `python -m pytest`",
        "Do not run:\n```bash\npython -m pytest\n```",
        "Do not run:\n\npython -m pytest",
        "Examples:\n`python -m pytest`",
        "Run `pytest",
        "Run pytest`",
        "Run ``pytest`",
        "Run `pytest``",
        "Run ```pytest```",
        "Should we run `pytest`?",
        "How do I run `pytest`?",
        "The docs mention command `pytest`.",
        "PYTEST",
        "Run PYTEST",
        "Run:\n```bash\npython -m pytest",
        "```bash\npytest",
        "```bash\nRun pytest",
        "```text\nRun pytest",
        "```\npytest",
        "Run:\n```bash\npython -m pytest\n```junk",
        "```toml\npytest\n```",
        "Run:\n```ini\npython -m pytest\n```",
        "```text\nRun pytest\n```",
        "```bash\n- pytest\n```",
        "```bash\n> pytest\n```",
        "```bash\n1. pytest\n```",
        "    ```bash\n    pytest\n    ```",
        "\t```bash\n\tpytest\n\t```",
        "```text\nprose\n    ```\nRun pytest\n```",
        "> ```text\n> Run:\n> pytest\n> ```",
        "- ```text\n  Run:\n  pytest\n  ```",
        "```ſh\npytest\n```",
        "Run pytest?",
        "Run: pytest?",
        "Execute `pytest`?",
        "运行 pytest？",
        "测试：pytest？",
        "١. pytest",
        "１. pytest",
        "Run:\n١. pytest",
        "Run:\n１) pytest",
        "python3.١ -m pytest",
        "Run python3.１ -m pytest",
    ),
)
def test_negated_pytest_commands_do_not_become_verification_intents(
    negated_command: str,
) -> None:
    snapshot = _snapshot_without_pytest_declaration(
        negated_command,
        '[project]\nname = "arithmetic"\n',
    )
    plan = PlanBuilder().build(
        snapshot,
        IssueInput(number=48, title="Improve this", body="Add regression coverage."),
    )
    assert plan.verification_intents == []
    assert plan.verification_readiness == "needs_human_input"


@pytest.mark.parametrize(
    "non_exact_command",
    (
        "pytest -q",
        "python -m pytest -q",
        "pytest && ruff",
        "pytest;",
        "Run pytest -q.",
        "Run pytest && ruff.",
        "Run pytest; ruff.",
    ),
)
def test_unmodeled_arguments_and_chained_commands_do_not_become_declarations(
    non_exact_command: str,
) -> None:
    snapshot = _snapshot_without_pytest_declaration(
        non_exact_command,
        '[project]\nname = "arithmetic"\n',
    )
    plan = PlanBuilder().build(
        snapshot,
        IssueInput(number=48, title="Improve this", body="Add regression coverage."),
    )
    assert plan.verification_intents == []
    assert plan.verification_readiness == "needs_human_input"


@pytest.mark.parametrize(
    "separator", ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
)
def test_non_markdown_line_separators_cannot_forge_fenced_commands(separator: str) -> None:
    document = _document(
        "README.md",
        EvidenceCategory.README,
        f"```text{separator}```{separator}Run pytest",
    )

    assert PlanBuilder._verification_declarations(document) == ()


@pytest.mark.parametrize(
    "readme",
    (
        "\u2028pytest",
        "pytest\u2028",
        "\u00a0pytest",
        "pytest\u00a0",
        "Run:\n\vpytest",
        "Run:\npytest\f",
        "```bash\n\u00a0pytest\n```",
        "```bash\npytest\u2029\n```",
        "uſe:\npytest",
    ),
)
def test_non_shell_whitespace_and_unicode_casefold_cannot_forge_commands(
    readme: str,
) -> None:
    document = _document("README.md", EvidenceCategory.README, readme)

    assert PlanBuilder._verification_declarations(document) == ()


def test_cross_line_verification_declaration_covers_its_authorizing_cue() -> None:
    document = _document(
        "README.md",
        EvidenceCategory.README,
        "Run:\npython -m pytest\n",
    )

    declarations = PlanBuilder._verification_declarations(document)

    assert [
        (declaration.tool, declaration.kind.value, declaration.line_start, declaration.line_end)
        for declaration in declarations
    ] == [("pytest", "command", 1, 2)]


def test_dense_non_command_line_is_rejected_in_bounded_time() -> None:
    documents = (
        _document(
            "README.md",
            EvidenceCategory.README,
            "Run " + "pytest " * 8_000,
        ),
        _document(
            "README.md",
            EvidenceCategory.README,
            "Run " + " " * 60_000 + "not-a-command",
        ),
        _document(
            "README.md",
            EvidenceCategory.README,
            "Run pytest" + " " * 60_000 + "not-punctuation",
        ),
    )

    started = perf_counter()
    assert all(PlanBuilder._verification_declarations(document) == () for document in documents)
    assert perf_counter() - started < 1.0


def test_dense_fenced_commands_reuse_one_long_positive_context_in_bounded_time() -> None:
    document = _document(
        "README.md",
        EvidenceCategory.README,
        "Run" + " " * 20_000 + ":\n```bash\n" + "pytest\n" * 5_000 + "```\n",
    )

    started = perf_counter()
    declarations = PlanBuilder._verification_declarations(document)
    assert perf_counter() - started < 1.0
    assert [
        (declaration.tool, declaration.kind.value, declaration.line_start, declaration.line_end)
        for declaration in declarations
    ] == [("pytest", "command", 1, 3)]


def test_dense_toml_string_headers_fail_closed_in_bounded_time() -> None:
    document = _document(
        "pyproject.toml",
        EvidenceCategory.PROJECT_CONFIG,
        '[project]\nname = "x"\ndescription = """\n'
        + "[tool.pytest.ini_options]\n" * 2_000
        + '"""\n',
    )

    started = perf_counter()
    assert PlanBuilder._verification_declarations(document) == ()
    assert perf_counter() - started < 1.0


def test_pathologically_nested_toml_fails_closed_without_escaping_the_parser() -> None:
    content = "value = " + "[" * 700 + "0" + "]" * 700 + "\n"
    document = _document(
        "pyproject.toml",
        EvidenceCategory.PROJECT_CONFIG,
        content,
    )

    assert PlanBuilder._verification_declarations(document) == ()


def test_oversized_toml_integer_fails_closed_without_escaping_the_parser() -> None:
    content = "value = " + "1" * 60_000 + "\n"
    document = _document(
        "pyproject.toml",
        EvidenceCategory.PROJECT_CONFIG,
        content,
    )

    assert PlanBuilder._verification_declarations(document) == ()


def test_ruff_configuration_does_not_invent_a_command_or_test_readiness() -> None:
    snapshot = _snapshot_without_pytest_declaration(
        "This repository has local quality configuration.\n",
        '[project]\nname = "arithmetic"\n\n[tool.ruff]\nline-length = 88\n',
    )
    plan = PlanBuilder().build(
        snapshot,
        IssueInput(number=49, title="Improve this", body="Add regression coverage."),
    )
    assert plan.verification_intents == []
    assert plan.verification_readiness == "needs_human_input"


@pytest.mark.parametrize(
    "invalid_header",
    (
        "[tool.pytest_fake]",
        "[tool.pytest.ini_options_typo]",
        "[pytest]junk",
    ),
)
def test_near_miss_pytest_configuration_headers_do_not_authorize_readiness(
    invalid_header: str,
) -> None:
    snapshot = _snapshot_without_pytest_declaration(
        "This repository has no declared test runner.\n",
        f'[project]\nname = "arithmetic"\n\n{invalid_header}\n',
    )
    plan = PlanBuilder().build(
        snapshot,
        IssueInput(number=50, title="Improve this", body="Add regression coverage."),
    )
    assert plan.verification_intents == []
    assert plan.verification_readiness == "needs_human_input"


def test_exact_pytest_toml_header_authorizes_configuration_readiness() -> None:
    snapshot = _snapshot_without_pytest_declaration(
        "This repository uses its project configuration.\n",
        '[project]\nname = "arithmetic"\n\n[tool.pytest.ini_options]\n',
    )
    plan = PlanBuilder().build(
        snapshot,
        IssueInput(number=51, title="Improve this", body="Add regression coverage."),
    )
    assert [(intent.tool, intent.arguments) for intent in plan.verification_intents] == [
        ("pytest", [])
    ]
    assert plan.verification_readiness == "ready"


@pytest.mark.parametrize(
    "readme_content",
    (
        "Do not use this configuration:\n[pytest]\n",
        "Examples only:\n```ini\n[pytest]\n```\n",
        "Deprecated:\n[tool.pytest.ini_options]\n",
    ),
)
def test_readme_configuration_examples_never_authorize_readiness(
    readme_content: str,
) -> None:
    snapshot = _snapshot_without_pytest_declaration(
        readme_content,
        '[project]\nname = "arithmetic"\n',
    )

    plan = PlanBuilder().build(
        snapshot,
        IssueInput(number=51, title="Improve this", body="Add regression coverage."),
    )

    assert plan.verification_intents == []
    assert plan.verification_readiness == "needs_human_input"


@pytest.mark.parametrize(
    ("path", "category", "content"),
    (
        ("pyproject.toml", EvidenceCategory.PROJECT_CONFIG, "[pytest]\n"),
        ("pytest.ini", EvidenceCategory.TEST_CONFIG, "[tool.pytest.ini_options]\n"),
        ("tox.ini", EvidenceCategory.TEST_CONFIG, "[tool.pytest.ini_options]\n"),
        ("setup.cfg", EvidenceCategory.PROJECT_CONFIG, "[pytest]\n"),
        ("requirements-dev.txt", EvidenceCategory.PROJECT_CONFIG, "[pytest]\n"),
        (".github/workflows/ci.yml", EvidenceCategory.TEST_CONFIG, "[pytest]\n"),
        (
            "pyproject.toml",
            EvidenceCategory.PROJECT_CONFIG,
            '[project]\nname = "x"\ndescription = """\n[tool.pytest.ini_options]\n"""\n',
        ),
        (
            "setup.cfg",
            EvidenceCategory.PROJECT_CONFIG,
            "[metadata]\ndescription = line one\n    [tool:pytest]\n",
        ),
        (
            "tox.ini",
            EvidenceCategory.TEST_CONFIG,
            "[tox]\nenvlist = py312\n[testenv]\ndescription =\n    [pytest]\n",
        ),
    ),
)
def test_configuration_headers_in_the_wrong_file_syntax_are_not_declarations(
    path: str,
    category: EvidenceCategory,
    content: str,
) -> None:
    document = _document(path, category, content)

    assert PlanBuilder._verification_declarations(document) == ()


@pytest.mark.parametrize(
    ("path", "category"),
    (
        ("pyproject.toml", EvidenceCategory.PROJECT_CONFIG),
        ("requirements-dev.txt", EvidenceCategory.PROJECT_CONFIG),
        ("pytest.ini", EvidenceCategory.TEST_CONFIG),
        ("tox.ini", EvidenceCategory.TEST_CONFIG),
        ("conftest.py", EvidenceCategory.TEST_CONFIG),
        ("noxfile.py", EvidenceCategory.TEST_CONFIG),
    ),
)
def test_bare_dependency_or_code_tokens_are_not_command_declarations(
    path: str,
    category: EvidenceCategory,
) -> None:
    document = _document(path, category, "pytest\n")

    assert PlanBuilder._verification_declarations(document) == ()


@pytest.mark.parametrize(
    "content",
    (
        "run: pytest\n",
        "pytest\n",
        "testing: pytest\n",
        "RUN: pytest\n",
        "run: `pytest`\n",
        "run: $ pytest\n",
        "run: pytest.\n",
        "env:\n  run: pytest\n",
        "env:\n  NOTE: |\n    run: pytest\n",
    ),
)
def test_workflow_text_never_claims_a_command_without_yaml_ast_evidence(content: str) -> None:
    document = _document(
        ".github/workflows/ci.yml",
        EvidenceCategory.TEST_CONFIG,
        content,
    )

    assert PlanBuilder._verification_declarations(document) == ()


@pytest.mark.parametrize(
    ("path", "category", "content"),
    (
        ("pyproject.toml", EvidenceCategory.PROJECT_CONFIG, "[tool.pytest.ini_options]\n"),
        ("pytest.ini", EvidenceCategory.TEST_CONFIG, "[pytest]\n"),
        ("tox.ini", EvidenceCategory.TEST_CONFIG, "[pytest]\n"),
        ("setup.cfg", EvidenceCategory.PROJECT_CONFIG, "[tool:pytest]\n"),
    ),
)
def test_pytest_configuration_headers_require_their_canonical_root_file(
    path: str,
    category: EvidenceCategory,
    content: str,
) -> None:
    document = _document(path, category, content)

    assert [
        (declaration.tool, declaration.kind.value)
        for declaration in PlanBuilder._verification_declarations(document)
    ] == [("pytest", "configuration")]


@pytest.mark.parametrize("repeat_count", (4, 8))
def test_repeated_verification_commands_keep_one_declaration_per_tool(
    repeat_count: int,
) -> None:
    snapshot = _snapshot_without_pytest_declaration(
        "\n".join("Run pytest" for _ in range(repeat_count)),
        '[project]\nname = "arithmetic"\n',
    )
    plan = PlanBuilder().build(
        snapshot,
        IssueInput(number=52, title="Improve this", body="Add regression coverage."),
    )
    readme_evidence = next(item for item in plan.evidence if item.path == "README.md")
    assert [
        (declaration.tool, declaration.kind.value, declaration.line_start)
        for declaration in readme_evidence.declared_tools
    ] == [("pytest", "command", 1)]
    assert [(intent.tool, intent.arguments) for intent in plan.verification_intents] == [
        ("pytest", [])
    ]


def test_mixed_declarations_keep_command_priority_and_one_entry_per_tool() -> None:
    snapshot = _snapshot_without_pytest_declaration(
        "\n".join(
            (
                "[tool.pytest.ini_options]",
                "Run pytest",
                "Run ruff",
                "Run mypy",
            )
        ),
        '[project]\nname = "arithmetic"\n',
    )
    plan = PlanBuilder().build(
        snapshot,
        IssueInput(number=53, title="Improve this", body="Add regression coverage."),
    )
    readme_evidence = next(item for item in plan.evidence if item.path == "README.md")
    assert [
        (declaration.tool, declaration.kind.value, declaration.line_start)
        for declaration in readme_evidence.declared_tools
    ] == [
        ("pytest", "command", 2),
        ("ruff", "command", 3),
        ("mypy", "command", 4),
    ]
    assert [(intent.tool, intent.arguments) for intent in plan.verification_intents] == [
        ("pytest", []),
        ("ruff", []),
        ("mypy", []),
    ]

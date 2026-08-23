from __future__ import annotations

import hashlib
import os

import pytest

from repopilot.config import Settings
from repopilot.inspection import InspectedDocument, InspectionLimits, RepositorySnapshot
from repopilot.models import (
    MAX_PLAN_EVIDENCE_ITEMS,
    EvidenceCategory,
    ImplementationPlan,
    InspectedRepository,
    IssueInput,
)
from repopilot.planning import PlanBuilder


def _clear_repopilot_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable_name in tuple(os.environ):
        if variable_name.startswith("REPOPILOT_"):
            monkeypatch.delenv(variable_name)


def _source_document(index: int) -> InspectedDocument:
    content = f"def target_{index}() -> int:\n    return {index}\n"
    payload = content.encode("utf-8")
    return InspectedDocument(
        path=f"src/package/module_{index:02d}.py",
        category=EvidenceCategory.SOURCE,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        content=content,
    )


def test_environment_accepts_plan_evidence_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_repopilot_environment(monkeypatch)
    monkeypatch.setenv("REPOPILOT_MAX_SELECTED_FILES", str(MAX_PLAN_EVIDENCE_ITEMS))

    settings = Settings.from_environment()

    assert settings.inspection_limits.max_selected_files == MAX_PLAN_EVIDENCE_ITEMS


def test_environment_rejects_selection_above_plan_evidence_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_repopilot_environment(monkeypatch)
    unsupported_value = MAX_PLAN_EVIDENCE_ITEMS + 1
    monkeypatch.setenv("REPOPILOT_MAX_SELECTED_FILES", str(unsupported_value))

    with pytest.raises(
        ValueError,
        match=(
            rf"REPOPILOT_MAX_SELECTED_FILES must be between 1 and "
            rf"{MAX_PLAN_EVIDENCE_ITEMS}"
        ),
    ):
        Settings.from_environment()


def test_programmatic_limits_reject_selection_above_plan_evidence_boundary() -> None:
    with pytest.raises(
        ValueError,
        match=rf"max_selected_files cannot exceed {MAX_PLAN_EVIDENCE_ITEMS}",
    ):
        InspectionLimits(max_selected_files=MAX_PLAN_EVIDENCE_ITEMS + 1)


def test_plan_builder_accepts_the_full_supported_evidence_boundary() -> None:
    documents = tuple(_source_document(index) for index in range(MAX_PLAN_EVIDENCE_ITEMS))
    limits = InspectionLimits(max_selected_files=MAX_PLAN_EVIDENCE_ITEMS)
    snapshot = RepositorySnapshot(
        repository=InspectedRepository(
            url="https://github.com/acme/boundary",
            owner="acme",
            name="boundary",
            ref="main",
            tree_sha="a" * 40,
        ),
        all_paths=tuple(document.path for document in documents),
        documents=documents,
        selection_truncated=False,
        limits=limits,
    )

    plan = PlanBuilder().build(
        snapshot,
        IssueInput(
            number=64,
            title="Update target_0 behavior",
            body="Change target_0 while preserving the other source modules.",
        ),
    )

    assert len(plan.evidence) == MAX_PLAN_EVIDENCE_ITEMS
    assert plan.inspection.documents_read == MAX_PLAN_EVIDENCE_ITEMS
    assert plan.inspection.max_selected_files == MAX_PLAN_EVIDENCE_ITEMS
    evidence_schema = ImplementationPlan.model_json_schema()["properties"]["evidence"]
    assert evidence_schema["maxItems"] == MAX_PLAN_EVIDENCE_ITEMS

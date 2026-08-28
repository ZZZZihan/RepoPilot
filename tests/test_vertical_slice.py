from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from repopilot.adapters.filesystem import FixedRootRepositoryInspector
from repopilot.api import create_app
from repopilot.config import Settings
from repopilot.errors import StoredPlanCorruptError
from repopilot.inspection import InspectionLimits
from repopilot.models import ISSUE_TITLE_MAX_LENGTH, ImplementationPlan

CREATE_REQUEST = {
    "repository": {"url": "https://github.com/acme/tiny-python", "ref": "main"},
    "issue": {
        "number": 17,
        "url": "https://github.com/acme/tiny-python/issues/17",
        "title": "Give divide() an explicit zero-divisor error",
        "body": (
            'In calculator.py, make divide() raise ValueError("divisor must not be zero") '
            "when divisor is zero. Preserve non-zero quotients and add a regression test "
            "asserting the exact exception type and message."
        ),
    },
}


def test_golden_issue_describes_an_unsatisfied_observable_delta(
    fixture_repository_root: Path,
) -> None:
    source = (fixture_repository_root / "src/tinycalc/calculator.py").read_text(encoding="utf-8")
    fixture_tests = (fixture_repository_root / "tests/test_calculator.py").read_text(
        encoding="utf-8"
    )
    readme = (fixture_repository_root / "README.md").read_text(encoding="utf-8")

    assert "return dividend / divisor" in source
    assert "ValueError" not in source
    assert "divisor must not be zero" not in source
    assert "test_divide_by_zero" not in fixture_tests
    assert "ZeroDivisionError" in readme
    assert 'ValueError("divisor must not be zero")' in CREATE_REQUEST["issue"]["body"]


def test_implementation_plan_schema_exposes_runtime_semantic_constraints(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.get("/v1/schemas/implementation-plan")

    assert response.status_code == 200
    schema = response.json()
    assert schema["title"] == "ImplementationPlan"
    semantic_constraints = schema["x-repopilot-semantic-constraints"]
    assert semantic_constraints["version"] == "1.0"
    assert semantic_constraints["enforced_by"] == "pydantic-runtime"
    assert {item["id"] for item in semantic_constraints["constraints"]} >= {
        "evidence-graph",
        "step-sequence-and-actions",
        "verification-declarations-and-readiness",
        "plan-state-and-approval",
    }


def test_uninspected_source_and_test_return_stable_limit_error(
    settings: Settings,
    fixture_repository_root: Path,
) -> None:
    limited_inspector = FixedRootRepositoryInspector(
        root=fixture_repository_root,
        owner="acme",
        name="tiny-python",
        limits=InspectionLimits(max_selected_files=1),
    )
    app = create_app(settings=settings, inspector=limited_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=CREATE_REQUEST)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "inspection_limit_exceeded"
    assert (
        "increase inspection limits or narrow the repository" in response.json()["error"]["message"]
    )


def test_explicit_uninspected_path_returns_path_specific_limit_error(
    settings: Settings,
    fixture_repository_root: Path,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["body"] = (
        "Update `src/tinycalc/calculator.py`. Preserve the observed test behavior."
    )
    limited_inspector = FixedRootRepositoryInspector(
        root=fixture_repository_root,
        owner="acme",
        name="tiny-python",
        limits=InspectionLimits(max_selected_files=5),
    )
    app = create_app(settings=settings, inspector=limited_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "inspection_limit_exceeded"
    assert "src/tinycalc/calculator.py" in response.json()["error"]["message"]
    assert (
        "increase inspection limits or narrow the repository" in response.json()["error"]["message"]
    )


def test_explicit_absent_paths_return_exact_create_references(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = "Add a dedicated zero-handling module"
    request["issue"]["body"] = (
        "Create \"src/tinycalc/zero_handling.py\". Add coverage in 'tests/test_zero_handling.py'."
    )
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)

    assert response.status_code == 201, response.text
    payload = response.json()
    steps = {step["kind"]: step for step in payload["steps"]}
    evidence_ids = {item["id"] for item in payload["evidence"]}
    implementation_reference = steps["implementation"]["file_references"][0]
    test_reference = steps["test"]["file_references"][0]
    assert implementation_reference["path"] == "src/tinycalc/zero_handling.py"
    assert implementation_reference["action"] == "create"
    assert implementation_reference["exists"] is False
    assert "absent from the inspected tree" in implementation_reference["reason"]
    assert implementation_reference["evidence_ids"]
    assert set(implementation_reference["evidence_ids"]) <= evidence_ids
    assert test_reference["path"] == "tests/test_zero_handling.py"
    assert test_reference["action"] == "create"
    assert test_reference["exists"] is False
    assert "absent from the inspected tree" in test_reference["reason"]
    assert test_reference["evidence_ids"]
    assert set(test_reference["evidence_ids"]) <= evidence_ids


def test_conflicting_create_path_returns_stable_error_without_persistence(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = "Add a nested calculator module"
    request["issue"]["body"] = (
        "Create `src/tinycalc/calculator.py/child.py` and preserve current behavior."
    )
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "conflicting_issue_path"
    assert "src/tinycalc/calculator.py" in response.json()["error"]["message"]
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)


@pytest.mark.parametrize(
    ("title", "separator_example", "literal_example"),
    (
        ("创建new.py", "创建 new.py", "路径:创建new.py"),
        (
            "请修改功能/模块.py",
            "请修改 功能/模块.py",
            "路径:请修改功能/模块.py",
        ),
        (
            "请于src/tinycalc/calculator.py中修改",
            "请于 src/tinycalc/calculator.py 中修改",
            "路径:请于src/tinycalc/calculator.py",
        ),
        ("在new.py中修改a.py。", "在 new.py 中修改a.py。", "路径:在new.py"),
        ("在new.py中修改src/a.py", "在 new.py 中修改src/a.py", "路径:在new.py"),
        ("在new.py中修改a.py?", "在 new.py 中修改a.py?", "路径:在new.py"),
        ("在new.py中修改a.py#next", "在 new.py 中修改a.py#next", "路径:在new.py"),
        (
            "然后请在new.py中修改a.py?随后更新",
            "然后请在 new.py 中修改a.py?随后更新",
            "路径:然后请在new.py",
        ),
        (
            "然后请在.github/workflows/ci.yml内检查README.md；然后更新v3.2",
            "然后请在 .github/workflows/ci.yml 内检查README.md；然后更新v3.2",
            "路径:然后请在.github/workflows/ci.yml",
        ),
        ("修改new.py并更新src/a.py", "修改 new.py", "路径:修改new.py"),
    ),
)
def test_ambiguous_attached_cjk_path_returns_stable_error(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    title: str,
    separator_example: str,
    literal_example: str,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = title
    request["issue"]["body"] = "Preserve behavior."
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ambiguous_issue_path"
    assert separator_example in response.json()["error"]["message"]
    assert literal_example in response.json()["error"]["message"]
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)


def test_structured_url_seam_preserves_ambiguous_path_error_and_zero_residue(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = 'URL = "opaque"→在src/tinycalc/new_module.py中修改'
    request["issue"]["body"] = "Preserve behavior."
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ambiguous_issue_path"
    assert "在 src/tinycalc/new_module.py 中修改" in response.json()["error"]["message"]
    assert "路径:在src/tinycalc/new_module.py" in response.json()["error"]["message"]
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)


def test_length_preserving_ambiguity_guidance_replays_at_the_title_limit(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    prefix = "在new.py中修改"
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = prefix + "边" * (ISSUE_TITLE_MAX_LENGTH - len(prefix))
    request["issue"]["body"] = "Preserve behavior."
    compact_title = "@new.py中修改" + "边" * (ISSUE_TITLE_MAX_LENGTH - len(prefix))
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        rejected = client.post("/v1/plans", json=request)
        request["issue"]["title"] = compact_title
        replayed = client.post("/v1/plans", json=request)

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "ambiguous_issue_path"
    assert compact_title in rejected.json()["error"]["message"]
    assert len(compact_title) == ISSUE_TITLE_MAX_LENGTH
    assert replayed.status_code == 201, replayed.text
    implementation = next(
        step for step in replayed.json()["steps"] if step["kind"] == "implementation"
    )
    assert implementation["file_references"][0]["path"] == "new.py"
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (1,)


@pytest.mark.parametrize("punctuation", ("?", "!", "#"))
def test_length_preserving_guidance_with_a_url_like_action_suffix_replays_exact_target(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    punctuation: str,
) -> None:
    prefix = f"在new.py中修改a.py{punctuation}随后更新"
    padding = "边" * (ISSUE_TITLE_MAX_LENGTH - len(prefix))
    ambiguous_title = prefix + padding
    compact_title = "@new.py" + ambiguous_title[len("在new.py") :]
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = ambiguous_title
    request["issue"]["body"] = "Preserve behavior."
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        rejected = client.post("/v1/plans", json=request)
        with sqlite3.connect(settings.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)
        request["issue"]["title"] = compact_title
        replayed = client.post("/v1/plans", json=request)

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "ambiguous_issue_path"
    assert compact_title in rejected.json()["error"]["message"]
    assert len(compact_title) == ISSUE_TITLE_MAX_LENGTH
    assert replayed.status_code == 201, replayed.text
    implementation = next(
        step for step in replayed.json()["steps"] if step["kind"] == "implementation"
    )
    assert implementation["file_references"][0]["path"] == "new.py"
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (1,)


def test_ambiguity_guidance_preserves_a_path_with_an_internal_supported_suffix(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    ambiguous_prefix = "在foo.py修改/bar.py中更新"
    suffix_padding = "边" * (ISSUE_TITLE_MAX_LENGTH - len(ambiguous_prefix))
    request["issue"]["title"] = ambiguous_prefix + suffix_padding
    request["issue"]["body"] = "Preserve behavior."
    compact_title = "@foo.py修改/bar.py中更新" + suffix_padding
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        rejected = client.post("/v1/plans", json=request)
        with sqlite3.connect(settings.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)
        request["issue"]["title"] = compact_title
        replayed = client.post("/v1/plans", json=request)

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "ambiguous_issue_path"
    assert compact_title in rejected.json()["error"]["message"]
    assert replayed.status_code == 201, replayed.text
    implementation = next(
        step for step in replayed.json()["steps"] if step["kind"] == "implementation"
    )
    assert implementation["file_references"][0]["path"] == "foo.py修改/bar.py"
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (1,)


def test_attached_action_compact_guidance_replays_the_exact_selected_endpoint(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = "修改foo.py错误a.py"
    request["issue"]["body"] = "Preserve behavior."
    compact_title = "@foo.py错误a.py"
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        rejected = client.post("/v1/plans", json=request)
        with sqlite3.connect(settings.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)
        request["issue"]["title"] = compact_title
        replayed = client.post("/v1/plans", json=request)

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "ambiguous_issue_path"
    assert compact_title in rejected.json()["error"]["message"]
    assert replayed.status_code == 201, replayed.text
    implementation = next(
        step for step in replayed.json()["steps"] if step["kind"] == "implementation"
    )
    assert implementation["file_references"][0]["path"] == "foo.py错误a.py"
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (1,)


@pytest.mark.parametrize(
    "title",
    (
        "@foo.py错误/../bar.py",
        "@foo.py错误/.git/bar.py",
        "@foo.py错误//bar.py",
        "@foo.py错误/bar.txt",
        "@foo.py错误\\bar.py",
        "@foo.py．old",
        "@foo.py｡old",
        "@foo.py。bak",
        "@foo.py．old/bar.py",
        "@foo.py｡ＯＬＤ/bar.py",
        "@foo.py。bak/bar.py",
        "@foo.py.old/bar.py",
        "@foo.py＿ＢＡＫ/bar.py",
        "@foo.pybackup/bar.py",
        "@foo.py?x/bar.py",
        "@foo.py?src/pkg.py",
    ),
)
def test_invalid_compact_label_never_authorizes_a_truncated_http_target(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    title: str,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = title
    request["issue"]["body"] = "Preserve observed behavior."
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)
        with sqlite3.connect(settings.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (1,)

    assert response.status_code == 201, response.text
    file_references = [
        reference for step in response.json()["steps"] for reference in step["file_references"]
    ]
    forbidden_targets = {
        "foo.py",
        "old/bar.py",
        "ＯＬＤ/bar.py",
        "bak/bar.py",
        "＿ＢＡＫ/bar.py",
        "backup/bar.py",
        "x/bar.py",
        "src/pkg.py",
    }
    assert forbidden_targets.isdisjoint(reference["path"] for reference in file_references)


@pytest.mark.parametrize(
    "title",
    (
        "修改src/foo.py．old/bar.py",
        "在src/foo.py中修改。old/bar.py",
        "Path:src/foo.py｡old/bar.py",
        '"src/foo.py"．old/bar.py',
        "https://example.test/?q=src/foo.py．old/bar.py",
    ),
)
def test_invalid_suffix_envelope_never_authorizes_a_truncated_http_target(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    title: str,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = title
    request["issue"]["body"] = "Preserve observed behavior."
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)
        with sqlite3.connect(settings.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (1,)

    assert response.status_code == 201, response.text
    file_references = [
        reference for step in response.json()["steps"] for reference in step["file_references"]
    ]
    assert all(reference["path"] != "old/bar.py" for reference in file_references)


@pytest.mark.parametrize(
    "title",
    (
        'URL="src/foo.py.old/bar.py";Update:"src/tinycalc/new_module.py"',
        'URL="src/foo.py．ＯＬＤ/bar.py";Update:"src/tinycalc/new_module.py"',
        '@foo.py.old/bar.py;Update:"src/tinycalc/new_module.py"',
        "src/foo.py．old/bar.py；然后修改“src/tinycalc/new_module.py”",
        "URL=src/foo.py。bk/bar.py→Update:src/tinycalc/new_module.py",
        'URL=src/foo.py。bk/bar.py|Update:"src/tinycalc/new_module.py"',
        "URL=src/foo.py。bk/bar.py—然后修改“src/tinycalc/new_module.py”",
        "URL=src/foo.py。bk/bar.py–Create:src/tinycalc/new_module.py",
        *(
            f'URL=src/foo.py{dot}{suffix}/bar.py;Update:"src/tinycalc/new_module.py"'
            for dot in ("。", "｡", "．")
            for suffix in ("bk", "diff", "patch")
        ),
    ),
)
def test_invalid_clause_preserves_a_following_http_target(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    title: str,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = title
    request["issue"]["body"] = "Preserve observed behavior."
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)
        with sqlite3.connect(settings.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (1,)

    assert response.status_code == 201, response.text
    implementation = next(
        step for step in response.json()["steps"] if step["kind"] == "implementation"
    )
    assert len(implementation["file_references"]) == 1
    reference = implementation["file_references"][0]
    assert reference["path"] == "src/tinycalc/new_module.py"
    assert reference["action"] == "create"
    assert reference["exists"] is False


@pytest.mark.parametrize(
    "title",
    (
        ("https://example.test/?q=src/foo.py。bk/bar.py;Update:src/tinycalc/new_module.py"),
        ("mailto:user@example.test?body=src/foo.py。bk/bar.py,Create:src/tinycalc/new_module.py"),
    ),
)
def test_uri_query_action_text_never_becomes_an_http_target(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    title: str,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = title
    request["issue"]["body"] = "Preserve observed behavior."
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)
        with sqlite3.connect(settings.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (1,)

    assert response.status_code == 201, response.text
    implementation = next(
        step for step in response.json()["steps"] if step["kind"] == "implementation"
    )
    assert len(implementation["file_references"]) == 1
    reference = implementation["file_references"][0]
    assert reference["path"] == "src/tinycalc/__init__.py"
    assert reference["action"] == "modify"
    assert reference["exists"] is True


@pytest.mark.parametrize(
    "title",
    (
        "URL = https://e.test/?q=x→Path:src/tinycalc/new_module.py",
        'URL = https://e.test/?q=x→Path: "src/tinycalc/new_module.py"',
        "URL=修改:src/tinycalc/hidden.py→Path:src/tinycalc/new_module.py",
        "网址=在 src/tinycalc/hidden.py 中修改→Path:src/tinycalc/new_module.py",
        'URL=URL=Add "src/tinycalc/hidden.py"→Path:src/tinycalc/new_module.py',
        'URL=URL="opaque src/tinycalc/hidden.py"→Path:src/tinycalc/new_module.py',
    ),
)
def test_http_separate_url_value_hard_boundary_reopens_a_repository_clause(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    title: str,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = title
    request["issue"]["body"] = "Preserve observed behavior."
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)
        with sqlite3.connect(settings.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (1,)

    assert response.status_code == 201, response.text
    implementation = next(
        step for step in response.json()["steps"] if step["kind"] == "implementation"
    )
    assert len(implementation["file_references"]) == 1
    reference = implementation["file_references"][0]
    assert reference["path"] == "src/tinycalc/new_module.py"
    assert reference["action"] == "create"
    assert reference["exists"] is False


@pytest.mark.parametrize(
    "title",
    (
        'URL = "opaque x" Path: "src/tinycalc/new_module.py"',
        'URL=URL=Add "src/tinycalc/new_module.py"',
        'URL=URL="opaque x" Path: "src/tinycalc/new_module.py"',
    ),
)
def test_http_completed_url_wrapper_requires_a_clause_separator(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    title: str,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = title
    request["issue"]["body"] = "Preserve observed behavior."
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)
        with sqlite3.connect(settings.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (1,)

    assert response.status_code == 201, response.text
    implementation = next(
        step for step in response.json()["steps"] if step["kind"] == "implementation"
    )
    assert len(implementation["file_references"]) == 1
    reference = implementation["file_references"][0]
    assert reference["path"] == "src/tinycalc/__init__.py"
    assert reference["action"] == "modify"
    assert reference["exists"] is True


def test_http_startup_rejects_a_schema_that_blocks_legal_approval(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('proposed', 'approved'))
                    CHECK (status != 'approved'),
                version INTEGER NOT NULL CHECK (version >= 1),
                document TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    app = create_app(settings=settings, inspector=fixture_inspector)

    with pytest.raises(StoredPlanCorruptError, match="schema is incompatible"):
        with TestClient(app):
            pass

    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)


@pytest.mark.parametrize(
    ("separator", "right_uri"),
    (
        ("→", "https://e.test"),
        ("|", "mailto:user@e.test?body=tests/leak.py"),
        ("—", "idea://open?file=tests/leak.py"),
        ("–", "//e.test/?file=tests/leak.py"),
    ),
)
def test_http_hard_semantic_boundary_keeps_the_left_target_before_an_opaque_uri(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    separator: str,
    right_uri: str,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = "Path:src/tinycalc/new_module.py" + separator + right_uri
    request["issue"]["body"] = "Preserve observed behavior."
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)
        with sqlite3.connect(settings.database_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (1,)

    assert response.status_code == 201, response.text
    implementation = next(
        step for step in response.json()["steps"] if step["kind"] == "implementation"
    )
    assert len(implementation["file_references"]) == 1
    reference = implementation["file_references"][0]
    assert reference["path"] == "src/tinycalc/new_module.py"
    assert reference["action"] == "create"
    assert reference["exists"] is False


@pytest.mark.parametrize("padding_length", (499, 500, 501))
def test_distant_invalid_compact_continuation_never_authorizes_a_short_http_target(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    padding_length: int,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = "Preserve behavior"
    request["issue"]["body"] = "@foo.py" + "错" * padding_length + "/../bar.py"
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)

    assert response.status_code == 201, response.text
    file_references = [
        reference for step in response.json()["steps"] for reference in step["file_references"]
    ]
    assert all(reference["path"] != "foo.py" for reference in file_references)


@pytest.mark.parametrize(
    ("title", "expected_path"),
    (
        ("请修改:src/tinycalc/calculator.py", "src/tinycalc/calculator.py"),
        ("并创建:src/tinycalc/new_module.py", "src/tinycalc/new_module.py"),
    ),
)
def test_prefixed_cjk_colon_labels_preserve_exact_target(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    title: str,
    expected_path: str,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = title
    request["issue"]["body"] = "Preserve behavior."
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)

    assert response.status_code == 201, response.text
    implementation = next(
        step for step in response.json()["steps"] if step["kind"] == "implementation"
    )
    assert implementation["file_references"][0]["path"] == expected_path


def test_delimited_cjk_actions_preserve_exact_modify_targets(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = "修改计算器实现"
    request["issue"]["body"] = (
        "请修改 src/tinycalc/calculator.py，并更新 tests/test_calculator.py 中的回归覆盖。"
    )
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)

    assert response.status_code == 201, response.text
    steps = {step["kind"]: step for step in response.json()["steps"]}
    implementation_reference = steps["implementation"]["file_references"][0]
    test_reference = steps["test"]["file_references"][0]
    assert implementation_reference["path"] == "src/tinycalc/calculator.py"
    assert implementation_reference["action"] == "modify"
    assert implementation_reference["exists"] is True
    assert test_reference["path"] == "tests/test_calculator.py"
    assert test_reference["action"] == "modify"
    assert test_reference["exists"] is True


def test_http_planning_keeps_markdown_and_url_values_opaque(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["title"] = "Update the explicitly wrapped implementation and test"
    request["issue"]["body"] = "\n".join(
        (
            '[guide](https://example.com/wiki/Foo_(bar)?file="src/tinycalc/leak.py")',
            "![diagram](tests/test_image_destination.py)",
            'URL = https://example.com/?file = "tests/test_query_value.py"',
            "https://example.com/?files=README.md,src/tinycalc/comma_leak.py",
            "https://example.com/?redirect=[src/tinycalc/markdown_leak.py](context)",
            ('https://example.com/?files="src/tinycalc/nested_leak.py",tests/test_tail_leak.py'),
            "https://example.com/?file=( src/tinycalc/grouped_leak.py )",
            'URI: "src/tinycalc/uri_value_leak.py"',
            "https://例子。中国/src/tinycalc/idna_leak.py",
            "参见URL = Path:src/tinycalc/labeled_leak.py。",
            "URL = Add`src/tinycalc/action_wrapper_leak.py`",
            "URI = Update:[src/tinycalc/action_markdown_leak.py](context)",
            'href = Path: "src/tinycalc/spaced_action_leak.py"',
            "link = Create【src/tinycalc/cjk_wrapper_leak.py】",
            "网址 = 在[src/tinycalc/location_markdown_leak.py](context)中修改",
            "URL=修改: src/tinycalc/compact_cjk_action_leak.py",
            ('URL = "opaque src/tinycalc/spaced_unclosed_leak.py→Path:src/tinycalc/other_leak.py'),
            "在“src/tinycalc/calculator.py”增加校验，并让【tests/test_calculator.py】覆盖它。",
        )
    )
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)

    assert response.status_code == 201, response.text
    steps = {step["kind"]: step for step in response.json()["steps"]}
    implementation_reference = steps["implementation"]["file_references"][0]
    test_reference = steps["test"]["file_references"][0]
    assert implementation_reference["path"] == "src/tinycalc/calculator.py"
    assert implementation_reference["action"] == "modify"
    assert implementation_reference["exists"] is True
    assert test_reference["path"] == "tests/test_calculator.py"
    assert test_reference["action"] == "modify"
    assert test_reference["exists"] is True
    planned_paths = {
        reference["path"] for step in steps.values() for reference in step["file_references"]
    }
    assert not any(path.endswith("leak.py") for path in planned_paths)


def test_create_persist_validate_and_approve_plan_end_to_end(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    fixture_repository_root: Path,
) -> None:
    app = create_app(settings=settings, inspector=fixture_inspector)
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        created_response = client.post("/v1/plans", json=CREATE_REQUEST)
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        plan_id = created["plan_id"]

        assert created["status"] == "proposed"
        assert created["version"] == 1
        assert created["approval"] is None
        assert created["repository"]["tree_sha"]
        assert created["inspection"]["files_seen"] == 6
        assert created["inspection"]["documents_read"] == 6
        assert {item["category"] for item in created["evidence"]} == {
            "readme",
            "project_config",
            "test_config",
            "test",
            "source",
        }

        evidence_ids = {item["id"] for item in created["evidence"]}
        for step in created["steps"]:
            for reference in step["file_references"]:
                assert set(reference["evidence_ids"]) <= evidence_ids
        steps_by_kind = {step["kind"]: step for step in created["steps"]}
        assert [
            reference["path"] for reference in steps_by_kind["analysis"]["file_references"]
        ] == [
            "src/tinycalc/calculator.py",
            "tests/test_calculator.py",
            "README.md",
        ]
        assert [
            reference["path"] for reference in steps_by_kind["implementation"]["file_references"]
        ] == ["src/tinycalc/calculator.py"]
        assert [reference["path"] for reference in steps_by_kind["test"]["file_references"]] == [
            "tests/test_calculator.py"
        ]
        assert (
            'ValueError("divisor must not be zero")'
            in steps_by_kind["implementation"]["description"]
        )
        assert 'ValueError("divisor must not be zero")' in steps_by_kind["test"]["description"]

        evidence_by_path = {item["path"]: item for item in created["evidence"]}
        expected_evidence_terms = {
            "README.md": {"divide", "zero"},
            "src/tinycalc/calculator.py": {"divide", "divisor", "zero"},
            "tests/test_calculator.py": {"divide", "quotient"},
        }
        for path, expected_terms in expected_evidence_terms.items():
            evidence_item = evidence_by_path[path]
            lines = (fixture_repository_root / path).read_text(encoding="utf-8").splitlines()
            snippet = "\n".join(
                lines[evidence_item["line_start"] - 1 : evidence_item["line_end"]]
            ).lower()
            assert all(term in snippet for term in expected_terms)
        assert created["verification_intents"] == [
            {
                "tool": "pytest",
                "arguments": [],
                "evidence_ids": [created["verification_intents"][0]["evidence_ids"][0]],
                "executed": False,
            }
        ]
        assert all(intent["executed"] is False for intent in created["verification_intents"])

        persisted_response = client.get(f"/v1/plans/{plan_id}")
        assert persisted_response.status_code == 200
        assert persisted_response.json() == created

        schema_response = client.get("/v1/schemas/implementation-plan")
        assert schema_response.status_code == 200
        assert schema_response.json()["title"] == "ImplementationPlan"

        invalid_plan = deepcopy(created)
        invalid_plan["steps"][0]["file_references"][0]["evidence_ids"] = ["E999"]
        with pytest.raises(ValidationError, match="unknown evidence"):
            ImplementationPlan.model_validate_json(json.dumps(invalid_plan))

        empty_verification_plan = deepcopy(created)
        empty_verification_plan["verification_intents"] = []
        with pytest.raises(ValidationError, match="at least 1 item"):
            ImplementationPlan.model_validate_json(json.dumps(empty_verification_plan))

        approved_response = client.post(
            f"/v1/plans/{plan_id}/approval",
            json={"approved_by": "Local Reviewer", "expected_version": 1},
        )
        assert approved_response.status_code == 200, approved_response.text
        approved = approved_response.json()
        assert approved["status"] == "approved"
        assert approved["version"] == 2
        assert approved["approval"]["approved_by"] == "Local Reviewer"
        assert approved["approval"]["from_version"] == 1

        duplicate_response = client.post(
            f"/v1/plans/{plan_id}/approval",
            json={"approved_by": "Local Reviewer", "expected_version": 2},
        )
        assert duplicate_response.status_code == 409
        assert duplicate_response.json()["error"]["code"] == "invalid_plan_transition"

    restarted_app = create_app(settings=settings, inspector=fixture_inspector)
    with TestClient(restarted_app) as restarted_client:
        restored_response = restarted_client.get(f"/v1/plans/{plan_id}")
        assert restored_response.status_code == 200
        assert restored_response.json() == approved


def test_http_get_reports_a_blob_plan_document_as_stored_corruption(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
) -> None:
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        created_response = client.post("/v1/plans", json=CREATE_REQUEST)
        assert created_response.status_code == 201, created_response.text
        plan_id = created_response.json()["plan_id"]
        with sqlite3.connect(settings.database_path) as connection:
            row = connection.execute(
                "SELECT document FROM plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            assert row is not None
            connection.execute(
                "UPDATE plans SET document = ? WHERE plan_id = ?",
                (sqlite3.Binary(row[0].encode("utf-8")), plan_id),
            )

        response = client.get(f"/v1/plans/{plan_id}")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "stored_plan_corrupt",
            "message": "stored plan document must be SQLite TEXT",
        }
    }


def test_stale_approval_version_is_rejected(
    settings: Settings, fixture_inspector: FixedRootRepositoryInspector
) -> None:
    app = create_app(settings=settings, inspector=fixture_inspector)
    with TestClient(app) as client:
        plan_id = client.post("/v1/plans", json=CREATE_REQUEST).json()["plan_id"]
        response = client.post(
            f"/v1/plans/{plan_id}/approval",
            json={"approved_by": "Reviewer", "expected_version": 999},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "plan_version_conflict"


@pytest.mark.parametrize("expected_version", (True, 1.0, "1"))
def test_approval_version_requires_a_strict_json_integer(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    expected_version: object,
) -> None:
    app = create_app(settings=settings, inspector=fixture_inspector)
    with TestClient(app) as client:
        plan_id = client.post("/v1/plans", json=CREATE_REQUEST).json()["plan_id"]
        response = client.post(
            f"/v1/plans/{plan_id}/approval",
            json={"approved_by": "Reviewer", "expected_version": expected_version},
        )
        persisted = client.get(f"/v1/plans/{plan_id}")

    assert response.status_code == 422
    assert persisted.status_code == 200
    assert persisted.json()["status"] == "proposed"
    assert persisted.json()["version"] == 1
    assert persisted.json()["approval"] is None


@pytest.mark.parametrize("issue_number", (True, 17.0, "17"))
def test_issue_number_requires_a_strict_json_integer(
    settings: Settings,
    fixture_inspector: FixedRootRepositoryInspector,
    issue_number: object,
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["number"] = issue_number
    app = create_app(settings=settings, inspector=fixture_inspector)

    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)

    assert response.status_code == 422


def test_issue_url_must_match_repository_and_no_execution_routes_exist(
    settings: Settings, fixture_inspector: FixedRootRepositoryInspector
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["issue"]["url"] = "https://github.com/other/repository/issues/17"
    app = create_app(settings=settings, inspector=fixture_inspector)
    with TestClient(app) as client:
        mismatch = client.post("/v1/plans", json=request)
        paths = client.get("/openapi.json").json()["paths"]

    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "issue_repository_mismatch"
    assert not any("execute" in path or "pull-request" in path for path in paths)


def test_repository_input_rejects_non_github_hosts(
    settings: Settings, fixture_inspector: FixedRootRepositoryInspector
) -> None:
    request = deepcopy(CREATE_REQUEST)
    request["repository"]["url"] = "https://example.com/acme/tiny-python"
    app = create_app(settings=settings, inspector=fixture_inspector)
    with TestClient(app) as client:
        response = client.post("/v1/plans", json=request)
    assert response.status_code == 422

from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from repopilot.adapters.filesystem import FixedRootRepositoryInspector
from repopilot.api import create_app
from repopilot.config import Settings
from repopilot.models import ImplementationPlan

CREATE_REQUEST = {
    "repository": {"url": "https://github.com/acme/tiny-python", "ref": "main"},
    "issue": {
        "number": 17,
        "url": "https://github.com/acme/tiny-python/issues/17",
        "title": "Give divide() a clear zero-divisor error",
        "body": "Update divide in calculator.py and keep a regression test for zero divisors.",
    },
}


def test_create_persist_validate_and_approve_plan_end_to_end(
    settings: Settings, fixture_inspector: FixedRootRepositoryInspector
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
        implementation_paths = {
            reference["path"] for reference in created["steps"][1]["file_references"]
        }
        assert "src/tinycalc/calculator.py" in implementation_paths
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
            ImplementationPlan.model_validate(invalid_plan)

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

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from repopilot.adapters.filesystem import FixedRootRepositoryInspector
from repopilot.errors import RepositoryUpstreamError
from repopilot.inspection import RepositorySnapshot
from repopilot.models import CreatePlanRequest, GitHubRepositoryInput, InspectedRepository
from repopilot.planning import PlanningService
from repopilot.storage import SQLitePlanStore


class _IdentityDriftingInspector:
    def __init__(
        self,
        delegate: FixedRootRepositoryInspector,
        returned_repository: InspectedRepository,
    ) -> None:
        self._delegate = delegate
        self._returned_repository = returned_repository

    async def inspect(self, repository: GitHubRepositoryInput) -> RepositorySnapshot:
        snapshot = await self._delegate.inspect(repository)
        return replace(snapshot, repository=self._returned_repository)


@pytest.mark.parametrize(
    "returned_repository",
    [
        InspectedRepository(
            url="https://github.com/other/wrong-repository",
            owner="other",
            name="wrong-repository",
            ref="main",
            tree_sha="a" * 40,
        ),
        InspectedRepository(
            url="https://github.com/acme/tiny-python",
            owner="acme",
            name="tiny-python",
            ref="other-ref",
            tree_sha="a" * 40,
        ),
    ],
    ids=["coordinates", "explicit-ref"],
)
def test_planning_service_rejects_inspector_identity_drift_before_persistence(
    returned_repository: InspectedRepository,
    fixture_inspector: FixedRootRepositoryInspector,
    tmp_path: Path,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    inspector = _IdentityDriftingInspector(fixture_inspector, returned_repository)
    service = PlanningService(inspector=inspector, store=store)
    request = CreatePlanRequest.model_validate(
        {
            "repository": {
                "url": "https://github.com/acme/tiny-python.git",
                "ref": "main",
            },
            "issue": {
                "number": 17,
                "title": "Preserve repository identity",
                "body": "Update the observed implementation without changing repositories.",
            },
        }
    )

    with pytest.raises(
        RepositoryUpstreamError,
        match="repository inspector returned an inconsistent repository identity",
    ):
        asyncio.run(service.create_plan(request))

    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM plans").fetchone() == (0,)

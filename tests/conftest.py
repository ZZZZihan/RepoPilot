from __future__ import annotations

from pathlib import Path

import pytest

from repopilot.adapters.filesystem import FixedRootRepositoryInspector
from repopilot.config import Settings
from repopilot.inspection import InspectionLimits


@pytest.fixture
def fixture_repository_root() -> Path:
    return Path(__file__).parent / "fixtures" / "tiny_python_repo"


@pytest.fixture
def inspection_limits() -> InspectionLimits:
    return InspectionLimits()


@pytest.fixture
def fixture_inspector(
    fixture_repository_root: Path, inspection_limits: InspectionLimits
) -> FixedRootRepositoryInspector:
    return FixedRootRepositoryInspector(
        root=fixture_repository_root,
        owner="acme",
        name="tiny-python",
        limits=inspection_limits,
    )


@pytest.fixture
def settings(tmp_path: Path, inspection_limits: InspectionLimits) -> Settings:
    return Settings(
        database_path=tmp_path / "repopilot.db",
        github_token=None,
        github_api_version="2026-03-10",
        inspection_limits=inspection_limits,
    )

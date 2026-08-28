"""Environment configuration with explicit, bounded defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from repopilot.inspection import InspectionLimits
from repopilot.models import MAX_PLAN_EVIDENCE_ITEMS

_API_VERSION_PATTERN = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")


def _environment_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    github_token: str | None
    github_api_version: str
    inspection_limits: InspectionLimits

    @classmethod
    def from_environment(cls) -> Settings:
        token = os.getenv("REPOPILOT_GITHUB_TOKEN", "").strip() or None
        api_version = os.getenv("REPOPILOT_GITHUB_API_VERSION", "2026-03-10").strip()
        if not _API_VERSION_PATTERN.fullmatch(api_version):
            raise ValueError("REPOPILOT_GITHUB_API_VERSION must use YYYY-MM-DD")

        limits = InspectionLimits(
            max_tree_entries=_environment_int(
                "REPOPILOT_MAX_TREE_ENTRIES", 2_000, minimum=1, maximum=100_000
            ),
            max_selected_files=_environment_int(
                "REPOPILOT_MAX_SELECTED_FILES",
                32,
                minimum=1,
                maximum=MAX_PLAN_EVIDENCE_ITEMS,
            ),
            max_file_bytes=_environment_int(
                "REPOPILOT_MAX_FILE_BYTES", 64 * 1024, minimum=1_024, maximum=1024 * 1024
            ),
            max_total_bytes=_environment_int(
                "REPOPILOT_MAX_TOTAL_BYTES",
                384 * 1024,
                minimum=1_024,
                maximum=8 * 1024 * 1024,
            ),
            max_response_bytes=_environment_int(
                "REPOPILOT_MAX_RESPONSE_BYTES",
                2 * 1024 * 1024,
                minimum=16 * 1024,
                maximum=8 * 1024 * 1024,
            ),
            request_timeout_seconds=float(
                _environment_int("REPOPILOT_REQUEST_TIMEOUT_SECONDS", 10, minimum=1, maximum=60)
            ),
            inspection_timeout_seconds=float(
                _environment_int("REPOPILOT_INSPECTION_TIMEOUT_SECONDS", 30, minimum=1, maximum=300)
            ),
        )
        return cls(
            database_path=Path(os.getenv("REPOPILOT_DATABASE_PATH", "var/repopilot.db")),
            github_token=token,
            github_api_version=api_version,
            inspection_limits=limits,
        )

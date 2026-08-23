"""SQLite-backed authoritative plan store and approval state transition."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from pydantic import ValidationError

from repopilot.errors import (
    InvalidPlanTransitionError,
    PlanNotFoundError,
    PlanVersionConflictError,
    StoredPlanCorruptError,
)
from repopilot.models import ApprovalRecord, ImplementationPlan, PlanStatus


def utc_now() -> datetime:
    return datetime.now(UTC)


class SQLitePlanStore:
    """Persist validated plan documents and own their one legal state transition."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    @property
    def database_path(self) -> Path:
        return self._database_path

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        existed = self._database_path.exists()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('proposed', 'approved')),
                    version INTEGER NOT NULL CHECK (version >= 1),
                    document TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        if not existed and self._database_path.exists():
            os.chmod(self._database_path, 0o600)

    def create(self, plan: ImplementationPlan) -> None:
        validated = ImplementationPlan.model_validate(plan.model_dump(mode="python"))
        serialized = validated.model_dump_json()
        timestamp = validated.created_at.isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO plans (
                    plan_id, schema_version, status, version, document, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(validated.plan_id),
                    validated.schema_version,
                    validated.status.value,
                    validated.version,
                    serialized,
                    timestamp,
                    timestamp,
                ),
            )

    def get(self, plan_id: UUID) -> ImplementationPlan:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM plans WHERE plan_id = ?", (str(plan_id),)
            ).fetchone()
        if row is None:
            raise PlanNotFoundError(f"plan {plan_id} was not found")
        return self._deserialize(cast(str, row["document"]))

    def approve(
        self,
        plan_id: UUID,
        *,
        approved_by: str,
        expected_version: int,
        approved_at: datetime | None = None,
    ) -> ImplementationPlan:
        transition_time = approved_at or utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT document FROM plans WHERE plan_id = ?", (str(plan_id),)
            ).fetchone()
            if row is None:
                raise PlanNotFoundError(f"plan {plan_id} was not found")

            current = self._deserialize(cast(str, row["document"]))
            if current.version != expected_version:
                raise PlanVersionConflictError(
                    f"expected plan version {expected_version}, found {current.version}"
                )
            if current.status is not PlanStatus.PROPOSED:
                raise InvalidPlanTransitionError(
                    f"plan {plan_id} cannot transition from {current.status.value} to approved"
                )

            candidate = current.model_dump(mode="python")
            candidate.update(
                {
                    "status": PlanStatus.APPROVED,
                    "version": current.version + 1,
                    "approval": ApprovalRecord(
                        approved_by=approved_by,
                        approved_at=transition_time,
                        from_version=current.version,
                    ),
                }
            )
            approved = ImplementationPlan.model_validate(candidate)
            cursor = connection.execute(
                """
                UPDATE plans
                SET status = ?, version = ?, document = ?, updated_at = ?
                WHERE plan_id = ? AND version = ? AND status = 'proposed'
                """,
                (
                    approved.status.value,
                    approved.version,
                    approved.model_dump_json(),
                    transition_time.isoformat(),
                    str(plan_id),
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise PlanVersionConflictError("plan changed while approval was being recorded")
            return approved

    def healthcheck(self) -> None:
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _deserialize(serialized: str) -> ImplementationPlan:
        try:
            return ImplementationPlan.model_validate_json(serialized)
        except ValidationError as exc:
            raise StoredPlanCorruptError(
                "stored plan no longer satisfies the implementation-plan schema"
            ) from exc

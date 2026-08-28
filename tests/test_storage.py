from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

import repopilot.storage as storage_module
from repopilot.errors import (
    InvalidPlanTransitionError,
    PlanNotFoundError,
    StoredPlanCorruptError,
)
from repopilot.models import (
    ApprovalRecord,
    EvidenceCategory,
    EvidenceItem,
    FileAction,
    FileReference,
    ImplementationPlan,
    InspectedRepository,
    InspectionSummary,
    IssueInput,
    PlanStatus,
    PlanStep,
    StepKind,
    VerificationDeclaration,
    VerificationDeclarationKind,
    VerificationIntent,
)
from repopilot.storage import SQLitePlanStore


def _minimal_plan() -> ImplementationPlan:
    evidence = EvidenceItem(
        id="E1",
        path="pyproject.toml",
        category=EvidenceCategory.PROJECT_CONFIG,
        line_start=1,
        line_end=1,
        sha256="a" * 64,
        observation="The project declares pytest.",
        declared_tools=[
            VerificationDeclaration(
                tool="pytest",
                kind=VerificationDeclarationKind.CONFIGURATION,
                arguments=[],
                line_start=1,
                line_end=1,
            )
        ],
    )
    return ImplementationPlan(
        plan_id=UUID("00000000-0000-4000-8000-000000000017"),
        status=PlanStatus.PROPOSED,
        version=1,
        repository=InspectedRepository(
            url="https://github.com/acme/tiny",
            owner="acme",
            name="tiny",
            ref="main",
            tree_sha="a" * 40,
        ),
        issue=IssueInput(number=17, title="Exercise storage", body=""),
        summary="Persist one minimal plan.",
        inspection=InspectionSummary(
            files_seen=1,
            documents_read=1,
            selection_truncated=False,
            max_tree_entries=1,
            max_selected_files=1,
            max_file_bytes=1,
            max_total_bytes=1,
        ),
        evidence=[evidence],
        steps=[
            PlanStep(
                sequence=1,
                kind=StepKind.VERIFICATION,
                title="Record verification",
                description="Record the declared pytest configuration.",
                file_references=[
                    FileReference(
                        path="pyproject.toml",
                        action=FileAction.VERIFY,
                        exists=True,
                        reason="The file declares pytest.",
                        evidence_ids=["E1"],
                    )
                ],
            )
        ],
        verification_intents=[VerificationIntent(tool="pytest", arguments=[], evidence_ids=["E1"])],
        verification_readiness="ready",
        assumptions=[],
        risks=[],
        out_of_scope=["Execution"],
        created_at=datetime(2026, 8, 27, tzinfo=UTC),
    )


def test_create_revalidates_and_rejects_a_forged_naive_timestamp(tmp_path: Path) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    plan = _minimal_plan()
    forged = plan.model_copy(update={"created_at": datetime(2026, 8, 27)})

    with pytest.raises(ValidationError, match="created_at must be timezone-aware"):
        store.create(forged)

    with pytest.raises(PlanNotFoundError):
        store.approve(plan.plan_id, approved_by="Reviewer", expected_version=1)


def test_approve_reports_a_legacy_naive_document_as_corrupt_not_type_error(
    tmp_path: Path,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    plan = _minimal_plan()
    store.create(plan)

    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT document FROM plans WHERE plan_id = ?",
            (str(plan.plan_id),),
        ).fetchone()
        assert row is not None
        document = json.loads(row[0])
        document["created_at"] = "2026-08-27T00:00:00"
        connection.execute(
            "UPDATE plans SET document = ? WHERE plan_id = ?",
            (json.dumps(document), str(plan.plan_id)),
        )

    with pytest.raises(StoredPlanCorruptError, match="no longer satisfies"):
        store.approve(plan.plan_id, approved_by="Reviewer", expected_version=1)


@pytest.mark.parametrize(
    "created_at",
    (
        "2026-08-27 00:00:00Z",
        "2026-08-27_00:00:00Z",
        "2026-08-27T00:00Z",
        "2026-08-27T00:00:00+0000",
    ),
)
def test_get_rejects_persisted_non_rfc3339_timestamp_lexemes(
    tmp_path: Path,
    created_at: str,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    plan = _minimal_plan()
    store.create(plan)

    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT document FROM plans WHERE plan_id = ?",
            (str(plan.plan_id),),
        ).fetchone()
        assert row is not None
        document = json.loads(row[0])
        document["created_at"] = created_at
        connection.execute(
            "UPDATE plans SET document = ? WHERE plan_id = ?",
            (json.dumps(document), str(plan.plan_id)),
        )

    with pytest.raises(StoredPlanCorruptError, match="no longer satisfies"):
        store.get(plan.plan_id)


def test_get_rejects_a_blob_document_before_json_parsing(tmp_path: Path) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    plan = _minimal_plan()
    store.create(plan)

    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT document FROM plans WHERE plan_id = ?",
            (str(plan.plan_id),),
        ).fetchone()
        assert row is not None
        connection.execute(
            "UPDATE plans SET document = ? WHERE plan_id = ?",
            (sqlite3.Binary(row[0].encode("utf-8")), str(plan.plan_id)),
        )

    with pytest.raises(StoredPlanCorruptError, match="must be SQLite TEXT"):
        store.get(plan.plan_id)


@pytest.mark.parametrize(
    "document_plan_id",
    (
        "00000000000040008000000000000017",
        "{00000000-0000-4000-8000-000000000017}",
        "urn:uuid:00000000-0000-4000-8000-000000000017",
    ),
)
def test_get_rejects_a_noncanonical_persisted_document_plan_id(
    tmp_path: Path,
    document_plan_id: str,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    plan = _minimal_plan()
    store.create(plan)

    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT document FROM plans WHERE plan_id = ?",
            (str(plan.plan_id),),
        ).fetchone()
        assert row is not None
        document = json.loads(row[0])
        document["plan_id"] = document_plan_id
        connection.execute(
            "UPDATE plans SET document = ? WHERE plan_id = ?",
            (json.dumps(document), str(plan.plan_id)),
        )

    with pytest.raises(StoredPlanCorruptError, match="no longer satisfies"):
        store.get(plan.plan_id)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("owner", " acme"),
        ("name", "tiny "),
        ("ref", " main"),
        ("tree_sha", "a" * 40 + " "),
    ),
)
def test_get_rejects_padded_persisted_repository_identity(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    plan = _minimal_plan()
    store.create(plan)

    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT document FROM plans WHERE plan_id = ?",
            (str(plan.plan_id),),
        ).fetchone()
        assert row is not None
        document = json.loads(row[0])
        document["repository"][field] = value
        connection.execute(
            "UPDATE plans SET document = ? WHERE plan_id = ?",
            (json.dumps(document), str(plan.plan_id)),
        )

    with pytest.raises(StoredPlanCorruptError, match="no longer satisfies"):
        store.get(plan.plan_id)


def test_create_rejects_a_preapproved_record_that_bypasses_the_transition(
    tmp_path: Path,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    proposed = _minimal_plan()
    payload = proposed.model_dump(mode="python")
    payload.update(
        {
            "status": PlanStatus.APPROVED,
            "version": 2,
            "approval": ApprovalRecord(
                approved_by="Reviewer",
                approved_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
                from_version=1,
            ),
        }
    )
    approved = ImplementationPlan.model_validate(payload)

    with pytest.raises(InvalidPlanTransitionError, match="proposed version 1"):
        store.create(approved)

    with pytest.raises(PlanNotFoundError):
        store.get(approved.plan_id)


def test_timezone_offset_round_trips_through_create_and_approval_in_utc(
    tmp_path: Path,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    offset = timezone(timedelta(hours=8))
    plan = _minimal_plan().model_copy(
        update={"created_at": datetime(2026, 8, 27, 8, tzinfo=offset)}
    )

    store.create(plan)
    persisted = store.get(plan.plan_id)
    approved = store.approve(
        plan.plan_id,
        approved_by="Reviewer",
        expected_version=1,
        approved_at=datetime(2026, 8, 27, 9, tzinfo=offset),
    )

    assert persisted.created_at == datetime(2026, 8, 27, tzinfo=UTC)
    assert persisted.created_at.tzinfo is UTC
    assert approved.approval is not None
    assert approved.approval.approved_at == datetime(2026, 8, 27, 1, tzinfo=UTC)
    assert approved.approval.approved_at.tzinfo is UTC
    assert store.get(plan.plan_id) == approved


@pytest.mark.parametrize("approved_at", (False, 0, 0.0, "", "2026-08-27T01:00:00Z"))
def test_approve_rejects_non_datetime_timestamps_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    approved_at: object,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    plan = _minimal_plan()
    store.create(plan)
    utc_now_calls = 0

    def unexpected_utc_now() -> datetime:
        nonlocal utc_now_calls
        utc_now_calls += 1
        return datetime(2026, 8, 27, 2, tzinfo=UTC)

    monkeypatch.setattr(storage_module, "utc_now", unexpected_utc_now)

    with pytest.raises(ValueError, match="approved_at must be a datetime or None"):
        store.approve(
            plan.plan_id,
            approved_by="Reviewer",
            expected_version=1,
            approved_at=approved_at,  # type: ignore[arg-type]
        )

    persisted = store.get(plan.plan_id)
    assert persisted.status is PlanStatus.PROPOSED
    assert persisted.version == 1
    assert persisted.approval is None
    assert utc_now_calls == 0


def test_approve_uses_utc_now_only_when_timestamp_is_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    plan = _minimal_plan()
    store.create(plan)
    fixed_now = datetime(2026, 8, 27, 2, tzinfo=UTC)
    utc_now_calls = 0

    def fixed_utc_now() -> datetime:
        nonlocal utc_now_calls
        utc_now_calls += 1
        return fixed_now

    monkeypatch.setattr(storage_module, "utc_now", fixed_utc_now)

    approved = store.approve(
        plan.plan_id,
        approved_by="Reviewer",
        expected_version=1,
        approved_at=None,
    )

    assert utc_now_calls == 1
    assert approved.approval is not None
    assert approved.approval.approved_at == fixed_now


def test_approve_rejects_a_naive_datetime_without_mutation(tmp_path: Path) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    plan = _minimal_plan()
    store.create(plan)

    with pytest.raises(ValidationError, match="approved_at must be timezone-aware"):
        store.approve(
            plan.plan_id,
            approved_by="Reviewer",
            expected_version=1,
            approved_at=datetime(2026, 8, 27, 2),
        )

    persisted = store.get(plan.plan_id)
    assert persisted.status is PlanStatus.PROPOSED
    assert persisted.version == 1
    assert persisted.approval is None


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("schema_version", "9.0"),
        ("status", "approved"),
        ("version", 2),
    ],
)
def test_get_rejects_sql_envelope_drift(
    tmp_path: Path,
    column: str,
    value: object,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    plan = _minimal_plan()
    store.create(plan)

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            f"UPDATE plans SET {column} = ? WHERE plan_id = ?",  # noqa: S608 - fixed parametrization
            (value, str(plan.plan_id)),
        )

    with pytest.raises(StoredPlanCorruptError, match="envelope does not match"):
        store.get(plan.plan_id)


def test_get_rejects_plan_id_envelope_drift(tmp_path: Path) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    plan = _minimal_plan()
    store.create(plan)
    changed_id = UUID("00000000-0000-4000-8000-000000000099")

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE plans SET plan_id = ? WHERE plan_id = ?",
            (str(changed_id), str(plan.plan_id)),
        )

    with pytest.raises(StoredPlanCorruptError, match="envelope does not match"):
        store.get(changed_id)


@pytest.mark.parametrize("expected_version", (True, 1.0, "1", 0))
def test_approve_rejects_non_strict_expected_versions_without_mutation(
    tmp_path: Path,
    expected_version: object,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    plan = _minimal_plan()
    store.create(plan)

    with pytest.raises(ValueError, match="positive integer"):
        store.approve(
            plan.plan_id,
            approved_by="Reviewer",
            expected_version=expected_version,  # type: ignore[arg-type]
        )

    persisted = store.get(plan.plan_id)
    assert persisted.status is PlanStatus.PROPOSED
    assert persisted.version == 1
    assert persisted.approval is None


@pytest.mark.parametrize("stored_version", (True, "1", 1.0))
def test_get_rejects_non_strict_document_versions(
    tmp_path: Path,
    stored_version: object,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    plan = _minimal_plan()
    store.create(plan)

    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT document FROM plans WHERE plan_id = ?",
            (str(plan.plan_id),),
        ).fetchone()
        assert row is not None
        document = json.loads(row[0])
        document["version"] = stored_version
        connection.execute(
            "UPDATE plans SET document = ? WHERE plan_id = ?",
            (json.dumps(document), str(plan.plan_id)),
        )

    with pytest.raises(StoredPlanCorruptError, match="no longer satisfies"):
        store.get(plan.plan_id)


def test_initialize_rejects_an_incompatible_existing_plans_table(tmp_path: Path) -> None:
    database_path = tmp_path / "repopilot.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE plans (plan_id TEXT PRIMARY KEY)")

    with pytest.raises(StoredPlanCorruptError, match="schema is incompatible"):
        SQLitePlanStore(database_path).initialize()


@pytest.mark.parametrize(
    "status_and_version_columns",
    [
        "status TEXT NOT NULL, version INTEGER NOT NULL CHECK (version >= 1)",
        "status TEXT NOT NULL CHECK (status IN ('proposed', 'approved')), version INTEGER NOT NULL",
        "status TEXT NOT NULL CHECK "
        "(status IN ('proposed', 'approved', 'archived')), "
        "version INTEGER NOT NULL CHECK (version >= 1)",
        "status TEXT NOT NULL CHECK (status IN ('proposed', 'approved ')), "
        "version INTEGER NOT NULL CHECK (version >= 1)",
    ],
)
def test_initialize_rejects_missing_state_constraints(
    tmp_path: Path,
    status_and_version_columns: str,
) -> None:
    database_path = tmp_path / "repopilot.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            f"""
            CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                {status_and_version_columns},
                document TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """  # noqa: S608 - the two complete schema fragments are fixed parametrization
        )

    with pytest.raises(StoredPlanCorruptError, match="schema is incompatible"):
        SQLitePlanStore(database_path).initialize()


@pytest.mark.parametrize(
    "extra_constraint",
    (
        "CHECK (status != 'approved')",
        "CHECK (version != 2)",
        "CHECK (plan_id LIKE 'schema-probe-%')",
        "UNIQUE (status)",
        "FOREIGN KEY (status) REFERENCES allowed_statuses(value)",
        "status_copy TEXT GENERATED ALWAYS AS (status) VIRTUAL",
    ),
)
def test_initialize_rejects_extra_constraints_before_real_operations(
    tmp_path: Path,
    extra_constraint: str,
) -> None:
    database_path = tmp_path / "repopilot.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            f"""
            CREATE TABLE plans (
                plan_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('proposed', 'approved')),
                version INTEGER NOT NULL CHECK (version >= 1),
                document TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                {extra_constraint}
            )
            """  # noqa: S608 - complete constraints are fixed parametrization
        )

    with pytest.raises(StoredPlanCorruptError, match="schema is incompatible"):
        SQLitePlanStore(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)


def test_initialize_rejects_a_plan_trigger_before_running_schema_probes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "repopilot.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(storage_module._PLAN_TABLE_SQL)
        connection.execute("CREATE TABLE trigger_effects (value TEXT NOT NULL)")
        connection.execute(
            """
            CREATE TRIGGER plans_probe_effect
            BEFORE INSERT ON plans
            BEGIN
                INSERT INTO trigger_effects VALUES ('trigger ran');
            END
            """
        )

    with pytest.raises(StoredPlanCorruptError, match="schema is incompatible"):
        SQLitePlanStore(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM trigger_effects").fetchone() == (0,)


def test_initialize_rejects_an_explicit_plan_index(tmp_path: Path) -> None:
    database_path = tmp_path / "repopilot.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(storage_module._PLAN_TABLE_SQL)
        connection.execute("CREATE INDEX plans_status_index ON plans(status)")

    with pytest.raises(StoredPlanCorruptError, match="schema is incompatible"):
        SQLitePlanStore(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)


def test_initialize_rolls_back_the_complete_state_transition_probe(tmp_path: Path) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")

    store.initialize()

    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM plans").fetchone() == (0,)


@pytest.mark.skipif(os.name != "posix", reason="POSIX path semantics are required")
def test_constructor_freezes_relative_database_path_across_cwd_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    construction_directory = tmp_path / "construction"
    later_directory = tmp_path / "later"
    construction_directory.mkdir()
    later_directory.mkdir()
    monkeypatch.chdir(construction_directory)

    store = SQLitePlanStore(Path("private-store/repopilot.db"))
    expected_path = construction_directory / "private-store" / "repopilot.db"
    assert store.database_path == expected_path
    assert store.database_path.is_absolute()

    monkeypatch.chdir(later_directory)
    store.initialize()

    assert expected_path.is_file()
    assert not (later_directory / "private-store" / "repopilot.db").exists()
    store.healthcheck()


@pytest.mark.skipif(os.name != "posix", reason="POSIX path semantics are required")
def test_constructor_resolves_symlink_and_parent_segments_to_one_target(tmp_path: Path) -> None:
    physical_root = tmp_path / "physical"
    nested = physical_root / "nested"
    database_parent = physical_root / "private-store"
    nested.mkdir(parents=True)
    database_parent.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(nested, target_is_directory=True)

    store = SQLitePlanStore(alias / ".." / "private-store" / "repopilot.db")

    assert store.database_path == database_parent / "repopilot.db"
    store.initialize()
    assert store.database_path.is_file()


@pytest.mark.skipif(os.name != "posix", reason="POSIX path semantics are required")
def test_memory_sentinel_is_frozen_as_a_disk_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    store = SQLitePlanStore(Path(":memory:"))

    assert store.database_path == tmp_path / ":memory:"
    store.initialize()
    store.healthcheck()
    assert store.database_path.is_file()


@pytest.mark.skipif(os.name != "posix", reason="POSIX connection checks are required")
def test_healthcheck_closes_connection_after_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    real_connect = sqlite3.connect
    opened: list[TrackingConnection] = []

    class TrackingConnection(sqlite3.Connection):
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    def tracking_connect(*args, **kwargs) -> sqlite3.Connection:
        kwargs["factory"] = TrackingConnection
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(storage_module.sqlite3, "connect", tracking_connect)

    store.healthcheck()

    assert len(opened) == 1
    assert opened[0].close_calls == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


@pytest.mark.skipif(os.name != "posix", reason="POSIX connection checks are required")
def test_healthcheck_closes_connection_after_query_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    store.initialize()
    real_connect = sqlite3.connect
    opened: list[FailingQueryConnection] = []

    class FailingQueryConnection(sqlite3.Connection):
        close_calls = 0

        def execute(self, statement, *args, **kwargs):
            if statement == "SELECT 1":
                raise sqlite3.OperationalError("healthcheck query failed")
            return super().execute(statement, *args, **kwargs)

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    def failing_connect(*args, **kwargs) -> sqlite3.Connection:
        kwargs["factory"] = FailingQueryConnection
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(storage_module.sqlite3, "connect", failing_connect)

    with pytest.raises(sqlite3.OperationalError, match="healthcheck query failed"):
        store.healthcheck()

    assert len(opened) == 1
    assert opened[0].close_calls == 1


def test_open_sqlite_connection_closes_after_post_connect_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")
    real_connect = sqlite3.connect
    opened: list[FailingSetupConnection] = []

    class FailingSetupConnection(sqlite3.Connection):
        close_calls = 0

        def execute(self, statement, *args, **kwargs):
            if statement == "PRAGMA busy_timeout = 5000":
                raise sqlite3.OperationalError("connection setup failed")
            return super().execute(statement, *args, **kwargs)

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    def failing_connect(*args, **kwargs) -> sqlite3.Connection:
        kwargs["factory"] = FailingSetupConnection
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(storage_module.sqlite3, "connect", failing_connect)

    with pytest.raises(sqlite3.OperationalError, match="connection setup failed"):
        store._open_sqlite_connection()

    assert len(opened) == 1
    assert opened[0].close_calls == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode-bit semantics are required")
@pytest.mark.parametrize("permissive_umask", [0o000, 0o022])
def test_initialize_creates_database_owner_only_before_first_connect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    permissive_umask: int,
) -> None:
    database_path = tmp_path / "new.db"
    store = SQLitePlanStore(database_path)
    observed_connect_modes: list[int] = []
    real_connect = store._connect

    def connect_after_recording_mode() -> sqlite3.Connection:
        observed_connect_modes.append(stat.S_IMODE(database_path.stat().st_mode))
        return real_connect()

    monkeypatch.setattr(store, "_connect", connect_after_recording_mode)
    previous_umask = os.umask(permissive_umask)
    try:
        store.initialize()
    finally:
        os.umask(previous_umask)

    assert observed_connect_modes == [0o600]
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode-bit semantics are required")
def test_initialize_tightens_existing_database_permissions_without_losing_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "existing.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE existing_data (value TEXT NOT NULL)")
        connection.execute("INSERT INTO existing_data VALUES ('preserved')")
    os.chmod(database_path, 0o644)

    SQLitePlanStore(database_path).initialize()

    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT value FROM existing_data").fetchone()
    assert row == ("preserved",)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode-bit semantics are required")
def test_initialize_tightens_live_wal_sidecars_without_losing_data(tmp_path: Path) -> None:
    database_path = tmp_path / "live-wal.db"
    wal_path = Path(f"{database_path}-wal")
    shm_path = Path(f"{database_path}-shm")

    keeper = sqlite3.connect(database_path)
    try:
        assert keeper.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("CREATE TABLE existing_data (value TEXT NOT NULL)")
        keeper.execute("INSERT INTO existing_data VALUES ('preserved in wal')")
        keeper.commit()
        assert wal_path.is_file()
        assert shm_path.is_file()

        for path in (database_path, wal_path, shm_path):
            os.chmod(path, 0o644)

        SQLitePlanStore(database_path).initialize()

        for path in (database_path, wal_path, shm_path):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert keeper.execute("SELECT value FROM existing_data").fetchone() == ("preserved in wal",)
    finally:
        keeper.close()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM existing_data").fetchone() == (
            "preserved in wal",
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode-bit semantics are required")
def test_initialize_propagates_sidecar_permission_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "permission-error.db"
    with sqlite3.connect(database_path):
        pass
    wal_path = Path(f"{database_path}-wal")
    wal_path.touch()
    wal_identity = (wal_path.stat().st_dev, wal_path.stat().st_ino)
    real_fchmod = os.fchmod

    def fail_for_wal(descriptor: int, mode: int) -> None:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) == wal_identity:
            raise PermissionError("sidecar permission update denied")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(storage_module.os, "fchmod", fail_for_wal)

    with pytest.raises(PermissionError, match="unsafe SQLite storage path"):
        SQLitePlanStore(database_path).initialize()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode-bit semantics are required")
@pytest.mark.parametrize("permissive_umask", [0o000, 0o002, 0o022])
def test_initialize_creates_missing_database_directory_owner_only(
    tmp_path: Path,
    permissive_umask: int,
) -> None:
    database_parent = tmp_path / "private-store"
    database_path = database_parent / "repopilot.db"
    previous_umask = os.umask(permissive_umask)
    try:
        SQLitePlanStore(database_path).initialize()
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(database_parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX path semantics are required")
def test_initialize_rejects_missing_intermediate_database_directories(tmp_path: Path) -> None:
    missing_root = tmp_path / "one"

    with pytest.raises(PermissionError, match="unsafe SQLite storage path"):
        SQLitePlanStore(missing_root / "two" / "repopilot.db").initialize()

    assert not missing_root.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode-bit semantics are required")
@pytest.mark.parametrize("unsafe_mode", [0o770, 0o707, 0o775, 0o777])
def test_initialize_rejects_writable_parent_without_touching_database(
    tmp_path: Path,
    unsafe_mode: int,
) -> None:
    database_parent = tmp_path / "shared"
    database_parent.mkdir(mode=0o700)
    database_path = database_parent / "repopilot.db"
    database_path.write_bytes(b"sentinel")
    os.chmod(database_path, 0o644)
    before = database_path.stat()
    before_identity = (before.st_dev, before.st_ino)
    before_mode = stat.S_IMODE(before.st_mode)
    os.chmod(database_parent, unsafe_mode)
    try:
        with pytest.raises(PermissionError, match="unsafe SQLite storage path"):
            SQLitePlanStore(database_path).initialize()
    finally:
        os.chmod(database_parent, 0o700)

    after = database_path.stat()
    assert database_path.read_bytes() == b"sentinel"
    assert (after.st_dev, after.st_ino) == before_identity
    assert stat.S_IMODE(after.st_mode) == before_mode


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics are required")
def test_constructor_freezes_symlinked_parent_before_it_is_repointed(tmp_path: Path) -> None:
    original_parent = tmp_path / "original"
    replacement_parent = tmp_path / "replacement"
    original_parent.mkdir(mode=0o700)
    replacement_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(original_parent, target_is_directory=True)
    store = SQLitePlanStore(linked_parent / "repopilot.db")

    assert store.database_path == original_parent / "repopilot.db"
    linked_parent.unlink()
    linked_parent.symlink_to(replacement_parent, target_is_directory=True)

    store.initialize()

    assert (original_parent / "repopilot.db").is_file()
    assert not (replacement_parent / "repopilot.db").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics are required")
def test_initialize_rejects_symlink_database_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    target.write_bytes(b"sentinel")
    os.chmod(target, 0o644)
    before = target.stat()
    linked_database = tmp_path / "linked.db"
    linked_database.symlink_to(target)

    with pytest.raises(PermissionError, match="unsafe SQLite storage path"):
        SQLitePlanStore(linked_database).initialize()

    after = target.stat()
    assert target.read_bytes() == b"sentinel"
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert stat.S_IMODE(after.st_mode) == 0o644


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics are required")
@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_initialize_rejects_symlink_sidecar_before_connect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    suffix: str,
) -> None:
    database_path = tmp_path / "repopilot.db"
    with sqlite3.connect(database_path):
        pass
    os.chmod(database_path, 0o600)
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"untouched")
    os.chmod(sentinel, 0o644)
    before = sentinel.stat()
    Path(f"{database_path}{suffix}").symlink_to(sentinel)
    store = SQLitePlanStore(database_path)

    def forbidden_connect() -> sqlite3.Connection:
        raise AssertionError("connect must not run")

    monkeypatch.setattr(store, "_connect", forbidden_connect)

    with pytest.raises(PermissionError, match="unsafe SQLite storage path"):
        store.initialize()

    after = sentinel.stat()
    assert sentinel.read_bytes() == b"untouched"
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert stat.S_IMODE(after.st_mode) == 0o644


@pytest.mark.skipif(os.name != "posix", reason="POSIX hard-link semantics are required")
@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_initialize_rejects_hard_linked_sidecar_before_connect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    suffix: str,
) -> None:
    database_path = tmp_path / "repopilot.db"
    with sqlite3.connect(database_path):
        pass
    os.chmod(database_path, 0o600)
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"untouched")
    os.chmod(sentinel, 0o644)
    os.link(sentinel, Path(f"{database_path}{suffix}"))
    store = SQLitePlanStore(database_path)

    def forbidden_connect() -> sqlite3.Connection:
        raise AssertionError("connect must not run")

    monkeypatch.setattr(store, "_connect", forbidden_connect)

    with pytest.raises(PermissionError, match="unsafe SQLite storage path"):
        store.initialize()

    assert sentinel.read_bytes() == b"untouched"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o644
    assert sentinel.stat().st_nlink == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX hard-link semantics are required")
def test_initialize_rejects_hard_linked_database_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    target.write_bytes(b"sentinel")
    os.chmod(target, 0o644)
    database_path = tmp_path / "linked.db"
    os.link(target, database_path)

    with pytest.raises(PermissionError, match="unsafe SQLite storage path"):
        SQLitePlanStore(database_path).initialize()

    assert target.read_bytes() == b"sentinel"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert target.stat().st_nlink == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX inode semantics are required")
def test_initialize_rejects_database_inode_swap_after_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "repopilot.db"
    database_path.write_bytes(b"original")
    os.chmod(database_path, 0o644)
    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(b"replacement")
    os.chmod(replacement, 0o644)
    displaced = tmp_path / "displaced.db"
    original_identity = (database_path.stat().st_dev, database_path.stat().st_ino)
    replacement_identity = (replacement.stat().st_dev, replacement.stat().st_ino)
    real_open = os.open
    swapped = False

    def open_then_swap(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == database_path.name and dir_fd is not None and not swapped:
            swapped = True
            os.replace(database_path, displaced)
            os.replace(replacement, database_path)
        return descriptor

    monkeypatch.setattr(storage_module.os, "open", open_then_swap)
    monkeypatch.setattr(
        SQLitePlanStore,
        "_require_secure_posix_features",
        staticmethod(lambda: None),
    )

    with pytest.raises(PermissionError, match="unsafe SQLite storage path"):
        SQLitePlanStore(database_path).initialize()

    assert (displaced.stat().st_dev, displaced.stat().st_ino) == original_identity
    assert (database_path.stat().st_dev, database_path.stat().st_ino) == replacement_identity
    assert displaced.read_bytes() == b"original"
    assert database_path.read_bytes() == b"replacement"
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name != "posix", reason="POSIX WAL semantics are required")
def test_initialize_fails_when_connection_cannot_enter_wal_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLitePlanStore(tmp_path / "repopilot.db")

    def memory_connection() -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(store, "_connect", memory_connection)

    with pytest.raises(sqlite3.OperationalError, match="SQLite WAL mode is required"):
        store.initialize()


@pytest.mark.skipif(os.name != "posix", reason="POSIX link semantics are required")
@pytest.mark.parametrize("replacement_kind", ["symlink", "hardlink"])
def test_later_operation_rejects_replaced_database_before_sqlite_connect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    database_path = tmp_path / "repopilot.db"
    store = SQLitePlanStore(database_path)
    store.initialize()
    displaced = tmp_path / "displaced.db"
    database_path.replace(displaced)
    replacement = tmp_path / "replacement.db"
    with sqlite3.connect(replacement):
        pass
    os.chmod(replacement, 0o600)
    if replacement_kind == "symlink":
        database_path.symlink_to(replacement)
    else:
        os.link(replacement, database_path)

    def forbidden_connect(*args, **kwargs) -> sqlite3.Connection:
        raise AssertionError("sqlite3.connect must not run")

    monkeypatch.setattr(storage_module.sqlite3, "connect", forbidden_connect)

    with pytest.raises(PermissionError, match="unsafe SQLite storage path"):
        store.healthcheck()


@pytest.mark.skipif(os.name != "posix", reason="POSIX link semantics are required")
@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
@pytest.mark.parametrize("replacement_kind", ["symlink", "hardlink"])
def test_later_operation_rejects_injected_sidecar_before_sqlite_connect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    suffix: str,
    replacement_kind: str,
) -> None:
    database_path = tmp_path / "repopilot.db"
    store = SQLitePlanStore(database_path)
    store.initialize()
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"untouched")
    os.chmod(sentinel, 0o600)
    sidecar_path = Path(f"{database_path}{suffix}")
    sidecar_path.unlink(missing_ok=True)
    if replacement_kind == "symlink":
        sidecar_path.symlink_to(sentinel)
    else:
        os.link(sentinel, sidecar_path)

    def forbidden_connect(*args, **kwargs) -> sqlite3.Connection:
        raise AssertionError("sqlite3.connect must not run")

    monkeypatch.setattr(storage_module.sqlite3, "connect", forbidden_connect)

    with pytest.raises(PermissionError, match="unsafe SQLite storage path"):
        store.healthcheck()

    assert sentinel.read_bytes() == b"untouched"

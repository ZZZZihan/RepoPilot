"""SQLite-backed authoritative plan store and approval state transition."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import UUID, uuid4

from pydantic import ValidationError

from repopilot.errors import (
    InvalidPlanTransitionError,
    PlanNotFoundError,
    PlanVersionConflictError,
    StoredPlanCorruptError,
)
from repopilot.models import ApprovalRecord, ImplementationPlan, PlanStatus

_STORAGE_PATH_ERROR = "unsafe SQLite storage path"
_STORAGE_SCHEMA_ERROR = "SQLite plans schema is incompatible"
_PLAN_COLUMNS = (
    ("plan_id", "TEXT", 0, None, 1, 0),
    ("schema_version", "TEXT", 1, None, 0, 0),
    ("status", "TEXT", 1, None, 0, 0),
    ("version", "INTEGER", 1, None, 0, 0),
    ("document", "TEXT", 1, None, 0, 0),
    ("created_at", "TEXT", 1, None, 0, 0),
    ("updated_at", "TEXT", 1, None, 0, 0),
)
_PLAN_TABLE_SQL = (
    "CREATE TABLE plans (\n"
    "                    plan_id TEXT PRIMARY KEY,\n"
    "                    schema_version TEXT NOT NULL,\n"
    "                    status TEXT NOT NULL CHECK (status IN ('proposed', 'approved')),\n"
    "                    version INTEGER NOT NULL CHECK (version >= 1),\n"
    "                    document TEXT NOT NULL,\n"
    "                    created_at TEXT NOT NULL,\n"
    "                    updated_at TEXT NOT NULL\n"
    "                )"
)
_CREATE_PLAN_TABLE_SQL = _PLAN_TABLE_SQL.replace(
    "CREATE TABLE plans",
    "CREATE TABLE IF NOT EXISTS plans",
    1,
)
_PLAN_SCHEMA_OBJECTS = (
    ("index", "sqlite_autoindex_plans_1", None),
    ("table", "plans", _PLAN_TABLE_SQL),
)
_PLAN_INDEXES = (("sqlite_autoindex_plans_1", 1, "pk", 0),)


def utc_now() -> datetime:
    return datetime.now(UTC)


class SQLitePlanStore:
    """Persist validated plans and own their one legal state transition.

    POSIX checks fail closed for static unsafe links, owners, modes, and observable
    inode changes. They are not an isolation boundary against a malicious same-EUID
    process racing pathname replacement, do not evaluate macOS extended ACL entries,
    and do not imply equivalent link or permission guarantees on non-POSIX systems.
    """

    def __init__(self, database_path: Path) -> None:
        # Freeze the parent at construction time so later cwd changes or a
        # repointed parent symlink cannot silently redirect this store.  Keep
        # the final component unresolved: POSIX initialization must inspect it
        # with no-follow semantics before SQLite is allowed to open it.
        self._database_path = database_path.parent.resolve(strict=False) / database_path.name

    @property
    def database_path(self) -> Path:
        return self._database_path

    def initialize(self) -> None:
        if os.name == "posix":
            self._initialize_posix()
            return

        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            self._initialize_schema(connection)

    def _initialize_posix(self) -> None:
        directory_fd: int | None = None
        database_fd: int | None = None
        try:
            self._require_secure_posix_features()
            directory_fd = self._open_secure_database_directory(create=True)
            database_fd = self._open_secure_regular_file(
                directory_fd,
                self._database_path.name,
                create=True,
            )
            if database_fd is None:
                self._raise_unsafe_storage_path()
            database_stat = os.fstat(database_fd)
            self._restrict_sidecars(directory_fd, self._database_path.name)

            with closing(self._connect()) as connection, connection:
                self._assert_named_inode(
                    directory_fd,
                    self._database_path.name,
                    database_stat,
                )
                self._initialize_schema(connection)
                self._assert_named_inode(
                    directory_fd,
                    self._database_path.name,
                    database_stat,
                )
                self._restrict_sidecars(directory_fd, self._database_path.name)

            self._assert_named_inode(
                directory_fd,
                self._database_path.name,
                database_stat,
            )
            self._restrict_sidecars(directory_fd, self._database_path.name)
        except PermissionError as exc:
            if str(exc) == _STORAGE_PATH_ERROR:
                raise
            raise PermissionError(_STORAGE_PATH_ERROR) from None
        except (NotImplementedError, OSError, ValueError):
            raise PermissionError(_STORAGE_PATH_ERROR) from None
        finally:
            if database_fd is not None:
                os.close(database_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "wal":
            raise sqlite3.OperationalError("SQLite WAL mode is required")
        try:
            connection.execute(_CREATE_PLAN_TABLE_SQL)
            SQLitePlanStore._validate_schema(connection)
        except StoredPlanCorruptError:
            raise
        except sqlite3.DatabaseError as exc:
            raise StoredPlanCorruptError(_STORAGE_SCHEMA_ERROR) from exc

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        rows = connection.execute("PRAGMA table_xinfo(plans)").fetchall()
        columns = tuple(
            (
                str(row[1]),
                str(row[2]).upper(),
                int(row[3]),
                None if row[4] is None else str(row[4]),
                int(row[5]),
                int(row[6]),
            )
            for row in rows
        )
        if columns != _PLAN_COLUMNS:
            raise StoredPlanCorruptError(_STORAGE_SCHEMA_ERROR)

        schema_objects = tuple(
            (str(row[0]), str(row[1]), row[2])
            for row in connection.execute(
                """
                SELECT type, name, sql
                FROM sqlite_master
                WHERE tbl_name = 'plans'
                ORDER BY type, name
                """
            ).fetchall()
        )
        if schema_objects != _PLAN_SCHEMA_OBJECTS:
            raise StoredPlanCorruptError(_STORAGE_SCHEMA_ERROR)
        indexes = tuple(
            (str(row[1]), int(row[2]), str(row[3]), int(row[4]))
            for row in connection.execute("PRAGMA index_list(plans)").fetchall()
        )
        if indexes != _PLAN_INDEXES:
            raise StoredPlanCorruptError(_STORAGE_SCHEMA_ERROR)
        if connection.execute("PRAGMA foreign_key_list(plans)").fetchall():
            raise StoredPlanCorruptError(_STORAGE_SCHEMA_ERROR)

        connection.execute("SAVEPOINT repopilot_schema_validation")
        try:
            probe_id = SQLitePlanStore._insert_schema_probe(
                connection,
                status="proposed",
                version=1,
            )
            timestamp = "1970-01-01T00:00:00+00:00"
            cursor = connection.execute(
                """
                UPDATE plans
                SET status = ?, version = ?, document = ?, updated_at = ?
                WHERE plan_id = ? AND version = ? AND status = 'proposed'
                """,
                ("approved", 2, "{}", timestamp, probe_id, 1),
            )
            transitioned = connection.execute(
                "SELECT status, version FROM plans WHERE plan_id = ?",
                (probe_id,),
            ).fetchone()
            if (
                cursor.rowcount != 1
                or transitioned is None
                or tuple(transitioned)
                != (
                    "approved",
                    2,
                )
            ):
                raise StoredPlanCorruptError(_STORAGE_SCHEMA_ERROR)
            for status, version in (("invalid", 1), ("proposed", 0)):
                try:
                    SQLitePlanStore._insert_schema_probe(
                        connection,
                        status=status,
                        version=version,
                    )
                except sqlite3.IntegrityError:
                    continue
                raise StoredPlanCorruptError(_STORAGE_SCHEMA_ERROR)
        finally:
            connection.execute("ROLLBACK TO repopilot_schema_validation")
            connection.execute("RELEASE repopilot_schema_validation")

    @staticmethod
    def _insert_schema_probe(
        connection: sqlite3.Connection,
        *,
        status: str,
        version: int,
    ) -> str:
        timestamp = "1970-01-01T00:00:00+00:00"
        probe_id = f"schema-probe-{uuid4()}"
        connection.execute(
            """
            INSERT INTO plans (
                plan_id, schema_version, status, version, document, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                probe_id,
                "1.0",
                status,
                version,
                "{}",
                timestamp,
                timestamp,
            ),
        )
        return probe_id

    @staticmethod
    def _require_secure_posix_features() -> None:
        required = (
            hasattr(os, "O_NOFOLLOW"),
            hasattr(os, "O_DIRECTORY"),
            hasattr(os, "fchmod"),
            os.open in os.supports_dir_fd,
            os.stat in os.supports_dir_fd,
            os.stat in os.supports_follow_symlinks,
            os.mkdir in os.supports_dir_fd,
        )
        if not all(required):
            SQLitePlanStore._raise_unsafe_storage_path()

    @staticmethod
    def _raise_unsafe_storage_path() -> NoReturn:
        raise PermissionError(_STORAGE_PATH_ERROR)

    @staticmethod
    def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)

    @classmethod
    def _validate_directory_stat(
        cls,
        value: os.stat_result,
        *,
        final_parent: bool,
    ) -> None:
        if not stat.S_ISDIR(value.st_mode) or value.st_uid not in {0, os.geteuid()}:
            cls._raise_unsafe_storage_path()
        writable_by_others = stat.S_IMODE(value.st_mode) & 0o022
        if final_parent:
            if value.st_uid != os.geteuid() or writable_by_others:
                cls._raise_unsafe_storage_path()
        elif writable_by_others and not value.st_mode & stat.S_ISVTX:
            cls._raise_unsafe_storage_path()

    @classmethod
    def _validate_regular_file_stat(cls, value: os.stat_result) -> None:
        if not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid() or value.st_nlink != 1:
            cls._raise_unsafe_storage_path()

    @classmethod
    def _validate_ancestor_chain(cls, directory: Path, *, final_parent: bool = True) -> None:
        absolute = Path(os.path.abspath(directory))
        current = Path(absolute.anchor)
        cls._validate_directory_stat(
            os.lstat(current),
            final_parent=final_parent and current == absolute,
        )
        for part in absolute.parts[1:]:
            current /= part
            value = os.lstat(current)
            cls._validate_directory_stat(
                value,
                final_parent=final_parent and current == absolute,
            )

    @classmethod
    def _open_verified_directory(cls, path: Path, *, final_parent: bool) -> int:
        before = os.lstat(path)
        cls._validate_directory_stat(before, final_parent=final_parent)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            cls._validate_directory_stat(opened, final_parent=final_parent)
            if not cls._same_inode(before, opened):
                cls._raise_unsafe_storage_path()
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_secure_database_directory(self, *, create: bool) -> int:
        database_name = self._database_path.name
        if database_name in {"", ".", ".."}:
            self._raise_unsafe_storage_path()

        parent = Path(os.path.abspath(self._database_path.parent))
        if parent == Path(parent.anchor):
            self._raise_unsafe_storage_path()

        try:
            os.lstat(parent)
        except FileNotFoundError:
            if not create:
                self._raise_unsafe_storage_path()
            anchor = parent.parent
            self._validate_ancestor_chain(anchor, final_parent=False)
            anchor_fd = self._open_verified_directory(anchor, final_parent=False)
            try:
                try:
                    os.mkdir(parent.name, 0o700, dir_fd=anchor_fd)
                except FileExistsError:
                    pass
            finally:
                os.close(anchor_fd)

        self._validate_ancestor_chain(parent)
        descriptor = self._open_verified_directory(parent, final_parent=True)
        opened = os.fstat(descriptor)
        if stat.S_IMODE(opened.st_mode) & 0o022:
            os.close(descriptor)
            self._raise_unsafe_storage_path()
        return descriptor

    @classmethod
    def _open_secure_regular_file(
        cls,
        directory_fd: int,
        name: str,
        *,
        create: bool,
    ) -> int | None:
        if name in {"", ".", ".."} or "/" in name:
            cls._raise_unsafe_storage_path()
        flags = os.O_RDWR | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC

        before: os.stat_result | None
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                return None
            before = None

        descriptor: int
        if before is None:
            try:
                descriptor = os.open(
                    name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                cls._validate_regular_file_stat(before)
                descriptor = os.open(name, flags, dir_fd=directory_fd)
        else:
            cls._validate_regular_file_stat(before)
            descriptor = os.open(name, flags, dir_fd=directory_fd)

        try:
            opened = os.fstat(descriptor)
            cls._validate_regular_file_stat(opened)
            if before is not None and not cls._same_inode(before, opened):
                cls._raise_unsafe_storage_path()
            cls._assert_named_inode(directory_fd, name, opened)
            os.fchmod(descriptor, 0o600)
            restricted = os.fstat(descriptor)
            cls._validate_regular_file_stat(restricted)
            if stat.S_IMODE(restricted.st_mode) != 0o600:
                cls._raise_unsafe_storage_path()
            cls._assert_named_inode(directory_fd, name, restricted)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @classmethod
    def _assert_named_inode(
        cls,
        directory_fd: int,
        name: str,
        opened: os.stat_result,
    ) -> None:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        cls._validate_regular_file_stat(current)
        if not cls._same_inode(current, opened):
            cls._raise_unsafe_storage_path()

    @classmethod
    def _restrict_sidecars(cls, directory_fd: int, database_name: str | None = None) -> None:
        if database_name is None:
            raise ValueError("database name is required")
        for suffix in ("-journal", "-wal", "-shm"):
            descriptor = cls._open_secure_regular_file(
                directory_fd,
                f"{database_name}{suffix}",
                create=False,
            )
            if descriptor is not None:
                os.close(descriptor)

    def create(self, plan: ImplementationPlan) -> None:
        validated = ImplementationPlan.model_validate(plan.model_dump(mode="python"))
        if (
            validated.status is not PlanStatus.PROPOSED
            or validated.version != 1
            or validated.approval is not None
        ):
            raise InvalidPlanTransitionError(
                "plan creation requires proposed version 1 with no approval record"
            )
        serialized = validated.model_dump_json()
        timestamp = validated.created_at.isoformat()
        with closing(self._connect()) as connection, connection:
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
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT plan_id, schema_version, status, version, document
                FROM plans
                WHERE plan_id = ?
                """,
                (str(plan_id),),
            ).fetchone()
        if row is None:
            raise PlanNotFoundError(f"plan {plan_id} was not found")
        return self._deserialize(row)

    def approve(
        self,
        plan_id: UUID,
        *,
        approved_by: str,
        expected_version: int,
        approved_at: datetime | None = None,
    ) -> ImplementationPlan:
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise ValueError("expected_version must be a positive integer")
        if approved_at is not None and not isinstance(approved_at, datetime):
            raise ValueError("approved_at must be a datetime or None")
        transition_time = utc_now() if approved_at is None else approved_at
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT plan_id, schema_version, status, version, document
                FROM plans
                WHERE plan_id = ?
                """,
                (str(plan_id),),
            ).fetchone()
            if row is None:
                raise PlanNotFoundError(f"plan {plan_id} was not found")

            current = self._deserialize(row)
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
        with closing(self._connect()) as connection, connection:
            connection.execute("SELECT 1").fetchone()

    def _connect(self) -> sqlite3.Connection:
        if os.name == "posix":
            return self._connect_posix()
        return self._open_sqlite_connection()

    def _connect_posix(self) -> sqlite3.Connection:
        directory_fd: int | None = None
        database_fd: int | None = None
        connection: sqlite3.Connection | None = None
        try:
            self._require_secure_posix_features()
            directory_fd = self._open_secure_database_directory(create=False)
            database_fd = self._open_secure_regular_file(
                directory_fd,
                self._database_path.name,
                create=False,
            )
            if database_fd is None:
                self._raise_unsafe_storage_path()
            database_stat = os.fstat(database_fd)
            self._restrict_sidecars(directory_fd, self._database_path.name)

            connection = self._open_sqlite_connection()
            self._assert_named_inode(
                directory_fd,
                self._database_path.name,
                database_stat,
            )
            self._restrict_sidecars(directory_fd, self._database_path.name)
            return connection
        except PermissionError as exc:
            if connection is not None:
                connection.close()
            if str(exc) == _STORAGE_PATH_ERROR:
                raise
            raise PermissionError(_STORAGE_PATH_ERROR) from None
        except (NotImplementedError, OSError, ValueError):
            if connection is not None:
                connection.close()
            raise PermissionError(_STORAGE_PATH_ERROR) from None
        finally:
            if database_fd is not None:
                os.close(database_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    def _open_sqlite_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _deserialize(row: sqlite3.Row) -> ImplementationPlan:
        document = row["document"]
        if not isinstance(document, str):
            raise StoredPlanCorruptError("stored plan document must be SQLite TEXT")
        try:
            plan = ImplementationPlan.model_validate_json(document)
        except ValidationError as exc:
            raise StoredPlanCorruptError(
                "stored plan no longer satisfies the implementation-plan schema"
            ) from exc
        envelope = (
            row["plan_id"],
            row["schema_version"],
            row["status"],
            row["version"],
        )
        expected = (
            str(plan.plan_id),
            plan.schema_version,
            plan.status.value,
            plan.version,
        )
        if envelope != expected:
            raise StoredPlanCorruptError("stored plan envelope does not match its document")
        return plan

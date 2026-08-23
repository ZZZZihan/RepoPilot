from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from repopilot.storage import SQLitePlanStore


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

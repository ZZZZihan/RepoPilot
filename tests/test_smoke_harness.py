from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest


def _load_smoke_module() -> ModuleType:
    script_path = Path(__file__).parents[1] / "scripts" / "m0_http_persistence_smoke.py"
    spec = importlib.util.spec_from_file_location("repopilot_m0_smoke", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the M0 smoke harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke_module()


def _valid_pass_capsule(
    expected_source: dict[str, object],
    *,
    include_outer_cleanup: bool = False,
) -> dict[str, object]:
    cleanup = dict(smoke.EXPECTED_INNER_CLEANUP)
    if include_outer_cleanup:
        cleanup.update(smoke.EXPECTED_OUTER_CLEANUP)
    return {
        "evidence_capsule_version": smoke.EVIDENCE_CAPSULE_VERSION,
        "gate": "M0-03",
        "observed_at": datetime.now(UTC).isoformat(),
        "status": "PASS",
        "source": dict(expected_source),
        "runtime": dict(smoke.EXPECTED_RUNTIME_EVIDENCE),
        "http": dict(smoke.EXPECTED_HTTP_EVIDENCE),
        "responses": {
            "response_sha256": "1" * 64,
            "approved_sha256": "2" * 64,
            "restart_read_sha256": "2" * 64,
            "restart_response_matches": True,
        },
        "sqlite": dict(smoke.EXPECTED_SQLITE_EVIDENCE),
        "semantic_checks": {
            "evidence_ids_unique": True,
            "evidence_references_valid": True,
            "evidence_count": 2,
            "step_count": 1,
            "step_evidence_reference_count": 1,
            "verification_evidence_reference_count": 1,
        },
        "cleanup": cleanup,
    }


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_snapshot_repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "-q")
    files = {
        "scripts/m0_http_persistence_smoke.py": "print('snapshot A')\n",
        "src/repopilot/__init__.py": "VERSION = 'A'\n",
        "tests/fixtures/tiny_python_repo/README.md": "fixture A\n",
        "uv.lock": "lock A\n",
    }
    for relative, content in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "user.name=RepoPilot Test",
        "-c",
        "user.email=repopilot@example.invalid",
        "commit",
        "-qm",
        "snapshot A",
    )
    return repository


class _CleanExitProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.stdout = None
        self.stderr = None

    def poll(self):
        return self.returncode

    def wait(self, *, timeout: float):
        self.returncode = 0
        return self.returncode

    def send_signal(self, signal_number: int) -> None:
        raise AssertionError(f"unexpected signal fallback: {signal_number}")

    def kill(self) -> None:
        raise AssertionError("unexpected kill fallback")


def test_uvicorn_child_normal_stop_uses_only_the_control_pipe(tmp_path: Path) -> None:
    child = smoke.UvicornChild(
        script_path=tmp_path / "smoke.py",
        source_root=tmp_path / "src",
        database_path=tmp_path / "smoke.db",
        fixture_root=tmp_path / "fixture",
    )
    process = _CleanExitProcess()
    shutdown_read_fd, shutdown_write_fd = os.pipe()
    child.process = process
    child._shutdown_write_fd = shutdown_write_fd

    try:
        result = child.stop()
    finally:
        os.close(shutdown_read_fd)

    assert result == {
        "started": True,
        "process_stopped": True,
        "port_closed": True,
        "exit_code": 0,
        "control_pipe_requested": True,
        "signal_fallback": False,
        "kill_fallback": False,
        "graceful": True,
    }


def test_child_environment_isolates_snapshot_source_and_removes_context_overrides(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "snapshot" / "src"
    monkeypatch.setenv("PYTHONPATH", "/existing/path")
    monkeypatch.setenv("PYTHONHOME", "/external/python-home")
    monkeypatch.setenv("PYTHONBREAKPOINT", "external.debugger")
    monkeypatch.setenv("GIT_COMMON_DIR", "/external/git-common-dir")
    monkeypatch.setenv("GIT_NAMESPACE", "external-namespace")
    for name in smoke.GITHUB_TOKEN_ENVIRONMENT_VARIABLES:
        monkeypatch.setenv(name, "secret")

    environment = smoke.child_environment(source_root)

    assert environment["PYTHONPATH"] == str(source_root)
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONSAFEPATH"] == "1"
    assert all(name not in environment for name in smoke.GITHUB_TOKEN_ENVIRONMENT_VARIABLES)
    assert not any(name.startswith("GIT_") for name in environment)
    assert environment == {**smoke.FIXED_CHILD_ENVIRONMENT, "PYTHONPATH": str(source_root)}


def test_git_environment_removes_every_git_context_override(monkeypatch) -> None:
    monkeypatch.setenv("GIT_DIR", "/external/repository")
    monkeypatch.setenv("GIT_COMMON_DIR", "/external/common")
    monkeypatch.setenv("GIT_NAMESPACE", "external-namespace")
    monkeypatch.setenv("REPOPILOT_UNRELATED", "preserved")

    environment = smoke.git_environment()

    assert environment == {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
    }
    assert "REPOPILOT_UNRELATED" not in environment


def test_snapshot_orchestrator_timeout_is_redacted_and_records_source_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repository = _create_snapshot_repository(tmp_path)
    script_path = repository / "scripts" / "m0_http_persistence_smoke.py"

    real_process_group = smoke.run_bounded_process_group

    def timeout_process_group(command, **kwargs):
        if command[0] == "git":
            return real_process_group(command, **kwargs)
        return None, {
            "timed_out": True,
            "output_limit_exceeded": False,
            "unexpected_descendants": False,
            "sigterm_used": True,
            "sigkill_used": True,
            "process_group_empty": True,
        }

    monkeypatch.setattr(smoke, "run_bounded_process_group", timeout_process_group)

    return_code, output = smoke.run_smoke(repository, script_path)
    capsule = json.loads(output)

    assert return_code == 1
    assert capsule["status"] == "FAIL"
    assert capsule["failure"] == {
        "stage": "snapshot_orchestrator",
        "type": "SnapshotOrchestratorTimeoutError",
    }
    assert capsule["cleanup"]["snapshot_orchestrator_timed_out"] is True
    assert capsule["cleanup"]["snapshot_orchestrator_output_limit_exceeded"] is False
    assert capsule["cleanup"]["snapshot_orchestrator_sigterm"] is True
    assert capsule["cleanup"]["snapshot_orchestrator_sigkill"] is True
    assert capsule["cleanup"]["snapshot_orchestrator_process_group_empty"] is True
    assert capsule["cleanup"]["source_snapshot_removed"] is True
    assert capsule["cleanup"]["source_temporary_directory_removed"] is True
    assert str(repository).encode() not in output


def test_bounded_process_group_starts_a_new_session(tmp_path: Path) -> None:
    result, cleanup = smoke.run_bounded_process_group(
        [
            sys.executable,
            "-c",
            "import os,sys; sys.stdout.write(str(os.getpid() == os.getpgrp()))",
        ],
        cwd=tmp_path,
        environment=smoke.FIXED_CHILD_ENVIRONMENT,
        timeout=3.0,
    )

    assert result is not None
    assert result.returncode == 0
    assert result.stdout == b"True"
    assert cleanup == {
        "timed_out": False,
        "output_limit_exceeded": False,
        "unexpected_descendants": False,
        "sigterm_used": False,
        "sigkill_used": False,
        "process_group_empty": True,
    }


def test_bounded_process_group_fails_closed_at_stdout_limit(tmp_path: Path) -> None:
    result, cleanup = smoke.run_bounded_process_group(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 4096)"],
        cwd=tmp_path,
        environment=smoke.FIXED_CHILD_ENVIRONMENT,
        timeout=3.0,
        max_stdout_bytes=32,
        max_stderr_bytes=32,
    )

    assert result is None
    assert cleanup["timed_out"] is False
    assert cleanup["output_limit_exceeded"] is True
    assert cleanup["process_group_empty"] is True


def test_pass_capsule_validation_is_exact_and_reconstructs() -> None:
    expected_source = {key: key for key in smoke.PASS_SOURCE_KEYS}
    candidate = _valid_pass_capsule(expected_source)

    validated = smoke.validate_pass_capsule(
        candidate,
        expected_source=expected_source,
        include_outer_cleanup=False,
    )

    assert validated == candidate
    candidate["unexpected_extension"] = "must not pass"
    with pytest.raises(smoke.SmokeFailure, match="versioned Evidence Capsule schema"):
        smoke.validate_pass_capsule(
            candidate,
            expected_source=expected_source,
            include_outer_cleanup=False,
        )


def test_outer_rejects_and_redacts_a_forged_minimal_pass(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repository = _create_snapshot_repository(tmp_path)
    script_path = repository / "scripts" / "m0_http_persistence_smoke.py"
    real_process_group = smoke.run_bounded_process_group
    forbidden = "outer-forged-secret-marker"

    def forged_or_real(command, **kwargs):
        if command[0] == "git":
            return real_process_group(command, **kwargs)
        result = subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"status": "PASS", "secret": forbidden}).encode(),
            b"sensitive stderr must not be forwarded",
        )
        return result, {
            "timed_out": False,
            "output_limit_exceeded": False,
            "unexpected_descendants": False,
            "sigterm_used": False,
            "sigkill_used": False,
            "process_group_empty": True,
        }

    monkeypatch.setattr(smoke, "run_bounded_process_group", forged_or_real)

    return_code, output = smoke.run_smoke(repository, script_path)
    capsule = json.loads(output)

    assert return_code == 1
    assert capsule["failure"] == {
        "stage": "snapshot_orchestrator",
        "type": "InvalidEvidenceCapsuleError",
    }
    assert forbidden.encode() not in output
    assert b"sensitive stderr" not in output


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are required")
def test_timeout_kills_a_sigterm_ignoring_descendant_and_closes_its_port(
    tmp_path: Path,
) -> None:
    metadata_path = tmp_path / "descendant.json"
    ready_path = tmp_path / "descendant.ready"
    descendant_code = (
        "import signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(sys.argv[1]).write_text('ready', encoding='utf-8'); "
        "time.sleep(60)"
    )
    leader_code = (
        "import json,socket,subprocess,sys,time; from pathlib import Path; "
        "listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM); "
        "listener.bind(('127.0.0.1',0)); listener.listen(1); "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[3],sys.argv[2]], "
        "pass_fds=(listener.fileno(),)); "
        "deadline=time.monotonic()+5; "
        "\nwhile not Path(sys.argv[2]).exists() and time.monotonic()<deadline: time.sleep(0.01)"
        "\nif not Path(sys.argv[2]).exists(): raise SystemExit(72)"
        "\nPath(sys.argv[1]).write_text("
        "json.dumps({'pid':child.pid,'port':listener.getsockname()[1]}), encoding='utf-8')"
        "\nraise SystemExit(0)"
    )

    result, cleanup = smoke.run_bounded_process_group(
        [
            sys.executable,
            "-c",
            leader_code,
            str(metadata_path),
            str(ready_path),
            descendant_code,
        ],
        cwd=tmp_path,
        environment=os.environ.copy(),
        timeout=1.0,
    )

    assert result is None
    assert cleanup["timed_out"] is True
    assert cleanup["sigterm_used"] is True
    assert cleanup["sigkill_used"] is True
    assert cleanup["process_group_empty"] is True
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    descendant_pid = int(metadata["pid"])
    port = int(metadata["port"])
    deadline = time.monotonic() + smoke.PROCESS_GROUP_KILL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("SIGKILL process-group fallback left a descendant alive")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        assert probe.connect_ex((smoke.HOST, port)) != 0


def test_snapshot_orchestrator_timeout_covers_internal_operation_bounds() -> None:
    assert smoke.SNAPSHOT_ORCHESTRATOR_TIMEOUT_SECONDS == 210.0


def test_snapshot_uses_captured_commit_after_live_tree_advances(tmp_path: Path) -> None:
    repository = _create_snapshot_repository(tmp_path)
    captured_commit, captured_tree, clean = smoke.capture_source_identity(repository)
    assert clean is True

    live_script = repository / "scripts" / "m0_http_persistence_smoke.py"
    live_script.write_text("print('snapshot B')\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "user.name=RepoPilot Test",
        "-c",
        "user.email=repopilot@example.invalid",
        "commit",
        "-qm",
        "snapshot B",
    )
    assert _git(repository, "rev-parse", "HEAD") != captured_commit

    snapshot_root = tmp_path / "snapshot"
    smoke.materialize_source_snapshot(repository, captured_commit, snapshot_root)

    assert (snapshot_root / "scripts" / "m0_http_persistence_smoke.py").read_text() == (
        "print('snapshot A')\n"
    )
    assert _git(repository, "rev-parse", f"{captured_commit}^{{tree}}") == captured_tree


@pytest.mark.skipif(os.name != "posix", reason="Git symlink semantics are required")
def test_snapshot_rejects_tracked_symlink(tmp_path: Path) -> None:
    repository = _create_snapshot_repository(tmp_path)
    (repository / "src" / "repopilot" / "escape").symlink_to("/tmp/outside")
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "user.name=RepoPilot Test",
        "-c",
        "user.email=repopilot@example.invalid",
        "commit",
        "-qm",
        "add symlink",
    )
    commit = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(smoke.SmokeFailure, match="unsafe archive member"):
        smoke.materialize_source_snapshot(repository, commit, tmp_path / "snapshot")


def test_snapshot_archive_enforces_member_limit(monkeypatch, tmp_path: Path) -> None:
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:") as archive:
        for name in ("uv.lock", "scripts/m0_http_persistence_smoke.py"):
            payload = b"x"
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))

    monkeypatch.setattr(smoke, "MAX_SNAPSHOT_ARCHIVE_MEMBERS", 1)
    monkeypatch.setattr(smoke, "_bounded_git", lambda *_, **__: archive_buffer.getvalue())

    with pytest.raises(smoke.SmokeFailure, match="member limit"):
        smoke.materialize_source_snapshot(tmp_path, "a" * 40, tmp_path / "snapshot")


def test_snapshot_manifest_enforces_file_and_total_byte_limits(
    monkeypatch,
    tmp_path: Path,
) -> None:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "oversized").write_bytes(b"1234")
    monkeypatch.setattr(smoke, "MAX_SNAPSHOT_FILE_BYTES", 3)

    with pytest.raises(smoke.SmokeFailure, match="byte limit"):
        smoke.snapshot_manifest(snapshot_root)


def test_snapshot_manifest_changes_with_used_file_content(tmp_path: Path) -> None:
    repository = _create_snapshot_repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    snapshot_root = tmp_path / "snapshot"
    smoke.materialize_source_snapshot(repository, commit, snapshot_root)
    before, file_count = smoke.snapshot_manifest(snapshot_root)

    (snapshot_root / "src" / "repopilot" / "__init__.py").write_text(
        "VERSION = 'changed'\n",
        encoding="utf-8",
    )
    after, after_file_count = smoke.snapshot_manifest(snapshot_root)

    assert before != after
    assert file_count == after_file_count == 4


def _create_approved_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("CREATE TABLE plans (status TEXT NOT NULL, version INTEGER NOT NULL)")
        connection.execute("INSERT INTO plans VALUES ('approved', 2)")
        connection.commit()
    finally:
        connection.close()
    for database_file in path.parent.glob(f"{path.name}*"):
        database_file.chmod(0o600)


def test_database_validation_checks_private_inode_and_sidecars(tmp_path: Path) -> None:
    database_path = tmp_path / "repopilot.db"
    _create_approved_database(database_path)

    assert smoke.check_database(database_path) == smoke.EXPECTED_SQLITE_EVIDENCE

    alias = tmp_path / "database-hardlink"
    os.link(database_path, alias)
    with pytest.raises(smoke.SmokeFailure, match="private-file contract"):
        smoke.check_database(database_path)
    alias.unlink()

    sidecar_target = tmp_path / "sidecar-target"
    sidecar_target.write_bytes(b"not a sidecar")
    sidecar_target.chmod(0o600)
    wal_path = Path(f"{database_path}-wal")
    wal_path.unlink(missing_ok=True)
    wal_path.symlink_to(sidecar_target)
    with pytest.raises(smoke.SmokeFailure, match="private-file contract"):
        smoke.check_database(database_path)


def test_snapshot_orchestrator_rejects_forged_manifest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repository = _create_snapshot_repository(tmp_path)
    commit, tree, _ = smoke.capture_source_identity(repository)
    snapshot_root = tmp_path / "snapshot"
    smoke.materialize_source_snapshot(repository, commit, snapshot_root)
    script_path = snapshot_root / "scripts" / "m0_http_persistence_smoke.py"
    monkeypatch.setattr(smoke, "git_is_clean", lambda _: True)

    capsule = smoke.run_snapshot_smoke(
        repository,
        script_path,
        source_commit=commit,
        source_tree=tree,
        snapshot_root=snapshot_root,
        expected_snapshot_manifest="0" * 64,
    )

    assert capsule["status"] == "FAIL"
    assert capsule["failure"] == {
        "stage": "source_identity",
        "type": "SnapshotManifestMismatchError",
    }


def test_snapshot_orchestrator_rejects_snapshot_from_another_clean_commit(
    tmp_path: Path,
) -> None:
    source_repository = _create_snapshot_repository(tmp_path / "source")
    source_commit, source_tree, _ = smoke.capture_source_identity(source_repository)
    snapshot_repository = _create_snapshot_repository(tmp_path / "snapshot-source")
    snapshot_script = snapshot_repository / "scripts" / "m0_http_persistence_smoke.py"
    snapshot_script.write_text("print('different snapshot')\n", encoding="utf-8")
    _git(snapshot_repository, "add", ".")
    _git(
        snapshot_repository,
        "-c",
        "user.name=RepoPilot Test",
        "-c",
        "user.email=repopilot@example.invalid",
        "commit",
        "-qm",
        "different snapshot",
    )
    snapshot_commit = _git(snapshot_repository, "rev-parse", "HEAD")
    snapshot_root = tmp_path / "materialized"
    smoke.materialize_source_snapshot(snapshot_repository, snapshot_commit, snapshot_root)
    manifest, _ = smoke.snapshot_manifest(snapshot_root)

    capsule = smoke.run_snapshot_smoke(
        source_repository,
        snapshot_root / "scripts" / "m0_http_persistence_smoke.py",
        source_commit=source_commit,
        source_tree=source_tree,
        snapshot_root=snapshot_root,
        expected_snapshot_manifest=manifest,
    )

    assert capsule["status"] == "FAIL"
    assert capsule["failure"] == {
        "stage": "source_identity",
        "type": "SnapshotSourceMismatchError",
    }


def test_source_snapshot_claim_rejects_forged_tree(tmp_path: Path) -> None:
    repository = _create_snapshot_repository(tmp_path)
    commit, _, _ = smoke.capture_source_identity(repository)
    snapshot_root = tmp_path / "snapshot"
    smoke.materialize_source_snapshot(repository, commit, snapshot_root)
    manifest, file_count = smoke.snapshot_manifest(snapshot_root)

    assert not smoke.source_snapshot_matches_claim(
        repository,
        source_commit=commit,
        source_tree="0" * 40,
        snapshot_manifest_sha256=manifest,
        snapshot_file_count=file_count,
    )


def test_final_snapshot_guard_downgrades_pass_after_content_drift(tmp_path: Path) -> None:
    repository = _create_snapshot_repository(tmp_path)
    commit = _git(repository, "rev-parse", "HEAD")
    snapshot_root = tmp_path / "snapshot"
    smoke.materialize_source_snapshot(repository, commit, snapshot_root)
    manifest, file_count = smoke.snapshot_manifest(snapshot_root)
    capsule = {"status": "PASS", "source": {}}

    (snapshot_root / "src" / "repopilot" / "__init__.py").write_text(
        "VERSION = 'drifted'\n",
        encoding="utf-8",
    )
    smoke.record_final_snapshot_state(
        capsule,
        snapshot_root,
        expected_manifest_sha256=manifest,
        expected_file_count=file_count,
    )

    assert capsule["status"] == "FAIL"
    assert capsule["source"]["snapshot_unchanged"] is False
    assert capsule["failure"] == {
        "stage": "source_identity",
        "type": "SnapshotDriftError",
    }


def test_final_worktree_guard_downgrades_an_apparent_pass(
    monkeypatch,
    tmp_path: Path,
) -> None:
    capsule = {
        "status": "PASS",
        "source": {
            "git_commit": "a" * 40,
            "git_tree": "b" * 40,
            "worktree_clean": True,
        },
    }
    monkeypatch.setattr(smoke, "git_is_clean", lambda _: False)
    monkeypatch.setattr(
        smoke,
        "git_value",
        lambda _, revision: "a" * 40 if revision == "HEAD" else "b" * 40,
    )

    smoke.record_final_worktree_state(capsule, tmp_path)

    assert capsule == {
        "status": "FAIL",
        "source": {
            "git_commit": "a" * 40,
            "git_tree": "b" * 40,
            "worktree_clean": True,
            "worktree_clean_after": False,
            "worktree_unchanged": False,
            "git_commit_after": "a" * 40,
            "git_tree_after": "b" * 40,
            "source_identity_unchanged": True,
        },
        "failure": {"stage": "repository_cleanup", "type": "DirtyWorktreeError"},
    }


def test_final_worktree_guard_records_a_clean_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    capsule = {
        "status": "PASS",
        "source": {
            "git_commit": "a" * 40,
            "git_tree": "b" * 40,
            "worktree_clean": True,
        },
    }
    monkeypatch.setattr(smoke, "git_is_clean", lambda _: True)
    monkeypatch.setattr(
        smoke,
        "git_value",
        lambda _, revision: "a" * 40 if revision == "HEAD" else "b" * 40,
    )

    smoke.record_final_worktree_state(capsule, tmp_path)

    assert capsule == {
        "status": "PASS",
        "source": {
            "git_commit": "a" * 40,
            "git_tree": "b" * 40,
            "worktree_clean": True,
            "worktree_clean_after": True,
            "worktree_unchanged": True,
            "git_commit_after": "a" * 40,
            "git_tree_after": "b" * 40,
            "source_identity_unchanged": True,
        },
    }


def test_final_worktree_guard_rejects_a_clean_but_changed_commit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    capsule = {
        "status": "PASS",
        "source": {
            "git_commit": "a" * 40,
            "git_tree": "b" * 40,
            "worktree_clean": True,
        },
    }
    monkeypatch.setattr(smoke, "git_is_clean", lambda _: True)
    monkeypatch.setattr(
        smoke,
        "git_value",
        lambda _, revision: "c" * 40 if revision == "HEAD" else "d" * 40,
    )

    smoke.record_final_worktree_state(capsule, tmp_path)

    assert capsule["status"] == "FAIL"
    assert capsule["source"]["worktree_clean_after"] is True
    assert capsule["source"]["worktree_unchanged"] is True
    assert capsule["source"]["git_commit_after"] == "c" * 40
    assert capsule["source"]["git_tree_after"] == "d" * 40
    assert capsule["source"]["source_identity_unchanged"] is False
    assert capsule["failure"] == {
        "stage": "repository_cleanup",
        "type": "SourceIdentityChangedError",
    }


def test_cli_fail_safe_redacts_an_absolute_path_cleanup_exception(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    forbidden_path = str(tmp_path / "sensitive-cleanup-target")

    def raise_cleanup_exception(*_):
        raise RuntimeError(f"cleanup failed for {forbidden_path}")

    monkeypatch.setattr(smoke, "run_smoke", raise_cleanup_exception)
    monkeypatch.setattr(smoke.sys, "argv", ["m0_http_persistence_smoke.py"])

    exit_code = smoke.safe_main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert json.loads(captured.out) == smoke.CLI_FAIL_SAFE_CAPSULE
    assert captured.err == ""
    assert "Traceback" not in captured.out
    assert forbidden_path not in captured.out


def test_cli_fail_safe_redacts_unknown_argv_before_argparse_writes_stderr(
    monkeypatch,
    capsys,
) -> None:
    forbidden_marker = "/tmp/repopilot-sensitive-argv-marker"
    monkeypatch.setattr(
        smoke.sys,
        "argv",
        ["m0_http_persistence_smoke.py", "--unknown-option", forbidden_marker],
    )

    exit_code = smoke.safe_main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert json.loads(captured.out) == smoke.CLI_FAIL_SAFE_CAPSULE
    assert captured.err == ""
    assert forbidden_marker not in captured.out


def test_cli_help_preserves_argparse_help_behavior(monkeypatch, capsys) -> None:
    monkeypatch.setattr(smoke.sys, "argv", ["m0_http_persistence_smoke.py", "--help"])

    with pytest.raises(SystemExit) as raised:
        smoke.safe_main()
    captured = capsys.readouterr()

    assert raised.value.code == 0
    assert "Run the reproducible M0 HTTP and SQLite persistence smoke" in captured.out
    assert captured.err == ""


def test_cli_fail_safe_redacts_keyboard_interrupt(monkeypatch, capsys) -> None:
    forbidden_marker = "/tmp/repopilot-sensitive-interrupt-marker"

    def raise_keyboard_interrupt(*_):
        raise KeyboardInterrupt(forbidden_marker)

    monkeypatch.setattr(smoke, "run_smoke", raise_keyboard_interrupt)
    monkeypatch.setattr(smoke.sys, "argv", ["m0_http_persistence_smoke.py"])

    exit_code = smoke.safe_main()
    captured = capsys.readouterr()

    assert exit_code == 130
    assert json.loads(captured.out) == smoke.CLI_FAIL_SAFE_CAPSULE
    assert captured.err == ""
    assert forbidden_marker not in captured.out


def test_cli_fail_safe_redacts_nonzero_system_exit(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    forbidden_path = str(tmp_path / "sensitive-system-exit-target")

    def raise_system_exit(*_):
        raise SystemExit(forbidden_path)

    monkeypatch.setattr(smoke, "run_smoke", raise_system_exit)
    monkeypatch.setattr(smoke.sys, "argv", ["m0_http_persistence_smoke.py"])

    exit_code = smoke.safe_main()
    captured = capsys.readouterr()

    assert exit_code == 1
    assert json.loads(captured.out) == smoke.CLI_FAIL_SAFE_CAPSULE
    assert captured.err == ""
    assert forbidden_path not in captured.out


def test_cleanup_continues_after_a_child_stop_exception() -> None:
    class RaisingChild:
        process = None
        port = None
        signal_fallback_used = False
        kill_fallback_used = False

        def stop(self):
            raise RuntimeError("sensitive cleanup failure")

    class RecordingChild:
        process = None
        port = None
        signal_fallback_used = False
        kill_fallback_used = False

        def __init__(self) -> None:
            self.stopped = False

        def stop(self):
            self.stopped = True
            return {
                "started": False,
                "process_stopped": True,
                "port_closed": True,
                "exit_code": None,
                "control_pipe_requested": False,
                "signal_fallback": False,
                "kill_fallback": False,
                "graceful": False,
            }

    later_child = RecordingChild()
    cleanup = []

    cleanup_errors = smoke.cleanup_remaining_children(
        [RaisingChild(), later_child],
        cleanup,
    )

    assert cleanup_errors == 1
    assert later_child.stopped is True
    assert cleanup[0] == {
        "started": False,
        "process_stopped": True,
        "port_closed": True,
        "exit_code": None,
        "control_pipe_requested": False,
        "signal_fallback": False,
        "kill_fallback": False,
        "graceful": False,
        "cleanup_error": True,
        "force_cleanup_attempted": False,
        "force_cleanup_succeeded": True,
    }


def test_cleanup_force_kills_a_live_child_after_normal_stop_raises(capsys) -> None:
    class RecordingStream:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class LiveProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.stdout = RecordingStream()
            self.stderr = RecordingStream()
            self.kill_called = False
            self.wait_timeouts = []

        def poll(self):
            return self.returncode

        def kill(self) -> None:
            self.kill_called = True

        def wait(self, *, timeout: float):
            self.wait_timeouts.append(timeout)
            if self.kill_called:
                self.returncode = -9
            return self.returncode

    class RaisingLiveChild:
        port = None
        signal_fallback_used = False
        kill_fallback_used = False

        def __init__(self, process, shutdown_write_fd: int) -> None:
            self.process = process
            self._shutdown_write_fd = shutdown_write_fd

        def stop(self):
            raise RuntimeError("sensitive /tmp/child-cleanup-marker")

    process = LiveProcess()
    shutdown_read_fd, shutdown_write_fd = os.pipe()
    child = RaisingLiveChild(process, shutdown_write_fd)
    cleanup = []

    try:
        cleanup_errors = smoke.cleanup_remaining_children([child], cleanup)
    finally:
        os.close(shutdown_read_fd)
    captured = capsys.readouterr()

    assert cleanup_errors == 1
    assert process.kill_called is True
    assert process.wait_timeouts == [smoke.SHUTDOWN_TIMEOUT_SECONDS]
    assert process.stdout.closed is True
    assert process.stderr.closed is True
    assert child._shutdown_write_fd is None
    assert cleanup[0]["process_stopped"] is True
    assert cleanup[0]["port_closed"] is True
    assert cleanup[0]["exit_code"] == -9
    assert cleanup[0]["kill_fallback"] is True
    assert cleanup[0]["cleanup_error"] is True
    assert cleanup[0]["force_cleanup_attempted"] is True
    assert cleanup[0]["force_cleanup_succeeded"] is True
    assert captured.out == ""
    assert captured.err == ""


def test_cleanup_records_a_failed_force_kill_and_continues() -> None:
    class UnkillableProcess:
        returncode = None
        stdout = None
        stderr = None

        def poll(self):
            return None

        def kill(self) -> None:
            raise RuntimeError("sensitive kill failure")

        def wait(self, *, timeout: float):
            raise RuntimeError(f"sensitive wait failure after {timeout}")

    class UnkillableChild:
        process = UnkillableProcess()
        port = None
        signal_fallback_used = False
        kill_fallback_used = False
        _shutdown_write_fd = None

        def stop(self):
            raise RuntimeError("sensitive normal stop failure")

    class RecordingChild:
        process = None
        port = None
        signal_fallback_used = False
        kill_fallback_used = False
        _shutdown_write_fd = None

        def __init__(self) -> None:
            self.stopped = False

        def stop(self):
            self.stopped = True
            return {
                "started": False,
                "process_stopped": True,
                "port_closed": True,
                "exit_code": None,
                "control_pipe_requested": False,
                "signal_fallback": False,
                "kill_fallback": False,
                "graceful": False,
            }

    later_child = RecordingChild()
    cleanup = []

    cleanup_errors = smoke.cleanup_remaining_children(
        [UnkillableChild(), later_child],
        cleanup,
    )

    assert cleanup_errors == 1
    assert cleanup[0]["process_stopped"] is False
    assert cleanup[0]["force_cleanup_attempted"] is True
    assert cleanup[0]["force_cleanup_succeeded"] is False
    assert cleanup[0]["kill_fallback"] is True
    assert later_child.stopped is True

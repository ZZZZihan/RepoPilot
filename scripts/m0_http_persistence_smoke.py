#!/usr/bin/env python3
"""Run the reproducible M0 HTTP and SQLite persistence smoke.

The parent process starts two separate Uvicorn child processes against one
temporary SQLite database. Both children receive an explicitly injected
fixed-root repository inspector, so this smoke never reads GitHub or accepts an
arbitrary host path through HTTP input.

Only a redacted, machine-readable Evidence Capsule is written to stdout. Plan
documents, absolute paths, process IDs, assigned ports, and environment values
are intentionally omitted.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import selectors
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, NoReturn

import httpx
import uvicorn

HOST = "127.0.0.1"
STARTUP_TIMEOUT_SECONDS = 10.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
HTTP_TIMEOUT_SECONDS = 5.0
PROCESS_GROUP_TERM_TIMEOUT_SECONDS = 2.0
PROCESS_GROUP_KILL_TIMEOUT_SECONDS = 5.0
GIT_COMMAND_TIMEOUT_SECONDS = 15.0
MAX_GIT_METADATA_STDOUT_BYTES = 1 * 1024 * 1024
MAX_SUBPROCESS_STDERR_BYTES = 256 * 1024
MAX_SNAPSHOT_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_ARCHIVE_MEMBERS = 4096
MAX_SNAPSHOT_FILE_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_TOTAL_FILE_BYTES = 24 * 1024 * 1024
MAX_SNAPSHOT_ORCHESTRATOR_STDOUT_BYTES = 1 * 1024 * 1024
MAX_SNAPSHOT_ORCHESTRATOR_STDERR_BYTES = 256 * 1024
READ_CHUNK_BYTES = 64 * 1024
EVIDENCE_CAPSULE_VERSION = "1.0"
PROCESS_CONTAINMENT_SCOPE = (
    "managed_direct_children_original_posix_process_group_and_observed_ports"
)
# Two starts can each consume a ready wait and a health wait; two stops can
# each consume the graceful, TERM, KILL, and port-close waits; the HTTP flow
# contains eight independently bounded requests. Keep a fixed margin for Git
# materialization, imports, SQLite validation, and cleanup.
SNAPSHOT_ORCHESTRATOR_TIMEOUT_SECONDS = (
    2 * (2 * STARTUP_TIMEOUT_SECONDS + 4 * SHUTDOWN_TIMEOUT_SECONDS)
    + 8 * HTTP_TIMEOUT_SECONDS
    + 50.0
)
EXPECTED_UVICORN_EXIT_CODES = {0}
GITHUB_TOKEN_ENVIRONMENT_VARIABLES = (
    "REPOPILOT_GITHUB_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)
FIXED_CHILD_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": os.defpath,
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONNOUSERSITE": "1",
    "PYTHONSAFEPATH": "1",
    "PYTHONUTF8": "1",
}
SNAPSHOT_PATHS = (
    "scripts/m0_http_persistence_smoke.py",
    "src/repopilot",
    "tests/fixtures/tiny_python_repo",
    "uv.lock",
)
SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
EXPECTED_HTTP_EVIDENCE = {
    "create": 201,
    "read": 200,
    "schema": 200,
    "openapi": 200,
    "stale_approval": 409,
    "stale_approval_code": "plan_version_conflict",
    "approve": 200,
    "duplicate_approval": 409,
    "duplicate_approval_code": "invalid_plan_transition",
    "restart_read": 200,
    "shell_or_execution_routes_present": False,
}
EXPECTED_SQLITE_EVIDENCE = {
    "journal_mode": "wal",
    "integrity_check": "ok",
    "file_mode": "0600",
    "file_owner_matches_euid": True,
    "file_type": "regular",
    "file_link_count": 1,
    "sidecar_contract_checked": True,
    "sidecars_absent_after_shutdown": True,
    "stored_plan_count": 1,
    "stored_status": "approved",
    "stored_version": 2,
}
EXPECTED_INNER_CLEANUP = {
    "processes_started": 2,
    "distinct_processes": True,
    "all_processes_stopped": True,
    "all_ports_closed": True,
    "control_pipe_shutdowns": 2,
    "graceful_shutdowns": 2,
    "all_shutdowns_graceful": True,
    "signal_fallbacks": 0,
    "kill_fallbacks": 0,
    "fallbacks_used": False,
    "child_exit_codes": [0, 0],
    "all_exit_codes_expected": True,
    "cleanup_errors": 0,
    "force_cleanup_attempts": 0,
    "force_cleanup_failures": 0,
    "temporary_database_removed": True,
    "temporary_directory_removed": True,
}
EXPECTED_OUTER_CLEANUP = {
    "snapshot_orchestrator_timed_out": False,
    "snapshot_orchestrator_output_limit_exceeded": False,
    "snapshot_orchestrator_unexpected_descendants": False,
    "snapshot_orchestrator_sigterm": False,
    "snapshot_orchestrator_sigkill": False,
    "snapshot_orchestrator_process_group_empty": True,
    "source_snapshot_removed": True,
    "source_temporary_directory_removed": True,
}
EXPECTED_RUNTIME_EVIDENCE = {
    "bind_host": HOST,
    "os_assigned_ports": True,
    "uvicorn_reload": False,
    "uvicorn_processes_expected": 2,
    "github_token_variables_unset": True,
    "repository_inspector": "FixedRootRepositoryInspector",
    "live_github_performed": False,
    "child_launch": "argv",
    "subprocess_shell": False,
    "shutdown_method": "control_pipe",
    "shutdown_signal_fallback": "SIGTERM",
    "expected_child_exit_codes": [0],
    "child_environment_policy": "fixed_minimal_allowlist",
    "process_containment_scope": PROCESS_CONTAINMENT_SCOPE,
}
OUTER_CLEANUP_KEYS = {
    "snapshot_orchestrator_timed_out",
    "snapshot_orchestrator_output_limit_exceeded",
    "snapshot_orchestrator_unexpected_descendants",
    "snapshot_orchestrator_sigterm",
    "snapshot_orchestrator_sigkill",
    "snapshot_orchestrator_process_group_empty",
    "source_snapshot_removed",
    "source_temporary_directory_removed",
}
PASS_TOP_LEVEL_KEYS = {
    "evidence_capsule_version",
    "gate",
    "observed_at",
    "status",
    "source",
    "runtime",
    "http",
    "responses",
    "sqlite",
    "semantic_checks",
    "cleanup",
}
PASS_SOURCE_KEYS = {
    "git_commit",
    "git_tree",
    "worktree_clean",
    "uv_lock_sha256",
    "harness_sha256",
    "snapshot_manifest_sha256",
    "snapshot_file_count",
    "execution_source",
    "snapshot_manifest_sha256_after",
    "snapshot_file_count_after",
    "snapshot_unchanged",
    "worktree_clean_after",
    "worktree_unchanged",
    "git_commit_after",
    "git_tree_after",
    "source_identity_unchanged",
}
RESPONSE_EVIDENCE_KEYS = {
    "response_sha256",
    "approved_sha256",
    "restart_read_sha256",
    "restart_response_matches",
}
SEMANTIC_EVIDENCE_KEYS = {
    "evidence_ids_unique",
    "evidence_references_valid",
    "evidence_count",
    "step_count",
    "step_evidence_reference_count",
    "verification_evidence_reference_count",
}

CREATE_REQUEST = {
    "repository": {"url": "https://github.com/acme/tiny-python", "ref": "main"},
    "issue": {
        "number": 17,
        "url": "https://github.com/acme/tiny-python/issues/17",
        "title": "Give divide() a clear zero-divisor error",
        "body": "Update divide in calculator.py and keep a regression test for zero divisors.",
    },
}

CLI_FAIL_SAFE_CAPSULE = {
    "evidence_capsule_version": EVIDENCE_CAPSULE_VERSION,
    "gate": "M0-03",
    "status": "FAIL",
    "failure": {"stage": "cli_fail_safe", "type": "UnhandledSmokeError"},
}


class SmokeFailure(RuntimeError):
    """A smoke invariant was not satisfied."""


class SmokeArgumentError(RuntimeError):
    """An argument error whose original text must not reach evidence output."""


class RedactedArgumentParser(argparse.ArgumentParser):
    """Raise on invalid arguments without writing argv-derived text to stderr."""

    def error(self, _message: str) -> NoReturn:
        raise SmokeArgumentError


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def git_environment() -> dict[str, str]:
    """Return a deterministic Git environment without caller credentials/context."""
    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
    }


def _bounded_git(
    repository_root: Path,
    arguments: list[str],
    *,
    stdout_limit: int,
) -> bytes:
    result, cleanup = run_bounded_process_group(
        ["git", *arguments],
        cwd=repository_root,
        environment=git_environment(),
        timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        max_stdout_bytes=stdout_limit,
        max_stderr_bytes=MAX_SUBPROCESS_STDERR_BYTES,
    )
    if result is None or cleanup["timed_out"] or cleanup["output_limit_exceeded"]:
        raise SmokeFailure("bounded Git command did not complete")
    if cleanup["unexpected_descendants"] or not cleanup["process_group_empty"]:
        raise SmokeFailure("Git command left its original process group populated")
    if result.returncode != 0:
        raise SmokeFailure("Git command returned a non-zero status")
    return result.stdout


def git_value(repository_root: Path, revision: str) -> str:
    raw = _bounded_git(
        repository_root,
        ["rev-parse", revision],
        stdout_limit=MAX_GIT_METADATA_STDOUT_BYTES,
    )
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise SmokeFailure("Git returned non-ASCII metadata") from exc
    if not value or "\x00" in value or "\n" in value:
        raise SmokeFailure("Git returned malformed metadata")
    return value


def git_is_clean(repository_root: Path) -> bool:
    output = _bounded_git(
        repository_root,
        ["status", "--porcelain=v1", "--untracked-files=all"],
        stdout_limit=MAX_GIT_METADATA_STDOUT_BYTES,
    )
    return not output


def capture_source_identity(repository_root: Path) -> tuple[str, str, bool]:
    top_level = Path(git_value(repository_root, "--show-toplevel")).resolve(strict=True)
    if top_level != repository_root.resolve(strict=True):
        raise SmokeFailure("source repository root did not match the Git worktree")
    commit = git_value(repository_root, "HEAD")
    tree = git_value(repository_root, f"{commit}^{{tree}}")
    return commit, tree, git_is_clean(repository_root)


def materialize_source_snapshot(
    repository_root: Path,
    commit: str,
    destination: Path,
) -> None:
    destination.mkdir(mode=0o700)
    archive_bytes = _bounded_git(
        repository_root,
        ["archive", "--format=tar", commit, "--", *SNAPSHOT_PATHS],
        stdout_limit=MAX_SNAPSHOT_ARCHIVE_BYTES,
    )
    if not archive_bytes:
        raise SmokeFailure("source snapshot archive was empty")

    allowed_roots = tuple(PurePosixPath(path) for path in SNAPSHOT_PATHS)
    seen_paths: set[str] = set()
    total_file_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        for member_index, member in enumerate(archive, start=1):
            if member_index > MAX_SNAPSHOT_ARCHIVE_MEMBERS:
                raise SmokeFailure("source snapshot exceeded its member limit")
            member_path = PurePosixPath(member.name)
            windows_path = PureWindowsPath(member.name)
            canonical_member_path = member_path.as_posix()
            if (
                not member_path.parts
                or member_path.is_absolute()
                or windows_path.is_absolute()
                or windows_path.drive
                or "\\" in member.name
                or ".." in member_path.parts
                or member.name != canonical_member_path
                or canonical_member_path in seen_paths
                or not any(
                    member_path == root
                    or root in member_path.parents
                    or member_path in root.parents
                    for root in allowed_roots
                )
                or not (member.isdir() or member.isreg())
            ):
                raise SmokeFailure("source snapshot contained an unsafe archive member")
            seen_paths.add(canonical_member_path)

            target = destination.joinpath(*member_path.parts)
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if member.size < 0 or member.size > MAX_SNAPSHOT_FILE_BYTES:
                raise SmokeFailure("source snapshot member exceeded its file limit")
            total_file_bytes += member.size
            if total_file_bytes > MAX_SNAPSHOT_TOTAL_FILE_BYTES:
                raise SmokeFailure("source snapshot exceeded its total file limit")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise SmokeFailure("source snapshot member could not be read")
            remaining = member.size
            with target.open("xb") as output:
                while remaining:
                    chunk = extracted.read(min(READ_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise SmokeFailure("source snapshot member was truncated")
                    output.write(chunk)
                    remaining -= len(chunk)
                if extracted.read(1):
                    raise SmokeFailure("source snapshot member exceeded its declared size")
            os.chmod(target, member.mode & 0o777)


def _secure_regular_file_bytes(path: Path, *, limit: int) -> tuple[bytes, os.stat_result]:
    """Read one regular file through O_NOFOLLOW and verify stable inode metadata."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SmokeFailure("bounded read target was not a single-link regular file")
    if before.st_size < 0 or before.st_size > limit:
        raise SmokeFailure("bounded read target exceeded its byte limit")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0:
        raise SmokeFailure("O_NOFOLLOW is required for the snapshot smoke")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | nofollow)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise SmokeFailure("bounded read target changed before open")
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(READ_CHUNK_BYTES, limit + 1 - observed))
            if not chunk:
                break
            observed += len(chunk)
            if observed > limit:
                raise SmokeFailure("bounded read target exceeded its byte limit")
            chunks.append(chunk)
        after_fd = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = path.lstat()
    stable_fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after_fd, field) for field in stable_fields) or any(
        getattr(before, field) != getattr(after_path, field) for field in stable_fields
    ):
        raise SmokeFailure("bounded read target changed during read")
    return b"".join(chunks), before


def snapshot_manifest(snapshot_root: Path) -> tuple[str, int]:
    entries: list[dict[str, str | int]] = []
    total_file_bytes = 0
    for path in sorted(snapshot_root.rglob("*")):
        relative = path.relative_to(snapshot_root).as_posix()
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not (
            stat.S_ISDIR(path_stat.st_mode) or stat.S_ISREG(path_stat.st_mode)
        ):
            raise SmokeFailure("source snapshot contained an unsafe filesystem entry")
        if stat.S_ISREG(path_stat.st_mode):
            payload, verified_stat = _secure_regular_file_bytes(
                path,
                limit=MAX_SNAPSHOT_FILE_BYTES,
            )
            total_file_bytes += len(payload)
            if total_file_bytes > MAX_SNAPSHOT_TOTAL_FILE_BYTES:
                raise SmokeFailure("source snapshot exceeded its total file limit")
            entries.append(
                {
                    "path": relative,
                    "mode": stat.S_IMODE(verified_stat.st_mode),
                    "size": len(payload),
                    "sha256": sha256_bytes(payload),
                }
            )
    if not entries:
        raise SmokeFailure("source snapshot was empty")
    framed_manifest = {
        "schema": "repopilot-source-snapshot-v1",
        "entries": entries,
    }
    return sha256_json(framed_manifest), len(entries)


def child_environment(source_root: Path) -> dict[str, str]:
    """Return the fixed minimal environment shared by all managed children."""
    environment = dict(FIXED_CHILD_ENVIRONMENT)
    environment["PYTHONPATH"] = str(source_root)
    return environment


def source_snapshot_matches_claim(
    repository_root: Path,
    *,
    source_commit: str,
    source_tree: str,
    snapshot_manifest_sha256: str,
    snapshot_file_count: int,
) -> bool:
    try:
        top_level = Path(git_value(repository_root, "--show-toplevel")).resolve(strict=True)
        if top_level != repository_root.resolve(strict=True):
            return False
        if git_value(repository_root, "HEAD") != source_commit:
            return False
        if git_value(repository_root, f"{source_commit}^{{tree}}") != source_tree:
            return False
        if not git_is_clean(repository_root):
            return False
        with tempfile.TemporaryDirectory(prefix="repopilot-m0-identity-") as temporary_directory:
            verification_root = Path(temporary_directory).resolve(strict=True) / "snapshot"
            materialize_source_snapshot(repository_root, source_commit, verification_root)
            manifest_sha256, file_count = snapshot_manifest(verification_root)
        return manifest_sha256 == snapshot_manifest_sha256 and file_count == snapshot_file_count
    except Exception:
        return False


def record_final_snapshot_state(
    capsule: dict[str, Any],
    snapshot_root: Path,
    *,
    expected_manifest_sha256: str,
    expected_file_count: int,
) -> None:
    source = capsule.get("source")
    if not isinstance(source, dict):
        return
    try:
        manifest_sha256, file_count = snapshot_manifest(snapshot_root)
        unchanged = (
            manifest_sha256 == expected_manifest_sha256 and file_count == expected_file_count
        )
    except Exception:
        manifest_sha256 = None
        file_count = None
        unchanged = False
    source["snapshot_manifest_sha256_after"] = manifest_sha256
    source["snapshot_file_count_after"] = file_count
    source["snapshot_unchanged"] = unchanged
    if capsule.get("status") == "PASS" and not unchanged:
        capsule["status"] = "FAIL"
        capsule["failure"] = {
            "stage": "source_identity",
            "type": "SnapshotDriftError",
        }


def expect_status(response: httpx.Response, expected: int, operation: str) -> dict[str, Any]:
    if response.status_code != expected:
        raise SmokeFailure(f"{operation} returned an unexpected status")
    payload = response.json()
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{operation} did not return an object")
    return payload


def check_evidence_references(plan: Mapping[str, Any]) -> dict[str, int | bool]:
    evidence = plan.get("evidence")
    steps = plan.get("steps")
    verification_intents = plan.get("verification_intents")
    if not isinstance(evidence, list) or not isinstance(steps, list):
        raise SmokeFailure("plan evidence or steps were not lists")
    if not isinstance(verification_intents, list):
        raise SmokeFailure("plan verification intents were not a list")

    evidence_ids = {
        item.get("id")
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if len(evidence_ids) != len(evidence) or not evidence_ids:
        raise SmokeFailure("plan evidence IDs were missing or duplicated")

    reference_count = 0
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("file_references"), list):
            raise SmokeFailure("plan step file references were invalid")
        for reference in step["file_references"]:
            if not isinstance(reference, dict) or not isinstance(
                reference.get("evidence_ids"), list
            ):
                raise SmokeFailure("plan file reference evidence IDs were invalid")
            raw_reference_ids = reference["evidence_ids"]
            if not all(isinstance(item, str) for item in raw_reference_ids):
                raise SmokeFailure("plan file reference evidence IDs were not strings")
            referenced_ids = set(raw_reference_ids)
            if not referenced_ids or not referenced_ids <= evidence_ids:
                raise SmokeFailure("plan file reference used unknown evidence")
            reference_count += len(referenced_ids)

    verification_reference_count = 0
    for intent in verification_intents:
        if not isinstance(intent, dict) or not isinstance(intent.get("evidence_ids"), list):
            raise SmokeFailure("verification intent evidence IDs were invalid")
        raw_reference_ids = intent["evidence_ids"]
        if not all(isinstance(item, str) for item in raw_reference_ids):
            raise SmokeFailure("verification intent evidence IDs were not strings")
        referenced_ids = set(raw_reference_ids)
        if not referenced_ids or not referenced_ids <= evidence_ids:
            raise SmokeFailure("verification intent used unknown evidence")
        verification_reference_count += len(referenced_ids)

    return {
        "evidence_ids_unique": True,
        "evidence_references_valid": True,
        "evidence_count": len(evidence_ids),
        "step_count": len(steps),
        "step_evidence_reference_count": reference_count,
        "verification_evidence_reference_count": verification_reference_count,
    }


def wait_for_health(base_url: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    with httpx.Client(timeout=0.5, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(f"{base_url}/healthz")
                if response.status_code == 200 and response.json() == {"status": "ok"}:
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.05)
    raise SmokeFailure("Uvicorn did not become healthy")


def wait_for_closed_port(port: int) -> bool:
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.1)
            if probe.connect_ex((HOST, port)) != 0:
                return True
        time.sleep(0.05)
    return False


def process_group_exists(process_group_id: int) -> bool:
    """Return whether a POSIX process group still has at least one member."""
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_process_group_exit(
    process: subprocess.Popen[bytes],
    process_group_id: int,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        # poll() also reaps the direct child when it has exited. Merely waiting
        # for that leader is insufficient because descendants can retain the
        # process group and its inherited listener/file descriptors.
        process.poll()
        if not process_group_exists(process_group_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def terminate_process_group(process: subprocess.Popen[bytes]) -> dict[str, bool]:
    """Boundedly terminate every process in a start_new_session subprocess."""
    process_group_id = process.pid
    sigterm_used = False
    sigkill_used = False
    try:
        os.killpg(process_group_id, signal.SIGTERM)
        sigterm_used = True
    except ProcessLookupError:
        pass

    process_group_empty = wait_for_process_group_exit(
        process,
        process_group_id,
        PROCESS_GROUP_TERM_TIMEOUT_SECONDS,
    )
    if not process_group_empty:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
            sigkill_used = True
        except ProcessLookupError:
            pass
        process_group_empty = wait_for_process_group_exit(
            process,
            process_group_id,
            PROCESS_GROUP_KILL_TIMEOUT_SECONDS,
        )

    return {
        "sigterm_used": sigterm_used,
        "sigkill_used": sigkill_used,
        "process_group_empty": process_group_empty,
    }


def run_bounded_process_group(
    command: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float,
    max_stdout_bytes: int = MAX_SNAPSHOT_ORCHESTRATOR_STDOUT_BYTES,
    max_stderr_bytes: int = MAX_SNAPSHOT_ORCHESTRATOR_STDERR_BYTES,
) -> tuple[subprocess.CompletedProcess[bytes] | None, dict[str, bool]]:
    """Capture a POSIX process group with hard time and output ceilings.

    This contains the direct child and members that remain in its original
    process group. A descendant can deliberately create a new session, so the
    Evidence Capsule names that narrower boundary rather than claiming global
    descendant containment.
    """
    if os.name != "posix":
        raise SmokeFailure("the snapshot smoke requires POSIX process groups")
    if timeout <= 0 or max_stdout_bytes < 0 or max_stderr_bytes < 0:
        raise SmokeFailure("bounded subprocess limits were invalid")

    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    cleanup = {
        "timed_out": False,
        "output_limit_exceeded": False,
        "unexpected_descendants": False,
        "sigterm_used": False,
        "sigkill_used": False,
        "process_group_empty": False,
    }
    stdout = bytearray()
    stderr = bytearray()
    buffers = {"stdout": (stdout, max_stdout_bytes), "stderr": (stderr, max_stderr_bytes)}
    selector = selectors.DefaultSelector()
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    try:
        for name, stream in streams.items():
            if stream is None:
                raise SmokeFailure("bounded subprocess pipe was unavailable")
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ, data=name)

        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cleanup["timed_out"] = True
                break
            for key, _ in selector.select(min(0.1, remaining)):
                name = str(key.data)
                descriptor = key.fd
                try:
                    chunk = os.read(descriptor, READ_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(descriptor)
                    continue
                buffer, limit = buffers[name]
                available = limit - len(buffer)
                if len(chunk) > available:
                    if available > 0:
                        buffer.extend(chunk[:available])
                    cleanup["output_limit_exceeded"] = True
                    break
                buffer.extend(chunk)
            if cleanup["output_limit_exceeded"]:
                break

        if not cleanup["timed_out"] and not cleanup["output_limit_exceeded"]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cleanup["timed_out"] = True
            else:
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    cleanup["timed_out"] = True

        if cleanup["timed_out"] or cleanup["output_limit_exceeded"]:
            cleanup.update(terminate_process_group(process))
            process.poll()
            return None, cleanup

        process_group_id = process.pid
        if not wait_for_process_group_exit(process, process_group_id, 0.0):
            cleanup["unexpected_descendants"] = True
            cleanup.update(terminate_process_group(process))
        else:
            cleanup["process_group_empty"] = True
        result = subprocess.CompletedProcess(
            command,
            process.returncode,
            bytes(stdout),
            bytes(stderr),
        )
        return result, cleanup
    except BaseException:
        cleanup.update(terminate_process_group(process))
        process.poll()
        raise
    finally:
        selector.close()
        for stream in streams.values():
            if stream is not None:
                stream.close()


class UvicornChild:
    """One real Uvicorn subprocess with an OS-assigned loopback port."""

    def __init__(
        self,
        *,
        script_path: Path,
        source_root: Path,
        database_path: Path,
        fixture_root: Path,
    ) -> None:
        self._script_path = script_path
        self._source_root = source_root
        self._database_path = database_path
        self._fixture_root = fixture_root
        self._shutdown_write_fd: int | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.port: int | None = None
        self.signal_fallback_used = False
        self.kill_fallback_used = False

    def start(self) -> None:
        ready_read_fd, ready_write_fd = os.pipe()
        shutdown_read_fd, shutdown_write_fd = os.pipe()
        self._shutdown_write_fd = shutdown_write_fd
        environment = child_environment(self._source_root)
        command = [
            sys.executable,
            "-s",
            str(self._script_path),
            "--serve",
            "--database",
            str(self._database_path),
            "--fixture-root",
            str(self._fixture_root),
            "--ready-fd",
            str(ready_write_fd),
            "--shutdown-fd",
            str(shutdown_read_fd),
        ]
        try:
            self.process = subprocess.Popen(
                command,
                env=environment,
                pass_fds=(ready_write_fd, shutdown_read_fd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                umask=0o077,
            )
        except Exception:
            os.close(shutdown_write_fd)
            self._shutdown_write_fd = None
            raise
        finally:
            os.close(ready_write_fd)
            os.close(shutdown_read_fd)

        selector = selectors.DefaultSelector()
        selector.register(ready_read_fd, selectors.EVENT_READ)
        try:
            events = selector.select(STARTUP_TIMEOUT_SECONDS)
            if not events:
                raise SmokeFailure("Uvicorn child did not allocate a port")
            ready_payload = os.read(ready_read_fd, 64).decode("ascii").strip()
            if not ready_payload:
                raise SmokeFailure("Uvicorn child exited before allocating a port")
            self.port = int(ready_payload)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SmokeFailure("Uvicorn child returned an invalid port signal") from exc
        finally:
            selector.close()
            os.close(ready_read_fd)

        if not 0 < self.port < 65_536:
            raise SmokeFailure("Uvicorn child allocated an invalid port")
        wait_for_health(self.base_url)

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise SmokeFailure("Uvicorn child has no assigned port")
        return f"http://{HOST}:{self.port}"

    def stop(self) -> Mapping[str, Any]:
        process = self.process
        port = self.port
        if process is None:
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

        control_pipe_requested = False
        if process.poll() is None:
            if self._shutdown_write_fd is not None:
                try:
                    os.write(self._shutdown_write_fd, b"shutdown\n")
                    control_pipe_requested = True
                except OSError:
                    self.signal_fallback_used = True
                finally:
                    os.close(self._shutdown_write_fd)
                    self._shutdown_write_fd = None
            try:
                process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self.signal_fallback_used = True
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    self.kill_fallback_used = True
                    process.kill()
                    process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)

        if self._shutdown_write_fd is not None:
            os.close(self._shutdown_write_fd)
            self._shutdown_write_fd = None

        process_stopped = process.poll() is not None
        port_closed = port is None or wait_for_closed_port(port)
        graceful = (
            control_pipe_requested
            and process.returncode == 0
            and not self.signal_fallback_used
            and not self.kill_fallback_used
        )
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        return {
            "started": True,
            "process_stopped": process_stopped,
            "port_closed": port_closed,
            "exit_code": process.returncode,
            "control_pipe_requested": control_pipe_requested,
            "signal_fallback": self.signal_fallback_used,
            "kill_fallback": self.kill_fallback_used,
            "graceful": graceful,
        }


def cleanup_remaining_children(
    children: list[UvicornChild],
    cleanup: list[Mapping[str, Any]],
) -> int:
    """Best-effort every remaining child without exposing cleanup exceptions."""
    cleanup_errors = 0
    for child in children[len(cleanup) :]:
        try:
            cleanup.append(child.stop())
        except Exception:
            cleanup_errors += 1
            cleanup.append(force_cleanup_child(child))
    return cleanup_errors


def force_cleanup_child(child: UvicornChild) -> Mapping[str, Any]:
    """Bound and redact emergency cleanup after the normal stop path raises."""
    process = child.process
    started = process is not None
    control_fd_closed = True
    streams_closed = True

    shutdown_write_fd = getattr(child, "_shutdown_write_fd", None)
    child._shutdown_write_fd = None
    if shutdown_write_fd is not None:
        try:
            os.close(shutdown_write_fd)
        except Exception:
            control_fd_closed = False

    process_stopped = not started
    if process is not None:
        try:
            process_stopped = process.poll() is not None
        except Exception:
            process_stopped = process.returncode is not None

        if not process_stopped:
            child.kill_fallback_used = True
            try:
                process.kill()
            except Exception:
                pass
            try:
                process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except Exception:
                pass
            try:
                process_stopped = process.poll() is not None
            except Exception:
                process_stopped = process.returncode is not None

        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    streams_closed = False

    port_closed = child.port is None
    if child.port is not None and process_stopped:
        try:
            port_closed = wait_for_closed_port(child.port)
        except Exception:
            port_closed = False

    exit_code = None if process is None else process.returncode
    force_cleanup_succeeded = (
        process_stopped and port_closed and control_fd_closed and streams_closed
    )
    return {
        "started": started,
        "process_stopped": process_stopped,
        "port_closed": port_closed,
        "exit_code": exit_code,
        "control_pipe_requested": False,
        "signal_fallback": child.signal_fallback_used,
        "kill_fallback": child.kill_fallback_used,
        "graceful": False,
        "cleanup_error": True,
        "force_cleanup_attempted": started,
        "force_cleanup_succeeded": force_cleanup_succeeded,
    }


def request_server_shutdown(server: uvicorn.Server, shutdown_fd: int) -> None:
    try:
        os.read(shutdown_fd, 64)
    finally:
        os.close(shutdown_fd)
    server.should_exit = True


def run_server(
    *,
    database_path: Path,
    fixture_root: Path,
    ready_fd: int,
    shutdown_fd: int,
) -> int:
    from repopilot.adapters.filesystem import FixedRootRepositoryInspector
    from repopilot.api import create_app
    from repopilot.config import Settings
    from repopilot.inspection import InspectionLimits

    if any(name in os.environ for name in GITHUB_TOKEN_ENVIRONMENT_VARIABLES):
        return 70

    settings = Settings(
        database_path=database_path,
        github_token=None,
        github_api_version="2026-03-10",
        inspection_limits=InspectionLimits(),
    )
    inspector = FixedRootRepositoryInspector(
        root=fixture_root,
        owner="acme",
        name="tiny-python",
        limits=settings.inspection_limits,
    )
    application = create_app(settings=settings, inspector=inspector)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((HOST, 0))
    listener.listen(2048)
    assigned_port = listener.getsockname()[1]
    os.write(ready_fd, f"{assigned_port}\n".encode("ascii"))
    os.close(ready_fd)

    config = uvicorn.Config(
        application,
        host=HOST,
        port=0,
        reload=False,
        access_log=False,
        log_config=None,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    shutdown_thread = threading.Thread(
        target=request_server_shutdown,
        args=(server, shutdown_fd),
        daemon=True,
        name="repopilot-smoke-shutdown",
    )
    shutdown_thread.start()
    server.run(sockets=[listener])
    shutdown_thread.join(timeout=1.0)
    return 0 if server.started else 71


def _sqlite_file_identity(path: Path, *, required: bool) -> tuple[int, int] | None:
    """Validate a SQLite file using lstat plus an O_NOFOLLOW inode comparison."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        if required:
            raise SmokeFailure("required SQLite file was absent") from None
        return None

    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o600
    ):
        raise SmokeFailure("SQLite file metadata violated the private-file contract")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0:
        raise SmokeFailure("O_NOFOLLOW is required for SQLite validation")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | nofollow)
    except OSError as exc:
        raise SmokeFailure("SQLite file could not be opened without following links") from exc
    try:
        opened = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = path.lstat()
    identity = (before.st_dev, before.st_ino)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or (opened.st_dev, opened.st_ino) != identity
        or (after.st_dev, after.st_ino) != identity
        or opened.st_uid != before.st_uid
        or after.st_uid != before.st_uid
        or opened.st_nlink != 1
        or after.st_nlink != 1
        or stat.S_IMODE(opened.st_mode) != 0o600
        or stat.S_IMODE(after.st_mode) != 0o600
    ):
        raise SmokeFailure("SQLite file identity changed during no-follow validation")
    return identity


def _sqlite_header_journal_mode(path: Path, expected_identity: tuple[int, int]) -> str:
    """Read SQLite's write/read-version bytes without following or reopening a link."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0:
        raise SmokeFailure("O_NOFOLLOW is required for SQLite validation")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | nofollow)
    try:
        opened = os.fstat(descriptor)
        header = os.pread(descriptor, 20, 0)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        (opened.st_dev, opened.st_ino) != expected_identity
        or (after.st_dev, after.st_ino) != expected_identity
        or len(header) != 20
        or header[:16] != b"SQLite format 3\x00"
    ):
        raise SmokeFailure("SQLite header identity or framing was invalid")
    if header[18:20] != b"\x02\x02":
        raise SmokeFailure("SQLite header did not retain WAL mode")
    return "wal"


def check_database(database_path: Path) -> dict[str, Any]:
    database_identity = _sqlite_file_identity(database_path, required=True)
    if database_identity is None:
        raise SmokeFailure("SQLite database identity was unavailable")
    sidecar_identities = {
        suffix: _sqlite_file_identity(Path(f"{database_path}{suffix}"), required=False)
        for suffix in SQLITE_SIDECAR_SUFFIXES
    }
    if any(identity is not None for identity in sidecar_identities.values()):
        raise SmokeFailure("SQLite sidecars remained after graceful shutdown")
    journal_mode = _sqlite_header_journal_mode(database_path, database_identity)
    database_uri = f"{database_path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(database_uri, uri=True, timeout=1.0)) as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        plan_row = connection.execute(
            "SELECT COUNT(*), MIN(status), MIN(version) FROM plans"
        ).fetchone()

    if _sqlite_file_identity(database_path, required=True) != database_identity:
        raise SmokeFailure("SQLite database inode changed during validation")
    for suffix, identity in sidecar_identities.items():
        if _sqlite_file_identity(Path(f"{database_path}{suffix}"), required=False) != identity:
            raise SmokeFailure("SQLite sidecar identity changed during validation")

    if plan_row is None:
        raise SmokeFailure("SQLite metadata was unavailable")
    integrity = [str(row[0]).lower() for row in integrity_rows]
    row_count, status_value, version = plan_row
    if journal_mode != "wal":
        raise SmokeFailure("SQLite is not using WAL")
    if integrity != ["ok"]:
        raise SmokeFailure("SQLite integrity check failed")
    if (row_count, status_value, version) != (1, "approved", 2):
        raise SmokeFailure("SQLite did not retain the approved plan")

    return {
        "journal_mode": journal_mode,
        "integrity_check": integrity[0],
        "file_mode": "0600",
        "file_owner_matches_euid": True,
        "file_type": "regular",
        "file_link_count": 1,
        "sidecar_contract_checked": True,
        "sidecars_absent_after_shutdown": True,
        "stored_plan_count": row_count,
        "stored_status": status_value,
        "stored_version": version,
    }


def base_capsule(
    repository_root: Path,
    script_path: Path,
    *,
    source_commit: str,
    source_tree: str,
    snapshot_root: Path,
    snapshot_manifest_sha256: str,
    snapshot_file_count: int,
) -> dict[str, Any]:
    uv_lock_payload, _ = _secure_regular_file_bytes(
        snapshot_root / "uv.lock",
        limit=MAX_SNAPSHOT_FILE_BYTES,
    )
    harness_payload, _ = _secure_regular_file_bytes(
        script_path,
        limit=MAX_SNAPSHOT_FILE_BYTES,
    )
    return {
        "evidence_capsule_version": EVIDENCE_CAPSULE_VERSION,
        "gate": "M0-03",
        "observed_at": datetime.now(UTC).isoformat(),
        "status": "FAIL",
        "source": {
            "git_commit": source_commit,
            "git_tree": source_tree,
            "worktree_clean": git_is_clean(repository_root),
            "uv_lock_sha256": sha256_bytes(uv_lock_payload),
            "harness_sha256": sha256_bytes(harness_payload),
            "snapshot_manifest_sha256": snapshot_manifest_sha256,
            "snapshot_file_count": snapshot_file_count,
            "execution_source": "git_archive_snapshot",
        },
        "runtime": {
            "bind_host": HOST,
            "os_assigned_ports": True,
            "uvicorn_reload": False,
            "uvicorn_processes_expected": 2,
            "github_token_variables_unset": not any(
                name in os.environ for name in GITHUB_TOKEN_ENVIRONMENT_VARIABLES
            ),
            "repository_inspector": "FixedRootRepositoryInspector",
            "live_github_performed": False,
            "child_launch": "argv",
            "subprocess_shell": False,
            "shutdown_method": "control_pipe",
            "shutdown_signal_fallback": "SIGTERM",
            "expected_child_exit_codes": sorted(EXPECTED_UVICORN_EXIT_CODES),
            "child_environment_policy": "fixed_minimal_allowlist",
            "process_containment_scope": PROCESS_CONTAINMENT_SCOPE,
        },
    }


def _expected_pass_source(
    repository_root: Path,
    script_path: Path,
    *,
    source_commit: str,
    source_tree: str,
    snapshot_root: Path,
    snapshot_manifest_sha256: str,
    snapshot_file_count: int,
) -> dict[str, Any]:
    capsule = base_capsule(
        repository_root,
        script_path,
        source_commit=source_commit,
        source_tree=source_tree,
        snapshot_root=snapshot_root,
        snapshot_manifest_sha256=snapshot_manifest_sha256,
        snapshot_file_count=snapshot_file_count,
    )
    source = capsule["source"]
    if not isinstance(source, dict):
        raise SmokeFailure("trusted source evidence was not an object")
    source.update(
        {
            "snapshot_manifest_sha256_after": snapshot_manifest_sha256,
            "snapshot_file_count_after": snapshot_file_count,
            "snapshot_unchanged": True,
            "worktree_clean_after": True,
            "worktree_unchanged": True,
            "git_commit_after": source_commit,
            "git_tree_after": source_tree,
            "source_identity_unchanged": True,
        }
    )
    return source


def _require_exact_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise SmokeFailure(f"{label} did not match the versioned Evidence Capsule schema")
    return value


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_observed_at(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise SmokeFailure("Evidence Capsule timestamp was invalid")
    try:
        observed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SmokeFailure("Evidence Capsule timestamp was invalid") from exc
    if observed.tzinfo is None or observed.utcoffset() != UTC.utcoffset(observed):
        raise SmokeFailure("Evidence Capsule timestamp was not UTC")
    return value


def validate_pass_capsule(
    candidate: Any,
    *,
    expected_source: Mapping[str, Any],
    include_outer_cleanup: bool,
) -> dict[str, Any]:
    """Validate every PASS field and rebuild a trusted versioned capsule."""
    top = _require_exact_keys(candidate, PASS_TOP_LEVEL_KEYS, "PASS capsule")
    if (
        top.get("evidence_capsule_version") != EVIDENCE_CAPSULE_VERSION
        or top.get("gate") != "M0-03"
        or top.get("status") != "PASS"
    ):
        raise SmokeFailure("Evidence Capsule framing was invalid")
    observed_at = _validate_observed_at(top.get("observed_at"))

    source = _require_exact_keys(top.get("source"), PASS_SOURCE_KEYS, "source evidence")
    if dict(source) != dict(expected_source):
        raise SmokeFailure("Evidence Capsule source evidence did not match observation")

    runtime = _require_exact_keys(
        top.get("runtime"),
        set(EXPECTED_RUNTIME_EVIDENCE),
        "runtime evidence",
    )
    if dict(runtime) != EXPECTED_RUNTIME_EVIDENCE:
        raise SmokeFailure("Evidence Capsule runtime evidence was invalid")

    http = _require_exact_keys(top.get("http"), set(EXPECTED_HTTP_EVIDENCE), "HTTP evidence")
    if dict(http) != EXPECTED_HTTP_EVIDENCE:
        raise SmokeFailure("Evidence Capsule HTTP evidence was invalid")

    sqlite_evidence = _require_exact_keys(
        top.get("sqlite"),
        set(EXPECTED_SQLITE_EVIDENCE),
        "SQLite evidence",
    )
    if dict(sqlite_evidence) != EXPECTED_SQLITE_EVIDENCE:
        raise SmokeFailure("Evidence Capsule SQLite evidence was invalid")

    responses = _require_exact_keys(
        top.get("responses"),
        RESPONSE_EVIDENCE_KEYS,
        "response evidence",
    )
    if (
        not all(
            _is_sha256(responses.get(name))
            for name in ("response_sha256", "approved_sha256", "restart_read_sha256")
        )
        or responses.get("approved_sha256") != responses.get("restart_read_sha256")
        or responses.get("restart_response_matches") is not True
    ):
        raise SmokeFailure("Evidence Capsule response evidence was invalid")

    semantic = _require_exact_keys(
        top.get("semantic_checks"),
        SEMANTIC_EVIDENCE_KEYS,
        "semantic evidence",
    )
    counts = (
        semantic.get("evidence_count"),
        semantic.get("step_count"),
        semantic.get("step_evidence_reference_count"),
        semantic.get("verification_evidence_reference_count"),
    )
    if (
        semantic.get("evidence_ids_unique") is not True
        or semantic.get("evidence_references_valid") is not True
        or not all(type(value) is int and 0 < value <= 1_000_000 for value in counts)
    ):
        raise SmokeFailure("Evidence Capsule semantic evidence was invalid")

    expected_cleanup = dict(EXPECTED_INNER_CLEANUP)
    if include_outer_cleanup:
        expected_cleanup.update(EXPECTED_OUTER_CLEANUP)
    cleanup = _require_exact_keys(top.get("cleanup"), set(expected_cleanup), "cleanup evidence")
    if dict(cleanup) != expected_cleanup:
        raise SmokeFailure("Evidence Capsule cleanup evidence was invalid")

    # Reconstruct rather than returning the untrusted decoded object. Exact key
    # checks above reject extensions, while this copy ensures only validated
    # primitive evidence reaches outer stdout.
    return {
        "evidence_capsule_version": EVIDENCE_CAPSULE_VERSION,
        "gate": "M0-03",
        "observed_at": observed_at,
        "status": "PASS",
        "source": dict(expected_source),
        "runtime": dict(EXPECTED_RUNTIME_EVIDENCE),
        "http": dict(EXPECTED_HTTP_EVIDENCE),
        "responses": dict(responses),
        "sqlite": dict(EXPECTED_SQLITE_EVIDENCE),
        "semantic_checks": dict(semantic),
        "cleanup": expected_cleanup,
    }


def record_final_worktree_state(capsule: dict[str, Any], repository_root: Path) -> None:
    source = capsule.get("source")
    if not isinstance(source, dict):
        return

    worktree_clean_after = git_is_clean(repository_root)
    git_commit_after = git_value(repository_root, "HEAD")
    git_tree_after = git_value(repository_root, "HEAD^{tree}")
    source["worktree_clean_after"] = worktree_clean_after
    source["worktree_unchanged"] = source.get("worktree_clean") is True and worktree_clean_after
    source["git_commit_after"] = git_commit_after
    source["git_tree_after"] = git_tree_after
    source["source_identity_unchanged"] = (
        source.get("git_commit") == git_commit_after and source.get("git_tree") == git_tree_after
    )
    if capsule.get("status") == "PASS" and source["source_identity_unchanged"] is not True:
        capsule["status"] = "FAIL"
        capsule["failure"] = {
            "stage": "repository_cleanup",
            "type": "SourceIdentityChangedError",
        }
    elif capsule.get("status") == "PASS" and source["worktree_unchanged"] is not True:
        capsule["status"] = "FAIL"
        capsule["failure"] = {"stage": "repository_cleanup", "type": "DirtyWorktreeError"}


def run_snapshot_smoke(
    repository_root: Path,
    script_path: Path,
    *,
    source_commit: str,
    source_tree: str,
    snapshot_root: Path,
    expected_snapshot_manifest: str,
) -> dict[str, Any]:
    manifest_sha256, snapshot_file_count = snapshot_manifest(snapshot_root)
    capsule = base_capsule(
        repository_root,
        script_path,
        source_commit=source_commit,
        source_tree=source_tree,
        snapshot_root=snapshot_root,
        snapshot_manifest_sha256=manifest_sha256,
        snapshot_file_count=snapshot_file_count,
    )
    source = capsule.get("source")
    if manifest_sha256 != expected_snapshot_manifest:
        capsule["failure"] = {
            "stage": "source_identity",
            "type": "SnapshotManifestMismatchError",
        }
        return capsule
    if not source_snapshot_matches_claim(
        repository_root,
        source_commit=source_commit,
        source_tree=source_tree,
        snapshot_manifest_sha256=manifest_sha256,
        snapshot_file_count=snapshot_file_count,
    ):
        capsule["failure"] = {
            "stage": "source_identity",
            "type": "SnapshotSourceMismatchError",
        }
        return capsule
    if not isinstance(source, dict) or source.get("worktree_clean") is not True:
        capsule["failure"] = {"stage": "source_identity", "type": "DirtyWorktreeError"}
        return capsule

    children: list[UvicornChild] = []
    cleanup: list[Mapping[str, Any]] = []
    stage = "initialize"
    first_pid: int | None = None
    second_pid: int | None = None

    with tempfile.TemporaryDirectory(prefix="repopilot-m0-smoke-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        database_path = temporary_root / "repopilot.db"
        source_root = snapshot_root / "src"
        fixture_root = snapshot_root / "tests" / "fixtures" / "tiny_python_repo"
        approved_payload: dict[str, Any] | None = None
        aggregate_responses: dict[str, Any] = {}
        evidence_reference_checks: dict[str, int | bool] = {}

        try:
            stage = "first_uvicorn_start"
            first = UvicornChild(
                script_path=script_path,
                source_root=source_root,
                database_path=database_path,
                fixture_root=fixture_root,
            )
            children.append(first)
            first.start()
            if first.process is None:
                raise SmokeFailure("first Uvicorn process was not created")
            first_pid = first.process.pid

            stage = "first_http_flow"
            with httpx.Client(
                base_url=first.base_url,
                timeout=HTTP_TIMEOUT_SECONDS,
                trust_env=False,
            ) as client:
                create_response = client.post("/v1/plans", json=CREATE_REQUEST)
                created = expect_status(create_response, 201, "create")
                if created.get("status") != "proposed" or created.get("version") != 1:
                    raise SmokeFailure("create did not return proposed version 1")
                evidence_reference_checks = check_evidence_references(created)
                plan_id = created.get("plan_id")
                if not isinstance(plan_id, str):
                    raise SmokeFailure("create did not return a plan ID")

                read_response = client.get(f"/v1/plans/{plan_id}")
                read_payload = expect_status(read_response, 200, "read")
                if read_payload != created:
                    raise SmokeFailure("first read did not match create")

                schema_response = client.get("/v1/schemas/implementation-plan")
                schema_payload = expect_status(schema_response, 200, "schema")
                if schema_payload.get("title") != "ImplementationPlan":
                    raise SmokeFailure("schema title did not match")

                openapi_response = client.get("/openapi.json")
                openapi_payload = expect_status(openapi_response, 200, "OpenAPI")
                paths = openapi_payload.get("paths")
                if not isinstance(paths, dict):
                    raise SmokeFailure("OpenAPI paths were invalid")
                if not all(isinstance(path, str) for path in paths):
                    raise SmokeFailure("OpenAPI path keys were invalid")
                forbidden_route_terms = ("execute", "shell", "pull-request")
                if any(term in path for path in paths for term in forbidden_route_terms):
                    raise SmokeFailure("OpenAPI exposed a deferred execution route")

                stale_response = client.post(
                    f"/v1/plans/{plan_id}/approval",
                    json={"approved_by": "M0 Smoke Reviewer", "expected_version": 999},
                )
                stale_payload = expect_status(stale_response, 409, "stale approval")
                if stale_payload.get("error", {}).get("code") != "plan_version_conflict":
                    raise SmokeFailure("stale approval did not report a version conflict")

                approve_response = client.post(
                    f"/v1/plans/{plan_id}/approval",
                    json={"approved_by": "M0 Smoke Reviewer", "expected_version": 1},
                )
                approved_payload = expect_status(approve_response, 200, "approve")
                if (
                    approved_payload.get("status") != "approved"
                    or approved_payload.get("version") != 2
                ):
                    raise SmokeFailure("approve did not return approved version 2")

                duplicate_response = client.post(
                    f"/v1/plans/{plan_id}/approval",
                    json={"approved_by": "M0 Smoke Reviewer", "expected_version": 2},
                )
                duplicate_payload = expect_status(duplicate_response, 409, "duplicate approval")
                if duplicate_payload.get("error", {}).get("code") != "invalid_plan_transition":
                    raise SmokeFailure("duplicate approval did not reject the transition")

                aggregate_responses = {
                    "create": created,
                    "read": read_payload,
                    "schema": schema_payload,
                    "openapi": openapi_payload,
                    "stale_approval": stale_payload,
                    "approve": approved_payload,
                    "duplicate_approval": duplicate_payload,
                }

            stage = "first_uvicorn_stop"
            cleanup.append(first.stop())
            if not all(
                (
                    cleanup[-1]["process_stopped"],
                    cleanup[-1]["port_closed"],
                    cleanup[-1]["exit_code"] in EXPECTED_UVICORN_EXIT_CODES,
                    cleanup[-1]["graceful"],
                    not cleanup[-1]["signal_fallback"],
                    not cleanup[-1]["kill_fallback"],
                )
            ):
                raise SmokeFailure("first Uvicorn process did not cleanly stop")

            stage = "second_uvicorn_start"
            second = UvicornChild(
                script_path=script_path,
                source_root=source_root,
                database_path=database_path,
                fixture_root=fixture_root,
            )
            children.append(second)
            second.start()
            if second.process is None:
                raise SmokeFailure("second Uvicorn process was not created")
            second_pid = second.process.pid
            if first_pid == second_pid:
                raise SmokeFailure("restart did not create a distinct process")

            stage = "restart_read"
            with httpx.Client(
                base_url=second.base_url,
                timeout=HTTP_TIMEOUT_SECONDS,
                trust_env=False,
            ) as client:
                restored_response = client.get(f"/v1/plans/{plan_id}")
                restored_payload = expect_status(restored_response, 200, "restart read")
            if approved_payload is None or restored_payload != approved_payload:
                raise SmokeFailure("restart read did not match the approved plan")

            stage = "second_uvicorn_stop"
            cleanup.append(second.stop())
            if not all(
                (
                    cleanup[-1]["process_stopped"],
                    cleanup[-1]["port_closed"],
                    cleanup[-1]["exit_code"] in EXPECTED_UVICORN_EXIT_CODES,
                    cleanup[-1]["graceful"],
                    not cleanup[-1]["signal_fallback"],
                    not cleanup[-1]["kill_fallback"],
                )
            ):
                raise SmokeFailure("second Uvicorn process did not cleanly stop")

            stage = "sqlite_validation"
            database_evidence = check_database(database_path)
            approved_hash = sha256_json(approved_payload)
            restart_hash = sha256_json(restored_payload)
            if approved_hash != restart_hash:
                raise SmokeFailure("restart response hash changed")

            capsule.update(
                {
                    "status": "PASS",
                    "http": {
                        "create": 201,
                        "read": 200,
                        "schema": 200,
                        "openapi": 200,
                        "stale_approval": 409,
                        "stale_approval_code": "plan_version_conflict",
                        "approve": 200,
                        "duplicate_approval": 409,
                        "duplicate_approval_code": "invalid_plan_transition",
                        "restart_read": 200,
                        "shell_or_execution_routes_present": False,
                    },
                    "responses": {
                        "response_sha256": sha256_json(aggregate_responses),
                        "approved_sha256": approved_hash,
                        "restart_read_sha256": restart_hash,
                        "restart_response_matches": True,
                    },
                    "sqlite": database_evidence,
                    "semantic_checks": evidence_reference_checks,
                }
            )
        except Exception as exc:  # Evidence output must remain redacted on every failure path.
            capsule["status"] = "FAIL"
            capsule["failure"] = {"stage": stage, "type": type(exc).__name__}
        finally:
            cleanup_errors = cleanup_remaining_children(children, cleanup)
            if cleanup_errors and capsule.get("status") == "PASS":
                capsule["status"] = "FAIL"
                capsule["failure"] = {
                    "stage": "process_cleanup",
                    "type": "ChildCleanupError",
                }

            started_cleanup = [item for item in cleanup if item["started"]]
            capsule["cleanup"] = {
                "processes_started": len(children),
                "distinct_processes": (
                    first_pid is not None and second_pid is not None and first_pid != second_pid
                ),
                "all_processes_stopped": all(
                    bool(item["process_stopped"]) for item in started_cleanup
                ),
                "all_ports_closed": all(bool(item["port_closed"]) for item in started_cleanup),
                "control_pipe_shutdowns": sum(
                    bool(item["control_pipe_requested"]) for item in started_cleanup
                ),
                "graceful_shutdowns": sum(bool(item["graceful"]) for item in started_cleanup),
                "all_shutdowns_graceful": len(started_cleanup) == 2
                and all(bool(item["graceful"]) for item in started_cleanup),
                "signal_fallbacks": sum(bool(item["signal_fallback"]) for item in started_cleanup),
                "kill_fallbacks": sum(bool(item["kill_fallback"]) for item in started_cleanup),
                "fallbacks_used": any(
                    bool(item["signal_fallback"]) or bool(item["kill_fallback"])
                    for item in started_cleanup
                ),
                "child_exit_codes": [item["exit_code"] for item in started_cleanup],
                "all_exit_codes_expected": all(
                    item["exit_code"] in EXPECTED_UVICORN_EXIT_CODES for item in started_cleanup
                ),
                "cleanup_errors": cleanup_errors,
                "force_cleanup_attempts": sum(
                    bool(item.get("force_cleanup_attempted")) for item in started_cleanup
                ),
                "force_cleanup_failures": sum(
                    item.get("force_cleanup_attempted") is True
                    and item.get("force_cleanup_succeeded") is not True
                    for item in started_cleanup
                ),
            }

            serialized = canonical_json_bytes(capsule).decode("utf-8")
            forbidden_values = (
                str(repository_root),
                str(snapshot_root),
                str(temporary_root),
                str(database_path),
                str(fixture_root),
            )
            if any(value in serialized for value in forbidden_values):
                capsule.clear()
                capsule.update(
                    {
                        "evidence_capsule_version": EVIDENCE_CAPSULE_VERSION,
                        "gate": "M0-03",
                        "status": "FAIL",
                        "failure": {"stage": "redaction", "type": "SensitiveOutputError"},
                    }
                )

    temporary_database_removed = not database_path.exists()
    temporary_directory_removed = not temporary_root.exists()
    cleanup_capsule = capsule.get("cleanup")
    if isinstance(cleanup_capsule, dict):
        cleanup_capsule.update(
            {
                "temporary_database_removed": temporary_database_removed,
                "temporary_directory_removed": temporary_directory_removed,
            }
        )
    if capsule.get("status") == "PASS" and not (
        temporary_database_removed and temporary_directory_removed
    ):
        capsule["status"] = "FAIL"
        capsule["failure"] = {"stage": "temporary_cleanup", "type": "SmokeFailure"}

    record_final_snapshot_state(
        capsule,
        snapshot_root,
        expected_manifest_sha256=manifest_sha256,
        expected_file_count=snapshot_file_count,
    )
    record_final_worktree_state(capsule, repository_root)
    if capsule.get("status") == "PASS":
        try:
            expected_source = _expected_pass_source(
                repository_root,
                script_path,
                source_commit=source_commit,
                source_tree=source_tree,
                snapshot_root=snapshot_root,
                snapshot_manifest_sha256=manifest_sha256,
                snapshot_file_count=snapshot_file_count,
            )
            capsule = validate_pass_capsule(
                capsule,
                expected_source=expected_source,
                include_outer_cleanup=False,
            )
        except Exception as exc:
            capsule["status"] = "FAIL"
            capsule["failure"] = {
                "stage": "inner_evidence_capsule_validation",
                "type": type(exc).__name__,
            }
    return capsule


def run_smoke(repository_root: Path, script_path: Path) -> tuple[int, bytes]:
    source_commit, source_tree, worktree_clean = capture_source_identity(repository_root)
    if not worktree_clean:
        capsule = {
            "evidence_capsule_version": EVIDENCE_CAPSULE_VERSION,
            "gate": "M0-03",
            "observed_at": datetime.now(UTC).isoformat(),
            "status": "FAIL",
            "source": {
                "git_commit": source_commit,
                "git_tree": source_tree,
                "worktree_clean": False,
            },
            "failure": {"stage": "source_identity", "type": "DirtyWorktreeError"},
        }
        return 1, json.dumps(capsule, sort_keys=True, indent=2).encode("utf-8") + b"\n"

    parsed: dict[str, Any]
    expected_source: dict[str, Any] | None = None
    process_group_cleanup: dict[str, bool]
    with tempfile.TemporaryDirectory(prefix="repopilot-m0-source-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        snapshot_root = temporary_root / "snapshot"
        materialize_source_snapshot(repository_root, source_commit, snapshot_root)
        manifest_sha256, snapshot_file_count = snapshot_manifest(snapshot_root)
        snapshot_script = snapshot_root / script_path.relative_to(repository_root)
        command = [
            sys.executable,
            "-s",
            str(snapshot_script),
            "--orchestrate-snapshot",
            "--source-repository-root",
            str(repository_root),
            "--source-commit",
            source_commit,
            "--source-tree",
            source_tree,
            "--snapshot-manifest",
            manifest_sha256,
        ]
        result, process_group_cleanup = run_bounded_process_group(
            command,
            cwd=snapshot_root,
            environment=child_environment(snapshot_root / "src"),
            timeout=SNAPSHOT_ORCHESTRATOR_TIMEOUT_SECONDS,
            max_stdout_bytes=MAX_SNAPSHOT_ORCHESTRATOR_STDOUT_BYTES,
            max_stderr_bytes=MAX_SNAPSHOT_ORCHESTRATOR_STDERR_BYTES,
        )

        def outer_failure(failure_type: str) -> dict[str, Any]:
            failure_capsule = base_capsule(
                repository_root,
                snapshot_script,
                source_commit=source_commit,
                source_tree=source_tree,
                snapshot_root=snapshot_root,
                snapshot_manifest_sha256=manifest_sha256,
                snapshot_file_count=snapshot_file_count,
            )
            failure_capsule["failure"] = {
                "stage": "snapshot_orchestrator",
                "type": failure_type,
            }
            return failure_capsule

        if result is None:
            failure_type = (
                "SnapshotOrchestratorOutputLimitError"
                if process_group_cleanup["output_limit_exceeded"]
                else "SnapshotOrchestratorTimeoutError"
            )
            parsed = outer_failure(failure_type)
        elif (
            process_group_cleanup["unexpected_descendants"]
            or not process_group_cleanup["process_group_empty"]
        ):
            parsed = outer_failure("OriginalProcessGroupLeakError")
        elif result.returncode != 0:
            # Never forward an inner FAIL object. Its status and diagnostics are
            # untrusted bytes; the outer process emits a fixed redacted reason.
            parsed = outer_failure("SnapshotOrchestratorReportedFailure")
        else:
            try:
                candidate = json.loads(result.stdout)
                expected_source = _expected_pass_source(
                    repository_root,
                    snapshot_script,
                    source_commit=source_commit,
                    source_tree=source_tree,
                    snapshot_root=snapshot_root,
                    snapshot_manifest_sha256=manifest_sha256,
                    snapshot_file_count=snapshot_file_count,
                )
                parsed = validate_pass_capsule(
                    candidate,
                    expected_source=expected_source,
                    include_outer_cleanup=False,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, SmokeFailure):
                parsed = outer_failure("InvalidEvidenceCapsuleError")

    snapshot_removed = not snapshot_root.exists()
    temporary_directory_removed = not temporary_root.exists()
    cleanup = parsed.setdefault("cleanup", {})
    if not isinstance(cleanup, dict):
        cleanup = {}
        parsed["cleanup"] = cleanup
    cleanup.update(
        {
            "snapshot_orchestrator_timed_out": process_group_cleanup["timed_out"],
            "snapshot_orchestrator_output_limit_exceeded": process_group_cleanup[
                "output_limit_exceeded"
            ],
            "snapshot_orchestrator_unexpected_descendants": process_group_cleanup[
                "unexpected_descendants"
            ],
            "snapshot_orchestrator_sigterm": process_group_cleanup["sigterm_used"],
            "snapshot_orchestrator_sigkill": process_group_cleanup["sigkill_used"],
            "snapshot_orchestrator_process_group_empty": process_group_cleanup[
                "process_group_empty"
            ],
            "source_snapshot_removed": snapshot_removed,
            "source_temporary_directory_removed": temporary_directory_removed,
        }
    )
    if parsed.get("status") == "PASS" and not all(
        (
            not process_group_cleanup["timed_out"],
            not process_group_cleanup["output_limit_exceeded"],
            not process_group_cleanup["unexpected_descendants"],
            process_group_cleanup["process_group_empty"],
            snapshot_removed,
            temporary_directory_removed,
        )
    ):
        parsed["status"] = "FAIL"
        parsed["failure"] = {
            "stage": "source_cleanup",
            "type": "SourceCleanupError",
        }

    record_final_worktree_state(parsed, repository_root)
    if parsed.get("status") == "PASS":
        try:
            if expected_source is None:
                raise SmokeFailure("trusted source evidence was unavailable")
            parsed = validate_pass_capsule(
                parsed,
                expected_source=expected_source,
                include_outer_cleanup=True,
            )
        except SmokeFailure:
            parsed["status"] = "FAIL"
            parsed["failure"] = {
                "stage": "outer_evidence_capsule_validation",
                "type": "InvalidEvidenceCapsuleError",
            }

    serialized = canonical_json_bytes(parsed).decode("utf-8")
    forbidden_values = (str(repository_root), str(snapshot_root), str(temporary_root))
    if any(value in serialized for value in forbidden_values):
        parsed = {
            "evidence_capsule_version": EVIDENCE_CAPSULE_VERSION,
            "gate": "M0-03",
            "status": "FAIL",
            "failure": {"stage": "redaction", "type": "SensitiveOutputError"},
        }

    output = json.dumps(parsed, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    if len(output) > MAX_SNAPSHOT_ORCHESTRATOR_STDOUT_BYTES:
        parsed = {
            "evidence_capsule_version": EVIDENCE_CAPSULE_VERSION,
            "gate": "M0-03",
            "status": "FAIL",
            "failure": {"stage": "redaction", "type": "EvidenceCapsuleSizeError"},
        }
        output = json.dumps(parsed, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    return (0 if parsed.get("status") == "PASS" else 1), output


def parse_arguments() -> argparse.Namespace:
    parser = RedactedArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--orchestrate-snapshot", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--database", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--fixture-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--ready-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--shutdown-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--source-repository-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--source-commit", help=argparse.SUPPRESS)
    parser.add_argument("--source-tree", help=argparse.SUPPRESS)
    parser.add_argument("--snapshot-manifest", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.serve:
        if (
            arguments.database is None
            or arguments.fixture_root is None
            or arguments.ready_fd is None
            or arguments.shutdown_fd is None
        ):
            return 64
        return run_server(
            database_path=arguments.database,
            fixture_root=arguments.fixture_root,
            ready_fd=arguments.ready_fd,
            shutdown_fd=arguments.shutdown_fd,
        )

    if arguments.orchestrate_snapshot:
        if (
            arguments.source_repository_root is None
            or arguments.source_commit is None
            or arguments.source_tree is None
            or arguments.snapshot_manifest is None
        ):
            return 64
        script_path = Path(__file__).resolve()
        snapshot_root = script_path.parent.parent
        capsule = run_snapshot_smoke(
            arguments.source_repository_root,
            script_path,
            source_commit=arguments.source_commit,
            source_tree=arguments.source_tree,
            snapshot_root=snapshot_root,
            expected_snapshot_manifest=arguments.snapshot_manifest,
        )
        print(json.dumps(capsule, sort_keys=True, indent=2))
        return 0 if capsule["status"] == "PASS" else 1

    for variable_name in GITHUB_TOKEN_ENVIRONMENT_VARIABLES:
        os.environ.pop(variable_name, None)
    script_path = Path(__file__).resolve()
    repository_root = script_path.parent.parent
    return_code, output = run_smoke(repository_root, script_path)
    sys.stdout.buffer.write(output)
    return return_code


def safe_main() -> int:
    try:
        return main()
    except SystemExit as exc:
        if exc.code is None or exc.code == 0:
            raise
        print(json.dumps(CLI_FAIL_SAFE_CAPSULE, sort_keys=True, indent=2))
        return 1
    except KeyboardInterrupt:
        print(json.dumps(CLI_FAIL_SAFE_CAPSULE, sort_keys=True, indent=2))
        return 130
    except Exception:
        print(json.dumps(CLI_FAIL_SAFE_CAPSULE, sort_keys=True, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(safe_main())

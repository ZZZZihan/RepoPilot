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
import json
import os
import selectors
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from repopilot.adapters.filesystem import FixedRootRepositoryInspector
from repopilot.api import create_app
from repopilot.config import Settings
from repopilot.inspection import InspectionLimits

HOST = "127.0.0.1"
STARTUP_TIMEOUT_SECONDS = 10.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
HTTP_TIMEOUT_SECONDS = 5.0
EXPECTED_UVICORN_EXIT_CODES = {0}
GITHUB_TOKEN_ENVIRONMENT_VARIABLES = (
    "REPOPILOT_GITHUB_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)

CREATE_REQUEST = {
    "repository": {"url": "https://github.com/acme/tiny-python", "ref": "main"},
    "issue": {
        "number": 17,
        "url": "https://github.com/acme/tiny-python/issues/17",
        "title": "Give divide() a clear zero-divisor error",
        "body": "Update divide in calculator.py and keep a regression test for zero divisors.",
    },
}


class SmokeFailure(RuntimeError):
    """A smoke invariant was not satisfied."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def git_value(repository_root: Path, revision: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", revision],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def git_is_clean(repository_root: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return not result.stdout


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


class UvicornChild:
    """One real Uvicorn subprocess with an OS-assigned loopback port."""

    def __init__(self, *, script_path: Path, database_path: Path, fixture_root: Path) -> None:
        self._script_path = script_path
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
        child_environment = os.environ.copy()
        for variable_name in GITHUB_TOKEN_ENVIRONMENT_VARIABLES:
            child_environment.pop(variable_name, None)
        command = [
            sys.executable,
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
                env=child_environment,
                pass_fds=(ready_write_fd, shutdown_read_fd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
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


def check_database(database_path: Path) -> dict[str, Any]:
    database_mode = stat.S_IMODE(database_path.stat().st_mode)
    with sqlite3.connect(database_path) as connection:
        journal_mode_row = connection.execute("PRAGMA journal_mode").fetchone()
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        plan_row = connection.execute(
            "SELECT COUNT(*), MIN(status), MIN(version) FROM plans"
        ).fetchone()

    if journal_mode_row is None or plan_row is None:
        raise SmokeFailure("SQLite metadata was unavailable")
    journal_mode = str(journal_mode_row[0]).lower()
    integrity = [str(row[0]).lower() for row in integrity_rows]
    row_count, status_value, version = plan_row
    if journal_mode != "wal":
        raise SmokeFailure("SQLite is not using WAL")
    if integrity != ["ok"]:
        raise SmokeFailure("SQLite integrity check failed")
    if database_mode != 0o600:
        raise SmokeFailure("SQLite file mode is not owner-only")
    if (row_count, status_value, version) != (1, "approved", 2):
        raise SmokeFailure("SQLite did not retain the approved plan")

    return {
        "journal_mode": journal_mode,
        "integrity_check": integrity[0],
        "file_mode": "0600",
        "stored_plan_count": row_count,
        "stored_status": status_value,
        "stored_version": version,
    }


def base_capsule(repository_root: Path, script_path: Path) -> dict[str, Any]:
    return {
        "evidence_capsule_version": "1.0",
        "gate": "M0-03",
        "observed_at": datetime.now(UTC).isoformat(),
        "status": "FAIL",
        "source": {
            "git_commit": git_value(repository_root, "HEAD"),
            "git_tree": git_value(repository_root, "HEAD^{tree}"),
            "worktree_clean": git_is_clean(repository_root),
            "uv_lock_sha256": sha256_bytes((repository_root / "uv.lock").read_bytes()),
            "harness_sha256": sha256_bytes(script_path.read_bytes()),
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
        },
    }


def record_final_worktree_state(capsule: dict[str, Any], repository_root: Path) -> None:
    source = capsule.get("source")
    if not isinstance(source, dict):
        return

    worktree_clean_after = git_is_clean(repository_root)
    source["worktree_clean_after"] = worktree_clean_after
    source["worktree_unchanged"] = source.get("worktree_clean") is True and worktree_clean_after
    if capsule.get("status") == "PASS" and source["worktree_unchanged"] is not True:
        capsule["status"] = "FAIL"
        capsule["failure"] = {"stage": "repository_cleanup", "type": "DirtyWorktreeError"}


def run_smoke(repository_root: Path, script_path: Path) -> dict[str, Any]:
    capsule = base_capsule(repository_root, script_path)
    source = capsule.get("source")
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
        fixture_root = repository_root / "tests" / "fixtures" / "tiny_python_repo"
        approved_payload: dict[str, Any] | None = None
        aggregate_responses: dict[str, Any] = {}
        evidence_reference_checks: dict[str, int | bool] = {}

        try:
            stage = "first_uvicorn_start"
            first = UvicornChild(
                script_path=script_path,
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
            for child in children[len(cleanup) :]:
                cleanup.append(child.stop())

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
            }

            serialized = canonical_json_bytes(capsule).decode("utf-8")
            forbidden_values = (
                str(repository_root),
                str(temporary_root),
                str(database_path),
                str(fixture_root),
            )
            if any(value in serialized for value in forbidden_values):
                capsule.clear()
                capsule.update(
                    {
                        "evidence_capsule_version": "1.0",
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

    record_final_worktree_state(capsule, repository_root)
    return capsule


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--database", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--fixture-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--ready-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--shutdown-fd", type=int, help=argparse.SUPPRESS)
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

    for variable_name in GITHUB_TOKEN_ENVIRONMENT_VARIABLES:
        os.environ.pop(variable_name, None)
    script_path = Path(__file__).resolve()
    repository_root = script_path.parent.parent
    capsule = run_smoke(repository_root, script_path)
    print(json.dumps(capsule, sort_keys=True, indent=2))
    return 0 if capsule["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

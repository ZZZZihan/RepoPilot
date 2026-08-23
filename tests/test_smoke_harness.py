from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType


def _load_smoke_module() -> ModuleType:
    script_path = Path(__file__).parents[1] / "scripts" / "m0_http_persistence_smoke.py"
    spec = importlib.util.spec_from_file_location("repopilot_m0_smoke", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the M0 smoke harness")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke_module()


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


def test_final_worktree_guard_downgrades_an_apparent_pass(
    monkeypatch,
    tmp_path: Path,
) -> None:
    capsule = {
        "status": "PASS",
        "source": {"worktree_clean": True},
    }
    monkeypatch.setattr(smoke, "git_is_clean", lambda _: False)

    smoke.record_final_worktree_state(capsule, tmp_path)

    assert capsule == {
        "status": "FAIL",
        "source": {
            "worktree_clean": True,
            "worktree_clean_after": False,
            "worktree_unchanged": False,
        },
        "failure": {"stage": "repository_cleanup", "type": "DirtyWorktreeError"},
    }


def test_final_worktree_guard_records_a_clean_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    capsule = {
        "status": "PASS",
        "source": {"worktree_clean": True},
    }
    monkeypatch.setattr(smoke, "git_is_clean", lambda _: True)

    smoke.record_final_worktree_state(capsule, tmp_path)

    assert capsule == {
        "status": "PASS",
        "source": {
            "worktree_clean": True,
            "worktree_clean_after": True,
            "worktree_unchanged": True,
        },
    }

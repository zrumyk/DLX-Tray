"""DLXProcess against a fake ``deeplx.exe`` (a Python script that serves ``GET /``)."""

from __future__ import annotations

import os
import signal
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest

from dlx_tray.core.models import ProviderUnavailableError
from dlx_tray.services import deeplx_process
from dlx_tray.services.deeplx_process import DLXProcess, free_port, resolve_binary
from tests.services.procs import (
    FAKE_COMMAND,
    answers,
    kill_holder_and_check_no_orphan,
    pid_alive,
    wait_until,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only process handling")


@pytest.fixture
def fake_mode(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("FAKE_DLX_MODE", "ok")
    return monkeypatch


# ------------------------------------------------------------------ binary path


def test_resolve_binary_in_dev_is_vendor() -> None:
    root = Path(__file__).resolve().parents[2]
    assert resolve_binary() == root / "vendor" / "deeplx.exe"


def test_resolve_binary_frozen_prefers_bundle_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle, app = tmp_path / "_internal", tmp_path / "app"
    bundle.mkdir()
    app.mkdir()
    (bundle / "deeplx.exe").write_bytes(b"")
    (app / "deeplx.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "DLX Tray.exe"))
    assert resolve_binary() == bundle / "deeplx.exe"


def test_resolve_binary_frozen_falls_back_to_install_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "missing"), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "DLX Tray.exe"))
    assert resolve_binary() == tmp_path / "deeplx.exe"


def test_free_port_is_bindable() -> None:
    port = free_port()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


# ------------------------------------------------------------------ lifecycle


def test_start_health_check_and_stop(fake_mode: pytest.MonkeyPatch) -> None:
    dlx = DLXProcess(FAKE_COMMAND)
    dlx.start()
    try:
        assert dlx.is_running()
        banner = httpx.get(dlx.base_url + "/", trust_env=False).json()
        assert banner["ip"] == "127.0.0.1"
        assert dlx.base_url == f"http://127.0.0.1:{banner['port']}"
        assert banner["token"] == dlx.token
        assert len(dlx.token) >= 32
    finally:
        dlx.stop()
    assert not dlx.is_running()
    assert dlx.pid is None
    assert wait_until(lambda: not answers(f"http://127.0.0.1:{banner['port']}"))


def test_each_start_gets_a_new_token_and_port(fake_mode: pytest.MonkeyPatch) -> None:
    with DLXProcess(FAKE_COMMAND) as first:
        token = first.token
    with DLXProcess(FAKE_COMMAND) as second:
        assert second.token != token


def test_context_manager_stops(fake_mode: pytest.MonkeyPatch) -> None:
    with DLXProcess(FAKE_COMMAND) as dlx:
        url = dlx.base_url
        assert answers(url)
    assert not dlx.is_running()


def test_stop_is_idempotent_and_safe_before_start() -> None:
    dlx = DLXProcess(FAKE_COMMAND)
    dlx.stop()
    dlx.stop()
    assert not dlx.is_running()


def test_url_and_token_need_a_started_process() -> None:
    dlx = DLXProcess(FAKE_COMMAND)
    with pytest.raises(RuntimeError):
        _ = dlx.base_url
    with pytest.raises(RuntimeError):
        _ = dlx.token


def test_start_twice_is_an_error(fake_mode: pytest.MonkeyPatch) -> None:
    with DLXProcess(FAKE_COMMAND) as dlx, pytest.raises(RuntimeError):
        dlx.start()


def test_stop_after_the_process_died_on_its_own(fake_mode: pytest.MonkeyPatch) -> None:
    dlx = DLXProcess(FAKE_COMMAND)
    dlx.start()
    assert dlx.pid is not None
    os.kill(dlx.pid, signal.SIGTERM)
    assert wait_until(lambda: not dlx.is_running())
    dlx.stop()
    assert dlx.pid is None


# ------------------------------------------------------------------ failures


def test_empty_command_is_rejected() -> None:
    with pytest.raises(ValueError, match="command"):
        DLXProcess([])


def test_startup_timeout_is_a_hard_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_DLX_MODE", "hang")
    began = time.monotonic()
    with pytest.raises(ProviderUnavailableError):
        DLXProcess(FAKE_COMMAND, startup_timeout=0.5).start()
    assert time.monotonic() - began < 0.5 + 0.6  # the budget plus process spawn/stop


def test_missing_binary(tmp_path: Path) -> None:
    dlx = DLXProcess([str(tmp_path / "deeplx.exe")])
    with pytest.raises(ProviderUnavailableError, match="not found"):
        dlx.start()


def test_default_command_is_the_resolved_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deeplx_process, "resolve_binary", lambda: Path("Z:/nowhere/deeplx.exe"))
    with pytest.raises(ProviderUnavailableError, match="nowhere"):
        DLXProcess().start()


def test_binary_that_cannot_be_executed(tmp_path: Path) -> None:
    not_an_exe = tmp_path / "deeplx.exe"
    not_an_exe.write_bytes(b"not a PE file")
    with pytest.raises(ProviderUnavailableError, match="cannot start"):
        DLXProcess([str(not_an_exe)]).start()


def test_exit_on_startup_retries_then_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("FAKE_DLX_MODE", "exit")
    monkeypatch.setattr(deeplx_process.secrets, "token_urlsafe", lambda _: "-test-token")
    dlx = DLXProcess(FAKE_COMMAND)
    with pytest.raises(ProviderUnavailableError, match="code 3"):
        dlx.start()
    assert not dlx.is_running()
    attempts = [r for r in caplog.records if "during startup" in r.getMessage()]
    assert len(attempts) == deeplx_process.START_ATTEMPTS


def test_no_answer_times_out_and_kills_the_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("FAKE_DLX_MODE", "hang")
    monkeypatch.setenv("FAKE_DLX_PIDFILE", str(pidfile))
    dlx = DLXProcess(FAKE_COMMAND, startup_timeout=1.5)
    with pytest.raises(ProviderUnavailableError, match=r"within 1\.5 s"):
        dlx.start()
    assert not dlx.is_running()
    assert pidfile.is_file()
    # The fake's own pid may be a grandchild (venv launcher); the Job Object kills it too.
    assert wait_until(lambda: not pid_alive(int(pidfile.read_text(encoding="ascii"))))


def test_token_is_never_logged(
    fake_mode: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    with DLXProcess(FAKE_COMMAND) as dlx:
        token = dlx.token
    assert caplog.records
    assert token not in caplog.text


def test_job_object_failure_is_logged_not_fatal(
    fake_mode: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(pid: int) -> int:
        raise OSError("access denied")

    fake_mode.setattr(deeplx_process._win32, "assign_to_new_job", refuse)
    with DLXProcess(FAKE_COMMAND) as dlx:
        assert answers(dlx.base_url)
    assert "Job Object" in caplog.text


# ------------------------------------------------------------------ crash of the parent


def test_no_orphan_after_the_parent_is_killed(fake_mode: pytest.MonkeyPatch) -> None:
    kill_holder_and_check_no_orphan(FAKE_COMMAND)

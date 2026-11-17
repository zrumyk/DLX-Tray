"""Lifetime of the bundled DLX server: spawn on a free loopback port, health-check, stop."""

from __future__ import annotations

import logging
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx

from dlx_tray.core.models import ProviderUnavailableError

if sys.platform == "win32":
    from dlx_tray.services import _win32

log = logging.getLogger(__name__)

BINARY_NAME = "deeplx.exe"
HOST = "127.0.0.1"
DEFAULT_STARTUP_TIMEOUT = 5.0
STOP_TIMEOUT = 5.0
# A process that exits during startup most likely lost the race for its port: try another one.
START_ATTEMPTS = 3
_POLL_INTERVAL = 0.1

_REPO_ROOT = Path(__file__).resolve().parents[3]


def resolve_binary() -> Path:
    """Where ``deeplx.exe`` lives: next to the frozen app, or ``vendor/`` in a dev checkout.

    The path may not exist; ``DLXProcess.start()`` reports that.
    """
    if getattr(sys, "frozen", False):
        candidates = [Path(sys.executable).parent / BINARY_NAME]
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            candidates.insert(0, Path(bundle) / BINARY_NAME)
        return next((path for path in candidates if path.is_file()), candidates[-1])
    return _REPO_ROOT / "vendor" / BINARY_NAME


def free_port(host: str = HOST) -> int:
    """A TCP port that was free a moment ago (the OS picks it)."""
    with socket.socket() as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


class DLXProcess:
    """One DLX child process bound to ``127.0.0.1`` with a random token.

    On Windows the child is put in a Job Object with kill-on-close, so it dies with this process
    even on a crash. ``command`` replaces ``[resolve_binary()]`` (the program, as a path to an
    existing file, and any leading arguments before DLX's flags); tests use it to run a fake.
    """

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    ) -> None:
        self._command = list(command) if command is not None else [str(resolve_binary())]
        if not self._command:
            raise ValueError("command must name the DLX program")
        self._startup_timeout = startup_timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._job: _JobObject | None = None
        self._port = 0
        self._token = ""

    @property
    def base_url(self) -> str:
        if self._process is None:
            raise RuntimeError("DLX is not started")
        return f"http://{HOST}:{self._port}"

    @property
    def token(self) -> str:
        if self._process is None:
            raise RuntimeError("DLX is not started")
        return self._token

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        """Spawn DLX and wait until ``GET /`` answers 200.

        Raises ``ProviderUnavailableError`` if the binary is missing, keeps exiting, or doesn't
        answer within ``startup_timeout``. Nothing is left running after a failure.
        """
        if self._process is not None:
            raise RuntimeError("DLX is already started")
        program = Path(self._command[0])
        if not program.is_file():
            raise ProviderUnavailableError(f"DLX binary not found: {program}")
        exit_code: int | None = None
        for attempt in range(1, START_ATTEMPTS + 1):
            exit_code = self._start_once()
            if exit_code is None:
                log.info("DLX started on %s (pid %s, attempt %d)", self.base_url, self.pid, attempt)
                return
            log.warning("DLX exited with code %s during startup (attempt %d)", exit_code, attempt)
        raise ProviderUnavailableError(
            f"DLX exited with code {exit_code} on startup ({START_ATTEMPTS} attempts)"
        )

    def stop(self) -> None:
        """Terminate DLX if it runs. Safe to call more than once."""
        process, job = self._process, self._job
        self._process, self._job = None, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:  # pragma: no cover - TerminateProcess is immediate
                process.kill()
                process.wait(timeout=STOP_TIMEOUT)
            log.info("DLX stopped (pid %s)", process.pid)
        if job is not None:
            job.close()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def _start_once(self) -> int | None:
        """One spawn + health check. Returns the exit code if DLX died during startup."""
        self._port, self._token = free_port(), secrets.token_urlsafe(24)
        args = [*self._command, "-ip", HOST, "-port", str(self._port), "-token", self._token]
        try:
            self._process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise ProviderUnavailableError(f"cannot start DLX: {exc}") from exc
        self._job = _JobObject.for_process(self._process.pid)
        try:
            exit_code = self._wait_healthy()
        except BaseException:
            self.stop()
            raise
        if exit_code is not None:
            self.stop()
        return exit_code

    def _wait_healthy(self) -> int | None:
        assert self._process is not None
        url = f"http://{HOST}:{self._port}/"
        deadline = time.monotonic() + self._startup_timeout
        with httpx.Client(trust_env=False) as client:  # trust_env: see ADR-017
            while True:
                remaining = deadline - time.monotonic()
                try:
                    answered = client.get(url, timeout=max(min(1.0, remaining), 0.05))
                    healthy = answered.status_code == 200
                except httpx.TransportError:
                    healthy = False
                # Checked before trusting a 200: if our child already died (e.g. it lost the race
                # for its port), the answer came from another process.
                code = self._process.poll()
                if code is not None:
                    return code
                if healthy:
                    return None
                if time.monotonic() >= deadline:
                    raise ProviderUnavailableError(
                        f"DLX did not answer GET / within {self._startup_timeout:g} s"
                    )
                time.sleep(min(_POLL_INTERVAL, max(deadline - time.monotonic(), 0)))


class _JobObject:
    """A Windows Job Object that kills its processes when its last handle closes."""

    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def for_process(cls, pid: int) -> _JobObject | None:
        """Put ``pid`` in a new kill-on-close job. ``None`` off Windows or if Windows refuses."""
        if sys.platform != "win32":  # pragma: no cover - the app and CI run on Windows
            return None
        try:
            return cls(_win32.assign_to_new_job(pid))
        except OSError:
            log.exception("could not put DLX in a Job Object; it may outlive a crash")
            return None

    def close(self) -> None:
        if self._handle and sys.platform == "win32":
            _win32.close_handle(self._handle)
        self._handle = 0

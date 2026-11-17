"""Live test: the DLX client against a real ``vendor/deeplx.exe`` (run with ``-m live``)."""

import os
import secrets
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from dlx_tray.core.models import Language, RateLimitedError
from dlx_tray.providers.deeplx import DeeplxTranslator

BINARY = Path(__file__).resolve().parents[2] / "vendor" / "deeplx.exe"

pytestmark = pytest.mark.live


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def dlx_server() -> Iterator[tuple[str, str]]:
    """A running DLX as ``(base_url, token)``. ``DLX_DEEPLX_URL`` (+ ``_TOKEN``) reuses one."""
    external = os.environ.get("DLX_DEEPLX_URL")
    if external:
        yield external, os.environ.get("DLX_DEEPLX_TOKEN", "")
        return
    if not BINARY.is_file():
        pytest.skip("vendor/deeplx.exe missing: run scripts/fetch_deeplx.py")
    port, token = _free_port(), secrets.token_urlsafe(24)
    process = subprocess.Popen(
        [str(BINARY), "-ip", "127.0.0.1", "-port", str(port), "-token", token],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                if httpx.get(base + "/", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if process.poll() is not None:
                pytest.fail(f"deeplx.exe exited with code {process.returncode} on startup")
            if time.monotonic() > deadline:
                pytest.fail("DLX did not come up within 10 s")
            time.sleep(0.2)
        yield base, token
    finally:
        process.kill()
        process.wait(timeout=10)


def test_translates_hello(dlx_server: tuple[str, str]) -> None:
    base, token = dlx_server
    with DeeplxTranslator(base, token) as dlx:
        try:
            [result] = dlx.translate(["Hello"], Language.EN, Language.UK)
        except RateLimitedError as exc:  # DeepL blocks the IP for a while; never retry in a loop
            pytest.skip(f"DeepL rate-limited this IP (the 429 mapping works): {exc}")
    assert result.text.strip()
    assert result.text != "Hello"
    assert result.provider == "dlx"

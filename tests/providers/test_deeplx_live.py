"""Live test: the DLX client against a real ``vendor/deeplx.exe`` (run with ``-m live``)."""

import os
from collections.abc import Iterator

import pytest

from dlx_tray.core.models import Language, RateLimitedError
from dlx_tray.providers.deeplx import DeeplxTranslator
from dlx_tray.services.deeplx_process import DLXProcess, resolve_binary

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def dlx_server() -> Iterator[tuple[str, str]]:
    """A running DLX as ``(base_url, token)``. ``DLX_DEEPLX_URL`` (+ ``_TOKEN``) reuses one."""
    external = os.environ.get("DLX_DEEPLX_URL")
    if external:
        yield external, os.environ.get("DLX_DEEPLX_TOKEN", "")
        return
    if not resolve_binary().is_file():
        pytest.skip("vendor/deeplx.exe missing: run scripts/fetch_deeplx.py")
    with DLXProcess(startup_timeout=10) as dlx:
        yield dlx.base_url, dlx.token


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

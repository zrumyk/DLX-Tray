"""Live: DLXProcess with the real ``vendor/deeplx.exe`` (run with ``-m live``). No DeepL calls."""

from __future__ import annotations

import httpx
import pytest

from dlx_tray.services.deeplx_process import DLXProcess, resolve_binary
from tests.services.procs import answers, kill_holder_and_check_no_orphan, wait_until

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not resolve_binary().is_file(), reason="run scripts/fetch_deeplx.py"),
]


def test_real_binary_starts_answers_and_stops() -> None:
    with DLXProcess() as dlx:
        url = dlx.base_url
        assert httpx.get(url + "/", trust_env=False).status_code == 200
        # The token is enforced: an unauthenticated translate is refused locally.
        response = httpx.post(url + "/translate", json={"text": "x"}, trust_env=False)
        assert response.status_code == 401
    assert wait_until(lambda: not answers(url))


def test_real_binary_leaves_no_orphan_when_the_parent_is_killed() -> None:
    kill_holder_and_check_no_orphan([])

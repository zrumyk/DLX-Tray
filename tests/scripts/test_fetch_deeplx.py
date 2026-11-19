"""Check the DLX source patch and fail-safe binary replacement."""

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import fetch_deeplx


def test_patch_only_replaces_free_endpoint(tmp_path: Path) -> None:
    source = tmp_path / "translate.go"
    endpoint = fetch_deeplx.UPSTREAM_FREE_ENDPOINT
    original = f'// {endpoint}\noneshotFreeEndpoint = "{endpoint}"\n'
    source.write_bytes(original.encode())

    fetch_deeplx.patch_free_endpoint(source, "https://example.org/v1/translate")

    assert source.read_bytes().decode() == (
        f'// {endpoint}\noneshotFreeEndpoint = "https://example.org/v1/translate"\n'
    )
    with pytest.raises(ValueError, match="exactly one"):
        fetch_deeplx.patch_free_endpoint(source, "https://example.org/v1/translate")


def test_failed_patched_build_preserves_existing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "vendor" / "deeplx.exe"
    target.parent.mkdir()
    target.write_bytes(b"existing binary")
    lock = tmp_path / "deeplx.lock.json"
    lock.write_text(
        json.dumps(
            {
                "version": "test",
                "target": "vendor/deeplx.exe",
                "patched_build": {"sha256": "0" * 64},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(fetch_deeplx, "ROOT", tmp_path)
    monkeypatch.setattr(fetch_deeplx, "LOCK_FILE", lock)
    monkeypatch.setattr(sys, "argv", ["fetch_deeplx.py", "--force"])

    def build(_lock: dict[str, str], _work: Path, binary: Path) -> None:
        binary.write_bytes(b"unexpected binary")

    monkeypatch.setattr(fetch_deeplx, "build_patched_dlx", build)
    download = Mock()
    monkeypatch.setattr(fetch_deeplx, "download", download)

    assert fetch_deeplx.main() == 1
    assert target.read_bytes() == b"existing binary"
    assert list(target.parent.iterdir()) == [target]
    download.assert_not_called()

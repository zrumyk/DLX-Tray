"""Check the DLX source patch and fail-safe binary replacement."""

import json
import sys
from pathlib import Path
from unittest.mock import Mock
from zipfile import ZipFile

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


@pytest.mark.parametrize(
    ("build_key", "endpoint"),
    [("patched_build", "https://example.org/v1/translate"), ("source_build", None)],
)
def test_failed_source_build_preserves_existing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_key: str, endpoint: str | None
) -> None:
    target = tmp_path / "vendor" / "deeplx.exe"
    target.parent.mkdir()
    target.write_bytes(b"existing binary")
    lock = tmp_path / "deeplx.lock.json"
    build_lock = {"sha256": "0" * 64, "revision": "fixture"}
    if endpoint is not None:
        build_lock["free_endpoint"] = endpoint
    lock.write_text(
        json.dumps(
            {
                "version": "test",
                "target": "vendor/deeplx.exe",
                build_key: build_lock,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(fetch_deeplx, "ROOT", tmp_path)
    monkeypatch.setattr(fetch_deeplx, "LOCK_FILE", lock)
    monkeypatch.setattr(sys, "argv", ["fetch_deeplx.py", "--force"])

    def build(
        build: dict[str, str], _work: Path, binary: Path, *, free_endpoint: str | None
    ) -> None:
        assert build == build_lock
        assert free_endpoint == endpoint
        binary.write_bytes(b"unexpected binary")

    monkeypatch.setattr(fetch_deeplx, "build_dlx_from_source", build)
    download = Mock()
    monkeypatch.setattr(fetch_deeplx, "download", download)

    assert fetch_deeplx.main() == 1
    assert target.read_bytes() == b"existing binary"
    assert list(target.parent.iterdir()) == [target]
    download.assert_not_called()


def test_direct_source_build_keeps_archive_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_bytes = b"package translate\n"
    lock = {
        "source_url": "https://example.org/fork/fixture.zip",
        "source_sha256": "1" * 64,
        "go_url": "https://example.org/go.zip",
        "go_sha256": "2" * 64,
        "revision": "fixture",
    }

    def download(url: str, dest: Path, expected: str) -> None:
        with ZipFile(dest, "w") as archive:
            if url == lock["source_url"]:
                assert expected == lock["source_sha256"]
                archive.writestr("DLX-fixture/translate/translate.go", source_bytes)
            else:
                assert url == lock["go_url"]
                assert expected == lock["go_sha256"]
                archive.writestr("go/bin/go.exe", b"fixture")

    binary = tmp_path / "deeplx.exe"

    def run(args: list[str], *, cwd: Path, env: dict[str, str], check: bool) -> None:
        assert (cwd / "translate" / "translate.go").read_bytes() == source_bytes
        assert Path(args[0]) == tmp_path / "go" / "bin" / "go.exe"
        assert args[1:] == [
            "build",
            "-trimpath",
            "-buildvcs=false",
            "-mod=readonly",
            "-o",
            str(binary),
            ".",
        ]
        assert env["GOOS"] == "windows" and env["GOARCH"] == "amd64" and check

    monkeypatch.setattr(fetch_deeplx, "download", download)
    monkeypatch.setattr(fetch_deeplx.subprocess, "run", run)

    fetch_deeplx.build_dlx_from_source(lock, tmp_path, binary)

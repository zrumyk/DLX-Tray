"""Prepare the pinned DLX Windows binary in vendor/ and verify its SHA-256."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import httpx

ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = ROOT / "scripts" / "deeplx.lock.json"
UPSTREAM_FREE_ENDPOINT = "https://oneshot-free.www.deepl.com/v1/translate"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> None:
    actual = sha256_of(path)
    if actual != expected.lower():
        raise ValueError(f"{path.name} SHA-256 mismatch (expected {expected}, got {actual})")


def download(url: str, dest: Path, expected: str) -> None:
    with (
        httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response,
        dest.open("wb") as output,
    ):
        response.raise_for_status()
        for chunk in response.iter_bytes():
            output.write(chunk)
    verify_sha256(dest, expected)


def patch_free_endpoint(path: Path, endpoint: str) -> None:
    original = f'oneshotFreeEndpoint = "{UPSTREAM_FREE_ENDPOINT}"'.encode()
    patched = f'oneshotFreeEndpoint = "{endpoint}"'.encode()
    source = path.read_bytes()
    if source.count(original) != 1:
        raise ValueError("Expected exactly one upstream DLX free endpoint in translate.go")
    path.write_bytes(source.replace(original, patched))


def build_dlx_from_source(
    build: dict[str, str], work: Path, binary: Path, *, free_endpoint: str | None = None
) -> None:
    source_zip = work / "source.zip"
    go_zip = work / "go.zip"
    download(build["source_url"], source_zip, build["source_sha256"])
    download(build["go_url"], go_zip, build["go_sha256"])
    for archive in (source_zip, go_zip):
        with ZipFile(archive) as verified_zip:
            verified_zip.extractall(work)

    source = work / f"DLX-{build['revision']}"
    if free_endpoint is not None:
        patch_free_endpoint(source / "translate" / "translate.go", free_endpoint)
    go_root = work / "go"
    env = {
        **os.environ,
        "CGO_ENABLED": "0",
        "GOAMD64": "v1",
        "GOARCH": "amd64",
        "GOENV": "off",
        "GOFLAGS": "",
        "GOOS": "windows",
        "GOROOT": str(go_root),
        "GOTOOLCHAIN": "local",
        "GOWORK": "off",
    }
    subprocess.run(
        [
            str(go_root / "bin" / "go.exe"),
            "build",
            "-trimpath",
            "-buildvcs=false",
            "-mod=readonly",
            "-o",
            str(binary),
            ".",
        ],
        cwd=source,
        env=env,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare the pinned DLX binary in vendor/.")
    parser.add_argument("--force", action="store_true", help="prepare again even if present")
    args = parser.parse_args()

    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    target = ROOT / lock["target"]
    try:
        patched = lock.get("patched_build")
        source = lock.get("source_build")
        if patched is not None and source is not None:
            raise ValueError("Choose either source_build or patched_build in the DLX lock")
        build = source if patched is None else patched
        expected = (build if build is not None else lock)["sha256"]
        if target.is_file() and not args.force and sha256_of(target) == expected.lower():
            print(f"DLX {lock['version']} already present: {target}")
            return 0
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent, prefix="dlx-build-") as tmp:
            work = Path(tmp)
            binary = work / target.name
            if build is None:
                print(f"Downloading DLX {lock['version']} from {lock['url']}", flush=True)
                download(lock["url"], binary, expected)
            else:
                print(f"Building DLX {lock['version']} from {build['revision']}", flush=True)
                free_endpoint = None if patched is None else patched["free_endpoint"]
                build_dlx_from_source(build, work, binary, free_endpoint=free_endpoint)
                verify_sha256(binary, expected)
            binary.replace(target)
    except (OSError, ValueError, BadZipFile, httpx.HTTPError, subprocess.SubprocessError) as exc:
        print(f"Could not prepare DLX: {exc}", file=sys.stderr)
        return 1

    print(f"OK: {target} (sha256 verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

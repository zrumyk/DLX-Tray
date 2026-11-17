"""Download the pinned DLX (DeepLX) Windows binary into vendor/ and verify its SHA-256.

Usage:
    uv run python scripts/fetch_deeplx.py           # download if missing or mismatched
    uv run python scripts/fetch_deeplx.py --force   # always re-download

The version, URL and checksum are pinned in scripts/deeplx.lock.json.
To upgrade DLX, update that file (see .dev/docs/DEEPLX.md for the procedure).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = ROOT / "scripts" / "deeplx.lock.json"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False, suffix=".part") as tmp:
        tmp_path = Path(tmp.name)
        with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                tmp.write(chunk)
    tmp_path.replace(dest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download the pinned DLX binary into vendor/.")
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    args = parser.parse_args()

    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    target = ROOT / lock["target"]
    expected = lock["sha256"].lower()

    if target.exists() and not args.force:
        if sha256_of(target) == expected:
            print(f"DLX {lock['version']} already present: {target}")
            return 0
        print("Existing binary has a different checksum, re-downloading.")

    print(f"Downloading DLX {lock['version']} from {lock['url']}")
    download(lock["url"], target)

    actual = sha256_of(target)
    if actual != expected:
        target.unlink(missing_ok=True)
        print(f"Checksum mismatch!\n  expected {expected}\n  actual   {actual}", file=sys.stderr)
        return 1

    print(f"OK: {target} (sha256 verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

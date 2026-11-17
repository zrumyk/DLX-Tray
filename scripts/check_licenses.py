"""Fail if a runtime dependency's license is incompatible with shipping DLX Tray under MIT.

Run it in an isolated environment that holds only the runtime dependencies:

    uv run --isolated --no-dev --with pip-licenses python scripts/check_licenses.py

Dev-only tools (pytest, hypothesis, ...) are not shipped, so they are not checked.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

# Copyleft families that always need review. They are checked before ALLOWED, so a dual
# "MIT OR GPL" license still fails here and must be reviewed by hand.
DENIED = re.compile(r"GPL|General Public License|EUPL|SSPL|Commons Clause", re.IGNORECASE)

# Whole words/phrases (case-insensitive) of license names that are fine to bundle in an MIT app.
ALLOWED = (
    "MIT",
    "BSD",
    "Apache",
    "ISC",
    "Python Software Foundation",
    "PSF",
    "Mozilla Public License 2.0",
    "MPL-2.0",
    "Unlicense",
    "Zlib",
    "HPND",
)
ALLOWED_RE = re.compile(r"\b(" + "|".join(map(re.escape, ALLOWED)) + r")\b", re.IGNORECASE)

# Reviewed exceptions: package-name prefix -> the reason (refers to an ADR in DECISIONS.md).
EXCEPTION_PREFIXES = {
    "pyside6": "Qt for Python, LGPL-3.0 option, dynamically linked (ADR-001)",
    "shiboken6": "Qt for Python, LGPL-3.0 option, dynamically linked (ADR-001)",
}

# The checker itself and its helpers end up in the isolated env; they are not shipped.
TOOLING = {"pip_licenses", "prettytable", "wcwidth", "tomli"}


def normalize(name: str) -> str:
    return name.lower().replace("-", "_")


def is_allowed(license_name: str) -> bool:
    return not DENIED.search(license_name) and bool(ALLOWED_RE.search(license_name))


def main() -> int:
    raw = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--from=mixed", "--format=json"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout

    problems: list[str] = []
    for pkg in json.loads(raw):
        name, version, license_name = pkg["Name"], pkg["Version"], pkg["License"]
        key = normalize(name)
        if key in TOOLING:
            continue
        reason = next((r for p, r in EXCEPTION_PREFIXES.items() if key.startswith(p)), None)
        if reason:
            print(f"  exception  {name} {version}: {license_name} ({reason})")
        elif is_allowed(license_name):
            print(f"  ok         {name} {version}: {license_name}")
        else:
            problems.append(f"{name} {version}: {license_name}")

    if problems:
        print("\nLicenses that need review (extend ALLOWED/EXCEPTION_PREFIXES only with an ADR):")
        for line in problems:
            print(f"  FAIL       {line}")
        return 1
    print("\nAll runtime dependency licenses are compatible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

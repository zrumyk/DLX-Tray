"""Repository-level guards: source hygiene and the dev scripts' own logic."""

from __future__ import annotations

import importlib.util
import unicodedata
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("src", "tests", "scripts")


def _source_files() -> list[Path]:
    files: list[Path] = []
    for folder in SOURCE_DIRS:
        files += [p for p in (ROOT / folder).rglob("*.py") if "fixtures" not in p.parts]
    return files


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_invisible_characters_in_source(path: Path) -> None:
    """Invisible characters (ZWJ, NBSP, variation selectors, combining marks...) must be written
    as escapes, so they can be seen in review. Test data belongs in tests/fixtures/."""
    text = path.read_text(encoding="utf-8")
    bad = sorted(
        {
            f"U+{ord(c):04X}"
            for c in text
            if unicodedata.category(c) in ("Cf", "Mn", "Zs", "Cc") and c not in " \n\t"
        }
    )
    assert not bad, f"use \\uXXXX escapes instead of literal {', '.join(bad)}"


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("license_name", "allowed"),
    [
        ("MIT", True),
        ("MIT License", True),
        ("MIT-0", True),
        ("MIT-CMU", True),
        ("BSD-3-Clause", True),
        ("Apache-2.0 OR BSD-3-Clause", True),
        ("Apache Software License", True),
        ("PSF-2.0", True),
        ("Mozilla Public License 2.0 (MPL 2.0)", True),
        ("GPL-3.0-only", False),
        ("LGPL-3.0-only", False),
        ("AGPL-3.0", False),
        ("GNU General Public License v2 (GPLv2)", False),
        ("MIT OR GPL-2.0", False),
        ("LIMITED commercial license", False),
        ("UNKNOWN", False),
        ("", False),
    ],
)
def test_license_classification(license_name: str, allowed: bool) -> None:
    assert _load_script("check_licenses").is_allowed(license_name) is allowed

"""Load Qt and DLX Tray catalogs for the selected interface language."""

from __future__ import annotations

import logging
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from PySide6.QtCore import QLibraryInfo, Qt, QTranslator

if TYPE_CHECKING:
    from collections.abc import Iterator

    from PySide6.QtWidgets import QApplication

    from dlx_tray.config import UiLanguage

log = logging.getLogger(__name__)

_CATALOG = Path(__file__).resolve().parents[1] / "i18n" / "uk.qm"


def _ukrainian_catalog() -> bytes:
    if _CATALOG.is_file():
        return _CATALOG.read_bytes()

    executables = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.LibraryExecutablesPath))
    compiler = executables / ("lrelease.exe" if os.name == "nt" else "lrelease")
    with TemporaryDirectory() as directory:
        output = Path(directory) / _CATALOG.name
        subprocess.run(
            [compiler, "-silent", _CATALOG.with_suffix(".ts"), "-qm", output],
            check=True,
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return output.read_bytes()


@contextmanager
def interface_language(app: QApplication, language: UiLanguage) -> Iterator[None]:
    if language == "en":
        yield
        return

    app_translator = QTranslator()
    try:
        # Keep the catalog bytes alive for as long as Qt can use the translator.
        catalog = _ukrainian_catalog()
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not compile or read the Ukrainian UI catalog (%s)", type(exc).__name__)
        yield
        return
    if not app_translator.load(catalog):
        log.warning("Could not load the Ukrainian UI catalog")
        yield
        return

    qt_translator = QTranslator()
    qt_translations = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if qt_translator.load("qtbase_uk", qt_translations):
        app.installTranslator(qt_translator)
    else:
        log.warning("Could not load the Ukrainian Qt catalog")

    # Native Windows file dialog labels follow Windows' language.
    attribute = Qt.ApplicationAttribute.AA_DontUseNativeDialogs
    used_qt_dialogs = app.testAttribute(attribute)
    app.setAttribute(attribute)
    app.installTranslator(app_translator)
    try:
        yield
    finally:
        app.removeTranslator(app_translator)
        app.removeTranslator(qt_translator)
        app.setAttribute(attribute, used_qt_dialogs)

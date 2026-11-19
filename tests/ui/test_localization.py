"""Verify that Ukrainian catalogs match the source and apply at startup."""

import os
import shutil
import subprocess
import sysconfig
from contextlib import ExitStack
from pathlib import Path
from string import Formatter
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFileDialog, QLabel
from pytestqt.qtbot import QtBot

from dlx_tray import app, config
from dlx_tray.config import Settings
from dlx_tray.ui import localization
from dlx_tray.ui.main_window import MainWindow
from dlx_tray.ui.settings_dialog import SettingsDialog

SOURCE = Path(__file__).resolve().parents[2] / "src" / "dlx_tray"
CATALOG = SOURCE / "i18n" / "uk.ts"


def _messages(path: Path) -> dict[tuple[str, str], ElementTree.Element]:
    return {
        (context.findtext("name", ""), message.findtext("source", "")): message
        for context in ElementTree.parse(path).getroot().findall("context")
        for message in context.findall("message")
    }


def test_ukrainian_catalog_covers_extracted_strings(tmp_path: Path) -> None:
    extracted = tmp_path / "uk.ts"
    script = "pyside6-lupdate.exe" if os.name == "nt" else "pyside6-lupdate"
    subprocess.run(
        [
            Path(sysconfig.get_path("scripts")) / script,
            "-extensions",
            "py",
            SOURCE,
            "-ts",
            extracted,
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    messages = _messages(CATALOG)
    assert messages.keys() == _messages(extracted).keys()

    formatter = Formatter()
    for (context, source), message in messages.items():
        translation = message.find("translation")
        assert translation is not None and translation.text and not translation.attrib, (
            context,
            source,
        )
        assert sorted(field for _, field, _, _ in formatter.parse(translation.text) if field) == (
            sorted(field for _, field, _, _ in formatter.parse(source) if field)
        ), (context, source)


def test_saved_interface_language_applies_on_next_start(
    qapp: QApplication, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    monkeypatch.setattr(config, "app_data_dir", lambda: profile)
    monkeypatch.delenv("DLX_UI_LANGUAGE", raising=False)
    monkeypatch.setattr(app, "QApplication", lambda _: qapp)
    compiled = tmp_path / "uk.qm"
    shutil.copyfile(CATALOG, compiled.with_suffix(".ts"))
    monkeypatch.setattr(localization, "_CATALOG", compiled)
    Settings(ui_language="en").save()

    def check_window(_app: QApplication, settings: Settings, _stack: ExitStack) -> int:
        window = MainWindow()
        qtbot.addWidget(window)
        change_to = "uk" if settings.ui_language == "en" else "en"
        button_text = "Translate" if change_to == "uk" else "Перекласти"
        assert window.translate_button.text() == button_text
        if settings.ui_language == "uk":
            assert QCoreApplication.translate("Startup", "DLX ready") == "DLX готовий"
            assert qapp.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
            picker = QFileDialog(window)
            label = picker.findChild(QLabel, "fileNameLabel")
            assert label is not None
            assert label.text() == "&Ім'я файлу:"

        def save(updated: Settings, _api_key: str | None) -> str | None:
            updated.save(changes_from=settings)
            return None

        dialog = SettingsDialog(settings, None, key_overridden=False, save=save, check_key=Mock())
        qtbot.addWidget(dialog)
        dialog.ui_language.setCurrentIndex(dialog.ui_language.findData(change_to))
        save_button = dialog.buttons.button(QDialogButtonBox.StandardButton.Save)
        if settings.ui_language == "uk":
            assert save_button.text().replace("&", "") == "Зберегти"
        save_button.click()
        assert window.translate_button.text() == button_text
        assert QCoreApplication.translate("MainWindow", "Translate") == button_text
        return 0

    monkeypatch.setattr(app, "_run_window", check_window)
    for expected in ("uk", "en"):
        assert app.run([]) == 0
        assert Settings.load().ui_language == expected
        assert QCoreApplication.translate("MainWindow", "Translate") == "Translate"
    assert not compiled.exists()

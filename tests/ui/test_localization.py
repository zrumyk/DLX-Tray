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
from dlx_tray.core.models import Language
from dlx_tray.core.pipeline import JobResult
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


def _check_language_lists(window: MainWindow, ukrainian: bool) -> None:
    source, target = window.source_languages, window.target_languages
    assert source.itemData(0) == source.currentData() == Language.AUTO
    assert target.currentData() == Language.PT_BR
    assert {source.itemData(i) for i in range(source.count())} == {
        language.value for language in Language if language.can_be_source
    }
    assert {target.itemData(i) for i in range(target.count())} == {
        language.value for language in Language if language.can_be_target
    }
    assert source.itemText(0) == ("Визначити автоматично" if ukrainian else "Auto-detect")
    assert target.currentText() == (
        "Португальська (бразильська)" if ukrainian else "Portuguese (Brazilian)"
    )
    for combo in (source, target):
        czech = combo.findData(Language.CS)
        indonesian = combo.findData(Language.ID)
        korean = combo.findData(Language.KO)
        assert combo.itemText(czech) == ("Чеська" if ukrainian else "Czech")
        if ukrainian:
            assert indonesian < korean < czech
        else:
            assert czech < indonesian < korean

    window._show_finished(JobResult("Translated", Language.EN))
    assert window.detected_language.text() == (
        "Визначено мову: Англійська" if ukrainian else "Detected: English"
    )


def test_saved_interface_language_applies_on_next_start(
    qapp: QApplication, qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "app_data_dir", lambda: profile)
    monkeypatch.delenv("DLX_UI_LANGUAGE", raising=False)
    monkeypatch.setattr(app, "QApplication", lambda _: qapp)
    compiled = tmp_path / "uk.qm"
    shutil.copyfile(CATALOG, compiled.with_suffix(".ts"))
    shutil.copyfile(CATALOG.with_name("qt_uk.ts"), tmp_path / "qt_uk.ts")
    monkeypatch.setattr(localization, "_CATALOG", compiled)
    Settings(ui_language="en", target_lang=Language.PT_BR).save()

    def check_window(
        _app: QApplication,
        settings: Settings,
        _stack: ExitStack,
        _server: object,
        *,
        start_in_tray: bool,
    ) -> int:
        assert not start_in_tray
        window = MainWindow(target=settings.target_lang, ui_language=settings.ui_language)
        qtbot.addWidget(window)
        change_to = "uk" if settings.ui_language == "en" else "en"
        button_text = "Translate" if change_to == "uk" else "Перекласти"
        assert window.translate_button.text() == button_text
        _check_language_lists(window, ukrainian=settings.ui_language == "uk")
        if settings.ui_language == "uk":
            assert QCoreApplication.translate("Startup", "DLX ready") == "DLX готовий"
            assert qapp.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
            picker = QFileDialog(window)
            labels = {label.objectName(): label.text() for label in picker.findChildren(QLabel)}
            assert labels["fileNameLabel"] == "&Ім'я файлу:"
            assert labels["lookInLabel"] == "&Тека:"
            assert labels["fileTypeLabel"] == "Тип &файлів:"
            assert QCoreApplication.translate("QFileDialog", "Show ") == "Показати "

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

"""Exercise history dialog mutations and error handling with a real SQLite file."""

from functools import partial
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox, QWidget
from pytestqt.qtbot import QtBot

from dlx_tray.core.models import Language
from dlx_tray.services.history import HistoryStore
from dlx_tray.ui.history_dialog import HistoryDialog


def test_clear_requires_confirmation_and_applies_to_hidden_entries(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path, 10)
    for source in ("alpha", "beta"):
        store.add(
            source_lang=Language.EN,
            target_lang=Language.UK,
            source_text=source,
            target_text="Переклад",
            provider="dlx",
        )
    parent = QWidget()
    qtbot.addWidget(parent)
    dialog = HistoryDialog(partial(HistoryStore, path, 10), parent)
    dialog.show()
    try:
        dialog.search.setText("alpha")
        qtbot.waitUntil(lambda: dialog.entries.count() == 1)
        monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
        dialog.clear_button.click()
        assert len(store.list()) == 2
        monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
        dialog.clear_button.click()
        qtbot.waitUntil(lambda: dialog.status.text() == "No matching translations.")
        assert not store.list()
    finally:
        dialog.wait_for_history()
        dialog.close()


def test_database_open_failure_is_visible(qtbot: QtBot, tmp_path: Path) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    dialog = HistoryDialog(partial(HistoryStore, tmp_path, 10), parent)
    dialog.show()
    try:
        qtbot.waitUntil(lambda: dialog.status.text() == "Could not access translation history.")
        assert not dialog.open_button.isEnabled()
        assert not dialog.delete_button.isEnabled()
        assert not dialog.clear_button.isEnabled()
    finally:
        dialog.wait_for_history()
        dialog.close()

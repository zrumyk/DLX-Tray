"""Browse, search and manage saved translations."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

from PySide6.QtCore import QDateTime, QLocale, Qt, QThread, QTimer, Slot
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dlx_tray.services.history import HistoryEntry, HistoryStore

log = logging.getLogger(__name__)


class _HistoryQuery(QThread):
    def __init__(
        self,
        open_store: Callable[[], HistoryStore],
        query: str,
        change: Callable[[HistoryStore], None] | None,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.query = query
        self.entries: list[HistoryEntry] | None = None
        self._open_store = open_store
        self._change = change

    def run(self) -> None:
        try:
            store = self._open_store()
            if self._change is not None:
                self._change(store)
            self.entries = store.search(self.query)
        except (sqlite3.Error, OSError, ValueError) as exc:
            log.warning("Could not access translation history (%s)", type(exc).__name__)


class HistoryDialog(QDialog):
    def __init__(self, open_store: Callable[[], HistoryStore], parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Translation history"))
        self.resize(760, 460)
        self._open_store = open_store
        self._worker: _HistoryQuery | None = None
        self._entries: list[HistoryEntry] = []
        self._current = False
        self._close_pending = False
        self.selected_entry: HistoryEntry | None = None

        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("Search source text and translations"))
        self.search.setAccessibleName(self.tr("Search history"))
        self.entries = QListWidget()
        self.entries.setAccessibleName(self.tr("Saved translations"))
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.open_button = QPushButton(self.tr("&Open in editors"))
        self.delete_button = QPushButton(self.tr("&Delete"))
        self.clear_button = QPushButton(self.tr("Clear &all"))
        close_button = QPushButton(self.tr("&Close"))
        for button in (self.open_button, self.delete_button, self.clear_button, close_button):
            button.setAutoDefault(False)

        actions = QHBoxLayout()
        actions.addWidget(self.open_button)
        actions.addWidget(self.delete_button)
        actions.addWidget(self.clear_button)
        actions.addStretch()
        actions.addWidget(close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(self.entries, 1)
        layout.addWidget(self.status)
        layout.addLayout(actions)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self._load)
        self.search.textChanged.connect(self._search_changed)
        self.entries.currentRowChanged.connect(self._update_actions)
        self.entries.itemActivated.connect(self._open_entry)
        self.open_button.clicked.connect(self._open_entry)
        self.delete_button.clicked.connect(self._delete_entry)
        self.clear_button.clicked.connect(self._clear_entries)
        close_button.clicked.connect(self.reject)
        self.search.setFocus()
        self._load()

    def wait_for_history(self) -> None:
        if self._worker is not None:
            self._worker.wait()

    def reject(self) -> None:
        if self._worker is None:
            super().reject()
        else:
            self._close_pending = True
            self._debounce.stop()
            self.search.setEnabled(False)

    @Slot()
    def _search_changed(self) -> None:
        self._current = False
        self._update_actions()
        self._debounce.start()

    @Slot()
    def _load(self) -> None:
        if self._worker is None:
            self._start()

    def _start(self, change: Callable[[HistoryStore], None] | None = None) -> None:
        self._current = False
        self._update_actions()
        self.search.setEnabled(change is None)
        self.status.setText(self.tr("Loading history…"))
        worker = _HistoryQuery(self._open_store, self.search.text(), change, self)
        self._worker = worker
        worker.finished.connect(self._loaded)
        worker.start()

    @Slot()
    def _loaded(self) -> None:
        worker = self._worker
        assert worker is not None
        worker.wait()
        self._worker = None
        worker.deleteLater()
        if self._close_pending:
            self.reject()
            return
        if not self.isVisible():
            return
        self.search.setEnabled(True)
        if worker.query != self.search.text():
            if not self._debounce.isActive():
                self._load()
            return

        self._debounce.stop()
        self._entries = worker.entries or []
        self.entries.clear()
        locale = QLocale()
        for entry in self._entries:
            date_time = QDateTime.fromMSecsSinceEpoch(int(entry.created_at.timestamp() * 1000))
            timestamp = locale.toString(date_time, QLocale.FormatType.ShortFormat)
            source = " ".join(entry.source_text[:160].split())
            target = " ".join(entry.target_text[:160].split())
            if entry.file_name:
                source = self.tr("{file}: {text}").format(file=entry.file_name, text=source)
            self.entries.addItem(
                self.tr("{time} · {source_lang} → {target_lang} · {source} → {target}").format(
                    time=timestamp,
                    source_lang=entry.source_lang.value,
                    target_lang=entry.target_lang.value,
                    source=source,
                    target=target,
                )
            )
        self._current = worker.entries is not None
        if not self._current:
            message = self.tr("Could not access translation history.")
        elif not self._entries:
            message = (
                self.tr("No matching translations.")
                if worker.query
                else self.tr("History is empty.")
            )
        else:
            message = ""
        self.status.setText(message)
        self._update_actions()

    def _selected(self) -> HistoryEntry | None:
        row = self.entries.currentRow()
        return self._entries[row] if self._current and 0 <= row < len(self._entries) else None

    @Slot()
    def _update_actions(self) -> None:
        selected = self._selected() is not None
        self.open_button.setEnabled(selected)
        self.delete_button.setEnabled(selected)
        self.clear_button.setEnabled(
            self._current and (bool(self._entries) or bool(self.search.text()))
        )

    @Slot()
    def _open_entry(self) -> None:
        self.selected_entry = self._selected()
        if self.selected_entry is not None:
            self.accept()

    @Slot()
    def _delete_entry(self) -> None:
        entry = self._selected()
        if entry is not None:
            self._start(lambda store: store.delete(entry.id))

    @Slot()
    def _clear_entries(self) -> None:
        answer = QMessageBox.question(
            self,
            self.tr("Clear translation history?"),
            self.tr("Delete all saved translations, including entries outside this search?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start(HistoryStore.clear)

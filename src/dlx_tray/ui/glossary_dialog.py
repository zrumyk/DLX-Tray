"""Manage saved glossaries and review candidate document terms."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dlx_tray.contracts.cloud_v1 import ExtractedTerm
from dlx_tray.contracts.glossary_match import canonical_term
from dlx_tray.core.models import GlossaryEntry, Language
from dlx_tray.services.glossary import (
    Glossary,
    GlossaryFormatError,
    GlossaryStore,
    validate_entries,
)

log = logging.getLogger(__name__)
type GlossarySelection = tuple[Glossary, tuple[GlossaryEntry, ...]]


class _TermTable(QWidget):
    def __init__(
        self,
        entries: tuple[GlossaryEntry, ...],
        parent: QWidget,
        *,
        candidates: tuple[ExtractedTerm, ...] = (),
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget()
        self.table.setAccessibleName(self.tr("Glossary terms"))
        headers = [
            self.tr("Source term"),
            self.tr("Equivalent"),
            self.tr("Part of speech"),
            self.tr("Notes"),
            self.tr("Priority"),
        ]
        if candidates:
            headers.extend((self.tr("Category"), self.tr("Occurrences")))
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for column in (0, 1, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        if candidates:
            for term in candidates:
                self._append(
                    GlossaryEntry(term.source_term, term.target_term, notes=term.note), term
                )
        else:
            for entry in entries:
                self._append(entry)
        actions = QHBoxLayout()
        add = QPushButton(self.tr("Add term"))
        add.clicked.connect(self._add)
        actions.addWidget(add)
        remove = QPushButton(self.tr("Remove selected terms"))
        remove.clicked.connect(self._remove)
        actions.addWidget(remove)
        actions.addStretch()
        layout.addLayout(actions)

    def _append(self, entry: GlossaryEntry, candidate: ExtractedTerm | None = None) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        fields = (
            entry.source_term,
            entry.target_term,
            entry.pos or "",
            entry.notes or "",
            str(entry.priority),
        )
        for column, value in enumerate(fields):
            self.table.setItem(row, column, QTableWidgetItem(value))
        if candidate is not None:
            for column, value in enumerate((candidate.category, str(candidate.count)), start=5):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, column, item)

    def _add(self) -> None:
        row = self.table.rowCount()
        self._append(GlossaryEntry("", ""))
        self.table.setCurrentCell(row, 0)
        item = self.table.item(row, 0)
        assert item is not None
        self.table.editItem(item)

    def _remove(self) -> None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        for row in sorted(rows, reverse=True):
            self.table.removeRow(row)

    def entries(self) -> tuple[GlossaryEntry, ...]:
        result = []
        for row in range(self.table.rowCount()):
            values = []
            for column in range(5):
                item = self.table.item(row, column)
                assert item is not None
                values.append(item.text())
            source, target, pos, notes, priority = values
            result.append(
                GlossaryEntry(source, target, int(priority or "0"), pos or None, notes or None)
            )
        return validate_entries(result)


class _GlossaryEditor(QDialog):
    def __init__(
        self,
        glossary: Glossary,
        entries: tuple[GlossaryEntry, ...],
        parent: QWidget,
        *,
        metadata_only: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Glossary details"))
        self.selection: GlossarySelection | None = None
        self._glossary = glossary
        self._name = QLineEdit(glossary.name)
        self._description = QLineEdit(glossary.description)
        self._source = QComboBox()
        self._target = QComboBox()
        for language in Language:
            if language.can_be_source:
                label = self.tr("Detect language") if language is Language.AUTO else language.value
                self._source.addItem(label, language.value)
            if language.can_be_target:
                self._target.addItem(language.value, language.value)
        self._source.setCurrentIndex(self._source.findData(glossary.source.value))
        self._target.setCurrentIndex(self._target.findData(glossary.target.value))
        form = QFormLayout()
        form.addRow(self.tr("Name"), self._name)
        form.addRow(self.tr("Source language"), self._source)
        form.addRow(self.tr("Target language"), self._target)
        form.addRow(self.tr("Description"), self._description)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        self._terms = None if metadata_only else _TermTable(entries, self)
        if self._terms is not None:
            layout.addWidget(self._terms)
            self.resize(760, 460)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        name = self._name.text().strip()
        if not name:
            QMessageBox.warning(self, self.windowTitle(), self.tr("Enter a glossary name."))
            return
        try:
            entries = self._terms.entries() if self._terms is not None else ()
        except ValueError:
            QMessageBox.warning(
                self,
                self.windowTitle(),
                self.tr("Enter unique source terms, nonempty equivalents, and integer priorities."),
            )
            return
        glossary = replace(
            self._glossary,
            name=name,
            source=Language(self._source.currentData()),
            target=Language(self._target.currentData()),
            description=self._description.text(),
        )
        self.selection = glossary, entries
        super().accept()


class _StoreTask[T](QThread):
    def __init__(self, parent: QWidget, operation: Callable[[], T]) -> None:
        super().__init__(parent)
        self._operation = operation
        self.result: tuple[T] | None = None
        self.error: type[Exception] | None = None

    def run(self) -> None:
        try:
            self.result = (self._operation(),)
        except Exception as exc:
            self.error = type(exc)
            log.warning("Glossary operation failed (%s)", self.error.__name__)


class GlossaryManager(QDialog):
    glossary_changed = Signal(int, object)

    def __init__(
        self,
        path: Path,
        source: Language,
        target: Language,
        parent: QWidget,
        *,
        can_apply: bool,
        active_id: int | None = None,
        document: tuple[GlossaryEntry, ...] = (),
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            self.tr("Save reviewed terms") if document else self.tr("Glossary Manager")
        )
        self.resize(640, 360)
        self.selection: GlossarySelection | None = None
        self._store = partial(GlossaryStore, path)
        self._source, self._target = source, target
        self._document = document
        self._can_apply = can_apply
        self._task: QThread | None = None
        self._glossaries: tuple[Glossary, ...] = ()
        self.glossaries = QListWidget()
        self.glossaries.setAccessibleName(self.tr("Saved glossaries"))
        self.glossaries.currentRowChanged.connect(self._update_actions)
        self._status = QLabel()
        layout = QVBoxLayout(self)
        layout.addWidget(self.glossaries)
        layout.addWidget(self._status)
        actions = QHBoxLayout()
        self.new_button = QPushButton(
            self.tr("Save as new glossary…") if document else self.tr("New…")
        )
        self.edit_button = QPushButton(self.tr("Edit…"))
        self.delete_button = QPushButton(self.tr("Delete…"))
        self.import_button = QPushButton(self.tr("Import…"))
        self.export_button = QPushButton(self.tr("Export…"))
        self.use_button = QPushButton(
            self.tr("Merge reviewed terms") if document else self.tr("Use for translation")
        )
        self.close_button = QPushButton(self.tr("Close"))
        for button, handler in (
            (self.new_button, self._new),
            (self.edit_button, self._edit),
            (self.delete_button, self._delete),
            (self.import_button, self._import),
            (self.export_button, self._export),
            (self.use_button, self._merge if document else self._use),
            (self.close_button, self.reject),
        ):
            button.setAutoDefault(False)
            button.clicked.connect(handler)
            actions.addWidget(button)
        layout.addLayout(actions)
        if document:
            for button in (
                self.edit_button,
                self.delete_button,
                self.import_button,
                self.export_button,
            ):
                button.hide()
            self.use_button.setToolTip(
                self.tr("Reviewed equivalents replace matching saved terms.")
            )
        application = QCoreApplication.instance()
        assert application is not None
        application.aboutToQuit.connect(self._join_task)
        self._refresh(active_id)

    def _join_task(self) -> None:
        if self._task is not None:
            self._task.wait()

    def reject(self) -> None:
        if self._task is None:
            super().reject()

    def _run[T](self, operation: Callable[[], T], success: Callable[[T], None]) -> None:
        assert self._task is None
        task = _StoreTask(self, operation)
        self._task = task
        self._status.setText(self.tr("Working…"))
        self._update_actions()

        def finished() -> None:
            task.wait()
            self._task = None
            task.deleteLater()
            if task.result is None:
                message = (
                    self.tr("The glossary file or its entries are invalid.")
                    if task.error is not None and issubclass(task.error, GlossaryFormatError)
                    else self.tr("Could not complete the glossary operation.")
                )
                self._status.setText(message)
                self._update_actions()
                return
            self._status.clear()
            self._update_actions()
            success(*task.result)

        task.finished.connect(finished, Qt.ConnectionType.QueuedConnection)
        task.start()

    def _refresh(self, selected_id: int | None = None) -> None:
        def update(glossaries: tuple[Glossary, ...]) -> None:
            self._glossaries = glossaries
            self.glossaries.clear()
            index = 0
            for row, glossary in enumerate(glossaries):
                label = self.tr("{name} ({source} → {target})").format(
                    name=glossary.name, source=glossary.source.value, target=glossary.target.value
                )
                self.glossaries.addItem(label)
                if glossary.id == selected_id:
                    index = row
            if glossaries:
                self.glossaries.setCurrentRow(index)
            self._update_actions()

        self._run(lambda: self._store().list(), update)

    def _selected(self) -> Glossary | None:
        index = self.glossaries.currentRow()
        return self._glossaries[index] if index >= 0 else None

    def _update_actions(self) -> None:
        idle = self._task is None
        self.glossaries.setEnabled(idle)
        self.new_button.setEnabled(idle)
        self.import_button.setEnabled(idle)
        self.close_button.setEnabled(idle)
        selected = self._selected()
        for button in (self.edit_button, self.delete_button, self.export_button):
            button.setEnabled(idle and selected is not None)
        self.use_button.setEnabled(
            idle
            and (self._can_apply or bool(self._document))
            and selected is not None
            and selected.supports(self._source, self._target)
        )

    def _edit_details(
        self,
        glossary: Glossary,
        entries: tuple[GlossaryEntry, ...],
        save: Callable[[Glossary, tuple[GlossaryEntry, ...]], None],
        *,
        metadata_only: bool = False,
    ) -> None:
        dialog = _GlossaryEditor(glossary, entries, self, metadata_only=metadata_only)

        def finished() -> None:
            selection = dialog.selection
            dialog.deleteLater()
            if selection is not None:
                save(*selection)

        dialog.finished.connect(finished)
        dialog.open()

    def _default(self, name: str = "") -> Glossary:
        return Glossary(name, self._source, self._target)

    def _save(self, glossary: Glossary, entries: tuple[GlossaryEntry, ...]) -> None:
        def saved(updated: Glossary) -> None:
            assert updated.id is not None
            self.glossary_changed.emit(updated.id, (updated, entries))
            if self._document:
                self.accept()
            else:
                self._refresh(updated.id)

        self._run(lambda: self._store().save(glossary, entries), saved)

    def _new(self) -> None:
        self._edit_details(self._default(), self._document, self._save)

    def _edit(self) -> None:
        glossary = self._selected()
        assert glossary is not None and glossary.id is not None
        glossary_id = glossary.id

        def edit(entries: tuple[GlossaryEntry, ...]) -> None:
            self._edit_details(glossary, entries, self._save)

        self._run(lambda: self._store().entries(glossary_id), edit)

    def _delete(self) -> None:
        glossary = self._selected()
        assert glossary is not None and glossary.id is not None
        if (
            QMessageBox.question(
                self,
                self.windowTitle(),
                self.tr("Delete glossary “{name}”?").format(name=glossary.name),
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        glossary_id = glossary.id

        def deleted(_result: None) -> None:
            self.glossary_changed.emit(glossary_id, None)
            self._refresh()

        self._run(lambda: self._store().delete(glossary_id), deleted)

    def _import(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, self.tr("Import glossary"), "", self.tr("Glossaries (*.csv *.tsv)")
        )
        if filename:
            path = Path(filename)

            def save(glossary: Glossary, _entries: tuple[GlossaryEntry, ...]) -> None:
                self._run(
                    lambda: self._store().import_file(path, glossary),
                    lambda saved: self._refresh(saved.id),
                )

            self._edit_details(self._default(path.stem), (), save, metadata_only=True)

    def _export(self) -> None:
        glossary = self._selected()
        assert glossary is not None and glossary.id is not None
        filters = self.tr("CSV glossary (*.csv)"), self.tr("TSV glossary (*.tsv)")
        filename, file_filter = QFileDialog.getSaveFileName(
            self, self.tr("Export glossary"), "", ";;".join(filters)
        )
        if filename:
            path = Path(filename)
            if not path.suffix:
                path = path.with_suffix(".tsv" if file_filter == filters[1] else ".csv")
            glossary_id = glossary.id
            self._run(
                lambda: self._store().export_file(glossary_id, path),
                lambda _: self._status.setText(self.tr("Glossary exported.")),
            )

    def _use(self) -> None:
        glossary = self._selected()
        assert glossary is not None and glossary.id is not None
        glossary_id = glossary.id

        def apply(entries: tuple[GlossaryEntry, ...]) -> None:
            self.selection = glossary, entries
            self.accept()

        self._run(lambda: self._store().entries(glossary_id), apply)

    def _merge(self) -> None:
        glossary = self._selected()
        assert glossary is not None and glossary.id is not None
        glossary_id = glossary.id

        def merge() -> tuple[GlossaryEntry, ...]:
            store = self._store()
            store.merge(glossary_id, self._document)
            return store.entries(glossary_id)

        def merged(entries: tuple[GlossaryEntry, ...]) -> None:
            self.glossary_changed.emit(glossary_id, (glossary, entries))
            self.accept()

        self._run(merge, merged)


class DocumentTermReview(QDialog):
    glossary_changed = Signal(int, object)

    def __init__(
        self,
        candidates: list[ExtractedTerm],
        path: Path,
        source: Language,
        target: Language,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Review document terms"))
        self.resize(900, 470)
        self.approved: tuple[GlossaryEntry, ...] = ()
        self._path, self._source, self._target = path, source, target
        unique: dict[str, ExtractedTerm] = {}
        for term in candidates:
            key = canonical_term(term.source_term)
            previous = unique.get(key)
            if previous is None or previous.count < term.count:
                unique[key] = term
        self._terms = _TermTable((), self, candidates=tuple(unique.values()))
        layout = QVBoxLayout(self)
        if not candidates:
            layout.addWidget(QLabel(self.tr("No document terms were found. You can add your own.")))
        layout.addWidget(self._terms)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        translate = buttons.addButton(self.tr("Translate"), QDialogButtonBox.ButtonRole.AcceptRole)
        translate.setDefault(True)
        save = buttons.addButton(
            self.tr("Save to glossary…"), QDialogButtonBox.ButtonRole.ActionRole
        )
        save.clicked.connect(self._save)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _entries(self) -> tuple[GlossaryEntry, ...] | None:
        try:
            return self._terms.entries()
        except ValueError:
            QMessageBox.warning(
                self,
                self.windowTitle(),
                self.tr("Enter unique source terms, nonempty equivalents, and integer priorities."),
            )
            return None

    def accept(self) -> None:
        if (entries := self._entries()) is not None:
            self.approved = entries
            super().accept()

    def _save(self) -> None:
        if entries := self._entries():
            dialog = GlossaryManager(
                self._path, self._source, self._target, self, can_apply=False, document=entries
            )
            dialog.glossary_changed.connect(self.glossary_changed)
            dialog.finished.connect(dialog.deleteLater)
            dialog.open()

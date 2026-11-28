"""Exercise glossary management and the two-pass desktop workflow."""

import asyncio
import json
from pathlib import Path
from threading import Event

import httpx
import pytest
import respx
from docx import Document
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
)
from pytestqt.qtbot import QtBot

from dlx_tray.contracts.cloud_v1 import ExtractedTerm
from dlx_tray.core.models import GlossaryEntry, Language
from dlx_tray.core.pipeline import TranslationJob
from dlx_tray.providers.cloud_ai import CloudAiTranslator
from dlx_tray.services.glossary import Glossary, GlossaryStore
from dlx_tray.ui.glossary_dialog import DocumentTermReview, GlossaryManager
from dlx_tray.ui.main_window import MainWindow

_CLOUD_URL = "https://cloud.example"


def _button(dialog: QDialog, label: str) -> QPushButton:
    return next(button for button in dialog.findChildren(QPushButton) if button.text() == label)


def _window(qtbot: QtBot, path: Path) -> MainWindow:
    cloud = CloudAiTranslator(_CLOUD_URL, "fixture")
    window = MainWindow(
        source=Language.EN,
        target=Language.UK,
        glossary_path=path,
        translation_job=TranslationJob(translator=cloud, request_delay=0),
    )

    def discard_test_output(_widget: MainWindow) -> None:
        window._file_unsaved = False

    qtbot.addWidget(window, before_close_func=discard_test_output)
    window.set_glossary_provider(cloud, 60_000)
    window.show()
    return window


def test_create_import_and_use_saved_glossary(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "glossaries.sqlite3"
    window = _window(qtbot, database)
    window.glossary_selector.click()
    manager = window.findChild(GlossaryManager)
    assert manager is not None
    qtbot.waitUntil(manager.new_button.isEnabled)
    manager.new_button.click()
    editor = manager.findChild(QDialog)
    assert editor is not None and editor.isVisible()
    name = editor.findChild(QLineEdit)
    table = editor.findChild(QTableWidget)
    assert name is not None and table is not None
    name.setText("Created glossary")
    _button(editor, "Add term").click()
    cell_editor = table.focusWidget()
    assert cell_editor is not None
    qtbot.keyClick(cell_editor, Qt.Key.Key_Escape)
    table.setItem(0, 0, QTableWidgetItem("tide"))
    table.setItem(0, 1, QTableWidgetItem("flow"))
    table.setItem(0, 3, QTableWidgetItem("reviewed in class"))
    table.setItem(0, 4, QTableWidgetItem("11"))
    buttons = editor.findChild(QDialogButtonBox)
    assert buttons is not None
    buttons.button(QDialogButtonBox.StandardButton.Save).click()
    qtbot.waitUntil(lambda: manager.glossaries.count() == 1 and manager.import_button.isEnabled())

    path = tmp_path / "Imported.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_: (str(path), ""))
    manager.export_button.click()
    qtbot.waitUntil(lambda: path.exists() and manager.import_button.isEnabled())
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: (str(path), ""))
    manager.import_button.click()
    editor = manager.findChild(QDialog)
    assert editor is not None and editor.isVisible()
    buttons = editor.findChild(QDialogButtonBox)
    assert buttons is not None
    buttons.button(QDialogButtonBox.StandardButton.Save).click()
    qtbot.waitUntil(lambda: manager.glossaries.count() == 2 and manager.use_button.isEnabled())
    assert manager.glossaries.currentRow() == 1
    manager.use_button.click()
    qtbot.waitUntil(window.clear_glossary_action.isEnabled)
    assert "Imported" in window.clear_glossary_action.text()
    assert window.glossary_selector.text() == "Glossary: Imported"
    store = GlossaryStore(database)
    created, imported = store.list()
    assert created.id is not None and imported.id is not None
    expected = (GlossaryEntry("tide", "flow", 11, notes="reviewed in class"),)
    assert store.entries(created.id) == store.entries(imported.id) == expected


@pytest.mark.parametrize("suffix", [".docx", ".txt"])
@respx.mock
def test_reviewed_file_snapshot_reaches_pass_two(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    database = tmp_path / "glossaries.sqlite3"
    store = GlossaryStore(database)
    store.save(
        Glossary("Saved terms", Language.EN, Language.UK),
        [
            GlossaryEntry("tide", "saved tide", priority=99),
            GlossaryEntry("harbor", "saved harbor"),
            GlossaryEntry("unmatched", "do not send"),
        ],
    )
    paragraphs = ["Tide and harbor.", "Tide returns.", "Tide returns."]
    path = tmp_path / ("source" + suffix)
    if suffix == ".docx":
        document = Document()
        for paragraph in paragraphs:
            document.add_paragraph(paragraph)
        document.save(str(path))
    else:
        path.write_text("\n\n".join(paragraphs), encoding="utf-8")
    extraction = respx.post(f"{_CLOUD_URL}/v1/extract-terms").respond(
        json={
            "terms": [
                {"source_term": "Tide", "target_term": "suggested", "category": "term", "count": 3},
                {"source_term": "harbor", "target_term": "harbour", "category": "term", "count": 1},
                {"source_term": "tidE", "target_term": "duplicate", "category": "term", "count": 1},
            ]
        }
    )

    def translate(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "segments": [
                    {"index": segment["index"], "text": "Translated " + segment["text"]}
                    for segment in body["segments"]
                ]
            },
        )

    translation = respx.post(f"{_CLOUD_URL}/v1/translate").mock(side_effect=translate)
    window = _window(qtbot, database)
    window.manage_glossaries_action.trigger()
    manager = window.findChild(GlossaryManager)
    assert manager is not None
    qtbot.waitUntil(manager.use_button.isEnabled)
    manager.use_button.click()
    qtbot.waitUntil(window.clear_glossary_action.isEnabled)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: (str(path), ""))
    window.open_file_action.trigger()
    qtbot.waitUntil(window.close_file_action.isEnabled)
    # Pass 1 and Pass 2 must keep using the file we opened.
    path.write_bytes(b"changed after open")
    window.extract_terms_action.trigger()
    assert not window.translate_button.isEnabled()

    def review_open() -> bool:
        dialog = window.findChild(DocumentTermReview)
        return dialog is not None and dialog.isVisible()

    qtbot.waitUntil(review_open)
    dialog = window.findChild(DocumentTermReview)
    assert dialog is not None
    table = dialog.findChild(QTableWidget)
    assert table is not None and table.rowCount() == 2
    first = table.item(0, 0)
    assert first is not None and first.text() == "Tide"
    table.setItem(0, 1, QTableWidgetItem("reviewed tide"))
    table.selectRow(1)
    _button(dialog, "Remove selected terms").click()
    _button(dialog, "Translate").click()
    qtbot.waitUntil(window.save_file_action.isEnabled)
    assert "Translated Tide returns." in window.result_editor.toPlainText()
    assert json.loads(extraction.calls.last.request.content)["segments"] == paragraphs[:2]
    body = json.loads(translation.calls.last.request.content)
    assert [segment["text"] for segment in body["segments"]] == paragraphs
    assert [(term["source_term"], term["target_term"]) for term in body["glossary"]] == [
        ("tide", "reviewed tide"),
        ("harbor", "saved harbor"),
    ]
    saved_id = store.list()[0].id
    assert saved_id is not None
    assert any(entry.target_term == "saved tide" for entry in store.entries(saved_id))


@respx.mock
def test_cancels_an_in_flight_pass_one(qtbot: QtBot, tmp_path: Path) -> None:
    entered = Event()

    async def stalled(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.sleep(30)
        return httpx.Response(200, json={"terms": []})

    extraction = respx.post(f"{_CLOUD_URL}/v1/extract-terms").mock(side_effect=stalled)
    translation = respx.post(f"{_CLOUD_URL}/v1/translate")
    window = _window(qtbot, tmp_path / "glossaries.sqlite3")
    window.source_editor.setPlainText("Text for extraction")
    window.extract_terms_action.trigger()
    # The mock is invoked before its asynchronous response is recorded.
    qtbot.waitUntil(entered.is_set)
    window.cancel_button.click()
    qtbot.waitUntil(window.open_file_action.isEnabled)
    assert not translation.called
    assert window.findChild(DocumentTermReview) is None
    assert not extraction.called or extraction.calls.last.has_response is False


def test_review_can_merge_into_saved_glossary(qtbot: QtBot, tmp_path: Path) -> None:
    database = tmp_path / "glossaries.sqlite3"
    store = GlossaryStore(database)
    glossary = store.save(
        Glossary("Existing glossary", Language.EN, Language.UK),
        [GlossaryEntry("tide", "previous", priority=99), GlossaryEntry("port", "harbor")],
    )
    assert glossary.id is not None
    window = _window(qtbot, database)
    review = DocumentTermReview(
        [ExtractedTerm(source_term="Tide", target_term="reviewed", category="term", count=2)],
        database,
        Language.EN,
        Language.UK,
        window,
    )
    review.open()
    _button(review, "Save to glossary…").click()
    manager = review.findChild(GlossaryManager)
    assert manager is not None
    qtbot.waitUntil(manager.use_button.isEnabled)
    with qtbot.waitSignal(review.glossary_changed):
        manager.use_button.click()
    assert review.isVisible()
    assert store.entries(glossary.id) == (
        GlossaryEntry("port", "harbor"),
        GlossaryEntry("Tide", "reviewed"),
    )
    review.reject()

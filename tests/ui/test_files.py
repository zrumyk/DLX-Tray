"""Exercise document open, translation and export through the window."""

from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event

import pytest
from docx import Document
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog
from pytestqt.qtbot import QtBot
from tests.conftest import FIXTURES_DIR
from tests.fakes.fake_dlx import FakeDLXFactory

from dlx_tray.core.pipeline import TranslationJob
from dlx_tray.fileio.pdf_io import read_pdf
from dlx_tray.providers.deeplx import DeeplxTranslator
from dlx_tray.ui.error_dialog import ErrorDialog
from dlx_tray.ui.main_window import MainWindow


@pytest.fixture
def file_window(qtbot: QtBot) -> Iterator[Callable[[TranslationJob], MainWindow]]:
    windows: list[MainWindow] = []

    def make(job: TranslationJob) -> MainWindow:
        window = MainWindow(translation_job=job)
        windows.append(window)
        qtbot.addWidget(window)
        window.show()
        return window

    yield make
    for window in windows:
        window.stop_translation()


def _open(window: MainWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: (str(path), ""))
    window.open_file_action.trigger()
    qtbot.waitUntil(window.open_file_action.isEnabled, timeout=5000)


def _save(window: MainWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_: (str(path), ""))
    window.save_file_action.trigger()
    qtbot.waitUntil(window.open_file_action.isEnabled, timeout=5000)


@pytest.mark.parametrize("encoding", ["cp1251", "utf-16"])
@pytest.mark.filterwarnings(
    "ignore:Function.*QDragEnterEvent.*marked as deprecated:DeprecationWarning"
)
def test_drop_txt_on_editor_and_save_unicode(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_dlx: FakeDLXFactory,
    file_window: Callable[[TranslationJob], MainWindow],
    encoding: str,
) -> None:
    text = "Сьогодні читаємо українською та перекладаємо цей текст.\r\n\r\n"
    source = tmp_path / "джерело.TXT"
    source.write_bytes(text.encode(encoding))
    destination = tmp_path / "переклад.txt"
    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        window = file_window(TranslationJob(translator=translator, request_delay=0))
        target = window.source_editor.viewport()
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(source))])
        actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
        enter = QDragEnterEvent(
            QPoint(10, 10),
            actions,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ShiftModifier,
        )
        QApplication.sendEvent(target, enter)
        assert enter.isAccepted()
        drop = QDropEvent(
            QPointF(10, 10),
            actions,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ShiftModifier,
        )
        QApplication.sendEvent(target, drop)
        assert drop.isAccepted()
        assert drop.dropAction() == Qt.DropAction.CopyAction
        qtbot.waitUntil(window.open_file_action.isEnabled)
        assert source.name in window.windowTitle()
        assert window.source_editor.toPlainText() == text.replace("\r\n", "\n")
        assert window.source_editor.isReadOnly()
        assert not window.save_file_action.isEnabled()

        window.translate_button.click()
        qtbot.waitUntil(window.save_file_action.isEnabled)
        _save(window, qtbot, monkeypatch, source)
        assert "keep the original file" in window.statusBar().currentMessage()
        assert source.read_bytes() == text.encode(encoding)
        _save(window, qtbot, monkeypatch, destination.with_suffix(""))
        assert window.statusBar().currentMessage() == "Translation saved."
        assert destination.read_bytes() == f"[UK] {text}".encode()
        window.close_file_action.trigger()
        assert not window.source_editor.isReadOnly()
        assert not window.save_file_action.isEnabled()


def test_twenty_page_docx_through_window(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_dlx: FakeDLXFactory,
    file_window: Callable[[TranslationJob], MainWindow],
) -> None:
    source = tmp_path / "source.docx"
    destination = tmp_path / "result.docx"
    document = Document()
    for index in range(20):
        heading = document.add_heading(f"Chapter {index}", level=1)
        if index:
            heading.paragraph_format.page_break_before = True
        document.add_paragraph(style="List Number").add_run(f"Item {index}").bold = True
        table = document.add_table(rows=1, cols=1, style="Table Grid")
        table.cell(0, 0).text = f"Cell {index}"
    document.sections[0].header.paragraphs[0].text = "Header"
    document.sections[0].footer.paragraphs[0].text = "Footer"
    document.save(str(source))
    waits: list[float] = []

    def record_delay(seconds: float, cancel: Event) -> bool:
        waits.append(seconds)
        return not cancel.is_set()

    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        job = TranslationJob(translator=translator, request_delay=1.5, sleep=record_delay)
        window = file_window(job)
        _open(window, qtbot, monkeypatch, source)
        # Exports must still use the package the user opened if the path is replaced later.
        source.write_bytes(b"changed on disk")
        window.translate_button.click()
        assert not window.open_file_action.isEnabled()
        qtbot.waitUntil(window.save_file_action.isEnabled, timeout=10000)
        assert window.progress_bar.value() == window.progress_bar.maximum() == 62
        assert len(waits) == 61 and all(delay == 1.5 for delay in waits)
        _save(window, qtbot, monkeypatch, destination)

    translated = Document(str(destination))
    assert len(translated.paragraphs) == 40
    assert len(translated.tables) == 20
    for index, table in enumerate(translated.tables):
        heading, item = translated.paragraphs[2 * index : 2 * index + 2]
        assert heading.text == f"[UK] Chapter {index}"
        assert heading.style is not None and heading.style.name == "Heading 1"
        assert bool(heading.paragraph_format.page_break_before) == bool(index)
        assert item.text == f"[UK] Item {index}"
        assert item.style is not None and item.style.name == "List Number"
        assert item.runs[0].bold
        assert table.style is not None and table.style.name == "Table Grid"
        assert table.cell(0, 0).text == f"[UK] Cell {index}"
    assert translated.sections[0].header.paragraphs[0].text == "[UK] Header"
    assert translated.sections[0].footer.paragraphs[0].text == "[UK] Footer"


def test_pdf_exports_and_scanned_file_error_preserves_current_result(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_dlx: FakeDLXFactory,
    file_window: Callable[[TranslationJob], MainWindow],
) -> None:
    source = FIXTURES_DIR / "field_notes.pdf"
    txt = tmp_path / "notes.txt"
    docx = tmp_path / "notes.docx"
    with fake_dlx([]) as server, DeeplxTranslator(server.url, server.token) as translator:
        window = file_window(TranslationJob(translator=translator, request_delay=0))
        _open(window, qtbot, monkeypatch, source)
        window.translate_button.click()
        qtbot.waitUntil(window.save_file_action.isEnabled, timeout=5000)
        _open(window, qtbot, monkeypatch, FIXTURES_DIR / "scanned_field_notes.pdf")
        assert "no extractable text layer" in window.statusBar().currentMessage()
        assert source.name in window.windowTitle()
        _save(window, qtbot, monkeypatch, tmp_path / "missing" / txt.name)
        assert "Could not save" in window.statusBar().currentMessage()
        _save(window, qtbot, monkeypatch, txt)
        _save(window, qtbot, monkeypatch, docx)

    paragraphs = [f"[UK] {paragraph}" for paragraph in read_pdf(source)]
    assert txt.read_bytes() == "\n\n".join(paragraphs).encode()
    assert [paragraph.text for paragraph in Document(str(docx)).paragraphs] == paragraphs


def test_file_failure_dialog_uses_document_progress_and_cancel_preserves_preview(
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_dlx: FakeDLXFactory,
    file_window: Callable[[TranslationJob], MainWindow],
) -> None:
    source = tmp_path / "chapters.docx"
    document = Document()
    document.add_paragraph("First")
    document.add_paragraph("Second")
    document.save(str(source))
    with (
        fake_dlx(["ok", "401"]) as server,
        DeeplxTranslator(server.url, server.token) as translator,
    ):
        window = file_window(TranslationJob(translator=translator, request_delay=0))
        _open(window, qtbot, monkeypatch, source)
        window.translate_button.click()
        qtbot.waitUntil(lambda: window.findChild(ErrorDialog) is not None)
        dialog = window.findChild(ErrorDialog)
        assert dialog is not None
        assert dialog.text() == "Could not translate chunk 2 of 2."
        assert window.progress_label.text() == "File translation · Chunk 1 of 2"
        dialog.cancel_button.click()
        qtbot.waitUntil(window.translate_button.isEnabled)
        assert window.result_editor.toPlainText() == "[UK] First"
        assert not window.save_file_action.isEnabled()
        assert window.statusBar().currentMessage() == "Translation cancelled."

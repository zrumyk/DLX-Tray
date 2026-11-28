"""Translate Word document text in place in the loaded package and save a copy."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from re import compile as re_compile
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

import lxml.etree as etree
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.oxml.text.hyperlink import CT_Hyperlink
from docx.oxml.text.run import CT_R
from docx.table import _Cell
from docx.text.paragraph import Paragraph

from dlx_tray import APP_NAME
from dlx_tray.core.models import PhilologicalNote

if TYPE_CHECKING:
    from docx.document import Document as WordDocument
    from docx.oxml.text.paragraph import CT_P
    from docx.parts.hdrftr import FooterPart, HeaderPart
    from docx.table import Table

_TEXT_RUN_CHILDREN = {
    qn(f"w:{tag}") for tag in ("rPr", "t", "tab", "cr", "noBreakHyphen", "lastRenderedPageBreak")
}
_FIELD_MARKER = qn("w:fldChar")
_FIELD_TYPE = qn("w:fldCharType")
_EXCLUDED_FIELD_CONTAINERS = {
    qn(f"w:{tag}") for tag in ("del", "fldSimple", "moveFrom", "txbxContent")
}
_INVALID_XML_CHARACTERS = re_compile(r"[^\t\n\r\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def translate_docx(
    source: Path,
    destination: Path,
    translate: Callable[[str], str],
    *,
    segment_notes: Sequence[tuple[PhilologicalNote, ...]] = (),
    include_notes_comments: bool = False,
) -> None:
    """Translate body, table and header/footer text, then atomically publish the document.

    The synchronous callback receives each nonblank text segment in order. A callback or
    save failure leaves the destination untouched. Returning the original text preserves
    its runs, so a caller can skip a segment without changing it.
    """
    with source.open("rb") as stream:
        document = Document(stream)

    _translate_document(document, translate, segment_notes if include_notes_comments else ())
    _save_docx(document, destination)


class _NoteComments:
    def __init__(
        self, document: WordDocument, notes: Sequence[tuple[PhilologicalNote, ...]]
    ) -> None:
        self._document = document
        self._notes = iter(notes)
        self._next_id: int | None = None

    def attach(self, first: etree._Element, last: etree._Element) -> None:
        notes = next(self._notes, ())
        if not notes:
            return
        text = _INVALID_XML_CHARACTERS.sub("\ufffd", "\n".join(note.text for note in notes))
        comments = self._document.comments
        if self._next_id is None:
            self._next_id = max((comment.comment_id for comment in comments), default=-1) + 1
        comment = comments.add_comment(text, author=APP_NAME)
        comment._comment_elm.id = self._next_id
        self._next_id += 1
        comment_id = str(comment.comment_id)
        start = OxmlElement("w:commentRangeStart")
        start.set(qn("w:id"), comment_id)
        end = OxmlElement("w:commentRangeEnd")
        end.set(qn("w:id"), comment_id)
        reference_run = OxmlElement("w:r")
        reference_run.get_or_add_rPr().get_or_add_rStyle().val = "CommentReference"
        reference = OxmlElement("w:commentReference")
        reference.set(qn("w:id"), comment_id)
        reference_run.append(reference)
        first.addprevious(start)
        last.addnext(end)
        end.addnext(reference_run)


def _translate_document(
    document: WordDocument,
    translate: Callable[[str], str],
    notes: Sequence[tuple[PhilologicalNote, ...]] = (),
) -> None:
    comments = _NoteComments(document, notes) if notes else None
    _translate_story(document.iter_inner_content(), translate, comments)
    for blocks in _header_footer_blocks(document):
        _translate_story(blocks, translate)


def _save_docx(document: WordDocument, destination: Path) -> None:
    zoom = document.settings.element.find(qn("w:zoom"))
    if zoom is not None and zoom.get(qn("w:percent")) is None:
        # The python-docx default template omits this required attribute.
        zoom.set(qn("w:percent"), "100")
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            document.save(stream)
        temporary.replace(destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _paragraphs(blocks: Iterator[Paragraph | Table]) -> Iterator[Paragraph]:
    for block in blocks:
        if isinstance(block, Paragraph):
            yield block
            continue

        # Walk physical cells once. The grid view repeats merged cells and recursively
        # resolves a vertical continuation through every row above it.
        for row in block.rows:
            for cell_element in row._tr.tc_lst:
                if cell_element.vMerge != "continue":
                    cell = _Cell(cell_element, block)
                    yield from _paragraphs(cell.iter_inner_content())


def _header_footer_blocks(document: WordDocument) -> Iterator[Iterator[Paragraph | Table]]:
    seen: set[HeaderPart | FooterPart] = set()
    for section in document.sections:
        for story in (
            section.header,
            section.first_page_header,
            section.even_page_header,
            section.footer,
            section.first_page_footer,
            section.even_page_footer,
        ):
            # Accessing an undefined story's part can create an empty header or footer.
            if story.is_linked_to_previous:
                continue
            part = story.part
            if part not in seen:
                seen.add(part)
                yield story.iter_inner_content()


def _translate_story(
    blocks: Iterator[Paragraph | Table],
    translate: Callable[[str], str],
    comments: _NoteComments | None = None,
) -> None:
    field_depth = 0
    for paragraph in _paragraphs(blocks):
        field_depth = _translate_container(paragraph._p, translate, field_depth, comments)


def _translate_container(
    container: CT_P | CT_Hyperlink,
    translate: Callable[[str], str],
    field_depth: int,
    comments: _NoteComments | None,
) -> int:
    runs: list[CT_R] = []
    for child in list(container):
        if not field_depth and isinstance(child, CT_R) and all(_is_text_child(n) for n in child):
            runs.append(child)
            continue

        _translate_runs(container, runs, translate, comments)
        runs.clear()
        if isinstance(child, CT_Hyperlink):
            field_depth = _translate_container(child, translate, field_depth, comments)
        else:
            field_depth = _field_depth_after(child, field_depth)
    _translate_runs(container, runs, translate, comments)
    return field_depth


def _field_depth_after(element: etree._Element, field_depth: int) -> int:
    # An inline wrapper can contain one boundary of a field spanning multiple runs or paragraphs.
    for field in element.iter(_FIELD_MARKER):
        if any(ancestor.tag in _EXCLUDED_FIELD_CONTAINERS for ancestor in field.iterancestors()):
            continue
        match field.get(_FIELD_TYPE):
            case "begin":
                field_depth += 1
            case "end":
                field_depth = max(0, field_depth - 1)
    return field_depth


def _is_text_child(node: etree._Element) -> bool:
    if node.tag == qn("w:br"):
        return node.get(qn("w:type"), "textWrapping") == "textWrapping" and node.get(
            qn("w:clear")
        ) in (None, "none")
    return node.tag in _TEXT_RUN_CHILDREN


def _translate_runs(
    container: CT_P | CT_Hyperlink,
    runs: list[CT_R],
    translate: Callable[[str], str],
    comments: _NoteComments | None,
) -> None:
    texts = [run.text for run in runs]
    source = "".join(texts)
    if not source.strip():
        return
    translation = translate(source)
    if translation != source:
        runs = [_replace_runs(container, runs, texts, translation)]
    if comments is not None:
        if isinstance(container, CT_Hyperlink):
            comments.attach(container, container)
        else:
            comments.attach(runs[0], runs[-1])


def _replace_runs(
    container: CT_P | CT_Hyperlink, runs: list[CT_R], texts: list[str], translation: str
) -> CT_R:
    # Word may split one formatting style across several runs. Weight the combined style
    # by its text, with the first occurrence winning a tie.
    weights: Counter[bytes] = Counter()
    representatives: dict[bytes, CT_R] = {}
    for run, text in zip(runs, texts, strict=True):
        properties = run.rPr
        style = (
            etree.tostring(properties, method="c14n", exclusive=True)
            if properties is not None and (len(properties) or properties.attrib)
            else b""
        )
        weights[style] += sum(not char.isspace() for char in text)
        representatives.setdefault(style, run)
    dominant = representatives[max(weights, key=weights.__getitem__)]
    dominant.text = translation
    for run in runs:
        if run is not dominant:
            container.remove(run)
    return dominant

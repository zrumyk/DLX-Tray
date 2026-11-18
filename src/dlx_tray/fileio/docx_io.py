"""Translate Word document text in place in the loaded package and save a copy."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

import lxml.etree as etree
from docx import Document
from docx.oxml.ns import qn
from docx.oxml.text.hyperlink import CT_Hyperlink
from docx.oxml.text.run import CT_R
from docx.text.paragraph import Paragraph

if TYPE_CHECKING:
    from docx.document import Document as WordDocument
    from docx.oxml.table import CT_Tc
    from docx.oxml.text.paragraph import CT_P
    from docx.parts.hdrftr import FooterPart, HeaderPart
    from docx.table import Table

_TEXT_RUN_CHILDREN = {qn(f"w:{tag}") for tag in ("rPr", "t", "tab", "cr", "lastRenderedPageBreak")}
_FIELD_TYPE = qn("w:fldCharType")


def translate_docx(source: Path, destination: Path, translate: Callable[[str], str]) -> None:
    """Translate body, table and header/footer text, then atomically publish the document.

    The synchronous callback receives each nonblank text segment in order. A callback or
    save failure leaves the destination untouched. Returning the original text preserves
    its runs, so a caller can skip a segment without changing it.
    """
    with source.open("rb") as stream:
        document = Document(stream)

    _translate_story(document.iter_inner_content(), translate)
    for blocks in _header_footer_blocks(document):
        _translate_story(blocks, translate)

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

        seen: set[CT_Tc] = set()
        for row in block.rows:
            for cell in row.cells:
                # A merged cell can occupy several grid positions, including later rows.
                if cell._tc not in seen:
                    seen.add(cell._tc)
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


def _translate_story(blocks: Iterator[Paragraph | Table], translate: Callable[[str], str]) -> None:
    field_depth = 0
    for paragraph in _paragraphs(blocks):
        field_depth = _translate_container(paragraph._p, translate, field_depth)


def _translate_container(
    container: CT_P | CT_Hyperlink, translate: Callable[[str], str], field_depth: int
) -> int:
    runs: list[CT_R] = []
    for child in list(container):
        if not field_depth and isinstance(child, CT_R) and all(_is_text_child(n) for n in child):
            runs.append(child)
            continue

        _translate_runs(container, runs, translate)
        runs.clear()
        if isinstance(child, CT_R):
            # Field instructions and cached results can span multiple paragraphs.
            for field in child.xpath("./w:fldChar"):
                match field.get(_FIELD_TYPE):
                    case "begin":
                        field_depth += 1
                    case "end":
                        field_depth = max(0, field_depth - 1)
        elif isinstance(child, CT_Hyperlink):
            field_depth = _translate_container(child, translate, field_depth)
    _translate_runs(container, runs, translate)
    return field_depth


def _is_text_child(node: etree._Element) -> bool:
    if node.tag == qn("w:br"):
        return node.get(qn("w:type"), "textWrapping") == "textWrapping" and node.get(
            qn("w:clear")
        ) in (None, "none")
    return node.tag in _TEXT_RUN_CHILDREN


def _translate_runs(
    container: CT_P | CT_Hyperlink, runs: list[CT_R], translate: Callable[[str], str]
) -> None:
    texts = [run.text for run in runs]
    source = "".join(texts)
    if not source.strip():
        return
    translation = translate(source)
    if translation == source:
        return

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

"""Word document translation round-trip tests using a deterministic callback."""

from pathlib import Path
from typing import BinaryIO

import pytest
from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Inches

from dlx_tray.core.models import NoteKind, PhilologicalNote
from dlx_tray.fileio.docx_io import translate_docx
from dlx_tray.fileio.translation import SourceFile


def test_export_notes_preserves_existing_comments_and_hoists_hyperlink_anchors(
    tmp_path: Path,
) -> None:
    source = tmp_path / "comment-source.docx"
    destination = tmp_path / "comment-export.docx"
    plain = tmp_path / "comment-plain.docx"
    document = Document()
    first = document.add_paragraph(style="Subtitle")
    first.add_run("Unchanged ").bold = True
    first.add_run("quote").italic = True
    earlier = document.add_paragraph()
    existing = document.add_comment(earlier.add_run("Earlier comment"), "Existing note", "Author")
    existing._comment_elm.id = 7
    for marker in earlier._p.xpath(
        ".//w:commentRangeStart | .//w:commentRangeEnd | .//w:commentReference"
    ):
        marker.set(qn("w:id"), "7")
    linked = document.add_paragraph()
    link = OxmlElement("w:hyperlink")
    link.set(
        qn("r:id"), document.part.relate_to("https://example.test", RT.HYPERLINK, is_external=True)
    )
    link.append(parse_xml(f"<w:r {nsdecls('w')}><w:t>Link</w:t></w:r>"))
    linked._p.append(link)
    table = document.add_table(rows=1, cols=1, style="Table Grid")
    table.cell(0, 0).text = "Cell"
    document.sections[0].header.paragraphs[0].text = "Header"
    document.sections[0].footer.paragraphs[0].text = "Footer"
    document.save(str(source))

    snapshot = SourceFile.read(source)
    assert snapshot.segments == (
        "Unchanged quote",
        "Earlier comment",
        "Link",
        "Cell",
        "Header",
        "Footer",
    )
    notes = tuple(
        (PhilologicalNote(NoteKind.ALTERNATIVE, text),)
        for text in (
            "Unchanged segment note",
            "Extra note",
            "Link note",
            "Cell note",
            "Header note",
            "Footer note",
        )
    )
    translations = (
        "Unchanged quote",
        "Earlier comment",
        "Translated link",
        "New cell",
        "New header",
        "New footer",
    )
    snapshot.save(destination, translations, segment_notes=notes, include_notes_comments=True)
    snapshot.save(plain, translations, segment_notes=notes)

    word = Document(str(destination))
    assert [(comment.comment_id, comment.text) for comment in word.comments] == [
        (7, "Existing note"),
        (8, "Unchanged segment note"),
        (9, "Extra note"),
        (10, "Link note"),
        (11, "Cell note"),
    ]
    assert [comment.text for comment in Document(str(plain)).comments] == ["Existing note"]
    unchanged, earlier, translated_link = word.paragraphs
    assert unchanged.style is not None and unchanged.style.name == "Subtitle"
    assert unchanged.text == "Unchanged quote"
    assert [run.text for run in unchanged.runs if run.text] == ["Unchanged ", "quote"]
    assert unchanged.runs[0].bold and unchanged.runs[1].italic
    assert earlier._p.xpath("./w:r/w:commentReference/@w:id") == ["9", "7"]
    (hyperlink,) = translated_link.hyperlinks
    assert hyperlink.text == "Translated link" and hyperlink.url == "https://example.test"
    assert [element.tag for element in translated_link._p] == [
        qn("w:commentRangeStart"),
        qn("w:hyperlink"),
        qn("w:commentRangeEnd"),
        qn("w:r"),
    ]
    assert translated_link._p.xpath("./w:r/w:commentReference/@w:id") == ["10"]
    (translated_table,) = word.tables
    assert translated_table.style is not None and translated_table.style.name == "Table Grid"
    assert translated_table.cell(0, 0).text == "New cell"
    section = word.sections[0]
    for story, expected in ((section.header, "New header"), (section.footer, "New footer")):
        (paragraph,) = story.paragraphs
        assert paragraph.text == expected
        assert not paragraph._p.xpath(".//w:commentRangeStart | .//w:commentReference")


def test_twenty_page_document_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "джерело.docx"
    destination = tmp_path / "переклад.docx"
    document = Document()
    for index in range(20):
        heading = document.add_heading(f"Chapter {index}", level=1)
        if index:
            heading.paragraph_format.page_break_before = True
        item = document.add_paragraph(f"Item {index}", style="List Number")
        numbering = item._p.get_or_add_pPr().get_or_add_numPr()
        numbering.get_or_add_numId().val = 5
        numbering.get_or_add_ilvl().val = 0
        table = document.add_table(rows=2, cols=2, style="Table Grid")
        cell = table.cell(0, 0).merge(table.cell(1, 0))
        cell.text = f"Merged {index}"
        cell.add_table(rows=1, cols=1).cell(0, 0).text = f"Nested {index}"
        table.cell(0, 1).text = f"Right {index}"
    document.save(str(source))
    original = source.read_bytes()

    translate_docx(source, destination, str.upper)

    assert source.read_bytes() == original
    translated = Document(str(destination))
    assert len(translated.tables) == 20
    assert len(translated.paragraphs) == 40
    for index, table in enumerate(translated.tables):
        heading, item = translated.paragraphs[2 * index : 2 * index + 2]
        assert heading.text == f"CHAPTER {index}"
        assert heading.style is not None and heading.style.name == "Heading 1"
        assert bool(heading.paragraph_format.page_break_before) == bool(index)
        assert item.text == f"ITEM {index}"
        assert item.style is not None and item.style.name == "List Number"
        assert item._p.xpath("./w:pPr/w:numPr/w:numId/@w:val") == ["5"]
        assert table.style is not None and table.style.name == "Table Grid"
        merged = table.cell(0, 0)
        assert merged._tc is table.cell(1, 0)._tc
        assert merged.paragraphs[0].text == f"MERGED {index}"
        assert merged.tables[0].cell(0, 0).text == f"NESTED {index}"
        assert table.cell(0, 1).text == f"RIGHT {index}"


def test_headers_and_footers_translate_once_without_creating_missing_parts(tmp_path: Path) -> None:
    path = tmp_path / "sections.docx"
    document = Document()
    first = document.sections[0]
    first.different_first_page_header_footer = True
    document.settings.odd_and_even_pages_header_footer = True
    first.header.paragraphs[0].text = "Header"
    first.first_page_header.paragraphs[0].text = "First header"
    first.even_page_header.paragraphs[0].text = "Even header"
    first.footer.paragraphs[0].text = "Footer"
    first.first_page_footer.paragraphs[0].text = "First footer"
    footer_table = first.footer.add_table(rows=1, cols=1, width=Inches(5))
    footer_table.cell(0, 0).text = "Footer table"
    second = document.add_section(WD_SECTION_START.NEW_PAGE)
    second.footer.is_linked_to_previous = False
    second.footer.paragraphs[0].text = "Second footer"
    document.save(str(path))

    translate_docx(path, path, lambda text: f"[{text}]")

    translated = Document(str(path))
    first, second = translated.sections
    assert first.header.paragraphs[0].text == "[Header]"
    assert first.first_page_header.paragraphs[0].text == "[First header]"
    assert first.even_page_header.paragraphs[0].text == "[Even header]"
    assert first.footer.paragraphs[0].text == "[Footer]"
    assert first.first_page_footer.paragraphs[0].text == "[First footer]"
    assert first.footer.tables[0].cell(0, 0).text == "[Footer table]"
    assert first.even_page_footer.is_linked_to_previous
    assert second.header.is_linked_to_previous
    assert second.footer.paragraphs[0].text == "[Second footer]"


def test_tall_vertical_merge_does_not_require_recursive_grid_lookup(tmp_path: Path) -> None:
    path = tmp_path / "tall.docx"
    document = Document()
    table = document.add_table(rows=1100, cols=1)
    table.rows[0].cells[0].text = "Merged cell"
    for index, row in enumerate(table.rows):
        (cell,) = row._tr.tc_lst
        cell.vMerge = "continue" if index else "restart"
    document.save(str(path))

    translate_docx(path, path, lambda text: f"[{text}]")

    (table,) = Document(str(path)).tables
    assert table.rows[0].cells[0].text == "[Merged cell]"
    assert len(table.rows) == 1100
    assert table.rows[-1]._tr.tc_lst[0].vMerge == "continue"


def test_translation_merges_runs_with_dominant_formatting_and_keeps_blank_runs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "styles.docx"
    document = Document()
    paragraph = document.add_paragraph(style="Subtitle")
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.add_run("One ").style = "Strong"
    paragraph.add_run("plain ").italic = True
    paragraph.add_run("word").style = "Strong"
    tie = document.add_paragraph()
    tie.add_run("ab").italic = True
    tie.add_run("cd").bold = True
    blank = document.add_paragraph()
    blank.add_run("\t \n").bold = True
    document.save(str(path))
    calls: list[str] = []

    def translate(text: str) -> str:
        calls.append(text)
        return "translated"

    translate_docx(path, path, translate)

    assert calls == ["One plain word", "abcd"]
    paragraph, tie, blank = Document(str(path)).paragraphs
    (run,) = paragraph.runs
    assert run.text == "translated"
    assert run.style.name == "Strong"
    assert paragraph.style is not None and paragraph.style.name == "Subtitle"
    assert paragraph.alignment is WD_ALIGN_PARAGRAPH.CENTER
    (run,) = tie.runs
    assert run.italic is True
    (run,) = blank.runs
    assert run.text == "\t \n" and run.bold is True


def test_hyperlinks_and_protected_content_survive(tmp_path: Path) -> None:
    path = tmp_path / "rich.docx"
    document = Document()
    paragraph = document.add_paragraph("Before ")
    url = "https://example.com/"
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True))
    paragraph._p.append(link)
    link_run = paragraph.add_run("click here")
    link_run.underline = True
    link.append(link_run._r)
    paragraph.add_run(" then ")
    paragraph.add_run().add_break(WD_BREAK.PAGE)
    paragraph.add_run("after")
    field_begin = document.add_paragraph("before field ")
    field_end = document.add_paragraph("cached result")
    for paragraph_with_field, marker in (
        (field_begin, "begin"),
        (field_begin, "separate"),
        (field_end, "end"),
    ):
        node = OxmlElement("w:fldChar")
        node.set(qn("w:fldCharType"), marker)
        run = paragraph_with_field.add_run()
        run._r.append(node)
        if marker in ("begin", "end"):
            wrapper = OxmlElement("w:smartTag")
            wrapper.set(qn("w:uri"), "urn:example:smart-tags")
            wrapper.set(qn("w:element"), "fieldBoundary")
            paragraph_with_field._p.append(wrapper)
            wrapper.append(run._r)
    field_end.add_run(" after field")
    unchanged = document.add_paragraph("keep ")
    unchanged.add_run("both").bold = True
    document.save(str(path))

    translate_docx(path, path, lambda text: text if text == "keep both" else text.upper())

    paragraph, field_begin, field_end, unchanged = Document(str(path)).paragraphs
    (hyperlink,) = paragraph.hyperlinks
    assert paragraph.text == "BEFORE CLICK HERE THEN AFTER"
    assert hyperlink.url == url
    assert hyperlink.runs[0].underline is True
    assert paragraph._p.xpath("./w:r/w:br/@w:type") == ["page"]
    assert field_begin.text == "BEFORE FIELD "
    assert field_end.text == "cached result AFTER FIELD"
    assert field_begin._p.xpath(".//w:fldChar/@w:fldCharType") == ["begin", "separate"]
    assert field_end._p.xpath(".//w:fldChar/@w:fldCharType") == ["end"]
    assert [run.text for run in unchanged.runs] == ["keep ", "both"]


def test_nonbreaking_hyphen_reaches_the_callback_as_text(tmp_path: Path) -> None:
    path = tmp_path / "hyphen.docx"
    document = Document()
    run = document.add_paragraph().add_run("part")
    run._r.append(OxmlElement("w:noBreakHyphen"))
    run._r.add_t("time work")
    document.save(str(path))

    translate_docx(path, path, lambda text: {"part-time work": "неповна зайнятість"}[text])

    (paragraph,) = Document(str(path)).paragraphs
    assert paragraph.text == "неповна зайнятість"


def test_field_in_text_box_does_not_affect_body_translation(tmp_path: Path) -> None:
    path = tmp_path / "textbox.docx"
    document = Document()
    run = document.add_paragraph().add_run()
    run._r.append(
        parse_xml(
            f'<w:pict {nsdecls("w")} xmlns:v="urn:schemas-microsoft-com:vml">'
            '<v:shape id="TextBox1"><v:textbox><w:txbxContent>'
            '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r></w:p>'
            "</w:txbxContent></v:textbox></v:shape></w:pict>"
        )
    )
    document.add_paragraph("Body text")
    document.save(str(path))

    translate_docx(path, path, str.upper)

    container, paragraph = Document(str(path)).paragraphs
    assert paragraph.text == "BODY TEXT"
    assert container._p.xpath(".//w:txbxContent//w:fldChar/@w:fldCharType") == ["begin"]


def test_callback_error_does_not_publish_partial_translation(tmp_path: Path) -> None:
    source = tmp_path / "source.docx"
    destination = tmp_path / "existing.docx"
    document = Document()
    document.add_paragraph("First")
    document.add_paragraph("Second")
    document.save(str(source))
    destination.write_bytes(b"original destination")

    def translate(text: str) -> str:
        if text == "Second":
            raise RuntimeError("translation failed")
        return text.upper()

    with pytest.raises(RuntimeError, match="translation failed"):
        translate_docx(source, destination, translate)

    assert destination.read_bytes() == b"original destination"
    assert set(tmp_path.iterdir()) == {source, destination}


def test_save_error_cleans_up_and_preserves_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.docx"
    destination = tmp_path / "existing.docx"
    document = Document()
    document.save(str(source))
    destination.write_bytes(b"original destination")

    def fail_save(self: object, stream: BinaryIO) -> None:
        stream.write(b"partial file")
        raise OSError("save failed")

    monkeypatch.setattr(type(document), "save", fail_save)
    with pytest.raises(OSError, match="save failed"):
        translate_docx(source, destination, str.upper)

    assert destination.read_bytes() == b"original destination"
    assert set(tmp_path.iterdir()) == {source, destination}

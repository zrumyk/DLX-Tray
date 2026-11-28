"""Render structured notes in the workbench and selection popup."""

from collections.abc import Iterable

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QPlainTextEdit

from dlx_tray.core.models import NoteKind, PhilologicalNote, SegmentAnnotation, StylisticRegister


def register_label(register: StylisticRegister) -> str:
    return {
        StylisticRegister.NEUTRAL: QCoreApplication.translate("Philology", "Neutral"),
        StylisticRegister.LITERARY: QCoreApplication.translate("Philology", "Literary"),
        StylisticRegister.ACADEMIC: QCoreApplication.translate("Philology", "Academic"),
        StylisticRegister.JOURNALISTIC: QCoreApplication.translate("Philology", "Journalistic"),
        StylisticRegister.OFFICIAL: QCoreApplication.translate("Philology", "Official"),
        StylisticRegister.LITERAL: QCoreApplication.translate("Philology", "Literal"),
    }[register]


def note_sections(notes: Iterable[PhilologicalNote]) -> list[str]:
    grouped: dict[NoteKind, list[str]] = {}
    for note in notes:
        grouped.setdefault(note.kind, []).append(note.text)
    labels = {
        NoteKind.ALTERNATIVE: QCoreApplication.translate("Philology", "Alternatives"),
        NoteKind.IDIOM: QCoreApplication.translate("Philology", "Idioms and cultural references"),
        NoteKind.GRAMMAR: QCoreApplication.translate("Philology", "Grammar and syntax"),
    }
    return [
        "\n".join((label, *(f"• {text}" for text in grouped[kind])))
        for kind, label in labels.items()
        if kind in grouped
    ]


class InspectorPanel(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setAccessibleName(self.tr("Philological Inspector"))
        self.setPlaceholderText(self.tr("Select a translated segment to inspect it."))

    def show_segment(
        self, number: int, register: StylisticRegister, segment: SegmentAnnotation
    ) -> None:
        provider_names = {"cloud_ai": "Cloud AI", "dlx": "DLX", "deepl": "DeepL API"}
        facts = [
            self.tr("Segment {number}").format(number=number),
            self.tr("Register: {register}").format(register=register_label(register)),
        ]
        if segment.providers:
            facts.append(
                self.tr("Provider: {provider}").format(
                    provider=", ".join(provider_names.get(name, name) for name in segment.providers)
                )
            )
        sections = ["\n".join(facts)]
        if segment.glossary:
            sections.append(
                "\n".join(
                    (
                        self.tr("Matched glossary terms"),
                        *(f"{term.source_term} → {term.target_term}" for term in segment.glossary),
                    )
                )
            )
        sections.extend(note_sections(segment.notes) or [self.tr("No notes for this segment.")])
        self.setPlainText("\n\n".join(sections))

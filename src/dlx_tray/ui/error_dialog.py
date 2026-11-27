"""Ask how to handle a chunk that could not be translated."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from dlx_tray.core.models import (
    AuthError,
    ProviderUnavailableError,
    RateLimitedError,
    TextTooLongError,
    TranslationError,
)
from dlx_tray.core.pipeline import FailureAction


class ErrorDialog(QDialog):
    diagnostics_requested = Signal()

    def __init__(
        self, chunk_number: int, total: int, error: TranslationError, parent: QWidget
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Translation paused"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.action = FailureAction.CANCEL

        layout = QVBoxLayout(self)
        body = QHBoxLayout()
        layout.addLayout(body)
        icon = QLabel()
        icon.setPixmap(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning).pixmap(32)
        )
        body.addWidget(icon, alignment=Qt.AlignmentFlag.AlignTop)
        labels = QVBoxLayout()
        body.addLayout(labels)
        heading = self.tr("Could not translate chunk {number} of {total}.")
        self.heading = QLabel(heading.format(number=chunk_number, total=total))
        labels.addWidget(self.heading)
        choices = self.tr(
            "Continue retries this chunk. Skip keeps its original text. "
            "Cancel stops and keeps the result so far."
        )
        self.detail = QLabel(f"{self._reason(error)}\n\n{choices}")
        self.detail.setWordWrap(True)
        labels.addWidget(self.detail)

        buttons = QDialogButtonBox()
        self.continue_button = buttons.addButton(
            self.tr("Continue"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.skip_button = buttons.addButton(
            self.tr("Skip"), QDialogButtonBox.ButtonRole.ActionRole
        )
        self.cancel_button = buttons.addButton(
            self.tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole
        )
        self.diagnostics_button = buttons.addButton(
            self.tr("Copy diagnostics"), QDialogButtonBox.ButtonRole.HelpRole
        )
        layout.addWidget(buttons)
        for button in (self.continue_button, self.skip_button, self.diagnostics_button):
            button.setAutoDefault(False)
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
        self.continue_button.clicked.connect(lambda: self._choose(FailureAction.CONTINUE))
        self.skip_button.clicked.connect(lambda: self._choose(FailureAction.SKIP))
        buttons.rejected.connect(self.reject)
        self.diagnostics_button.clicked.connect(self.diagnostics_requested)
        self.diagnostics_feedback = QLabel()
        layout.addWidget(self.diagnostics_feedback)

    def _choose(self, action: FailureAction) -> None:
        self.action = action
        self.accept()

    def _reason(self, error: TranslationError) -> str:
        if isinstance(error, AuthError):
            return self.tr("The translation provider rejected its credentials.")
        if isinstance(error, RateLimitedError):
            return self.tr("The translation provider is rate-limited.")
        if isinstance(error, ProviderUnavailableError):
            return self.tr("The translation provider is unavailable.")
        if isinstance(error, TextTooLongError):
            return self.tr("The translation provider rejected the chunk because it was too long.")
        return self.tr("The translation provider could not complete the request.")

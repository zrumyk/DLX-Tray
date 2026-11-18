"""Ask how to handle a chunk that could not be translated."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget

from dlx_tray.core.models import (
    AuthError,
    ProviderUnavailableError,
    RateLimitedError,
    TextTooLongError,
    TranslationError,
)
from dlx_tray.core.pipeline import FailureAction


class ErrorDialog(QMessageBox):
    def __init__(
        self, chunk_number: int, total: int, error: TranslationError, parent: QWidget
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Translation paused"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setIcon(QMessageBox.Icon.Warning)
        heading = self.tr("Could not translate chunk {number} of {total}.")
        self.setText(heading.format(number=chunk_number, total=total))
        choices = self.tr(
            "Continue retries this chunk. Skip keeps its original text. "
            "Cancel stops and keeps the result so far."
        )
        self.setInformativeText(f"{self._reason(error)}\n\n{choices}")
        self.continue_button = self.addButton(
            self.tr("Continue"), QMessageBox.ButtonRole.AcceptRole
        )
        self.skip_button = self.addButton(self.tr("Skip"), QMessageBox.ButtonRole.ActionRole)
        self.cancel_button = self.addButton(self.tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        self.setDefaultButton(self.cancel_button)
        self.setEscapeButton(self.cancel_button)

    @property
    def action(self) -> FailureAction:
        clicked = self.clickedButton()
        if clicked == self.continue_button:
            return FailureAction.CONTINUE
        if clicked == self.skip_button:
            return FailureAction.SKIP
        return FailureAction.CANCEL

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

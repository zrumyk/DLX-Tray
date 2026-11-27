"""Show the running version and license notices."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

from dlx_tray import APP_NAME, __version__


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("About DLX Tray"))

        layout = QVBoxLayout(self)
        version = QLabel(f"{APP_NAME} {__version__}")
        version.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(version)

        notices = QLabel(
            "\n".join(
                (
                    self.tr("DLX Tray: MIT License"),
                    self.tr("Bundled DLX: MIT License"),
                    self.tr(
                        "Qt and PySide6: GNU Lesser General Public License, version 3 (LGPLv3)."
                    ),
                )
            )
        )
        notices.setWordWrap(True)
        layout.addWidget(notices)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

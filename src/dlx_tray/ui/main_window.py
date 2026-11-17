"""Main application window. v0.1.0: an empty shell; the translator UI lands in v0.3.0."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QMainWindow, QWidget

from dlx_tray import APP_NAME, __version__


class MainWindow(QMainWindow):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1100, 700)
        placeholder = QLabel(self.tr("DLX Tray is under construction."), self)
        placeholder.setMargin(24)
        self.setCentralWidget(placeholder)

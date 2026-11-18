"""Apply the appearance preference through Qt's platform theme."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from dlx_tray.config import Theme

_COLOR_SCHEMES: dict[Theme, Qt.ColorScheme] = {
    "light": Qt.ColorScheme.Light,
    "dark": Qt.ColorScheme.Dark,
    "system": Qt.ColorScheme.Unknown,
}


def apply_theme(app: QApplication, theme: Theme) -> None:
    # Qt's WindowsVista widget style uses light colors even with a dark platform palette.
    if app.style().name() == "windowsvista":
        app.setStyle("windows")

    # Unknown clears the override so Qt can follow subsequent Windows theme changes.
    app.styleHints().setColorScheme(_COLOR_SCHEMES[theme])

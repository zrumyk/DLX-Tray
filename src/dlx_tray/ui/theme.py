"""Apply the appearance preference through Qt's platform theme."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QStyleHints

from dlx_tray.config import Theme

_COLOR_SCHEMES: dict[Theme, Qt.ColorScheme] = {
    "light": Qt.ColorScheme.Light,
    "dark": Qt.ColorScheme.Dark,
    "system": Qt.ColorScheme.Unknown,
}


def apply_theme(hints: QStyleHints, theme: Theme) -> None:
    # Unknown clears the override so Qt can follow subsequent Windows theme changes.
    hints.setColorScheme(_COLOR_SCHEMES[theme])

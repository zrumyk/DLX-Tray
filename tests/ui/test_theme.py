"""Check widget style compatibility when applying the theme."""

from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from dlx_tray.ui.theme import apply_theme


@pytest.mark.parametrize("style", ["windowsvista", "windows11", "fusion"])
def test_only_windowsvista_is_replaced(style: str) -> None:
    app = Mock(spec=QApplication)
    app.style.return_value.name.return_value = style

    apply_theme(app, "dark")

    if style == "windowsvista":
        app.setStyle.assert_called_once_with("windows")
    else:
        app.setStyle.assert_not_called()
    app.styleHints.return_value.setColorScheme.assert_called_once_with(Qt.ColorScheme.Dark)

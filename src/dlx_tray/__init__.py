"""DLX Tray - free desktop translator powered by a bundled DLX server."""

from importlib.metadata import PackageNotFoundError, version

APP_NAME = "DLX Tray"

try:
    __version__ = version("dlx-tray")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0"

"""Write the PyInstaller version resource for the installed DLX Tray distribution."""

import re
import sys
from importlib.metadata import version
from pathlib import Path

from dlx_tray import APP_NAME


def render_version_info(app_version: str) -> str:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", app_version):
        raise ValueError(f"Expected an X.Y.Z release version, got {app_version!r}")
    major, minor, patch = map(int, app_version.split("."))
    if max(major, minor, patch) > 65535:
        raise ValueError(f"Windows version components must fit in 16 bits: {app_version}")
    windows_version = (major, minor, patch, 0)

    # 0409 is en-US and 04B0 is the Unicode code page used by the string table.
    return f"""\
VSVersionInfo(
    ffi=FixedFileInfo(
        filevers={windows_version},
        prodvers={windows_version},
        mask=0x3f,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo([
            StringTable('040904B0', [
                StringStruct('FileDescription', {APP_NAME!r}),
                StringStruct('FileVersion', {app_version!r}),
                StringStruct('OriginalFilename', {f"{APP_NAME}.exe"!r}),
                StringStruct('ProductName', {APP_NAME!r}),
                StringStruct('ProductVersion', {app_version!r}),
            ]),
        ]),
        VarFileInfo([VarStruct('Translation', [0x0409, 1200])]),
    ],
)
"""


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(render_version_info(version("dlx-tray")), encoding="utf-8")

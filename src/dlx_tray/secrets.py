"""Store the official DeepL API key in Windows Credential Manager."""

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from keyring.backends import Windows
from keyring.errors import KeyringError, NoKeyringError, PasswordDeleteError

from dlx_tray import APP_NAME

log = logging.getLogger(__name__)

_SERVICE = APP_NAME
_USERNAME = "deepl_api_key"


@contextmanager
def _credential_manager() -> Iterator[Windows.WinVaultKeyring]:
    if sys.platform != "win32" or not Windows.WinVaultKeyring.viable:
        raise NoKeyringError("Windows Credential Manager is unavailable")
    # Catch the OS error from whichever Win32 binding keyring loaded.
    try:
        yield Windows.WinVaultKeyring()
    except (Windows.pywintypes.error, OSError, UnicodeError):  # pyright: ignore[reportPrivateImportUsage]
        raise KeyringError(
            "Windows Credential Manager could not access the DeepL API key"
        ) from None


def get_deepl_api_key(override: str | None = None) -> str | None:
    """Resolve a key with the environment/.env override supplied by Settings."""
    if override and (key := override.strip()):
        return key
    try:
        with _credential_manager() as vault:
            stored = vault.get_password(_SERVICE, _USERNAME)
    except KeyringError:
        log.warning("Could not read the DeepL API key from Windows Credential Manager")
        return None
    return (stored.strip() or None) if isinstance(stored, str) else None


def set_deepl_api_key(key: str) -> None:
    key = key.strip()
    if not key:
        raise ValueError("The DeepL API key must not be blank")
    with _credential_manager() as vault:
        existing = vault._read_credential(_SERVICE)
        if existing is not None and existing["UserName"] != _USERNAME:
            vault.set_password(_SERVICE, _USERNAME, key)
        else:
            # WinVault's public setter would keep the previous key in a second entry.
            vault._set_password(_SERVICE, _USERNAME, key)


def delete_deepl_api_key() -> None:
    """Remove the stored key if present."""
    # WinVault raises PasswordDeleteError when the credential is absent.
    with _credential_manager() as vault, suppress(PasswordDeleteError):
        vault.delete_password(_SERVICE, _USERNAME)

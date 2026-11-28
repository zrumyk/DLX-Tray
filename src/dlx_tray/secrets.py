"""Store provider credentials in Windows Credential Manager."""

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from keyring.backends import Windows
from keyring.errors import KeyringError, NoKeyringError, PasswordDeleteError

from dlx_tray import APP_NAME

log = logging.getLogger(__name__)

_DEEPL = (APP_NAME, "deepl_api_key")
_CLOUD = (f"{APP_NAME} Cloud", "cloud_api_token")


@contextmanager
def _credential_manager() -> Iterator[Windows.WinVaultKeyring]:
    if sys.platform != "win32" or not Windows.WinVaultKeyring.viable:
        raise NoKeyringError("Windows Credential Manager is unavailable")
    # Catch the OS error from whichever Win32 binding keyring loaded.
    try:
        yield Windows.WinVaultKeyring()
    except (Windows.pywintypes.error, OSError, UnicodeError):  # pyright: ignore[reportPrivateImportUsage]
        raise KeyringError(
            "Windows Credential Manager could not access the provider credential"
        ) from None


def _get_credential(identity: tuple[str, str], override: str | None) -> str | None:
    if override and (key := override.strip()):
        return key
    try:
        with _credential_manager() as vault:
            stored = vault.get_password(*identity)
    except KeyringError:
        log.warning("Could not read %s from Windows Credential Manager", identity[1])
        return None
    return (stored.strip() or None) if isinstance(stored, str) else None


def _set_credential(identity: tuple[str, str], key: str) -> None:
    service, username = identity
    key = key.strip()
    if not key:
        raise ValueError(f"{username} must not be blank")
    with _credential_manager() as vault:
        existing = vault._read_credential(service)
        if existing is not None and existing["UserName"] != username:
            vault.set_password(service, username, key)
        else:
            # WinVault's public setter would keep the previous key in a second entry.
            vault._set_password(service, username, key)


def _delete_credential(identity: tuple[str, str]) -> None:
    # WinVault raises PasswordDeleteError when the credential is absent.
    with _credential_manager() as vault, suppress(PasswordDeleteError):
        vault.delete_password(*identity)


def get_deepl_api_key(override: str | None = None) -> str | None:
    """Resolve a key with the environment/.env override supplied by Settings."""
    return _get_credential(_DEEPL, override)


def set_deepl_api_key(key: str) -> None:
    _set_credential(_DEEPL, key)


def delete_deepl_api_key() -> None:
    _delete_credential(_DEEPL)


def get_cloud_api_token(override: str | None = None) -> str | None:
    """Resolve a token with the environment/.env override supplied by Settings."""
    return _get_credential(_CLOUD, override)


def set_cloud_api_token(token: str) -> None:
    _set_credential(_CLOUD, token)


def delete_cloud_api_token() -> None:
    _delete_credential(_CLOUD)

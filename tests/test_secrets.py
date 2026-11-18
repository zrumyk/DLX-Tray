"""Test credential handling without accessing the user's Windows vault."""

import logging
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from keyring.backends.Windows import DecodingCredential, WinVaultKeyring
from keyring.errors import KeyringError, NoKeyringError, PasswordDeleteError

from dlx_tray import secrets


class _VaultError(Exception):
    pass


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> Mock:
    vault = Mock()
    monkeypatch.setattr(secrets, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        secrets,
        "Windows",
        SimpleNamespace(
            WinVaultKeyring=Mock(viable=True, return_value=vault),
            pywintypes=SimpleNamespace(error=_VaultError),
        ),
    )
    return vault


def test_override_skips_keyring(vault: Mock) -> None:
    assert secrets.get_deepl_api_key(" override:fx ") == "override:fx"
    assert vault.mock_calls == []


@pytest.mark.parametrize("override", [None, "", " \t"])
def test_get_stored_key(vault: Mock, override: str | None) -> None:
    vault.get_password.return_value = " stored:fx "
    assert secrets.get_deepl_api_key(override) == "stored:fx"
    vault.get_password.assert_called_once_with("DLX Tray", "deepl_api_key")


@pytest.mark.parametrize("stored", [None, " \t"])
def test_no_stored_key(vault: Mock, stored: str | None) -> None:
    vault.get_password.return_value = stored
    assert secrets.get_deepl_api_key() is None


def test_replacing_key_does_not_leave_a_second_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials: dict[str, DecodingCredential] = {}
    vault = WinVaultKeyring()

    def write(target: str, username: str, key: str) -> None:
        credentials[target] = DecodingCredential(
            UserName=username, CredentialBlob=key.encode("utf-16-le")
        )

    monkeypatch.setattr(vault, "_read_credential", credentials.get)
    monkeypatch.setattr(vault, "_set_password", write)
    monkeypatch.setattr(secrets, "_credential_manager", lambda: nullcontext(vault))

    secrets.set_deepl_api_key("old:fx")
    secrets.set_deepl_api_key(" new:fx ")
    assert secrets.get_deepl_api_key() == "new:fx"
    del credentials["DLX Tray"]
    assert secrets.get_deepl_api_key() is None


def test_set_key_preserves_another_username(vault: Mock) -> None:
    vault._read_credential.return_value = {"UserName": "another-user"}
    secrets.set_deepl_api_key(" stored:fx ")
    vault.set_password.assert_called_once_with("DLX Tray", "deepl_api_key", "stored:fx")
    vault._set_password.assert_not_called()


def test_reject_blank_key(vault: Mock) -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        secrets.set_deepl_api_key(" \t")
    assert vault.mock_calls == []


def test_delete_key_can_be_repeated(vault: Mock) -> None:
    secrets.delete_deepl_api_key()
    vault.delete_password.assert_called_once_with("DLX Tray", "deepl_api_key")
    vault.delete_password.side_effect = PasswordDeleteError()
    secrets.delete_deepl_api_key()


def test_unavailable_backend(vault: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets.Windows.WinVaultKeyring, "viable", False)
    assert secrets.get_deepl_api_key() is None
    with pytest.raises(NoKeyringError):
        secrets.set_deepl_api_key("test-key")
    with pytest.raises(NoKeyringError):
        secrets.delete_deepl_api_key()
    assert vault.mock_calls == []


def test_failed_read_logs_without_backend_details(
    vault: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    vault.get_password.side_effect = _VaultError("test-key")
    assert secrets.get_deepl_api_key() is None
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert "test-key" not in caplog.text


def test_failed_writes_raise_safe_errors(vault: Mock) -> None:
    vault._read_credential.return_value = None
    vault._set_password.side_effect = _VaultError("test-key")
    vault.delete_password.side_effect = _VaultError("test-key")
    with pytest.raises(KeyringError, match="could not access") as set_error:
        secrets.set_deepl_api_key("test-key")
    with pytest.raises(KeyringError, match="could not access") as delete_error:
        secrets.delete_deepl_api_key()
    assert "test-key" not in str(set_error.value)
    assert "test-key" not in str(delete_error.value)

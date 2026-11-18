"""Exercise settings defaults, source priority, and JSON persistence."""

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from dlx_tray import APP_NAME, config
from dlx_tray.config import Settings
from dlx_tray.core.models import Formality, Language
from dlx_tray.core.retry import RetryPolicy


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in os.environ:
        if key.upper().startswith("DLX_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize(
    ("system_language", "expected"),
    [("uk_UA", "uk"), ("Ukrainian_Ukraine", "uk"), ("en_US", "en"), ("de_DE", "en"), (None, "en")],
)
def test_missing_file_uses_defaults(
    monkeypatch: pytest.MonkeyPatch, system_language: str | None, expected: str
) -> None:
    monkeypatch.setattr(config.locale, "getlocale", lambda: (system_language, None))
    settings = Settings.load()
    assert settings.source_lang is Language.AUTO
    assert settings.target_lang is Language.UK
    assert settings.chunk_size == 1400
    assert settings.request_delay == 1.5
    assert settings.retries == 3
    assert RetryPolicy(max_attempts=settings.max_attempts).max_attempts == 4
    assert settings.history_enabled and settings.history_limit == 500
    assert settings.theme == "system"
    assert settings.ui_language == expected


def test_load_applies_environment_then_dotenv_then_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "custom.json"
    path.write_text(
        '{"chunk_size": 900, "request_delay": 4, "target_lang": "en-gb", "retries": 0}',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "DLX_CHUNK_SIZE=1100\nDLX_REQUEST_DELAY=2.5\nDLX_DEEPLX_PORT=\nUNRELATED=1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DLX_CHUNK_SIZE", "1200")
    settings = Settings.load(path)
    assert settings.chunk_size == 1200
    assert settings.request_delay == 2.5
    assert settings.target_lang is Language.EN_GB
    assert settings.max_attempts == 1
    assert settings.deeplx_port is None


def test_frozen_app_ignores_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DLX_CHUNK_SIZE=300\n", encoding="utf-8")
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    assert Settings.load().chunk_size == 1400


def test_save_preserves_preferences_shadowed_by_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"chunk_size": 900}', encoding="utf-8")
    (tmp_path / ".env").write_text("DLX_REQUEST_DELAY=6\nTHEME=light\n", encoding="utf-8")
    monkeypatch.setenv("DLX_CHUNK_SIZE", "1200")
    settings = Settings.load(path)
    assert settings.chunk_size == 1200
    assert settings.request_delay == 6
    assert settings.theme == "system"
    settings.history_enabled = False
    settings.save(path)

    monkeypatch.delenv("DLX_CHUNK_SIZE")
    loaded = Settings.load(path, env_file=None)
    assert loaded.chunk_size == 900
    assert loaded.request_delay == 1.5
    assert not loaded.history_enabled
    assert "request_delay" not in json.loads(path.read_text(encoding="utf-8"))


def test_round_trip_keeps_preferences_and_excludes_runtime_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = {
        "deepl_api_key": "test-key",
        "deeplx_token": "test-token",
        "deeplx_url": "http://localhost:1234/",
        "deeplx_port": "1234",
        "log_level": "DEBUG",
    }
    path = tmp_path / APP_NAME / "settings.json"
    for field, value in runtime.items():
        monkeypatch.setenv(f"DLX_{field.upper()}", value)

    settings = Settings.load()
    assert settings.deepl_api_key is not None
    assert settings.deepl_api_key.get_secret_value() == "test-key"
    assert settings.deeplx_token is not None
    assert settings.deeplx_token.get_secret_value() == "test-token"
    assert str(settings.deeplx_url) == runtime["deeplx_url"]
    assert settings.deeplx_port == 1234
    assert settings.log_level == "DEBUG"
    settings.formality = Formality.MORE
    settings.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert not runtime.keys() & data.keys()

    for field in runtime:
        monkeypatch.delenv(f"DLX_{field.upper()}")
    path.write_text(json.dumps(data | runtime), encoding="utf-8")
    loaded = Settings.load()
    assert loaded.model_dump() == settings.model_dump()
    assert (
        loaded.deepl_api_key
        is loaded.deeplx_token
        is loaded.deeplx_url
        is loaded.deeplx_port
        is None
    )
    assert loaded.log_level == "INFO"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CHUNK_SIZE", "1501"),
        ("REQUEST_DELAY", "nan"),
        ("RETRIES", "-1"),
        ("TARGET_LANG", "auto"),
        ("SOURCE_LANG", "EN-US"),
        ("DEEPLX_PORT", "65536"),
        ("DEEPLX_URL", "ftp://localhost"),
        ("LOG_LEVEL", "verbose"),
    ],
)
def test_invalid_override_is_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(f"DLX_{name}", value)
    with pytest.raises(ValidationError, match=name.lower()):
        Settings.load()


def test_invalid_language_is_not_echoed_in_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLX_TARGET_LANG", "private-sentinel")
    with pytest.raises(ValidationError, match="Unknown language code") as error:
        Settings.load()
    assert "private-sentinel" not in str(error.value)


@pytest.mark.parametrize("content", ["[", "[]"])
def test_invalid_file_is_preserved(tmp_path: Path, content: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        Settings.load(path)
    assert path.read_text(encoding="utf-8") == content


def test_failed_save_preserves_previous_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"chunk_size": 700}', encoding="utf-8")
    settings = Settings.load(path)
    settings.chunk_size = 999

    def fail_replace(source: Path, target: Path) -> Path:
        raise PermissionError("destination locked")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(PermissionError, match="destination locked"):
        settings.save(path)
    assert Settings.load(path).chunk_size == 700
    assert list(tmp_path.iterdir()) == [path]

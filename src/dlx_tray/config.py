"""Load user preferences and development overrides at application startup."""

from __future__ import annotations

import json
import locale
import os
import re
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, PrivateAttr, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from dlx_tray import APP_NAME
from dlx_tray._file_lock import file_lock
from dlx_tray.core.chunker import DEFAULT_MAX_UNITS, DLX_MAX_UNITS
from dlx_tray.core.models import (
    AnalysisTier,
    Formality,
    Language,
    StylisticRegister,
    TranslationOptions,
)
from dlx_tray.core.retry import DEFAULT_REQUEST_DELAY

type UiLanguage = Literal["en", "uk"]
type Theme = Literal["system", "light", "dark"]
type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
type ProviderMode = Literal["automatic", "cloud_only", "dlx_only", "official_only"]

DEFAULT_SELECTION_HOTKEY = "Ctrl+C,C"
_SETTINGS_VERSION = 2
_LEGACY_REQUEST_DELAY = 2.5


def app_data_dir() -> Path:
    if appdata := os.getenv("APPDATA"):
        return Path(appdata) / APP_NAME
    return Path.home() / "AppData" / "Roaming" / APP_NAME


def system_ui_language() -> UiLanguage:
    language, _ = locale.getlocale()
    primary = (language or "").lower().replace("-", "_").split("_", 1)[0]
    return "uk" if primary in {"uk", "ukrainian"} else "en"


def _read_preferences(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("settings.json must contain a JSON object")
    return data


def _write_preferences(path: Path, data: dict[str, object]) -> None:
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _needs_migration(data: dict[str, object]) -> bool:
    return "settings_version" not in data or data["settings_version"] == 1


def _migrate_preferences(data: dict[str, object]) -> None:
    if "settings_version" not in data and data.get("request_delay") == _LEGACY_REQUEST_DELAY:
        data["request_delay"] = DEFAULT_REQUEST_DELAY
    if _needs_migration(data):
        data["settings_version"] = _SETTINGS_VERSION
        data.pop("cloud_api_token", None)


class Settings(BaseSettings):
    """Effective preferences, with environment and dotenv taking priority over JSON."""

    model_config = SettingsConfigDict(
        env_prefix="DLX_",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        validate_assignment=True,
        hide_input_in_errors=True,
    )

    settings_version: int = Field(default=_SETTINGS_VERSION, ge=1)
    source_lang: Language = Language.AUTO
    target_lang: Language = Language.UK
    window_geometry: str | None = None
    provider_mode: ProviderMode = "automatic"
    cloud_url: AnyHttpUrl | None = None
    hybrid_dlx_threshold: int = Field(default=DLX_MAX_UNITS, ge=0, le=DLX_MAX_UNITS)
    chunk_size: int = Field(default=DEFAULT_MAX_UNITS, ge=200, le=DLX_MAX_UNITS)
    request_delay: float = Field(default=DEFAULT_REQUEST_DELAY, ge=0, le=10)
    dlx_concurrency: int = Field(default=2, ge=1, le=2)
    retries: int = Field(default=3, ge=0)
    formality: Formality = Formality.DEFAULT
    stylistic_register: StylisticRegister = StylisticRegister.NEUTRAL
    analysis_tier: AnalysisTier = AnalysisTier.STANDARD
    style_context: str = ""
    include_notes: bool = False
    history_enabled: bool = True
    history_limit: int = Field(default=500, ge=1)
    cache_enabled: bool = True
    theme: Theme = "system"
    ui_language: UiLanguage = Field(default_factory=system_ui_language)
    selection_hotkey: str = DEFAULT_SELECTION_HOTKEY

    deepl_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    cloud_api_token: SecretStr | None = Field(default=None, exclude=True, repr=False)
    deeplx_token: SecretStr | None = Field(default=None, exclude=True, repr=False)
    deeplx_url: AnyHttpUrl | None = Field(default=None, exclude=True, repr=False)
    deeplx_port: int | None = Field(default=None, ge=1, le=65535, exclude=True)
    log_level: LogLevel = Field(default="INFO", exclude=True)

    _overridden_preferences: set[str] = PrivateAttr(default_factory=set)
    _migration_error: str | None = PrivateAttr(default=None)

    @field_validator("style_context")
    @classmethod
    def _normalize_style_context(cls, value: str) -> str:
        return TranslationOptions(style_context=value).style_context

    @field_validator("selection_hotkey", mode="before")
    @classmethod
    def _validate_selection_hotkey(cls, value: object) -> str:
        if isinstance(value, str):
            shortcut = value.strip().upper()
            if shortcut == DEFAULT_SELECTION_HOTKEY.upper():
                return DEFAULT_SELECTION_HOTKEY
            if re.fullmatch(r"CTRL\+SHIFT\+[A-Z]", shortcut):
                return "Ctrl+Shift+" + shortcut[-1]
            if not shortcut:
                return ""
        raise ValueError("invalid selection hotkey")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # load() reads the ambient sources once to track which fields are temporary.
        return (init_settings,)

    @field_validator("source_lang", "target_lang", mode="before")
    @classmethod
    def _parse_language(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        try:
            return Language.from_code(value)
        except ValueError:
            raise ValueError("Unknown language code") from None

    @field_validator("source_lang")
    @classmethod
    def _validate_source(cls, value: Language) -> Language:
        if not value.can_be_source:
            raise ValueError("Regional language variants are valid only as targets")
        return value

    @field_validator("target_lang")
    @classmethod
    def _validate_target(cls, value: Language) -> Language:
        if not value.can_be_target:
            raise ValueError("The target language must be specified")
        return value

    @property
    def max_attempts(self) -> int:
        """Convert retries into the total attempt budget expected by RetryPolicy."""
        return 1 + self.retries

    @field_validator("window_geometry", mode="before")
    @classmethod
    def _optional_geometry(cls, value: object) -> str | None:
        return value if isinstance(value, str) else None

    def is_overridden(self, name: str) -> bool:
        return name in self._overridden_preferences

    @property
    def migration_error(self) -> str | None:
        """Return the error type if startup could not persist a migration."""
        return self._migration_error

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        *,
        env_file: Path | None = Path(".env"),
        migrate: bool = False,
    ) -> Self:
        if path is None:
            path = app_data_dir() / "settings.json"
        dotenv = DotEnvSettingsSource(
            cls, env_file=None if getattr(sys, "frozen", False) else env_file
        )
        # Match declared DLX_* fields only, just as for the process environment.
        overrides = EnvSettingsSource.__call__(dotenv) | EnvSettingsSource(cls)()
        overrides.pop("settings_version", None)
        data = _read_preferences(path)
        if migrate and _needs_migration(data):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with file_lock(path.with_name(f"{path.name}.lock")):
                    data = _read_preferences(path)
                    if _needs_migration(data):
                        _migrate_preferences(data)
                        settings = cls._with_overrides(data, overrides)
                        _write_preferences(path, data)
                        return settings
            except OSError as exc:
                _migrate_preferences(data)
                settings = cls._with_overrides(data, overrides)
                settings._migration_error = type(exc).__name__
                return settings
        if data.get("settings_version") == 1:
            _migrate_preferences(data)
        return cls._with_overrides(data, overrides)

    @classmethod
    def _with_overrides(cls, data: dict[str, object], overrides: dict[str, object]) -> Self:
        preference_fields = {name for name, field in cls.model_fields.items() if not field.exclude}
        preferences = {name: data[name] for name in preference_fields & data.keys()}
        settings = cls.model_validate(preferences | overrides)
        settings._overridden_preferences = preference_fields & overrides.keys()
        return settings

    def save(self, path: Path | None = None, *, changes_from: Settings | None = None) -> None:
        """Atomically save preferences, preserving settings unedited since changes_from."""
        if path is None:
            path = app_data_dir() / "settings.json"
        saved = self.model_dump(mode="json", exclude=self._overridden_preferences)
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path.with_name(f"{path.name}.lock")):
            preserve = self._overridden_preferences.copy()
            if changes_from is not None:
                before = changes_from.model_dump(mode="json")
                preserve.update(name for name, value in saved.items() if before[name] == value)
                saved = {name: value for name, value in saved.items() if name not in preserve}
            if preserve or self._migration_error is not None:
                existing = _read_preferences(path)
                if self._migration_error is not None:
                    _migrate_preferences(existing)
                    saved["settings_version"] = existing["settings_version"]
                saved.update({name: existing[name] for name in preserve & existing.keys()})

            _write_preferences(path, saved)

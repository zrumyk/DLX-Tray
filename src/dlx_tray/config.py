"""Load user preferences and development overrides at application startup."""

from __future__ import annotations

import json
import locale
import os
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
from dlx_tray.core.models import Formality, Language

type UiLanguage = Literal["en", "uk"]
type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


def app_data_dir() -> Path:
    if appdata := os.getenv("APPDATA"):
        return Path(appdata) / APP_NAME
    return Path.home() / "AppData" / "Roaming" / APP_NAME


def _system_ui_language() -> UiLanguage:
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


class Settings(BaseSettings):
    """Effective preferences, with environment and dotenv taking priority over JSON."""

    model_config = SettingsConfigDict(
        env_prefix="DLX_",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        validate_assignment=True,
        hide_input_in_errors=True,
    )

    source_lang: Language = Language.AUTO
    target_lang: Language = Language.UK
    chunk_size: int = Field(default=1400, ge=200, le=1500)
    request_delay: float = Field(default=1.5, ge=0, le=10)
    retries: int = Field(default=3, ge=0)
    formality: Formality = Formality.DEFAULT
    history_enabled: bool = True
    history_limit: int = Field(default=500, ge=1)
    theme: Literal["system", "light", "dark"] = "system"
    ui_language: UiLanguage = Field(default_factory=_system_ui_language)

    deepl_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    deeplx_token: SecretStr | None = Field(default=None, exclude=True, repr=False)
    deeplx_url: AnyHttpUrl | None = Field(default=None, exclude=True, repr=False)
    deeplx_port: int | None = Field(default=None, ge=1, le=65535, exclude=True)
    log_level: LogLevel = Field(default="INFO", exclude=True)

    _overridden_preferences: set[str] = PrivateAttr(default_factory=set)

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

    def is_overridden(self, name: str) -> bool:
        return name in self._overridden_preferences

    @classmethod
    def load(cls, path: Path | None = None, *, env_file: Path | None = Path(".env")) -> Self:
        if path is None:
            path = app_data_dir() / "settings.json"
        data = _read_preferences(path)
        preference_fields = {name for name, field in cls.model_fields.items() if not field.exclude}
        preferences = {name: data[name] for name in preference_fields & data.keys()}
        dotenv = DotEnvSettingsSource(
            cls, env_file=None if getattr(sys, "frozen", False) else env_file
        )
        # Match declared DLX_* fields only, just as for the process environment.
        overrides = EnvSettingsSource.__call__(dotenv) | EnvSettingsSource(cls)()
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
            if preserve:
                existing = _read_preferences(path)
                saved.update({name: existing[name] for name in preserve & existing.keys()})

            temporary: Path | None = None
            try:
                with NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=path.parent, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(json.dumps(saved, indent=2, ensure_ascii=False) + "\n")
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

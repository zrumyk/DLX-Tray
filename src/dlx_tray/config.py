"""Load user preferences and development overrides at application startup."""

from __future__ import annotations

import json
import locale
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from dlx_tray import APP_NAME
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


class Settings(BaseSettings):
    """Effective preferences, with environment and dotenv taking priority over JSON."""

    model_config = SettingsConfigDict(
        env_prefix="DLX_",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return env_settings, dotenv_settings, init_settings

    @field_validator("source_lang", "target_lang", mode="before")
    @classmethod
    def _parse_language(cls, value: object) -> object:
        return Language.from_code(value) if isinstance(value, str) else value

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

    @classmethod
    def load(cls, path: Path | None = None, *, env_file: Path | None = Path(".env")) -> Self:
        if path is None:
            path = app_data_dir() / "settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("settings.json must contain a JSON object")
        settings_kwargs = {
            name: data[name]
            for name, field in cls.model_fields.items()
            if name in data and not field.exclude
        }
        settings_kwargs["_env_file"] = None if getattr(sys, "frozen", False) else env_file
        return cls(**settings_kwargs)

    def save(self, path: Path | None = None) -> None:
        """Atomically save the current preferences without development-only fields."""
        if path is None:
            path = app_data_dir() / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(self.model_dump_json(indent=2) + "\n")
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

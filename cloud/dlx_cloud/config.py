"""Environment settings for the Cloud Run proxy and its admin CLI."""

from google.cloud import firestore
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from dlx_tray.core.models import AnalysisTier


class CloudSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DLX_CLOUD_", hide_input_in_errors=True)

    token_pepper: SecretStr = Field(default=SecretStr(""), min_length=32, validate_default=False)
    gemini_api_key: SecretStr = SecretStr("")
    firestore_project: str | None = None
    firestore_database: str = "(default)"
    model_standard: str = "gemini-4-hydrogen"
    model_deep: str = "gemini-4-neon"
    cache_ttl_days: int = Field(default=60, ge=1)
    max_body_size: int = Field(default=512 * 1024, ge=1)

    def model_for(self, tier: AnalysisTier) -> str:
        return self.model_deep if tier is AnalysisTier.DEEP else self.model_standard

    def firestore(self) -> firestore.Client:
        return firestore.Client(project=self.firestore_project, database=self.firestore_database)

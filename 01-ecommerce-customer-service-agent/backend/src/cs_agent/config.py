from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CS_", extra="ignore", hide_input_in_errors=True, populate_by_name=True
    )

    database_url: SecretStr | None = None
    query_database_url: SecretStr | None = None
    query_plan_cost_limit: float | None = None
    isolation_file: Path = PROJECT_ROOT / ".runtime/isolation.json"
    expected_database: str = "cs_agent_b0"
    browser_origin: str = "http://127.0.0.1:5173"
    cookie_secure: bool = True
    session_seconds: int = 28800
    expiration_worker_enabled: bool = True
    chat_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("CS_CHAT_API_KEY", "DEEPSEEK_API_KEY")
    )
    sql_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("CS_SQL_API_KEY", "DEEPSEEK_API_KEY")
    )
    model_services_file: Path = PROJECT_ROOT / ".runtime/model-services.json"
    embedding_api_key: SecretStr | None = None
    rerank_api_key: SecretStr | None = None
    agent_worker_enabled: bool = True
    operations_api_key: SecretStr | None = None
    operations_store_id: str | None = None
    activity_ops_base_url: str | None = None
    activity_service_token: SecretStr | None = None
    activity_service_stores: list[str] = Field(default_factory=list)
    activity_snapshot_inputs_file: Path | None = None
    activity_delivery_worker_enabled: bool = False
    integration_run_id: str | None = None
    loaded_source_digest: str | None = None

    @field_validator("session_seconds")
    @classmethod
    def valid_ttl(cls, value):
        if not 60 <= value <= 86400:
            raise ValueError("invalid session lifetime")
        return value

    @field_validator("browser_origin")
    @classmethod
    def valid_origin(cls, value):
        from urllib.parse import urlsplit

        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.netloc
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError("origin must contain only scheme and authority")
        return value

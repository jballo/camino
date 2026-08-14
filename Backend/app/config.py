from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    database_pool_size: int = Field(default=5, gt=0)
    database_max_overflow: int = Field(default=10, ge=0)
    clerk_wh_key: str
    clerk_secret_key: str
    clerk_jwt_key: str | None = None
    gh_app_id: int
    gh_app_client_id: str
    gh_app_secret: str
    gh_app_private_key: str
    encryption_key: str
    gh_webhook_secret: str
    openai_api_key: str
    agent_model: str = "gpt-4o-mini"
    rate_limit_agent_ask_requests: int = 20
    rate_limit_agent_ask_window_seconds: int = 600
    rate_limit_repository_ingest_requests: int = 2
    rate_limit_repository_ingest_window_seconds: int = 3600
    rate_limit_repository_search_requests: int = 60
    rate_limit_repository_search_window_seconds: int = 60
    rate_limit_journey_create_requests: int = 5
    rate_limit_journey_create_window_seconds: int = 3600
    cors_origins: list[str] = ["http://localhost:3000"]
    model_config = SettingsConfigDict(env_file=".env")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_cors_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


settings = Settings()

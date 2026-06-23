from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
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
    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
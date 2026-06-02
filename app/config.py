"""Application configuration from environment."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings from environment variables."""

    database_url: str = (
        "postgresql+asyncpg://ticketsupport:ticketsupport@db:5432/ticketsupport"
    )
    redis_url: str = "redis://redis:6379"
    summarizer_backend: str = "noop"
    debug: bool = False

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()

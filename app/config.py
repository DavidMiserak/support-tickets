"""Application configuration from environment."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings from environment variables."""

    database_url: str = (
        "postgresql+asyncpg://ticketsupport:ticketsupport@db:5432/ticketsupport"
    )
    redis_url: str = "redis://redis:6379"
    summarizer_backend: str = "noop"
    log_level: str = "INFO"
    debug: bool = False
    db_pool_size: int = 20
    db_max_overflow: int = 10

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()

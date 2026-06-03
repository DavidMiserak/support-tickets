"""Application configuration from environment."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings from environment variables."""

    database_url: str = (
        "postgresql+asyncpg://ticketsupport:ticketsupport@db:5432/ticketsupport"
    )
    redis_url: str = "redis://redis:6379"
    summarizer_backend: str = "noop"
    anthropic_api_key: str | None = None
    classifier_backend: str = "rules"
    classifier_model: str = "valhalla/distilbart-mnli-12-3"
    log_level: str = "INFO"
    debug: bool = False
    db_pool_size: int = 20
    db_max_overflow: int = 10
    rate_limit_create_ticket: str = "20/minute"
    worker_metrics_port: int = 9091

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()

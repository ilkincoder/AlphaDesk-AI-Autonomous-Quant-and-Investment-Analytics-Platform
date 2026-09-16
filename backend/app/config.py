"""Application configuration, read from environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-based configuration.

    Real environment variables take priority; a local `.env` file is used as a
    fallback when the app runs outside Docker.
    """

    # Full SQLAlchemy connection URL, for example:
    # postgresql+psycopg2://user:password@postgres:5432/dbname
    database_url: str

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

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

    # Market-data provider key (Twelve Data). Optional on purpose: no provider client
    # exists yet, and a missing key must never stop /health, /portfolio or
    # /portfolio/valuation from starting. Code that actually calls the provider is
    # responsible for reporting a missing key as its own error.
    #
    # Read from the TWELVE_DATA_API_KEY environment variable. Never log it, and never
    # send it to the frontend.
    twelve_data_api_key: str | None = None

    # SEC EDGAR requires a User-Agent that identifies the application and gives a
    # contact address, for example "AlphaDesk AI (contact: you@example.com)".
    # Optional for the same reason as the key above.
    #
    # Read from the SEC_USER_AGENT environment variable.
    sec_user_agent: str | None = None

    # --- Search index ---------------------------------------------------------------
    #
    # None of these is a secret, and none of them is touched at import time. The Qdrant
    # client and the embedding model are both created lazily on first use, so the API
    # starts and serves every existing endpoint whether or not either one is available.

    # Where Qdrant is. Inside Compose that is the service name; from the Mac it is
    # localhost:6333, which the service publishes bound to 127.0.0.1 only.
    qdrant_url: str = "http://qdrant:6333"

    # The collection holding the filing passages. One collection, filtered by company.
    qdrant_collection: str = "sec_filings"

    # Where FastEmbed keeps the model it downloads. This MUST point at a mounted volume.
    # FastEmbed's own default is `{tempdir}/fastembed_cache` -- /tmp -- which is neither
    # shared with the host nor preserved when the container is recreated, so every
    # `docker compose up --build` would re-download the model.
    fastembed_cache_path: str = "/models/fastembed"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

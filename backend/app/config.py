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

    # The collection holding news passages. A *second* collection rather than a filter
    # inside the first: news and filings are different corpora with different lifetimes,
    # and keeping them apart means either one can be dropped and rebuilt without touching
    # the other. Both are written with the same embedding model and the same chunker.
    qdrant_news_collection: str = "news"

    # Where FastEmbed keeps the model it downloads. This MUST point at a mounted volume.
    # FastEmbed's own default is `{tempdir}/fastembed_cache` -- /tmp -- which is neither
    # shared with the host nor preserved when the container is recreated, so every
    # `docker compose up --build` would re-download the model.
    fastembed_cache_path: str = "/models/fastembed"

    # --- Alpaca paper trading (Module 2) ------------------------------------------------
    #
    # The paper account AlphaDesk synchronises its portfolio from. Both credentials are
    # optional for the same reason as the keys above: the API must start and serve every
    # existing endpoint whether or not they are present. Nothing here is read at import
    # time -- the sync endpoint constructs the client when it is actually called, and a
    # missing credential is that call's error to report.
    #
    # Read from ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY. Never log either, never send
    # either to the frontend, and never place one in a URL: they travel as request headers.
    alpaca_api_key_id: str | None = None
    alpaca_api_secret_key: str | None = None

    # The paper trading host, and the ONLY host this integration will talk to. The client
    # refused anything else -- see `app/alpaca.py` -- so pointing this at the live account
    # is a startup-visible error rather than a set of real orders.
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # --- Module 1 agent -----------------------------------------------------------------
    #
    # DeepSeek, spoken to over its OpenAI-compatible chat-completions endpoint. The API key
    # is optional for the same reason the Twelve Data key is: the API must start and serve
    # every existing endpoint whether or not a key is present. Nothing here is read at
    # import time -- the client is constructed inside the analysis run, by the code that
    # actually needs it, and a missing key is that code's error to report.
    #
    # Read from DEEPSEEK_API_KEY. Never log it, never send it to the frontend, and never
    # put it in a prompt.
    deepseek_api_key: str | None = None

    # The model and endpoint are settings rather than constants so a future model change is
    # a configuration change, not a code change. Both defaults are the current documented
    # values.
    deepseek_model: str = "deepseek-flash"
    deepseek_base_url: str = "https://api.deepseek.com"

    # Seconds. A ceiling on one provider request, so an unreachable or stalled endpoint
    # yields an error rather than a command that never returns. The run's own deadline is
    # enforced separately, across all its requests.
    deepseek_timeout_seconds: float = 60.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

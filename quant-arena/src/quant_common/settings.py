"""Environment-driven settings. Every service loads these via `get_settings()`."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # Safety flags
    trading_mode: Literal["paper", "live"] = "paper"
    live_trading_enabled: bool = False
    live_trading_confirmation: str = ""
    kill_switch: bool = False

    # IBKR
    ib_host: str = "ib-gateway"
    ib_port: int = 4004
    ib_client_id: int = 101
    ib_expected_account_prefix: str = "DU"

    # Postgres (app role — never the admin role)
    pg_host: str = "host.docker.internal"
    pg_port: int = 5432
    quant_db_name: str = "quant_trading"
    quant_db_user: str = "quant_app"
    quant_db_password: SecretStr = SecretStr("")

    # LiteLLM / Qdrant
    litellm_base_url: str = "http://host.docker.internal:4000"
    litellm_api_key: SecretStr = SecretStr("")
    litellm_model_research: str = "research-default"
    qdrant_url: str = "http://host.docker.internal:6333"
    qdrant_api_key: SecretStr = SecretStr("")
    qdrant_collection_prefix: str = "quant_"
    qdrant_vector_size: int = Field(1024, gt=0)

    # Alerts
    discord_webhook_url: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    risk_rules_path: str = "config/risk_rules.yaml"

    def pg_conninfo(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.quant_db_name} "
            f"user={self.quant_db_user} password={self.quant_db_password.get_secret_value()} "
            "connect_timeout=5 application_name=quant-arena"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

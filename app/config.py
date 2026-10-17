"""Central configuration. All settings come from .env (see .env.example)."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Neo4j Aura
    neo4j_uri: str = ""
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    # ai& OpenAI-compatible endpoint
    aiand_base_url: str = ""
    aiand_api_key: str = ""

    # Per-task model ids (confirmed against GET /models by scripts/check_models.py)
    parse_model: str = "moonshotai/kimi-k3"
    plan_model: str = "deepseek-ai/deepseek-v4-pro"
    msg_model: str = "deepseek-ai/deepseek-v4.1-flash"
    report_model: str = "deepseek-ai/deepseek-v4-pro"
    resident_model: str = "qwen/qwen3.8-27b"  # plain-language summary in the resident's language

    scenario_seed: int = 42
    llm_mode: Literal["live", "off"] = "live"
    llm_timeout_s: float = 18.0
    llm_max_concurrency: int = 8

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def neo4j_configured(self) -> bool:
        return bool(self.neo4j_uri and self.neo4j_password)

    @property
    def llm_configured(self) -> bool:
        return bool(self.aiand_base_url and self.aiand_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()

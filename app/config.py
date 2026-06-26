"""
config.py — Application settings loaded from environment variables.

Supports multiple GROQ API keys as comma-separated values:
  GROQ_API_KEY=key1,key2,key3

The Groq client uses a round-robin pool with automatic fallback.
"""
import os
from functools import lru_cache
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()


class Settings(BaseSettings):
    # ── Groq ──────────────────────────────────────────────────────────────────
    groq_api_key: str = ""                      # comma-separated keys
    groq_model: str = "llama-3.3-70b-versatile"
    groq_temperature: float = 0.1
    groq_max_tokens: int = 1000
    groq_max_retries: int = 3                   # retries per key
    groq_timeout_budget_seconds: float = 20.0   # hard wall for entire call

    # ── Safety ────────────────────────────────────────────────────────────────
    complaint_max_length: int = 10_000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def groq_api_keys(self) -> list[str]:
        """Parse comma-separated keys into a list, stripping whitespace."""
        if not self.groq_api_key:
            return []
        return [k.strip() for k in self.groq_api_key.split(",") if k.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

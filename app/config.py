"""Configuration : les clés ne viennent jamais du code, seulement de l'environnement."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


@dataclass
class Settings:
    mistral_api_key: str = field(default_factory=lambda: _env("MISTRAL_API_KEY"))
    mistral_model: str = field(default_factory=lambda: _env("MISTRAL_MODEL", "mistral-medium-latest"))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-sonnet-5-5"))
    google_factcheck_api_key: str = field(default_factory=lambda: _env("GOOGLE_FACTCHECK_API_KEY"))
    serpapi_api_key: str = field(default_factory=lambda: _env("SERPAPI_API_KEY"))
    ai_detector_url: str = field(default_factory=lambda: _env("AI_DETECTOR_URL"))
    ai_detector_token: str = field(default_factory=lambda: _env("AI_DETECTOR_TOKEN"))
    db_path: str = field(default_factory=lambda: _env("CHECKCA_DB", "checkca.sqlite3"))
    rate_limit_per_hour: int = field(default_factory=lambda: int(_env("RATE_LIMIT_PER_HOUR", "20") or 20))
    max_analyses_per_day: int = field(default_factory=lambda: int(_env("MAX_ANALYSES_PER_DAY", "200") or 200))


settings = Settings()

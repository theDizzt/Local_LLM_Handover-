from dataclasses import dataclass
from os import getenv
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str
    app_env: str
    database_path: Path
    llm_provider: str
    llm_endpoint: str
    llm_timeout_seconds: int
    vector_store: str
    vector_store_path: Path


def load_settings() -> Settings:
    return Settings(
        app_name=getenv("APP_NAME", "Local LLM Handover"),
        app_env=getenv("APP_ENV", "development"),
        database_path=Path(getenv("DATABASE_PATH", "data/handover.db")),
        llm_provider=getenv("LLM_PROVIDER", "ollama"),
        llm_endpoint=getenv("LLM_ENDPOINT", "http://127.0.0.1:11434"),
        llm_timeout_seconds=int(getenv("LLM_TIMEOUT_SECONDS", "60")),
        vector_store=getenv("VECTOR_STORE", "chroma"),
        vector_store_path=Path(getenv("VECTOR_STORE_PATH", "data/chroma")),
    )

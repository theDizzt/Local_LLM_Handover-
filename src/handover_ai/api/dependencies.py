from functools import lru_cache

from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.config import Settings, load_settings


@lru_cache
def get_settings() -> Settings:
    return load_settings()


@lru_cache
def get_database() -> SQLiteDatabase:
    return SQLiteDatabase(get_settings().database_path)

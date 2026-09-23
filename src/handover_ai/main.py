from contextlib import asynccontextmanager

from fastapi import FastAPI

from handover_ai import __version__
from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.api.dependencies import get_database, get_settings
from handover_ai.api.routes import evaluations, health
from handover_ai.config import Settings


def create_app(
    settings: Settings | None = None,
    database: SQLiteDatabase | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    app_database = database or get_database()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app_database.initialize()
        yield

    application = FastAPI(
        title=app_settings.app_name,
        version=__version__,
        lifespan=lifespan,
    )
    application.include_router(health.router, prefix="/api/v1")
    application.include_router(evaluations.router, prefix="/api/v1")
    return application


app = create_app()

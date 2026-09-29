from contextlib import asynccontextmanager

from fastapi import FastAPI

from handover_ai import __version__
from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.api.dependencies import get_database, get_settings
from handover_ai.api.routes import documents, evaluations, health
from handover_ai.config import Settings


def create_app(
    settings: Settings | None = None,
    database: SQLiteDatabase | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    app_database = database or SQLiteDatabase(app_settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app_database.initialize()
        yield

    application = FastAPI(
        title=app_settings.app_name,
        version=__version__,
        lifespan=lifespan,
    )
    # 시작 시 초기화한 DB와 요청 처리에 주입되는 DB가 반드시 같아야 한다.
    # 별도 설정을 전달한 테스트/서버 인스턴스도 전역 기본 DB를 사용하지 않는다.
    application.dependency_overrides[get_settings] = lambda: app_settings
    application.dependency_overrides[get_database] = lambda: app_database
    application.include_router(health.router, prefix="/api/v1")
    application.include_router(evaluations.router, prefix="/api/v1")
    application.include_router(documents.router, prefix="/api/v1")
    return application


app = create_app()

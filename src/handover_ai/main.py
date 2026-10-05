# [읽기 안내] 서버의 진입점이다. uvicorn이 아래의 app 객체를 불러온다.
# create_app()은 설정·DB를 받아 독립된 앱을 만드는 팩토리이며 테스트에서도 재사용한다.
# 요청 흐름: router → Depends로 서비스 조립 → service → domain/port → adapter.
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from handover_ai import __version__
from handover_ai.adapters.draft_repository import SQLiteDraftRepository
from handover_ai.adapters.ocr_repository import SQLiteOcrRepository
from handover_ai.adapters.search_repository import SQLiteSearchRepository
from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.api.dependencies import get_database, get_settings
from handover_ai.api.routes import (
    chunks,
    documents,
    drafts,
    evaluations,
    health,
    layout,
    layout_review,
    ocr,
    search,
)
from handover_ai.config import Settings


def create_app(
    settings: Settings | None = None,
    database: SQLiteDatabase | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    app_database = database or SQLiteDatabase(app_settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # yield 앞은 서버 시작 시 한 번 실행된다. 뒤에는 서버 종료 시 정리할 코드를 둔다.
        # 테이블을 먼저 준비한 다음 이전 서버에서 끝내지 못한 OCR 작업을 복구한다.
        app_database.initialize()
        SQLiteOcrRepository(app_database).recover_interrupted()
        SQLiteSearchRepository(app_database).recover_interrupted()
        SQLiteDraftRepository(app_database).recover_interrupted()
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
    # 각 라우터는 자신의 경로만 정의하며 여기서 공통 API 버전 경로를 붙인다.
    application.include_router(evaluations.router, prefix="/api/v1")
    application.include_router(documents.router, prefix="/api/v1")
    application.include_router(ocr.router, prefix="/api/v1")
    application.include_router(chunks.router, prefix="/api/v1")
    application.include_router(layout.router, prefix="/api/v1")
    application.include_router(layout_review.router, prefix="/api/v1")
    application.include_router(search.router, prefix="/api/v1")
    application.include_router(drafts.router, prefix="/api/v1")
    # 첫 검토 화면은 별도 Node 서버 없이 기존 실행 명령 하나로 제공한다.
    # 정적 파일만 /static에 공개한다. PDF·DB·모델 캐시가 있는 data는 노출하지 않는다.
    # __file__ 기준 경로는 실행 디렉터리가 달라도 설치된 패키지에서 파일을 찾는다.
    web_root = Path(__file__).parent / "web"
    application.mount("/static", StaticFiles(directory=web_root), name="static")

    @application.get("/", include_in_schema=False)
    def review_page():
        return FileResponse(web_root / "index.html", headers={"Cache-Control": "no-cache"})

    return application


app = create_app()

# [읽기 안내] 의존성 주입은 '어떤 구현 객체를 사용할지' 한곳에서 결정하는 방식이다.
# FastAPI Depends는 요청 처리 전에 필요한 설정/DB/서비스를 준비해 함수 인자로 넣는다.
# 테스트는 dependency_overrides로 실제 OCR 대신 fixture 엔진을 연결할 수 있다.
from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from handover_ai.adapters.chroma import ChromaVectors
from handover_ai.adapters.chunk_repository import SQLiteChunkRepository
from handover_ai.adapters.document_repository import SQLiteDocumentRepository
from handover_ai.adapters.draft_repository import SQLiteDraftRepository
from handover_ai.adapters.embedding import LocalE5Embedder
from handover_ai.adapters.layout import GeometryLayoutAnalyzer
from handover_ai.adapters.ocr import PaddleOcrEngine, PyMuPdfRenderer
from handover_ai.adapters.ocr_repository import SQLiteOcrRepository
from handover_ai.adapters.ollama import OllamaDraftGateway
from handover_ai.adapters.pdf import PyMuPdfInspector
from handover_ai.adapters.search_cleanup_repository import SQLiteSearchCleanupRepository
from handover_ai.adapters.search_repository import SQLiteSearchRepository
from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.config import Settings, load_settings
from handover_ai.services.chunks import ChunkService
from handover_ai.services.documents import DocumentService
from handover_ai.services.drafts import DraftService
from handover_ai.services.ocr import OcrService
from handover_ai.services.search import SearchService
from handover_ai.services.search_cleanup import SearchCleanupService


@lru_cache
def get_settings() -> Settings:
    # 인자가 없는 캐시 함수이므로 같은 프로세스에서 기본 설정을 한 번만 만든다.
    return load_settings()


@lru_cache
def _search_backend(settings: Settings):
    return LocalE5Embedder(settings.embedding_model_path), ChromaVectors(settings.vector_store_path)


@lru_cache
def get_database() -> SQLiteDatabase:
    # SQLiteDatabase는 연결 자체가 아니라 경로와 연결 생성 방법을 보유한다.
    # 실제 sqlite3.Connection은 작업마다 새로 만들어 스레드 간 공유하지 않는다.
    return SQLiteDatabase(get_settings().database_path)


def get_search_service(
    database: Annotated[SQLiteDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SearchService:
    embedder, vectors = _search_backend(settings)
    return SearchService(SQLiteSearchRepository(database), embedder, vectors)


def get_search_cleanup_service(
    database: Annotated[SQLiteDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SearchCleanupService:
    # 정리는 임베딩 모델 파일 없이도 가능하다. 버전 해시나 추론을 요청하지 않는다.
    _, vectors = _search_backend(settings)
    return SearchCleanupService(SQLiteSearchCleanupRepository(database), vectors)


def get_draft_service(
    database: Annotated[SQLiteDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
    search: Annotated[SearchService, Depends(get_search_service)],
) -> DraftService:
    return DraftService(
        SQLiteDraftRepository(database),
        search,
        OllamaDraftGateway(
            settings.llm_endpoint,
            settings.llm_model,
            settings.llm_timeout_seconds,
            provider=settings.llm_provider,
        ),
        settings.llm_timeout_seconds,
    )


def get_document_service(
    database: Annotated[SQLiteDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DocumentService:
    # 외부 기술 선택은 이 조립 지점에 모은다. 서비스는 포트만 참조하므로
    # 테스트용 Repository나 다른 PDF 검사기로 교체할 수 있다.
    return DocumentService(
        SQLiteDocumentRepository(database),
        PyMuPdfInspector(),
        settings.document_store_path,
        settings.max_upload_bytes,
    )


def get_chunk_service(database: Annotated[SQLiteDatabase, Depends(get_database)]) -> ChunkService:
    # 구조 생성은 저장된 OCR 결과만 읽으므로 Paddle 모델 초기화가 필요 없다.
    return ChunkService(
        SQLiteDocumentRepository(database),
        SQLiteOcrRepository(database),
        SQLiteChunkRepository(database),
    )


def get_layout_service(database: Annotated[SQLiteDatabase, Depends(get_database)]) -> ChunkService:
    # 기본 페이지 방식과 별도 저장소 버전을 사용한다. 같은 OCR 원문에서도 두 결과가
    # 병존하므로 사용자가 비교할 수 있고 기존 생성 문서의 근거를 덮어쓰지 않는다.
    analyzer = GeometryLayoutAnalyzer()
    return ChunkService(
        SQLiteDocumentRepository(database),
        SQLiteOcrRepository(database),
        SQLiteChunkRepository(database, analyzer.version),
        analyzer,
    )


@lru_cache
def _ocr_engine(language: str, detection: str | None, recognition: str | None) -> PaddleOcrEngine:
    # 대형 모델을 요청마다 다시 로드하지 않는다. 실제 로드는 첫 추론 시 수행한다.
    return PaddleOcrEngine(language, detection, recognition)


def get_ocr_service(
    database: Annotated[SQLiteDatabase, Depends(get_database)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> OcrService:
    return OcrService(
        SQLiteDocumentRepository(database),
        SQLiteOcrRepository(database),
        PyMuPdfRenderer(settings.ocr_dpi, settings.ocr_max_pixels),
        _ocr_engine(
            settings.ocr_language,
            settings.ocr_detection_model_dir,
            settings.ocr_recognition_model_dir,
        ),
        settings.page_store_path,
    )

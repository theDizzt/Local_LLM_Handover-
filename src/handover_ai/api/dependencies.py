from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from handover_ai.adapters.document_repository import SQLiteDocumentRepository
from handover_ai.adapters.pdf import PyMuPdfInspector
from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.config import Settings, load_settings
from handover_ai.services.documents import DocumentService


@lru_cache
def get_settings() -> Settings:
    return load_settings()


@lru_cache
def get_database() -> SQLiteDatabase:
    return SQLiteDatabase(get_settings().database_path)


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

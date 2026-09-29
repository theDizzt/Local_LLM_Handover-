"""문서 등록 계약. HTTP나 PDF 라이브러리에 의존하지 않는 공통 데이터다."""

from typing import Literal

from pydantic import BaseModel, Field


class DocumentSummary(BaseModel):
    document_id: str
    file_name: str
    page_count: int = Field(gt=0)
    file_sha256: str
    document_version: int = Field(gt=0)
    ingestion_id: str
    status: Literal["pending", "ready", "failed"]
    created_at: str


class StoredDocument(DocumentSummary):
    # 서버의 로컬 경로는 저장소 내부에서만 사용하고 API 응답에는 포함하지 않는다.
    source_uri: str


class DocumentRegistration(BaseModel):
    document: DocumentSummary
    duplicate: bool


class InvalidDocument(ValueError):
    """사용자가 올린 파일을 등록할 수 없음."""


class DocumentTooLarge(InvalidDocument):
    """설정된 파일 크기 제한 초과."""


class DuplicateDocument(Exception):
    """동시에 등록된 동일 파일을 Repository가 감지했음."""

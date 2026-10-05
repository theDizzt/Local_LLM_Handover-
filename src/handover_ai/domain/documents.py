"""문서 등록 계약. HTTP나 PDF 라이브러리에 의존하지 않는 공통 데이터다."""

from typing import Literal

from pydantic import BaseModel, Field


class DocumentSummary(BaseModel):
    # 화면에 공개하는 메타데이터다. 상태는 현재 연결된 ingestion의 처리 결과를 뜻한다.
    # 최신 OCR 요청의 실행 상태는 별도 OcrJob에서 확인한다. 재처리 중에도 현재
    # 문서는 이전 성공 버전을 유지할 수 있으므로 두 상태를 같은 것으로 보면 안 된다.
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
    # duplicate=True이면 새 문서를 만든 것이 아니라 같은 해시의 기존 문서를 반환했다.
    document: DocumentSummary
    duplicate: bool


class InvalidDocument(ValueError):
    """사용자가 올린 파일을 등록할 수 없음."""


class DocumentTooLarge(InvalidDocument):
    """설정된 파일 크기 제한 초과."""


class DuplicateDocument(Exception):
    """동시에 등록된 동일 파일을 Repository가 감지했음."""

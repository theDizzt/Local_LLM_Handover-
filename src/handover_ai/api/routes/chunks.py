"""문서 구조 생성과 원문 근거 조회 API. 벡터 검색 API와는 구분한다."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from handover_ai.api.dependencies import get_chunk_service
from handover_ai.domain.chunks import (
    BuildStructureRequest,
    ChunkView,
    StructureConflict,
    StructureSummary,
)
from handover_ai.services.chunks import ChunkService

router = APIRouter(prefix="/documents", tags=["structure"])
Service = Annotated[ChunkService, Depends(get_chunk_service)]


@router.post("/{document_id}/structure", response_model=StructureSummary)
def build_structure(document_id: str, request: BuildStructureRequest, service: Service):
    # 외부 모델을 호출하지 않는 동기 작업이다. 재요청은 같은 결과를 반환한다.
    # 반환 200은 SQLite 저장 완료만 의미하며 Chroma 색인 완료를 뜻하지 않는다.
    try:
        return service.build(document_id, request.ingestion_id)
    except KeyError as exc:
        raise HTTPException(404, "문서를 찾을 수 없습니다.") from exc
    except StructureConflict as exc:
        raise HTTPException(409, str(exc)) from exc


def resolve_ingestion(service: ChunkService, document_id: str, ingestion_id: str | None) -> str:
    document = service.documents.get_document(document_id)
    if document is None:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    return ingestion_id or document.ingestion_id


@router.get("/{document_id}/structure", response_model=StructureSummary | None)
def get_structure(document_id: str, service: Service, ingestion_id: str | None = None):
    version = resolve_ingestion(service, document_id, ingestion_id)
    return service.chunks.get_structure(document_id, version)


@router.get("/{document_id}/chunks", response_model=list[ChunkView])
def list_chunks(
    document_id: str,
    service: Service,
    ingestion_id: str | None = None,
    page_number: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    version = resolve_ingestion(service, document_id, ingestion_id)
    return service.chunks.list_chunks(document_id, version, page_number, limit, offset)


@router.get("/{document_id}/chunks/{chunk_id}", response_model=ChunkView)
def get_chunk(document_id: str, chunk_id: str, service: Service):
    # document_id도 함께 검사하여 다른 문서의 Chunk를 잘못 연결하지 않는다.
    result = service.chunks.get_chunk(document_id, chunk_id)
    if result is None:
        raise HTTPException(404, "Chunk 근거를 찾을 수 없습니다.")
    return result

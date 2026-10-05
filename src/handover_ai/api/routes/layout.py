"""규칙 기반 배치 분석 API. 기존 /structure의 기본 동작은 유지한다."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from handover_ai.api.dependencies import get_layout_service
from handover_ai.api.routes.chunks import resolve_ingestion
from handover_ai.domain.chunks import (
    BuildStructureRequest,
    ChunkView,
    StructureConflict,
    StructureSummary,
)
from handover_ai.domain.layout import PageLayout
from handover_ai.services.chunks import ChunkService

router = APIRouter(prefix="/documents", tags=["layout"])
Service = Annotated[ChunkService, Depends(get_layout_service)]


@router.post("/{document_id}/layout", response_model=StructureSummary)
def build_layout(document_id: str, request: BuildStructureRequest, service: Service):
    try:
        return service.build(document_id, request.ingestion_id)
    except KeyError as exc:
        raise HTTPException(404, "문서를 찾을 수 없습니다.") from exc
    except StructureConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{document_id}/layout", response_model=StructureSummary | None)
def get_layout(document_id: str, service: Service, ingestion_id: str | None = None):
    return service.chunks.get_structure(
        document_id, resolve_ingestion(service, document_id, ingestion_id)
    )


@router.get("/{document_id}/layout/pages", response_model=list[PageLayout])
def get_layout_pages(
    document_id: str,
    service: Service,
    ingestion_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    version = resolve_ingestion(service, document_id, ingestion_id)
    return service.chunks.list_layouts(document_id, version, limit, offset)


@router.get("/{document_id}/layout/chunks", response_model=list[ChunkView])
def get_layout_chunks(
    document_id: str,
    service: Service,
    ingestion_id: str | None = None,
    page_number: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    version = resolve_ingestion(service, document_id, ingestion_id)
    return service.chunks.list_chunks(document_id, version, page_number, limit, offset)

"""구조 ID를 명시하여 페이지 순서와 배치 분석 결과가 섞이지 않게 한다."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from handover_ai.api.dependencies import get_search_cleanup_service, get_search_service
from handover_ai.domain.search import (
    CleanupRequest,
    IndexRequest,
    SearchConflict,
    SearchRequest,
    SearchUnavailable,
)
from handover_ai.services.search import SearchService
from handover_ai.services.search_cleanup import SearchCleanupService

router = APIRouter(tags=["search"])
Service = Annotated[SearchService, Depends(get_search_service)]
Cleanup = Annotated[SearchCleanupService, Depends(get_search_cleanup_service)]


@router.get("/documents/{document_id}/search-index/cleanup")
def cleanup_candidates(
    document_id: str,
    service: Cleanup,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return call(lambda: service.repository.candidates(document_id, limit, offset))


@router.post("/documents/{document_id}/search-index/cleanup")
def cleanup(document_id: str, body: CleanupRequest, service: Cleanup):
    return call(lambda: service.execute(document_id, body.run_ids))


def call(action):
    try:
        return action()
    except KeyError as exc:
        raise HTTPException(404, "문서를 찾을 수 없습니다.") from exc
    except SearchConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except SearchUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/documents/{document_id}/search-index", status_code=202)
def build(document_id: str, body: IndexRequest, tasks: BackgroundTasks, service: Service):
    run = call(lambda: service.enqueue(document_id, body.structure_id))
    tasks.add_task(service.build, run)
    return run


@router.get("/documents/{document_id}/search-index")
def state(
    document_id: str, service: Service, structure_id: str = Query(min_length=1, max_length=100)
):
    return call(lambda: service.state(document_id, structure_id))


@router.post("/documents/{document_id}/search")
def search(document_id: str, body: SearchRequest, service: Service):
    return call(lambda: service.search(document_id, body))

"""초안 생성 요청은 즉시 작업 ID를 반환하고, 결과/이력은 별도 조회한다."""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from handover_ai.api.dependencies import get_draft_service
from handover_ai.api.routes.search import call
from handover_ai.domain.drafts import DraftRequest
from handover_ai.services.drafts import DraftService

router = APIRouter(prefix="/documents/{document_id}/drafts", tags=["drafts"])
Service = Annotated[DraftService, Depends(get_draft_service)]


@router.post("", status_code=202)
def create(document_id: str, body: DraftRequest, tasks: BackgroundTasks, service: Service):
    job = call(lambda: service.enqueue(document_id, body))
    tasks.add_task(service.generate, job)
    return job


@router.get("")
def history(
    document_id: str,
    service: Service,
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
):
    return call(lambda: service.repository.list(document_id, limit, offset))


@router.get("/{draft_id}")
def detail(document_id: str, draft_id: str, service: Service):
    return call(lambda: service.repository.get(document_id, draft_id))

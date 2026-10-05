"""검토 저장 API. 과거 이력 조회는 허용하고 갱신은 현재 OCR 버전으로 제한한다."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from handover_ai.adapters.layout_review_repository import SQLiteLayoutReviewRepository
from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.api.dependencies import get_database
from handover_ai.domain.chunks import StructureConflict
from handover_ai.domain.layout_review import PageReview, ReviewRequest, ReviewSummary

router = APIRouter(prefix="/documents/{document_id}/layout/{structure_id}", tags=["layout-review"])


def repository(database: Annotated[SQLiteDatabase, Depends(get_database)]):
    return SQLiteLayoutReviewRepository(database)


Repository = Annotated[SQLiteLayoutReviewRepository, Depends(repository)]


def call(action):
    try:
        return action()
    except KeyError as exc:
        raise HTTPException(404, "배치 분석 페이지를 찾을 수 없습니다.") from exc
    except StructureConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/reviews", response_model=ReviewSummary)
def summary(document_id: str, structure_id: str, repo: Repository):
    return call(lambda: repo.summary(document_id, structure_id))


@router.get("/pages/{page_id}/review", response_model=PageReview)
def get(document_id: str, structure_id: str, page_id: str, repo: Repository):
    return call(lambda: repo.get(document_id, structure_id, page_id))


@router.put("/pages/{page_id}/review", response_model=PageReview)
def save(document_id: str, structure_id: str, page_id: str, body: ReviewRequest, repo: Repository):
    return call(lambda: repo.save(document_id, structure_id, page_id, body))


@router.get("/pages/{page_id}/review/history", response_model=list[PageReview])
def history(
    document_id: str,
    structure_id: str,
    page_id: str,
    repo: Repository,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return call(lambda: repo.history(document_id, structure_id, page_id, limit, offset))

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile

from handover_ai.api.dependencies import get_document_service
from handover_ai.domain.documents import (
    DocumentRegistration,
    DocumentSummary,
    DocumentTooLarge,
    InvalidDocument,
)
from handover_ai.services.documents import DocumentService

router = APIRouter(prefix="/documents", tags=["documents"])
ServiceDependency = Annotated[DocumentService, Depends(get_document_service)]


@router.post(
    "",
    response_model=DocumentRegistration,
    status_code=201,
    responses={200: {"model": DocumentRegistration}, 413: {}, 422: {}},
)
def register_document(file: UploadFile, response: Response, service: ServiceDependency):
    """multipart/form-data의 file 필드로 PDF를 등록한다. 동일 내용이면 기존 문서를 반환한다."""
    # 동기 라우트는 FastAPI의 작업 스레드에서 실행된다. 디스크/PDF 처리가
    # 이벤트 루프를 막지 않도록 이 단계에서는 async 함수로 선언하지 않는다.
    try:
        result = service.register(file.filename, file.file)
    except DocumentTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except InvalidDocument as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        file.file.close()
    response.status_code = 200 if result.duplicate else 201
    response.headers["Location"] = f"/api/v1/documents/{result.document.document_id}"
    return result


@router.get("", response_model=list[DocumentSummary])
def list_documents(
    service: ServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """최신 등록순으로 조회한다. limit/offset으로 목록을 나누어 가져온다."""
    return service.repository.list_documents(limit, offset)


@router.get("/{document_id}", response_model=DocumentSummary)
def get_document(document_id: str, service: ServiceDependency):
    document = service.repository.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
    return document

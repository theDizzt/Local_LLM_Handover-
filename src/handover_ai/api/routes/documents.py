# [읽기 안내] HTTP와 문서 등록 유스케이스를 연결하는 얇은 경계다.
# 파일 저장·중복 판별은 service로 위임하고 여기서는 입력, 상태 코드, 응답만 다룬다.
# response_model은 응답 형식을 검사하고 내부 전용 필드(source_uri)를 제외한다.
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
    # Location은 클라이언트가 등록 직후 상세 조회를 할 수 있도록 알려주는 표준 헤더다.
    response.headers["Location"] = f"/api/v1/documents/{result.document.document_id}"
    return result


@router.get("", response_model=list[DocumentSummary])
def list_documents(
    service: ServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """최신 등록순으로 조회한다. limit/offset으로 목록을 나누어 가져온다."""
    # Query의 ge/le 제약을 어기면 FastAPI가 함수 실행 전에 422 응답을 만든다.
    return service.repository.list_documents(limit, offset)


@router.get("/{document_id}", response_model=DocumentSummary)
def get_document(document_id: str, service: ServiceDependency):
    # 저장소의 None은 정상적인 '없음' 결과이고, HTTP 경계에서만 404로 변환한다.
    document = service.repository.get_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
    return document

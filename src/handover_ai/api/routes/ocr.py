# [읽기 안내] OCR은 오래 걸리므로 시작 API(202)와 상태 조회 API(200)를 나눈다.
# 클라이언트는 Location의 작업 URL을 반복 조회해 completed/failed가 될 때까지 확인한다.
# 처리 완료 이후 pages에서 텍스트/좌표를 읽고 image API로 원문 이미지를 표시한다.
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse

from handover_ai.api.dependencies import get_ocr_service
from handover_ai.domain.ocr import OcrConflict, OcrJob, OcrPage, OcrUnavailable
from handover_ai.services.ocr import OcrService

router = APIRouter(tags=["ocr"])
Service = Annotated[OcrService, Depends(get_ocr_service)]


@router.post("/documents/{document_id}/ocr", response_model=OcrJob, status_code=202)
def start_ocr(document_id: str, tasks: BackgroundTasks, response: Response, service: Service):
    """작업 ID를 즉시 반환한다. 같은 API를 다시 호출하면 새 처리 버전으로 재실행한다."""
    try:
        job = service.enqueue(document_id)
    except KeyError as exc:
        raise HTTPException(404, "문서를 찾을 수 없습니다.") from exc
    except OcrConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except OcrUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    # MVP는 단일 프로세스 BackgroundTasks를 사용한다. 작업 상태는 DB에 저장하며
    # 서버 중단 시 자동 재실행하지 않고 재시작 시 failed/interrupted로 표시한다.
    tasks.add_task(service.run, job.job_id)
    response.headers["Location"] = f"/api/v1/ocr-jobs/{job.job_id}"
    return job


@router.get("/ocr-jobs/{job_id}", response_model=OcrJob)
def get_job(job_id: str, service: Service):
    job = service.repository.get_job(job_id)
    if job is None:
        raise HTTPException(404, "OCR 작업을 찾을 수 없습니다.")
    return job


@router.get("/documents/{document_id}/ocr/latest", response_model=OcrJob | None)
def get_latest_job(document_id: str, service: Service):
    """첫 실행 전에는 null, 실행 이력이 있으면 마지막 작업을 반환한다."""
    # 문서의 ready는 마지막 성공 결과, 이 API는 최신 실행 상태다. 재처리 실패 때
    # ready 문서와 failed 작업이 함께 존재하는 것은 정상이며 UI도 둘을 나누어 표시한다.
    if service.documents.get_document(document_id) is None:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    return service.repository.latest_job(document_id)


@router.get("/documents/{document_id}/pages", response_model=list[OcrPage])
def get_pages(
    document_id: str,
    service: Service,
    ingestion_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    document = service.documents.get_document(document_id)
    if document is None:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    # 기본값은 마지막 성공 버전이다. 과거 버전은 ingestion_id로 명시해 조회한다.
    return service.repository.pages(
        document_id, ingestion_id or document.ingestion_id, limit, offset
    )


@router.get("/documents/{document_id}/pages/{page_id}/image")
def get_page_image(document_id: str, page_id: str, service: Service):
    path = service.repository.image_path(document_id, page_id)
    if path is None or not path.resolve().is_relative_to(service.image_root) or not path.is_file():
        raise HTTPException(404, "페이지 이미지를 찾을 수 없습니다.")
    return FileResponse(path, media_type="image/png")

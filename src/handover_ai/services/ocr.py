# [읽기 안내] OCR 작업의 진행 순서를 조정한다. HTTP나 구체적인 OCR 라이브러리는 모른다.
# enqueue는 작업만 저장하고 run은 BackgroundTasks가 나중에 호출한다.
# 한 페이지 저장이 끝날 때마다 진행률을 갱신하고 전체 성공 후에만 현재 버전을 교체한다.
import logging
from pathlib import Path

from handover_ai.domain.ocr import OcrJob, OcrUnavailable
from handover_ai.ports.ocr import OcrEngine, OcrRepository, PageRenderer
from handover_ai.ports.repositories import DocumentRepository

logger = logging.getLogger(__name__)


class OcrService:
    def __init__(
        self,
        documents: DocumentRepository,
        repository: OcrRepository,
        renderer: PageRenderer,
        engine: OcrEngine,
        image_root: Path,
    ):
        self.documents = documents
        self.repository = repository
        self.renderer = renderer
        self.engine = engine
        self.image_root = image_root.resolve()

    def enqueue(self, document_id: str) -> OcrJob:
        # 엔진 버전 확인이 실패하면 작업을 만들지 않아 실행 불가능한 queued가 남지 않는다.
        if self.documents.get_document(document_id) is None:
            raise KeyError(document_id)
        return self.repository.create_job(document_id, self.engine.version, self.renderer.version)

    def run(self, job_id: str) -> None:
        # start는 queued인 작업만 running으로 원자적으로 바꾼다. 중복 콜백은 무시한다.
        job = self.repository.get_job(job_id)
        if job is None or not self.repository.start(job_id):
            return
        try:
            document = self.documents.get_document(job.document_id)
            if document is None:
                raise ValueError("원본 문서를 찾을 수 없습니다.")
            destination = self.image_root / job.ingestion_id
            # 재시도별로 폴더를 나누어 기존 페이지 이미지/근거를 덮어쓰지 않는다.
            for page in self.renderer.render(Path(document.source_uri), destination):
                lines = self.engine.recognize(page)
                self.repository.save_page(job, page, lines)
            self.repository.complete(job)
        except Exception as exc:
            # 예외 원문에는 로컬 경로/문서 내용이 들어갈 수 있으므로 공개 상태와
            # 로그에는 분류 코드와 예외 타입만 기록한다. 실패 버전은 조회에서 제외한다.
            code = "ocr_unavailable" if isinstance(exc, OcrUnavailable) else "processing_failed"
            self.repository.fail(job, code)
            logger.warning("OCR job %s failed: %s", job_id, type(exc).__name__)

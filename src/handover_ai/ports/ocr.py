# [읽기 안내] OCR 처리를 세 계약으로 분리한다: PDF→이미지, 이미지→텍스트, 결과→DB.
# Iterator는 한 페이지씩 전달해 문서 전체의 이미지를 메모리에 쌓지 않는 계약이다.
# 서비스 테스트에서는 실제 렌더러/SQLite를 두고 OCR 엔진만 fixture로 교체한다.
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from handover_ai.domain.ocr import OcrJob, OcrLine, OcrPage, RenderedPage


class PageRenderer(Protocol):
    version: str

    def render(self, source: Path, destination: Path) -> Iterator[RenderedPage]: ...


class OcrEngine(Protocol):
    @property
    def version(self) -> str: ...

    def recognize(self, page: RenderedPage) -> list[OcrLine]: ...


class OcrRepository(Protocol):
    def create_job(self, document_id: str, engine: str, renderer: str) -> OcrJob: ...

    def get_job(self, job_id: str) -> OcrJob | None: ...

    # 브라우저 저장 정보 없이도 문서 상세 진입 시 마지막 요청의 상태를 복원한다.
    def latest_job(self, document_id: str) -> OcrJob | None: ...

    def start(self, job_id: str) -> bool: ...

    def save_page(self, job: OcrJob, page: RenderedPage, lines: list[OcrLine]) -> None: ...

    def complete(self, job: OcrJob) -> None: ...

    def fail(self, job: OcrJob, error_code: str) -> None: ...

    def pages(
        self, document_id: str, ingestion_id: str, limit: int, offset: int
    ) -> list[OcrPage]: ...

    def image_path(self, document_id: str, page_id: str) -> Path | None: ...

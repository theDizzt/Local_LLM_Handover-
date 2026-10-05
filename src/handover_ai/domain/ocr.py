"""OCR 기술과 무관한 좌표·페이지·작업 계약."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class OcrLine(BaseModel):
    # bbox 순서는 왼쪽/위/오른쪽/아래다. 예: (0.1, 0.2, 0.8, 0.3)은
    # 이미지 너비의 10~80%, 높이의 20~30% 영역을 뜻하므로 화면 크기와 무관하다.
    text: str = Field(min_length=1)
    bbox: tuple[float, float, float, float]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_box(self):
        x0, y0, x1, y1 = self.bbox
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError("bbox는 저장 이미지 기준 0~1 좌표이며 양의 면적이어야 합니다.")
        return self


class RenderedPage(BaseModel):
    # 렌더러가 저장한 파일과 실제 픽셀 크기다. API가 아닌 어댑터↔서비스 사이에서 사용한다.
    number: int = Field(ge=1)
    image_path: Path
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    dpi: int = Field(gt=0)
    rotation: int


class OcrJob(BaseModel):
    # queued → running → completed 또는 failed로 진행한다.
    # completed_pages는 DB에 저장된 페이지 수이며 실패 시 부분 진행 상황도 확인할 수 있다.
    job_id: str
    document_id: str
    ingestion_id: str
    status: Literal["queued", "running", "completed", "failed"]
    completed_pages: int
    total_pages: int
    error_code: str | None
    created_at: str
    finished_at: str | None


class OcrBlock(OcrLine):
    # DB 저장 후 생긴 block_id는 향후 Chunk/근거가 참조하는 안정적인 식별자다.
    block_id: str
    reading_order: int


class OcrPage(BaseModel):
    # 화면용 응답이다. 내부 image_path 대신 page_id로 이미지 API를 호출한다.
    page_id: str
    ingestion_id: str
    pdf_page_number: int
    width_px: int
    height_px: int
    render_dpi: int
    rotation: int
    blocks: list[OcrBlock]


class OcrUnavailable(RuntimeError):
    """선택 의존성 또는 OCR 모델을 사용할 수 없음."""


class OcrConflict(RuntimeError):
    """같은 문서의 처리가 이미 진행 중."""

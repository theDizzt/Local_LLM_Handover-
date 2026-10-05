"""배치 분석의 기술 중립 계약. 원문 행을 해석하되 텍스트/좌표 자체는 수정하지 않는다."""

from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field, computed_field

from handover_ai.domain.chunks import ChunkDraft, page_chunks, stable_id
from handover_ai.domain.ocr import OcrBlock, OcrPage

LAYOUT_VERSION = "geometry-layout-v1:chars=800"
RegionKind = Literal["heading", "paragraph", "table_candidate", "unknown"]


def enclosing_box(blocks: list[OcrBlock]) -> tuple[float, float, float, float]:
    return (
        min(b.bbox[0] for b in blocks),
        min(b.bbox[1] for b in blocks),
        max(b.bbox[2] for b in blocks),
        max(b.bbox[3] for b in blocks),
    )


class LayoutRegion(BaseModel):
    kind: RegionKind
    block_ids: list[str] = Field(min_length=1)
    bbox: tuple[float, float, float, float]
    column: int | None = None
    # 영역 분류는 확정된 의미가 아니라 제안이다. 표의 행은 묶되 셀 병합을 추정하지 않는다.
    review_reasons: list[str] = Field(default_factory=list)


class PageLayout(BaseModel):
    page_id: str
    ingestion_id: str
    pdf_page_number: int
    parser_version: str = LAYOUT_VERSION
    regions: list[LayoutRegion]
    warnings: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def needs_review(self) -> bool:
        return bool(self.warnings or any(region.review_reasons for region in self.regions))

    def validate_sources(self, page: OcrPage) -> None:
        # 어댑터를 교체해도 빠진 행/중복 행/다른 페이지 근거를 저장해서는 안 된다.
        # 공백 행만 제외하고 모든 OCR 행이 정확히 한 영역에 한 번 나타나야 한다.
        if (self.page_id, self.ingestion_id, self.pdf_page_number) != (
            page.page_id,
            page.ingestion_id,
            page.pdf_page_number,
        ):
            raise ValueError("Layout 원문 페이지/버전이 일치하지 않습니다.")
        source = {b.block_id: b for b in page.blocks if b.text.strip()}
        found = [block_id for region in self.regions for block_id in region.block_ids]
        if Counter(found) != Counter(source.keys()):
            raise ValueError("Layout 결과에 원문 행 누락/중복/외부 참조가 있습니다.")
        for region in self.regions:
            if region.bbox != enclosing_box([source[block_id] for block_id in region.block_ids]):
                raise ValueError("Layout 영역 좌표가 원문 행의 범위와 일치하지 않습니다.")


def layout_chunks(
    structure_id: str, page: OcrPage, result: PageLayout, start_order: int
) -> list[ChunkDraft]:
    result.validate_sources(page)
    originals = {b.block_id: b for b in page.blocks}
    chunks = []
    for index, region in enumerate(result.regions):
        # 영역을 넘겨 제목/본문/표를 합치지 않는다. 영역 안에서는 기존 800자 규칙을
        # 재사용한다. 아래 copy는 계산 전용이며 DB 원문의 reading_order를 바꾸지 않는다.
        blocks = [
            originals[block_id].model_copy(update={"reading_order": order})
            for order, block_id in enumerate(region.block_ids)
        ]
        ordered = page.model_copy(update={"blocks": blocks})
        drafts = page_chunks(f"{structure_id}:region={index}", ordered, start_order + len(chunks))
        for draft in drafts:
            draft.section_id = stable_id("SEC", structure_id, page.page_id)
            draft.content_type = region.kind
        chunks.extend(drafts)
    return chunks

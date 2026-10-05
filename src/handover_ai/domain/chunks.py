"""OCR 원문을 손상시키지 않는 첫 Chunk 규칙과 조회 계약.

아직 제목/표/다단을 해석하지 않는다. 페이지마다 임시 Section을 만들고 OCR의
reading_order대로 행을 묶는다. 이 한계를 parser_version에 고정하여 미래의 Layout
분석 결과와 섞지 않는다. DB와 모델 라이브러리는 이 모듈에서 사용하지 않는다.
"""

from hashlib import sha256
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field

from handover_ai.domain.models import Evidence
from handover_ai.domain.ocr import OcrBlock, OcrPage

TARGET_CHARS = 800
PARSER_VERSION = "page-lines-v1:chars=800"


def stable_id(prefix: str, *parts: str) -> str:
    # 길이를 앞에 붙여 구분 문자가 포함된 값도 모호하게 합쳐지지 않게 한다.
    # 같은 문서/처리/규칙의 재요청은 같은 ID를 얻고, 다른 OCR 버전은 다른 ID를 얻는다.
    value = "".join(f"{len(part)}:{part}" for part in parts)
    return f"{prefix}-{uuid5(NAMESPACE_URL, value).hex}"


class ChunkDraft(BaseModel):
    chunk_id: str
    section_id: str
    page_id: str
    page_number: int
    order: int
    text: str
    text_hash: str
    confidence: float
    block_ids: list[str]
    content_type: str = "ocr_text"


class StructureSummary(BaseModel):
    structure_id: str
    document_id: str
    ingestion_id: str
    parser_version: str
    section_count: int
    chunk_count: int
    created_at: str
    # 구조 생성 완료와 벡터 색인 완료는 별개다. 이 단계에서 ready라고 표시하지 않는다.
    layout_mode: Literal["page_fallback", "geometry_v1"] = "page_fallback"
    review_page_count: int = 0


class BuildStructureRequest(BaseModel):
    # 화면에서 보고 있던 버전을 반드시 전달하여 다른 탭의 재처리 결과를 실수로 만들지 않는다.
    ingestion_id: str = Field(min_length=1, max_length=100)


class ChunkView(BaseModel):
    chunk_id: str
    structure_id: str
    section_id: str
    section_title: str
    text: str
    text_hash: str
    chunk_order: int
    ocr_confidence: float
    index_status: Literal["pending", "ready", "failed"]
    page_id: str
    evidence: Evidence
    sources: list[OcrBlock]
    content_type: str = "ocr_text"


class StructureConflict(ValueError):
    """OCR이 완료되지 않았거나, 화면에서 선택한 버전이 더 이상 현재 버전이 아님."""


def page_chunks(structure_id: str, page: OcrPage, start_order: int) -> list[ChunkDraft]:
    """페이지/원문 행 경계를 보존하며 800자 안팎으로 묶는다. 빈 페이지는 Chunk가 없다."""
    section_id = stable_id("SEC", structure_id, page.page_id)
    groups: list[list[OcrBlock]] = []
    current: list[OcrBlock] = []
    length = 0
    for block in sorted(page.blocks, key=lambda item: item.reading_order):
        if not block.text.strip():
            continue
        size = len(block.text) + (1 if current else 0)
        if current and length + size > TARGET_CHARS:
            groups.append(current)
            current, length = [], 0
        current.append(block)
        length += len(block.text) + (1 if len(current) > 1 else 0)
    if current:
        groups.append(current)
    drafts = []
    for index, blocks in enumerate(groups):
        # 긴 OCR 행 하나가 800자를 넘으면 그대로 둔다. 중간 절단은 좌표/문장 근거를
        # 잃게 하므로 여기서는 소프트 제한이다. 모델 토큰 제한은 향후 색인 단계에서 처리한다.
        text = "\n".join(block.text for block in blocks)
        drafts.append(
            ChunkDraft(
                chunk_id=stable_id("CH", structure_id, page.page_id, str(index)),
                section_id=section_id,
                page_id=page.page_id,
                page_number=page.pdf_page_number,
                order=start_order + index,
                text=text,
                text_hash=sha256(text.encode()).hexdigest(),
                # 평균값이 저품질 원문을 가리지 않도록 포함된 행의 최저 신뢰도를 사용한다.
                confidence=min(block.confidence for block in blocks),
                block_ids=[block.block_id for block in blocks],
            )
        )
    return drafts

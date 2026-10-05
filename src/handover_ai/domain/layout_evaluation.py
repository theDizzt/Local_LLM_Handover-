"""배치 분석 정답 자료 계약. 미작성 정답과 빈 페이지 정답을 구분한다."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from handover_ai.domain.layout import RegionKind
from handover_ai.domain.ocr import OcrPage


def page_fingerprint(page: OcrPage) -> str:
    # 정답을 작성한 OCR 원문·좌표·순서가 바뀌면 평가를 거절한다. 이 해시는 실수로
    # 다른 버전의 자료를 섞는 것을 찾기 위한 것으로 작성자 신원 증명은 아니다.
    canonical = json.dumps(page.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AnnotatedPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: OcrPage
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    annotation_status: Literal["draft", "reviewed"] = "draft"
    # null은 미작성, []는 글자가 없는 페이지에 대한 명시적 정답이다.
    expected_order: list[str] | None = None
    # 분류는 일부 Block만 지정해도 된다. 보고서에서 분류 정답 작성률을 따로 표시한다.
    expected_kinds: dict[str, RegionKind] = Field(default_factory=dict)
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_annotation(self):
        ids = [block.block_id for block in self.page.blocks]
        if len(ids) != len(set(ids)):
            raise ValueError("OCR Block ID가 중복되었습니다.")
        eligible = {block.block_id for block in self.page.blocks if block.text.strip()}
        if self.source_sha256 != page_fingerprint(self.page):
            raise ValueError("정답 자료의 OCR 원문 해시가 일치하지 않습니다.")
        if self.expected_order is not None and (
            len(self.expected_order) != len(eligible) or set(self.expected_order) != eligible
        ):
            raise ValueError(
                "정답 읽기 순서는 공백을 제외한 모든 Block을 정확히 한 번 포함해야 합니다."
            )
        if not set(self.expected_kinds) <= eligible:
            raise ValueError("분류 정답에 해당 페이지에 없는 Block이 있습니다.")
        if self.annotation_status == "reviewed" and (
            self.expected_order is None and not self.expected_kinds
        ):
            raise ValueError("정답이 없는 페이지는 검토 완료로 지정할 수 없습니다.")
        return self


class LayoutDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    dataset_id: str = Field(min_length=1, max_length=200)
    source_kind: Literal["synthetic", "real_document"]
    document_id: str
    ingestion_id: str
    pages: list[AnnotatedPage] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_pages(self):
        ids = [item.page.page_id for item in self.pages]
        numbers = [item.page.pdf_page_number for item in self.pages]
        if len(ids) != len(set(ids)) or len(numbers) != len(set(numbers)):
            raise ValueError("평가 페이지 ID 또는 페이지 번호가 중복되었습니다.")
        if any(item.page.ingestion_id != self.ingestion_id for item in self.pages):
            raise ValueError("서로 다른 OCR 버전의 페이지를 섞을 수 없습니다.")
        return self

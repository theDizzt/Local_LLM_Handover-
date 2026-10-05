"""사람의 검토 기록. 자동 분석 경고나 OCR 원문을 덮어쓰지 않는다."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ReviewStatus = Literal["unreviewed", "confirmed", "needs_correction"]


class ReviewRequest(BaseModel):
    # revision은 화면에서 읽은 번호다. 두 탭에서 동시에 수정할 때 마지막 저장이
    # 앞선 검토를 조용히 덮어쓰지 않도록 저장소가 이 번호를 비교한다.
    expected_revision: int = Field(ge=0)
    status: ReviewStatus
    note: str = Field(default="", max_length=2000)
    text_checked: bool = False
    order_checked: bool = False
    regions_checked: bool = False

    @model_validator(mode="after")
    def validate_decision(self):
        self.note = self.note.strip()
        if self.status == "confirmed" and not (
            self.text_checked and self.order_checked and self.regions_checked
        ):
            raise ValueError("원문 텍스트·읽기 순서·영역 분류를 모두 확인해야 완료할 수 있습니다.")
        if self.status == "needs_correction" and not self.note:
            raise ValueError("수정이 필요한 내용을 메모에 남겨 주세요.")
        return self


class PageReview(BaseModel):
    structure_id: str
    page_id: str
    revision: int = 0
    status: ReviewStatus = "unreviewed"
    note: str = ""
    text_checked: bool = False
    order_checked: bool = False
    regions_checked: bool = False
    updated_at: str | None = None


class ReviewSummary(BaseModel):
    structure_id: str
    total: int
    confirmed: int
    needs_correction: int
    unreviewed: int

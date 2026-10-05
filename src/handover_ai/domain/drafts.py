"""첫 초안은 원문 발췌형이다. 모델은 문장을 새로 쓰지 않고 Block의 배치만 제안한다."""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from handover_ai.domain.search import SearchRequest

PROMPT_VERSION = "excerpt-draft-v1"
SCHEMA_VERSION = "excerpt-1.0"
Category = Literal["overview", "prerequisites", "procedure", "cautions", "troubleshooting"]
CATEGORIES = ("overview", "prerequisites", "procedure", "cautions", "troubleshooting")


class DraftRequest(SearchRequest):
    title: str = Field(min_length=1, max_length=120)
    reviewed_only: bool = True

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError("초안 제목을 입력해 주세요.")
        return value.strip()


class SelectedBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block_id: str = Field(min_length=1, max_length=100)
    category: Category


class DraftSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[SelectedBlock] = Field(max_length=30)


class DraftGateway(Protocol):
    model_id: str

    def validate_config(self) -> None: ...

    async def select(self, query: str, blocks: list[dict], *, retry: bool) -> DraftSelection: ...


class DraftFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def materialize(selection: DraftSelection, sources: dict[str, dict]) -> list[dict]:
    if not selection.items:
        raise DraftFailure("no_relevant_evidence")
    # 형식만 맞는 응답도 근거가 유효하다는 뜻은 아니다. 모델이 지정한 ID가 실제
    # 제공 목록에 있어야 하며, 본문/페이지/좌표는 모델 응답이 아닌 저장된 원문에서 만든다.
    seen, items = set(), []
    for item in selection.items:
        if item.block_id not in sources or item.block_id in seen:
            raise DraftFailure("invalid_model_output")
        seen.add(item.block_id)
        source = sources[item.block_id]
        items.append({"category": item.category, **source})
    return items

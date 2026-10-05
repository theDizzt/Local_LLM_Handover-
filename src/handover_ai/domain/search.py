"""검색 요청과 장애 계약. 유사도 순위는 사실 정확도나 OCR 신뢰도가 아니다."""

import math
from typing import Annotated, Protocol

from pydantic import BaseModel, Field, field_validator


class SearchConflict(Exception):
    pass


class SearchUnavailable(Exception):
    pass


class IndexRequest(BaseModel):
    structure_id: str = Field(min_length=1, max_length=100)


class CleanupRequest(BaseModel):
    # 미리보기에서 선택한 ID만 받는다. 클라이언트가 조건이나 컬렉션 이름을 임의로
    # 만들어 전달해도 저장소가 소속 문서와 현재 사용 여부를 다시 검증한다.
    run_ids: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=20
    )

    @field_validator("run_ids")
    @classmethod
    def unique_ids(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("중복된 색인 ID는 한 번만 지정해 주세요.")
        return values


class SearchRequest(IndexRequest):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("검색어를 입력해 주세요.")
        return value.strip()


class Embedder(Protocol):
    @property
    def version(self) -> str: ...

    def encode(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...


def validate_vectors(vectors: list[list[float]], count: int) -> None:
    if len(vectors) != count or not vectors:
        raise ValueError("임베딩 결과 개수가 일치하지 않습니다.")
    dimensions = len(vectors[0])
    if not dimensions or any(
        len(v) != dimensions or not all(math.isfinite(x) for x in v) or sum(x * x for x in v) == 0
        for v in vectors
    ):
        raise ValueError("임베딩 차원/수치가 올바르지 않습니다.")

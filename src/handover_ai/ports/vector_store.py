# [읽기 안내] 검색 인덱스 계약이다. 원본은 SQLite이며 Vector Store는 재생성 가능한 복제본이다.
# 실행 ID별 컬렉션을 만들어 재구축 중인 벡터가 기존 검색에 섞이지 않게 한다.
from typing import Protocol


class VectorStore(Protocol):
    def create(self, run_id: str, version: str) -> None: ...

    def upsert(self, run_id: str, ids: list[str], vectors: list[list[float]]) -> None: ...

    def count(self, run_id: str) -> int: ...

    def delete(self, run_id: str) -> None: ...

    def search(
        self,
        run_id: str,
        vector: list[float],
        limit: int,
    ) -> list[tuple[str, float]]: ...
    # 반환 ID와 거리는 후보일 뿐이다. 원문·근거는 SQLite에서 다시 검증한다.

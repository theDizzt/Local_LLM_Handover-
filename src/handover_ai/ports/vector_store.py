from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class VectorRecord:
    chunk_id: str
    document_id: str
    text: str
    metadata: dict[str, str | int | float]


class VectorStore(Protocol):
    def upsert(self, records: list[VectorRecord], index_version: str) -> None: ...

    def search(
        self,
        query: str,
        document_ids: list[str],
        top_k: int,
        index_version: str,
    ) -> list[VectorRecord]: ...

    def delete(self, document_id: str, index_version: str) -> None: ...

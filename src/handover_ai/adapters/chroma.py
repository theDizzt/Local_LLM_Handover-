"""실행별 독립 컬렉션을 사용한다. SQLite가 공개한 실행만 검색 대상이다."""

from functools import cached_property
from pathlib import Path


class ChromaVectors:
    def __init__(self, path: Path):
        self.path = path

    @cached_property
    def client(self):
        import chromadb
        from chromadb.config import Settings

        return chromadb.PersistentClient(
            path=str(self.path), settings=Settings(anonymized_telemetry=False)
        )

    def create(self, run_id: str, version: str):
        self.client.create_collection(
            run_id,
            embedding_function=None,
            metadata={"index_version": version},
            configuration={"hnsw": {"space": "cosine", "num_threads": 2}},
        )

    def upsert(self, run_id: str, ids: list[str], vectors: list[list[float]]):
        self.client.get_collection(run_id, embedding_function=None).upsert(
            ids=ids, embeddings=vectors
        )

    def count(self, run_id: str) -> int:
        return self.client.get_collection(run_id, embedding_function=None).count()

    def delete(self, run_id: str) -> None:
        from chromadb.errors import NotFoundError

        try:
            self.client.delete_collection(name=run_id)
        except NotFoundError:
            # 컬렉션 삭제 직후 SQLite 완료 기록 전에 중단됐어도 재시도는 성공이다.
            # 권한/디스크 장애 등 다른 예외는 삼키지 않아 실패 기록과 재시도가 가능하다.
            pass

    def search(self, run_id: str, vector: list[float], limit: int):
        collection = self.client.get_collection(run_id, embedding_function=None)
        count = collection.count()
        if not count:
            raise ValueError("빈 색인")
        result = collection.query(
            query_embeddings=[vector], n_results=min(limit, count), include=["distances"]
        )
        return list(zip(result["ids"][0], result["distances"][0], strict=True))

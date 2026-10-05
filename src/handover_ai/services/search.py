"""완전한 새 컬렉션을 만든 후 공개한다. 실패한 부분 색인은 검색하지 않는다."""

from handover_ai.domain.search import Embedder, SearchConflict, SearchUnavailable, validate_vectors
from handover_ai.ports.vector_store import VectorStore


class SearchService:
    def __init__(self, repository, embedder: Embedder, vectors: VectorStore):
        self.repository = repository
        self.embedder = embedder
        self.vectors = vectors

    def enqueue(self, document_id, structure_id):
        return self.repository.enqueue(document_id, structure_id, self.embedder.version)

    def state(self, document_id, structure_id):
        return self.repository.state(document_id, structure_id, self.embedder.version)

    def build(self, run):
        if not self.repository.start(run["run_id"]):
            return
        try:
            self.vectors.create(run["run_id"], run["index_version"])
            offset = 0
            while rows := self.repository.batch(run["structure_id"], offset):
                vectors = self.embedder.encode([row["text"] for row in rows])
                validate_vectors(vectors, len(rows))
                self.vectors.upsert(run["run_id"], [row["chunk_id"] for row in rows], vectors)
                self.repository.progress(run["run_id"], rows)
                offset += len(rows)
            if self.vectors.count(run["run_id"]) != run["total"]:
                raise ValueError("색인 개수 불일치")
            self.repository.finish(run)
        except Exception as exc:
            # 원문이나 로컬 경로를 API 오류에 노출하지 않는다. 이전 완료 색인은 유지한다.
            self.repository.fail(
                run["run_id"],
                "version_changed" if isinstance(exc, SearchConflict) else "index_failed",
            )

    def search(self, document_id, request):
        run = self.state(document_id, request.structure_id)["active"]
        if not run:
            raise SearchConflict("이 정리 방식의 검색 색인을 먼저 준비해 주세요.")
        try:
            vector = self.embedder.encode([request.query], query=True)
            validate_vectors(vector, 1)
            matches = self.vectors.search(run["run_id"], vector[0], request.top_k)
        except Exception as exc:
            raise SearchUnavailable(
                "검색 색인을 읽을 수 없습니다. 색인을 다시 준비해 주세요."
            ) from exc
        return {
            "run_id": run["run_id"],
            "structure_id": request.structure_id,
            "hits": self.repository.resolve(run, matches),
        }

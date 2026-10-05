"""사용자가 선택한 이전 색인만 순서대로 정리하고 항목별 결과를 반환한다."""

from handover_ai.domain.search import SearchConflict
from handover_ai.ports.vector_store import VectorStore


class SearchCleanupService:
    def __init__(self, repository, vectors: VectorStore):
        self.repository, self.vectors = repository, vectors

    def execute(self, document_id, run_ids):
        results = []
        for run_id in run_ids:
            try:
                claim = self.repository.claim(document_id, run_id)
            except SearchConflict:
                results.append({"run_id": run_id, "status": "protected"})
                continue
            if claim != "claimed":
                results.append({"run_id": run_id, "status": claim})
                continue
            try:
                self.vectors.delete(run_id)
            except Exception:
                self.repository.finish(run_id, success=False)
                results.append({"run_id": run_id, "status": "failed"})
            else:
                self.repository.finish(run_id, success=True)
                results.append({"run_id": run_id, "status": "deleted"})
        return results

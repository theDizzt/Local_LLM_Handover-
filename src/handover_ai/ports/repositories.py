# [읽기 안내] Protocol은 구현 코드가 아닌 '필요한 메서드의 모양'이다.
# 서비스는 SQLite 이름을 몰라도 이 메서드들을 호출할 수 있다. 실제 SQL은 adapters에 있다.
# ...은 계약만 선언한다는 뜻이다. 이 클래스를 직접 만들어 저장에 사용하지 않는다.
from typing import Any, Protocol

from handover_ai.domain.documents import StoredDocument


class DocumentRepository(Protocol):
    def save_document(self, document: StoredDocument) -> None: ...

    def get_document(self, document_id: str) -> StoredDocument | None: ...

    def get_by_hash(self, file_sha256: str) -> StoredDocument | None: ...

    def list_documents(self, limit: int, offset: int) -> list[StoredDocument]: ...


class TaskRepository(Protocol):
    # 아래 업무/평가 저장 계약은 향후 구현용이며 현재 실제 어댑터가 연결되지 않았다.
    def get_task(self, task_id: str) -> dict[str, Any] | None: ...

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]: ...


class EvaluationRepository(Protocol):
    def save_evaluation(self, evaluation: dict[str, Any]) -> None: ...


class UnitOfWork(Protocol):
    # 여러 Repository 변경을 한 트랜잭션으로 묶기 위한 미래 계약이다.
    # 현재 문서 저장은 SQLiteDatabase.connect() 내부 트랜잭션으로 구현되어 있다.
    def __enter__(self): ...

    def __exit__(self, exception_type, exception, traceback): ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

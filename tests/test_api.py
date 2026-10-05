# [읽기 안내] TestClient는 실제 포트를 열지 않고 FastAPI 요청/응답을 검증한다.
# 임시 DB를 주입해 개발 데이터에 영향을 주지 않으며 with를 통해 앱 lifespan도 실행한다.
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.api.dependencies import get_database
from handover_ai.main import create_app


def test_health_endpoint():
    with tempfile.TemporaryDirectory() as directory:
        database = SQLiteDatabase(Path(directory) / "handover.db")
        app = create_app(database=database)
        app.dependency_overrides[get_database] = lambda: database

        with TestClient(app) as client:
            response = client.get("/api/v1/health")

        assert response.status_code == 200
        assert response.json()["database"] == {
            "status": "ready",
            "schema_version": "1",
        }


def test_evaluation_compare_endpoint():
    with tempfile.TemporaryDirectory() as directory:
        database = SQLiteDatabase(Path(directory) / "handover.db")
        app = create_app(database=database)

        request = {
            "python_criteria": [
                {
                    "criterion_id": "R1",
                    "criterion": "DTC 확인",
                    "score": 3,
                    "max_score": 4,
                    "answer_evidence": "DTC를 확인한다.",
                    "source_evidence_ids": ["EV-001"],
                    "confidence": 1,
                }
            ],
            "llm_criteria": [
                {
                    "criterion_id": "R1",
                    "criterion": "DTC 확인",
                    "score": 3.1,
                    "max_score": 4,
                    "answer_evidence": "DTC를 확인한다.",
                    "source_evidence_ids": ["EV-001"],
                    "confidence": 0.9,
                }
            ],
            "evidence_valid": True,
        }

        with TestClient(app) as client:
            response = client.post("/api/v1/evaluations/compare", json=request)

        assert response.status_code == 200
        assert response.json()["status"] == "accepted_candidate"
        assert response.json()["needs_review"] is False

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

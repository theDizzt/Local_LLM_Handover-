"""검토 화면의 배포 경계와 한국어 OCR 평가 지표를 검사한다."""

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.main import create_app


def test_review_assets_and_private_files(tmp_path):
    with TestClient(create_app(database=SQLiteDatabase(tmp_path / "test.db"))) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert 'lang="ko"' in response.text
        for asset in ("review.js", "review.css"):
            assert client.get(f"/static/{asset}").status_code == 200
        # 정적 화면 추가가 저장 DB/원본 파일 공개로 이어지지 않는지 검증한다.
        assert client.get("/static/%2e%2e/config.py").status_code == 404
        assert client.get("/data/handover.db").status_code == 404


def test_character_error_rate_counts_insertions_deletions_and_korean_normalization():
    spec = importlib.util.spec_from_file_location(
        "validate_ocr", Path(__file__).parents[1] / "scripts" / "validate_ocr.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cer = module.character_error_rate
    assert cer("가 나", "가\n나") == 0
    assert cer("1234", "124") == pytest.approx(0.25)
    assert cer("1234", "12345") == pytest.approx(0.25)
    assert cer("1234", "1294") == pytest.approx(0.25)
    assert cer("가", "가나다") == 2
    assert cer("", "내용") is None

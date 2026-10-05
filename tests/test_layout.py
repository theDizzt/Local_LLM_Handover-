"""Layout 정답 fixture와 실제 SQLite/API 연결을 검증한다.

fixture 좌표는 사람이 구성한 OCR 출력이다. 이 테스트는 OCR 인식률이나 실제 업무 문서
정확도를 주장하지 않는다. 분류/읽기 순서/보존 계약과 두 규칙 버전의 공존을 검사한다.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from test_ocr import setup as ocr_fixture

from handover_ai.adapters.chunk_repository import SQLiteChunkRepository
from handover_ai.adapters.layout import GeometryLayoutAnalyzer
from handover_ai.domain.layout import LAYOUT_VERSION, layout_chunks
from handover_ai.domain.ocr import OcrBlock, OcrLine, OcrPage
from handover_ai.services.chunks import ChunkService

setup = ocr_fixture
CASES = json.loads(
    (Path(__file__).parent / "fixtures/layout_cases.json").read_text(encoding="utf-8")
)


def fixture_page(case):
    return OcrPage(
        page_id="P-" + case["name"],
        ingestion_id="ING-fixture",
        pdf_page_number=1,
        width_px=1000,
        height_px=1000,
        render_dpi=150,
        rotation=0,
        blocks=[
            OcrBlock(block_id=key, text=text, bbox=box, reading_order=order, confidence=0.95)
            for order, (key, text, box) in enumerate(case["blocks"])
        ],
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_layout_golden_cases(case):
    page = fixture_page(case)
    before = page.model_dump()
    result = GeometryLayoutAnalyzer().analyze(page)
    result.validate_sources(page)
    assert [key for region in result.regions for key in region.block_ids] == case["expected_order"]
    kinds = {key: region.kind for region in result.regions for key in region.block_ids}
    for key, kind in case["expected_kinds"].items():
        assert kinds[key] == kind
    reasons = set(result.warnings + [r for region in result.regions for r in region.review_reasons])
    assert set(case["expected_reasons"]) <= reasons
    assert result.needs_review
    chunks = layout_chunks("ST-test", page, result, 0)
    assert [key for chunk in chunks for key in chunk.block_ids] == case["expected_order"]
    originals = {b.block_id: b.text for b in page.blocks}
    assert "\n".join(c.text for c in chunks) == "\n".join(
        originals[key] for key in case["expected_order"]
    )
    assert page.model_dump() == before


def prepare(setup, monkeypatch):
    page = fixture_page(CASES[1])
    monkeypatch.setattr(
        setup[2],
        "recognize",
        lambda rendered: [
            OcrLine(text=b.text, bbox=b.bbox, confidence=b.confidence) for b in page.blocks
        ],
    )
    client, ocr, _, doc, database, _ = setup
    prefix = f"/api/v1/documents/{doc['document_id']}"
    version = client.post(prefix + "/ocr").json()["ingestion_id"]
    analyzer = GeometryLayoutAnalyzer()
    service = ChunkService(
        ocr.documents, ocr.repository, SQLiteChunkRepository(database, LAYOUT_VERSION), analyzer
    )
    return client, prefix, version, service


def test_layout_and_fallback_coexist_with_stable_sources(setup, monkeypatch):
    client, prefix, version, _ = prepare(setup, monkeypatch)
    original_pages = client.get(prefix + "/pages").json()
    basic = client.post(prefix + "/structure", json={"ingestion_id": version}).json()
    basic_chunks = client.get(prefix + "/chunks").json()
    result = client.post(prefix + "/layout", json={"ingestion_id": version})
    assert result.status_code == 200
    summary = result.json()
    assert summary["layout_mode"] == "geometry_v1"
    assert summary["review_page_count"] == 2
    assert summary["structure_id"] != basic["structure_id"]
    assert summary == client.post(prefix + "/layout", json={"ingestion_id": version}).json()
    assert summary == client.get(prefix + "/layout").json()
    assert client.get(prefix + "/structure").json() == basic
    assert client.get(prefix + "/chunks").json() == basic_chunks
    assert client.get(prefix + "/pages").json() == original_pages
    layouts = client.get(prefix + "/layout/pages").json()
    assert len(layouts) == 2 and layouts[0]["needs_review"]
    assert client.get(prefix + "/layout/pages?limit=1&offset=1").json() == layouts[1:]
    chunks = client.get(prefix + "/layout/chunks?page_number=1").json()
    expected_text = {b[0]: b[1] for b in CASES[1]["blocks"]}
    assert "\n".join(c["text"] for c in chunks) == "\n".join(
        expected_text[key] for key in CASES[1]["expected_order"]
    )
    for chunk in chunks:
        assert client.get(prefix + "/chunks/" + chunk["chunk_id"]).json() == chunk
        assert chunk["evidence"]["ingestion_id"] == version
        originals = {b["block_id"]: b for b in original_pages[0]["blocks"]}
        assert all(source == originals[source["block_id"]] for source in chunk["sources"])
    assert client.get(prefix + "/layout/chunks?limit=1&offset=1").json() == chunks[1:2]
    assert chunks[0]["content_type"] == "heading"
    assert chunks[0]["section_title"] == "운영 업무와 장애 대응"


def test_layout_version_after_new_ocr_and_cross_document_queries(setup, monkeypatch):
    client, prefix, version, _ = prepare(setup, monkeypatch)
    client.post(prefix + "/layout", json={"ingestion_id": version})
    first = client.get(prefix + "/layout/chunks").json()[0]
    client.post(prefix + "/ocr")
    assert client.get(prefix + "/layout").json() is None
    assert client.get(prefix + "/layout/pages").json() == []
    assert client.get(prefix + "/layout/chunks").json() == []
    assert client.post(prefix + "/layout", json={"ingestion_id": version}).status_code == 409
    assert len(client.get(prefix + f"/layout/pages?ingestion_id={version}").json()) == 2
    old = client.get(prefix + "/chunks/" + first["chunk_id"]).json()
    assert old["evidence"] == first["evidence"] and old["index_status"] == "failed"
    assert client.get(prefix + "/layout/pages?ingestion_id=other").json() == []
    for suffix in ("/layout", "/layout/pages", "/layout/chunks"):
        assert client.get("/api/v1/documents/missing" + suffix).status_code == 404
    assert client.get(prefix + "/layout/pages?limit=0").status_code == 422
    assert client.get(prefix + "/layout/chunks?page_number=0").status_code == 422


def test_layout_storage_failure_rolls_back_without_touching_basic_structure(setup, monkeypatch):
    client, prefix, version, service = prepare(setup, monkeypatch)
    client.post(prefix + "/structure", json={"ingestion_id": version})
    before = client.get(prefix + "/chunks").json()
    with setup[4].connect() as connection:
        connection.execute(
            "CREATE TRIGGER fail_layout BEFORE INSERT ON layout_page "
            "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        service.build(setup[3]["document_id"], version)
    with setup[4].connect() as connection:
        assert connection.execute("SELECT count(*) FROM structure_run").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM layout_page").fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_layout")
    assert client.get(prefix + "/chunks").json() == before
    assert service.build(setup[3]["document_id"], version).review_page_count == 2


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "foreign", "bbox", "page"])
def test_invalid_analyzer_output_rejected(mutation):
    page = fixture_page(CASES[0])
    result = GeometryLayoutAnalyzer().analyze(page)
    if mutation == "missing":
        result.regions.pop()
    elif mutation == "duplicate":
        result.regions.append(result.regions[0])
    elif mutation == "foreign":
        result.regions[0].block_ids = ["foreign-block"]
    elif mutation == "bbox":
        result.regions[0].bbox = (0, 0, 1, 1)
    else:
        result.page_id = "other"
    with pytest.raises(ValueError):
        layout_chunks("ST", page, result, 0)


def test_low_confidence_and_complex_page_are_explicit():
    page = fixture_page(CASES[0])
    page.blocks[0].confidence = 0.2
    assert "low_ocr_confidence" in GeometryLayoutAnalyzer().analyze(page).warnings
    page.blocks = [
        page.blocks[0].model_copy(update={"block_id": str(i), "reading_order": i})
        for i in range(1501)
    ]
    result = GeometryLayoutAnalyzer().analyze(page)
    assert result.regions[0].review_reasons == ["complex_page"]
    assert len(result.regions[0].block_ids) == 1501


def test_short_two_row_columns_remain_ambiguous():
    page = fixture_page(CASES[3])
    page.blocks = [b for b in page.blocks if b.block_id not in ("a3", "b3")]
    result = GeometryLayoutAnalyzer().analyze(page)
    assert result.regions[0].kind == "unknown"
    assert result.regions[0].review_reasons == ["table_or_columns"]
    assert result.regions[0].block_ids == [b.block_id for b in page.blocks]


def test_existing_structure_survives_additive_layout_initialization(setup, monkeypatch):
    client, prefix, version, _ = prepare(setup, monkeypatch)
    before = client.post(prefix + "/structure", json={"ingestion_id": version}).json()
    with setup[4].connect() as connection:
        connection.execute("DROP TABLE layout_page")
    # 이전 버전 DB처럼 Layout 테이블이 없는 상태에서도 시작 시 추가 생성만 수행한다.
    setup[4].initialize()
    setup[4].initialize()
    assert client.get(prefix + "/structure").json() == before
    assert client.post(prefix + "/layout", json={"ingestion_id": version}).status_code == 200

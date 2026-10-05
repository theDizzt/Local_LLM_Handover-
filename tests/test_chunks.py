"""기존 OCR fixture를 사용해 원문 보존·재처리·원자적 저장을 실제 DB/API로 확인한다."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_ocr import setup as ocr_fixture

from handover_ai.adapters.chunk_repository import SQLiteChunkRepository
from handover_ai.domain.chunks import StructureConflict, page_chunks
from handover_ai.domain.ocr import OcrBlock, OcrPage
from handover_ai.services.chunks import ChunkService

# pytest는 모듈에 노출된 fixture를 이름으로 주입한다. 기존 OCR 준비 과정을 재사용한다.
setup = ocr_fixture


def ready(setup):
    client, ocr, _, document, database, _ = setup
    prefix = f"/api/v1/documents/{document['document_id']}"
    job = client.post(prefix + "/ocr").json()
    service = ChunkService(ocr.documents, ocr.repository, SQLiteChunkRepository(database))
    return client, prefix, job["ingestion_id"], service


def test_structure_round_trip_and_idempotency(setup):
    client, prefix, version, _ = ready(setup)
    assert client.get(prefix + "/structure").json() is None
    result = client.post(prefix + "/structure", json={"ingestion_id": version})
    assert result.status_code == 200
    assert result.json()["section_count"] == result.json()["chunk_count"] == 2
    assert result.json()["layout_mode"] == "page_fallback"
    assert (
        client.post(prefix + "/structure", json={"ingestion_id": version}).json() == result.json()
    )
    assert client.get(prefix + "/structure").json() == result.json()
    chunks = client.get(prefix + "/chunks").json()
    pages = client.get(prefix + "/pages").json()
    assert [chunk["chunk_order"] for chunk in chunks] == [0, 1]
    for chunk, page in zip(chunks, pages, strict=True):
        assert chunk["index_status"] == "pending"
        assert chunk["sources"] == page["blocks"]
        assert chunk["text"] == "\n".join(block["text"] for block in page["blocks"])
        assert chunk["evidence"]["block_ids"] == [block["block_id"] for block in page["blocks"]]
        assert chunk["evidence"]["ingestion_id"] == version
        assert chunk["evidence"]["pdf_page_number"] == page["pdf_page_number"]
        assert client.get(prefix + "/chunks/" + chunk["chunk_id"]).json() == chunk
        assert client.get(prefix + f"/pages/{chunk['page_id']}/image").status_code == 200
    assert client.get(prefix + "/chunks?limit=1&offset=1").json() == chunks[1:]
    assert client.get(prefix + "/chunks?page_number=2").json() == chunks[1:]
    assert client.get(prefix + "/chunks?limit=0").status_code == 422
    assert client.get(prefix + "/chunks?page_number=0").status_code == 422
    assert client.get(prefix + "/chunks?ingestion_id=unrelated").json() == []
    with setup[4].connect() as connection:
        assert connection.execute("SELECT count(*) FROM structure_run").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM section").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT count(*) FROM document_block WHERE section_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


def test_not_ready_missing_and_cross_document_requests(setup):
    client, _, _, document, _, _ = setup
    prefix = f"/api/v1/documents/{document['document_id']}"
    assert (
        client.post(
            prefix + "/structure", json={"ingestion_id": document["ingestion_id"]}
        ).status_code
        == 409
    )
    assert client.post(prefix + "/structure", json={}).status_code == 422
    assert client.get(prefix + "/chunks").json() == []
    for suffix in ("/structure", "/chunks", "/chunks/missing"):
        assert client.get("/api/v1/documents/missing" + suffix).status_code == 404
    assert (
        client.post("/api/v1/documents/missing/structure", json={"ingestion_id": "x"}).status_code
        == 404
    )
    client, prefix, version, _ = ready(setup)
    client.post(prefix + "/structure", json={"ingestion_id": version})
    chunk_id = client.get(prefix + "/chunks").json()[0]["chunk_id"]
    assert client.get(f"/api/v1/documents/other/chunks/{chunk_id}").status_code == 404


def test_reprocessing_keeps_old_evidence_and_excludes_it_from_default_listing(setup):
    client, prefix, old, _ = ready(setup)
    client.post(prefix + "/structure", json={"ingestion_id": old})
    first = client.get(prefix + "/chunks").json()[0]
    new = client.post(prefix + "/ocr").json()["ingestion_id"]
    assert client.get(prefix + "/structure").json() is None
    assert client.get(prefix + "/chunks").json() == []
    assert client.post(prefix + "/structure", json={"ingestion_id": old}).status_code == 409
    old_chunk = client.get(prefix + "/chunks/" + first["chunk_id"]).json()
    assert old_chunk["evidence"] == first["evidence"]
    assert old_chunk["sources"] == first["sources"]
    assert old_chunk["index_status"] == "failed"
    assert client.get(prefix + f"/structure?ingestion_id={old}").json()["chunk_count"] == 2
    assert len(client.get(prefix + f"/chunks?ingestion_id={old}").json()) == 2
    client.post(prefix + "/structure", json={"ingestion_id": new})
    current = client.get(prefix + "/chunks").json()[0]
    assert current["chunk_id"] != first["chunk_id"]
    assert current["evidence"]["ingestion_id"] == new


def test_storage_failure_rolls_back_structure_sections_and_chunks(setup):
    _, _, version, service = ready(setup)
    with setup[4].connect() as connection:
        connection.execute(
            "CREATE TRIGGER reject_source BEFORE INSERT ON chunk_source "
            "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        service.build(setup[3]["document_id"], version)
    with setup[4].connect() as connection:
        for table in ("structure_run", "structure_section", "section", "chunk", "chunk_source"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        connection.execute("DROP TRIGGER reject_source")
    assert service.build(setup[3]["document_id"], version).chunk_count == 2


def test_version_change_during_calculation_rejected_before_save(setup, monkeypatch):
    _, _, version, service = ready(setup)
    ocr = setup[1]
    original_save = service.chunks.save_structure

    def changed_version(*args):
        job = ocr.enqueue(setup[3]["document_id"])
        ocr.run(job.job_id)
        return original_save(*args)

    monkeypatch.setattr(service.chunks, "save_structure", changed_version)
    with pytest.raises(StructureConflict):
        service.build(setup[3]["document_id"], version)
    with setup[4].connect() as connection:
        assert connection.execute("SELECT count(*) FROM structure_run").fetchone()[0] == 0


def test_concurrent_builds_share_one_result(setup):
    _, _, version, service = ready(setup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.build, setup[3]["document_id"], version) for _ in range(2)]
        results = [future.result() for future in futures]
    assert results[0] == results[1]
    with setup[4].connect() as connection:
        assert connection.execute("SELECT count(*) FROM chunk").fetchone()[0] == 2


def test_wrong_page_source_is_rejected_and_rolled_back(setup, monkeypatch):
    _, _, version, service = ready(setup)
    original_save = service.chunks.save_structure

    def wrong_source(document_id, ingestion_id, pages, drafts):
        # 존재하는 Block이더라도 다른 페이지의 원문을 연결하면 안 된다.
        drafts[0].block_ids = [pages[1].blocks[0].block_id]
        return original_save(document_id, ingestion_id, pages, drafts)

    monkeypatch.setattr(service.chunks, "save_structure", wrong_source)
    with pytest.raises(ValueError, match="원문 Block"):
        service.build(setup[3]["document_id"], version)
    with setup[4].connect() as connection:
        assert connection.execute("SELECT count(*) FROM structure_run").fetchone()[0] == 0


def test_empty_pages_are_recorded_without_invented_chunks(setup, monkeypatch):
    monkeypatch.setattr(setup[2], "recognize", lambda page: [])
    client, prefix, version, _ = ready(setup)
    result = client.post(prefix + "/structure", json={"ingestion_id": version}).json()
    assert result["section_count"] == 2 and result["chunk_count"] == 0
    assert client.get(prefix + "/chunks").json() == []


def test_chunk_boundaries_preserve_order_long_lines_and_coordinates():
    texts = ["가" * 400, "나" * 399, "다" * 801, "끝"]
    blocks = [
        OcrBlock(
            block_id=f"B-{i}",
            reading_order=i,
            text=text,
            bbox=(0.1, 0.1, 0.9, 0.2),
            confidence=0.9 - i / 10,
        )
        for i, text in enumerate(texts)
    ]
    page = OcrPage(
        page_id="P",
        ingestion_id="I",
        pdf_page_number=1,
        width_px=100,
        height_px=100,
        render_dpi=72,
        rotation=0,
        blocks=blocks[::-1],
    )
    chunks = page_chunks("S", page, 5)
    assert [len(chunk.text) for chunk in chunks] == [800, 801, 1]
    assert [chunk.order for chunk in chunks] == [5, 6, 7]
    assert "\n".join(chunk.text for chunk in chunks) == "\n".join(texts)
    assert chunks[0].confidence == 0.8
    assert chunks[0].block_ids == ["B-0", "B-1"]
    assert page_chunks("S", page, 5) == chunks
    assert page_chunks("OTHER", page, 5)[0].chunk_id != chunks[0].chunk_id


def test_additive_schema_keeps_existing_ocr_and_legacy_sections(setup):
    client, prefix, version, _ = ready(setup)
    before = client.get(prefix + "/pages").json()
    with setup[4].connect() as connection:
        connection.execute("DROP TABLE structure_section")
        connection.execute("DROP TABLE structure_run")
        connection.execute(
            "INSERT INTO section(section_id, document_id, title, level, page_start, page_end, "
            "section_path, title_source, structure_confidence) "
            "VALUES ('LEGACY', ?, '기존 제목', 1, 1, 1, 'legacy', 'inferred', 0)",
            (setup[3]["document_id"],),
        )
    setup[4].initialize()
    setup[4].initialize()
    assert client.get(prefix + "/pages").json() == before
    assert client.post(prefix + "/structure", json={"ingestion_id": version}).status_code == 200
    with setup[4].connect() as connection:
        assert (
            connection.execute("SELECT title FROM section WHERE section_id='LEGACY'").fetchone()[0]
            == "기존 제목"
        )

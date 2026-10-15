"""사용자 PDF와 초안 정답을 로컬 OCR·검색·발췌 파이프라인으로 예비 평가한다."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter

import pymupdf

ROOT = Path(__file__).resolve().parents[1]


def normalized(text):
    return "".join(unicodedata.normalize("NFC", text).split())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # 문서별 병렬 실행에서도 임시 파일 이름이 충돌하지 않는다.
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def audit(dataset):
    """파일 동일성과 페이지 범위를 확인한다. 텍스트 일치는 정답 확정을 뜻하지 않는다."""
    result, identifiers, document_ids = [], set(), set()
    for document in dataset["documents"]:
        code = document["document_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", code) or code in document_ids:
            raise ValueError("문서 ID 형식 오류 또는 중복")
        document_ids.add(code)
        path = (ROOT / document["source_pdf_path"]).resolve()
        if not path.is_relative_to(ROOT / "data/evaluation-input"):
            raise ValueError("평가 입력 폴더 밖의 PDF 경로")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != document["sha256"]:
            raise ValueError(f"원본 해시 불일치: {document['document_id']}")
        with pymupdf.open(path) as pdf:
            if len(pdf) != document["pages"]:
                raise ValueError("PDF 페이지 수 불일치")
            evidence = []
            for question in document["questions"]:
                if question["id"] in identifiers:
                    raise ValueError("중복 질문 ID")
                identifiers.add(question["id"])
                if not question["answerable"]:
                    if question["expected_answer"] != "문서에 없음" or question["evidence"]:
                        raise ValueError("무답 질문의 정답/근거 불일치")
                elif not question["evidence"]:
                    raise ValueError("유답 질문의 근거 누락")
                if sorted(set(e["pdf_page"] for e in question["evidence"])) != sorted(
                    question["evidence_pages"]
                ):
                    raise ValueError("근거 페이지 목록 불일치")
                for item in question["evidence"]:
                    number = item["pdf_page"]
                    if not 1 <= number <= len(pdf) or not item["quote"].strip():
                        raise ValueError("근거 페이지/인용문 오류")
                    text = pdf[number - 1].get_text(sort=True)
                    evidence.append(
                        {
                            "question_id": question["id"],
                            "page": number,
                            "quote": item["quote"],
                            "embedded_text_characters": len(text),
                            "exact_in_embedded_text": normalized(item["quote"]) in normalized(text),
                        }
                    )
            result.append(
                {
                    "document_id": document["document_id"],
                    "pages": len(pdf),
                    "sha256": digest,
                    "questions": len(document["questions"]),
                    "embedded_text_pages": sum(bool(p.get_text().strip()) for p in pdf),
                    "evidence": evidence,
                }
            )
    return {"gold_status": dataset["gold_status"], "documents": result}


def evidence_matches(question, page_texts):
    # 페이지를 고정한 공백 제외 완전 일치다. 의미 정확도나 전체 페이지 CER가 아니다.
    return [
        normalized(e["quote"]) in normalized(page_texts.get(e["pdf_page"], ""))
        for e in question["evidence"]
    ]


def ordered_page_texts(blocks, block_pages):
    """검색 순위/모델 선택 순서와 무관하게 원문 읽기 순서로 포함 여부를 측정한다."""
    unique = {block["block_id"]: block for block in blocks}
    pages = {}
    for block in sorted(unique.values(), key=lambda b: b["reading_order"]):
        page = block_pages[block["block_id"]]
        pages[page] = pages.get(page, "") + "\n" + block["text"]
    return pages


def outcome(answerable, status, error_code):
    if status == "ready":
        return "answer_generated" if answerable else "false_answer"
    if error_code == "no_relevant_evidence":
        return "missed_answer" if answerable else "correct_abstention"
    return "execution_error"


def run_document(
    document, output, search, database, settings, *, prepare_only=False, adapter=None
):
    from handover_ai.adapters.chunk_repository import SQLiteChunkRepository
    from handover_ai.adapters.document_repository import SQLiteDocumentRepository
    from handover_ai.adapters.draft_repository import SQLiteDraftRepository
    from handover_ai.adapters.layout import GeometryLayoutAnalyzer
    from handover_ai.adapters.local_qwen import LocalQwenDraftGateway
    from handover_ai.adapters.ocr import PaddleOcrEngine, PyMuPdfRenderer
    from handover_ai.adapters.ocr_repository import SQLiteOcrRepository
    from handover_ai.adapters.pdf import PyMuPdfInspector
    from handover_ai.domain.drafts import DraftRequest
    from handover_ai.domain.search import SearchRequest
    from handover_ai.services.chunks import ChunkService
    from handover_ai.services.documents import DocumentService
    from handover_ai.services.drafts import DraftService
    from handover_ai.services.ocr import OcrService

    code = document["document_id"]
    result_path = output / f"{code}.json"
    if result_path.exists():
        return json.loads(result_path.read_text(encoding="utf-8"))
    started = perf_counter()
    documents, ocr = SQLiteDocumentRepository(database), SQLiteOcrRepository(database)
    service = DocumentService(documents, PyMuPdfInspector(), output / "documents", 100_000_000)
    path = ROOT / document["source_pdf_path"]
    with path.open("rb") as source:
        registered = service.register(path.name, source).document
    document_id = registered.document_id
    result = {"document_id": code, "app_document_id": document_id, "questions": []}
    if registered.status != "ready":
        engine = PaddleOcrEngine("korean", settings["detection"], settings["recognition"])

        class ProgressEngine:
            version = engine.version

            def recognize(self, page):
                tick = perf_counter()
                lines = engine.recognize(page)
                print(
                    f"{code} OCR {page.number}/{document['pages']} {perf_counter() - tick:.1f}s",
                    flush=True,
                )
                return lines

        processor = OcrService(
            documents, ocr, PyMuPdfRenderer(), ProgressEngine(), output / "pages"
        )
        job = processor.enqueue(document_id)
        processor.run(job.job_id)
        completed = ocr.get_job(job.job_id)
        if completed.status != "completed":
            raise RuntimeError(f"{code}: {completed.error_code}")
    registered = documents.get_document(document_id)
    analyzer = GeometryLayoutAnalyzer()
    # 기본 비교 구조를 함께 보존한다. 사람의 검토 완료 상태는 자동 작성하지 않는다.
    baseline = ChunkService(documents, ocr, SQLiteChunkRepository(database)).build(
        document_id, registered.ingestion_id
    )
    structure = ChunkService(
        documents, ocr, SQLiteChunkRepository(database, analyzer.version), analyzer
    ).build(document_id, registered.ingestion_id)
    result.update(
        ingestion_id=registered.ingestion_id,
        structure_id=structure.structure_id,
        baseline_structure_id=baseline.structure_id,
        ocr_language="korean",
    )
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT p.pdf_page_number,b.block_id,b.text FROM page p "
            "JOIN document_block b ON p.page_id=b.page_id WHERE p.ingestion_id=? "
            "ORDER BY p.pdf_page_number,b.reading_order",
            (registered.ingestion_id,),
        ).fetchall()
    page_texts, block_pages = {}, {}
    for row in rows:
        number = row["pdf_page_number"]
        page_texts[number] = page_texts.get(number, "") + "\n" + row["text"]
        block_pages[row["block_id"]] = number
    save(output / f"{code}-ocr.json", page_texts)
    print(f"{code} indexing", flush=True)
    if not search.state(document_id, structure.structure_id)["active"]:
        search.build(search.enqueue(document_id, structure.structure_id))
    if not search.state(document_id, structure.structure_id)["active"]:
        raise RuntimeError(f"{code}: index_failed")
    save(output / f"{code}-prepared.json", result)
    if prepare_only:
        return result
    drafts = SQLiteDraftRepository(database)
    gateway = LocalQwenDraftGateway(ROOT / "data/models/Qwen3-4B", 120, adapter)
    generator = DraftService(drafts, search, gateway, 120)
    checkpoint = output / f"{code}-progress.json"
    if checkpoint.exists():
        result["questions"] = json.loads(checkpoint.read_text(encoding="utf-8"))["questions"]
    done = {q["id"] for q in result["questions"]}
    for question in document["questions"]:
        if question["id"] in done:
            continue
        tick = perf_counter()
        found = search.search(
            document_id,
            SearchRequest(structure_id=structure.structure_id, query=question["question"], top_k=5),
        )
        retrieved_blocks, hit_rows = [], []
        for rank, hit in enumerate(found["hits"], 1):
            chunk = hit["chunk"]
            pages = set()
            for block in chunk.sources:
                number = block_pages[block.block_id]
                pages.add(number)
                retrieved_blocks.append(block.model_dump(mode="json"))
            hit_rows.append(
                {"rank": rank, "pages": sorted(pages), "chunk": chunk.model_dump(mode="json")}
            )
        retrieved = ordered_page_texts(retrieved_blocks, block_pages)
        job = generator.enqueue(
            document_id,
            DraftRequest(
                structure_id=structure.structure_id,
                query=question["question"],
                title=question["id"],
                reviewed_only=False,
                top_k=5,
            ),
        )
        asyncio.run(generator.generate(job))
        generated = drafts.get(document_id, job["draft_id"])
        selected = ordered_page_texts(
            [item["block"] for item in (generated["result"] or {}).get("items", [])],
            block_pages,
        )
        with database.connect() as connection:
            metadata = json.loads(
                connection.execute(
                    "SELECT metadata_json FROM generation_run WHERE generation_run_id=?",
                    (job["generation_run_id"],),
                ).fetchone()[0]
            )
        expected_pages = set(question["evidence_pages"])
        record = {
            "id": question["id"],
            "question": question["question"],
            "answerable": question["answerable"],
            "expected_answer": question["expected_answer"],
            "gold_evidence": question["evidence"],
            "gold_review_status": question["review_status"],
            "ocr_quote_exact": evidence_matches(question, page_texts),
            "retrieved_quote_exact": evidence_matches(question, retrieved),
            "selected_quote_exact": evidence_matches(question, selected),
            "retrieved_pages": sorted(retrieved),
            "all_gold_pages_retrieved": expected_pages.issubset(retrieved)
            if expected_pages
            else None,
            "hits": hit_rows,
            "generation_metadata": metadata,
            "draft": generated,
            "outcome": outcome(
                question["answerable"], generated["status"], generated["error_code"]
            ),
            "seconds": round(perf_counter() - tick, 3),
        }
        result["questions"].append(record)
        save(checkpoint, result)
        print(f"{question['id']} {record['outcome']} {record['seconds']}s", flush=True)
    result["session_seconds"] = round(perf_counter() - started, 3)
    save(result_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ROOT / "data/evaluation-input/evaluation/questions_gold_draft.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--documents", nargs="+")
    parser.add_argument("--prepare-only", action="store_true", help="OCR·구조·색인까지만 실행")
    parser.add_argument("--adapter", type=Path, help="학습한 LoRA 어댑터 폴더")
    parser.add_argument(
        "--recover-interrupted",
        action="store_true",
        help="다른 평가 실행이 없을 때 중단 작업을 실패로 회수",
    )
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8-sig"))
    preflight = audit(dataset)
    if args.documents and set(args.documents) - {d["document_id"] for d in dataset["documents"]}:
        raise ValueError("평가 목록에 없는 문서 ID")
    args.output.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    manifest = args.output / "manifest.json"
    config = {
        "dataset_sha256": fingerprint,
        "top_k": 5,
        "reviewed_only": False,
        "ocr_language": "korean",
        "dpi": 150,
        "timeout": 120,
        "gold_status": dataset["gold_status"],
    }
    if args.adapter:
        # 기존 출력 폴더의 설정과 호환되도록 어댑터를 쓸 때만 기록한다.
        config["adapter"] = str(args.adapter.resolve())
    if manifest.exists() and json.loads(manifest.read_text(encoding="utf-8")) != config:
        raise ValueError("평가 설정이 다릅니다. 새 출력 폴더를 지정해 주세요.")
    save(manifest, config)
    save(args.output / "input-audit.json", preflight)
    print(
        json.dumps(
            {
                "documents": len(preflight["documents"]),
                "pages": sum(d["pages"] for d in preflight["documents"]),
                "questions": sum(d["questions"] for d in preflight["documents"]),
            }
        ),
        flush=True,
    )
    if args.audit_only:
        return
    # 평가 전용 저장소와 로컬 모델만 사용한다. 사용자 앱의 DB/검토 상태는 건드리지 않는다.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(ROOT / "data/ocr-validation/models")
    models = ROOT / "data/ocr-validation/models/official_models"
    settings = {
        "detection": str(models / "PP-OCRv5_mobile_det"),
        "recognition": str(models / "korean_PP-OCRv5_mobile_rec"),
    }
    if not all(Path(p).is_dir() for p in settings.values()):
        raise ValueError("로컬 OCR 모델 폴더가 없습니다.")
    from handover_ai.adapters.chroma import ChromaVectors
    from handover_ai.adapters.draft_repository import SQLiteDraftRepository
    from handover_ai.adapters.embedding import LocalE5Embedder
    from handover_ai.adapters.ocr_repository import SQLiteOcrRepository
    from handover_ai.adapters.search_repository import SQLiteSearchRepository
    from handover_ai.adapters.sqlite import SQLiteDatabase
    from handover_ai.services.search import SearchService

    database = SQLiteDatabase(args.output / "evaluation.db")
    database.initialize()
    if args.recover_interrupted:
        SQLiteOcrRepository(database).recover_interrupted()
        SQLiteSearchRepository(database).recover_interrupted()
        SQLiteDraftRepository(database).recover_interrupted()
    search = SearchService(
        SQLiteSearchRepository(database),
        LocalE5Embedder(ROOT / "data/models/multilingual-e5-small"),
        ChromaVectors(args.output / "chroma"),
    )
    for document in dataset["documents"]:
        if args.documents and document["document_id"] not in args.documents:
            continue
        run_document(
            document,
            args.output,
            search,
            database,
            settings,
            prepare_only=args.prepare_only,
            adapter=args.adapter,
        )


if __name__ == "__main__":
    main()

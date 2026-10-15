"""실행 장애를 무답 성공으로 세거나 다른 페이지의 문장을 근거로 세지 않는다."""

import hashlib
import importlib.util
from pathlib import Path

import pymupdf
import pytest

spec = importlib.util.spec_from_file_location(
    "real_evaluation", Path(__file__).parents[1] / "scripts/evaluate_real_documents.py"
)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


@pytest.mark.parametrize("error", ["model_timeout", "generation_failed", "no_eligible_evidence"])
def test_execution_errors_are_not_correct_abstentions(error):
    assert evaluation.outcome(False, "failed", error) == "execution_error"


def test_abstention_and_false_answers():
    assert evaluation.outcome(False, "failed", "no_relevant_evidence") == "correct_abstention"
    assert evaluation.outcome(True, "failed", "no_relevant_evidence") == "missed_answer"
    assert evaluation.outcome(False, "ready", None) == "false_answer"


def test_quote_matching_preserves_pages_numbers_and_punctuation():
    question = {"evidence": [{"pdf_page": 3, "quote": "48시간 안에 처리한다."}]}
    assert evaluation.evidence_matches(question, {3: "48시간\n안에 처리한다."}) == [True]
    assert evaluation.evidence_matches(question, {2: "48시간 안에 처리한다."}) == [False]
    assert evaluation.evidence_matches(question, {3: "49시간 안에 처리한다."}) == [False]


def test_empty_evidence_is_not_an_answer_score():
    assert evaluation.evidence_matches({"evidence": []}, {1: "무관한 내용"}) == []


def test_rank_order_and_duplicate_chunks_do_not_change_quote_coverage():
    blocks = [
        {"block_id": "B2", "reading_order": 1, "text": "안에 처리한다."},
        {"block_id": "B1", "reading_order": 0, "text": "48시간"},
        {"block_id": "B1", "reading_order": 0, "text": "48시간"},
    ]
    texts = evaluation.ordered_page_texts(blocks, {"B1": 3, "B2": 3})
    question = {"evidence": [{"pdf_page": 3, "quote": "48시간 안에 처리한다."}]}
    assert evaluation.evidence_matches(question, texts) == [True]


def test_audit_rejects_wrong_source_and_page(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    source = tmp_path / "data/evaluation-input/source.pdf"
    source.parent.mkdir(parents=True)
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(source)
    question = {
        "id": "D-Q1",
        "answerable": True,
        "expected_answer": "text",
        "evidence": [{"pdf_page": 1, "quote": "text"}],
        "evidence_pages": [1],
    }
    document = {
        "document_id": "D",
        "source_pdf_path": "data/evaluation-input/source.pdf",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "pages": 1,
        "questions": [question],
    }
    dataset = {"documents": [document], "gold_status": "draft"}
    assert (
        evaluation.audit(dataset)["documents"][0]["evidence"][0]["exact_in_embedded_text"] is False
    )
    document["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="해시"):
        evaluation.audit(dataset)
    document["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    question["evidence"][0]["pdf_page"] = 2
    question["evidence_pages"] = [2]
    with pytest.raises(ValueError, match="페이지"):
        evaluation.audit(dataset)

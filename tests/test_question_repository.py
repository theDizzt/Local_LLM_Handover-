"""문항 저장·검토·답안·QuizScore 입력 조회와 업무-문서 연결을 확인한다 (quiz-design.md 2단계)."""

import json
import sqlite3
from pathlib import Path

import pytest

from handover_ai.adapters.question_repository import SQLiteQuestionRepository
from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.adapters.workforce_repository import SQLiteWorkforceRepository
from handover_ai.domain import quiz
from handover_ai.domain import quiz_guard as guard
from handover_ai.domain.workforce import SyntheticDataset

ROOT = Path(__file__).parents[1]
DATASET = SyntheticDataset.model_validate(
    json.loads((ROOT / "datasets/records-v1/dataset.json").read_text(encoding="utf-8"))
)
MANUAL = DATASET.tasks[0].manual_references[0].document
TEXT = "기능분류는 정책분야, 정책영역, 대기능, 중기능, 소기능, 단위과제의 6단계로 구성된다."


@pytest.fixture
def database(tmp_path):
    database = SQLiteDatabase(tmp_path / "app.db")
    database.initialize()
    SQLiteWorkforceRepository(database).import_dataset(DATASET)
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO ingestion(ingestion_id,ocr_version,parser_version,status) "
            "VALUES ('ING-1','ocr','page','ready')"
        )
        connection.execute(
            "INSERT INTO document(document_id,file_name,page_count,file_sha256,source_uri,"
            "document_version,ingestion_id) VALUES ('DOC-1',?,1,'h','u',1,'ING-1')",
            (MANUAL,),
        )
        connection.execute(
            "INSERT INTO page(page_id,document_id,ingestion_id,pdf_page_number,image_uri,"
            "width_px,height_px,render_dpi) VALUES ('P-1','DOC-1','ING-1',10,'i',1,1,1)"
        )
        for n in range(1, 5):
            connection.execute(
                "INSERT INTO document_block(block_id,page_id,block_type,reading_order,text,"
                "bbox_json,ocr_confidence,structure_confidence) "
                "VALUES (?, 'P-1', 'text', ?, ?, '[0,0,1,1]', 0.9, 0.9)",
                (f"blk-{n}", n, TEXT if n == 1 else f"{n}단계 작업을 한다."),
            )
    return database


def short_question():
    raw = {
        "kind": "short_answer",
        "stem": "기능분류는 몇 단계로 구성되는가?",
        "answer": {"block": "B1", "quote": "6단계"},
        "evidence_blocks": ["B1"],
    }
    return guard.check("TASK-001", raw, {"B1": {"block_id": "blk-1", "text": TEXT}}).question


def choice_question():
    raw = {
        "kind": "multiple_choice",
        "stem": "기능분류의 단계 수는?",
        "answer": {"block": "B1", "quote": "6단계"},
        "distractors": ["4단계", "5단계", "7단계"],
        "evidence_blocks": ["B1"],
    }
    return guard.check("TASK-001", raw, {"B1": {"block_id": "blk-1", "text": TEXT}}).question


def test_save_records_evidence_versions_and_is_idempotent(database):
    repository = SQLiteQuestionRepository(database)
    question = short_question()
    assert repository.save(question, guard={"version": guard.VERSION}, generation_run_id="RUN-1")
    assert not repository.save(question)

    stored = repository.get(question.question_id)
    assert stored.question == question
    assert stored.revision == 1
    assert stored.generation_run_id == "RUN-1"
    [evidence] = stored.evidence
    assert evidence["document_id"] == "DOC-1"
    assert evidence["ingestion_id"] == "ING-1"
    assert evidence["pdf_page_number"] == 10
    assert len(evidence["text_sha256"]) == 64


def test_save_rejects_unknown_block_and_non_draft(database):
    repository = SQLiteQuestionRepository(database)
    question = short_question()
    with pytest.raises(ValueError, match="원문 Block"):
        repository.save(question.model_copy(update={"evidence_block_ids": ["blk-404"]}))
    with pytest.raises(ValueError, match="draft"):
        repository.save(question.model_copy(update={"status": "approved"}))
    assert repository.questions() == []


def test_review_transitions_and_conflicts(database):
    repository = SQLiteQuestionRepository(database)
    question = short_question()
    repository.save(question)
    qid = question.question_id

    approved = repository.review(
        qid, "approve", expected_revision=1, reviewer="검토자", accepted_answers=["여섯 단계", " "]
    )
    assert (approved.question.status, approved.revision) == ("approved", 2)
    assert approved.question.accepted_answers == ["여섯 단계"]
    assert approved.reviewed_by == "검토자"

    # 오래된 revision으로 저장하면 덮어쓰지 않는다.
    with pytest.raises(quiz.QuestionConflict):
        repository.review(qid, "retire", expected_revision=1, reviewer="다른 검토자")
    # 확정 문항은 다시 승인·반려하지 않고 retired로만 바꾼다.
    with pytest.raises(quiz.QuestionConflict):
        repository.review(qid, "reject", expected_revision=2, reviewer="검토자")
    retired = repository.review(qid, "retire", expected_revision=2, reviewer="검토자")
    assert retired.question.status == "retired"
    with pytest.raises(KeyError):
        repository.review("Q-none", "approve", expected_revision=1, reviewer="검토자")


def test_multiple_choice_needs_distractor_confirmation(database):
    repository = SQLiteQuestionRepository(database)
    question = choice_question()
    repository.save(question)
    with pytest.raises(quiz.InvalidReview):
        repository.review(question.question_id, "approve", expected_revision=1, reviewer="r")
    with pytest.raises(quiz.InvalidReview):
        repository.review(
            question.question_id,
            "approve",
            expected_revision=1,
            reviewer="r",
            confirmed_fields=["distractors"],
            accepted_answers=["6"],
        )
    assert repository.get(question.question_id).revision == 1
    stored = repository.review(
        question.question_id,
        "approve",
        expected_revision=1,
        reviewer="r",
        confirmed_fields=["distractors"],
    )
    assert stored.question.status == "approved"


def test_answers_feed_quiz_score_with_latest_attempt(database):
    repository = SQLiteQuestionRepository(database)
    short = short_question()
    steps = [quiz.Step(f"{n}단계 작업을 한다.", (f"blk-{n}",)) for n in range(2, 5)]
    order = quiz.ordering_question("TASK-002", "보존기간", steps)
    for question in (short, order):
        repository.save(question)

    # 확정 전 문항에는 답할 수 없다.
    with pytest.raises(quiz.QuestionConflict):
        repository.record_answer(
            short.question_id, "EMP-001", "6단계", quiz.Score(1, 1, "rule_exact")
        )
    for question in (short, order):
        repository.review(question.question_id, "approve", expected_revision=1, reviewer="r")

    repository.record_answer(
        short.question_id, "EMP-001", "5단계", quiz.score_answer(short, "5단계")
    )
    repository.record_answer(
        short.question_id, "EMP-001", "6단계", quiz.score_answer(short, "6단계")
    )
    a, b, c = order.answer_order
    repository.record_answer(
        order.question_id, "EMP-001", [b, c, a], quiz.score_answer(order, [b, c, a])
    )

    results = repository.latest_results("EMP-001")
    assert [(r.task_id, r.score) for r in results] == sorted([("TASK-001", 1.0), ("TASK-002", 0.5)])
    assert repository.latest_results("EMP-002") == []
    assert repository.quizzed_task_ids() == {"TASK-001", "TASK-002"}

    # 폐기한 문항의 답안은 QuizScore에서 빠진다.
    repository.review(order.question_id, "retire", expected_revision=2, reviewer="r")
    assert [r.task_id for r in repository.latest_results("EMP-001")] == ["TASK-001"]


def test_link_documents_by_file_name(database):
    repository = SQLiteWorkforceRepository(database)
    first = repository.link_documents(DATASET)
    assert first["linked_documents"] >= 1
    assert MANUAL not in first["unmatched_documents"]
    assert "DOC-1" in repository.task_documents("TASK-001")
    # 다시 실행해도 중복 연결하지 않는다.
    assert repository.link_documents(DATASET)["linked_documents"] == 0


def test_old_question_table_gets_new_columns(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        # quiz-v1 열이 추가되기 전 형식
        connection.executescript(
            "CREATE TABLE question (question_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
            "question_type TEXT NOT NULL, prompt TEXT NOT NULL, answer_json TEXT NOT NULL, "
            "difficulty TEXT NOT NULL, rubric_json TEXT NOT NULL, max_score REAL NOT NULL, "
            "evidence_json TEXT NOT NULL, rubric_version TEXT NOT NULL, created_at TEXT);"
            "INSERT INTO question VALUES ('Q-old','T','short_answer','p','{}','easy','[]',1,"
            "'[]','1.0',NULL);"
        )
    SQLiteDatabase(path).initialize()
    with SQLiteDatabase(path).connect() as connection:
        row = connection.execute("SELECT * FROM question WHERE question_id='Q-old'").fetchone()
    assert (row["status"], row["revision"], row["options_json"]) == ("draft", 1, "[]")

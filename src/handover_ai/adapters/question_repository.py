"""[읽기 안내] 업무 이해도 문항·검토·답안을 SQLite question/evaluation 테이블에 저장하고 읽는다.

문항 근거는 SQLite에 있는 Block만 허용한다. 저장할 때 Block의 문서·OCR 버전·페이지·본문 해시를
함께 남겨 OCR 재처리 후에도 과거 문항이 어떤 원문에서 나왔는지 추적한다.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass

from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.domain import quiz


@dataclass(frozen=True, slots=True)
class StoredQuestion:
    question: quiz.Question
    revision: int
    evidence: list[dict]
    guard: dict
    generation_run_id: str | None
    reviewed_by: str | None
    reviewed_at: str | None
    review_note: str | None


def _stored(row) -> StoredQuestion:
    answer = json.loads(row["answer_json"])
    question = quiz.Question(
        question_id=row["question_id"],
        task_id=row["task_id"],
        kind=row["question_type"],
        stem=row["prompt"],
        options=json.loads(row["options_json"]),
        answer=answer.get("answer"),
        answer_order=answer.get("answer_order", []),
        accepted_answers=answer.get("accepted_answers", []),
        evidence_block_ids=[e["block_id"] for e in json.loads(row["evidence_json"])],
        max_score=row["max_score"],
        status=row["status"],
        review_required_fields=json.loads(row["review_required_json"]),
        quiz_version=row["rubric_version"],
    )
    return StoredQuestion(
        question,
        row["revision"],
        json.loads(row["evidence_json"]),
        json.loads(row["guard_json"]),
        row["generation_run_id"],
        row["reviewed_by"],
        row["reviewed_at"],
        row["review_note"],
    )


def _answer_json(question: quiz.Question) -> str:
    return json.dumps(
        {
            "answer": question.answer,
            "answer_order": question.answer_order,
            "accepted_answers": question.accepted_answers,
        },
        ensure_ascii=False,
    )


class SQLiteQuestionRepository:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def _evidence(self, connection, block_ids):
        # 모델이나 호출자가 준 ID를 그대로 믿지 않는다. 실제 Block이 있어야 근거로 저장한다.
        placeholders = ",".join("?" * len(block_ids))
        rows = connection.execute(
            "SELECT b.block_id, b.text, p.document_id, p.ingestion_id, p.pdf_page_number "
            "FROM document_block b JOIN page p USING(page_id) "
            f"WHERE b.block_id IN ({placeholders})",
            block_ids,
        ).fetchall()
        found = {row["block_id"]: row for row in rows}
        missing = [block_id for block_id in block_ids if block_id not in found]
        if missing:
            raise ValueError(f"원문 Block을 찾을 수 없습니다: {', '.join(missing)}")
        return [
            {
                "block_id": block_id,
                "document_id": found[block_id]["document_id"],
                "ingestion_id": found[block_id]["ingestion_id"],
                "pdf_page_number": found[block_id]["pdf_page_number"],
                "text_sha256": hashlib.sha256(found[block_id]["text"].encode()).hexdigest(),
            }
            for block_id in block_ids
        ]

    def save(
        self,
        question: quiz.Question,
        *,
        guard: dict | None = None,
        generation_run_id: str | None = None,
    ) -> bool:
        """draft로 저장한다. 같은 ID가 이미 있으면 기존 검토 상태를 지키고 False를 돌려준다."""
        if question.status != "draft":
            raise ValueError("새 문항은 draft로만 저장합니다.")
        with self.database.connect() as connection:
            evidence = self._evidence(connection, question.evidence_block_ids)
            cursor = connection.execute(
                """
                INSERT INTO question (
                    question_id, task_id, question_type, prompt, answer_json, difficulty,
                    rubric_json, max_score, evidence_json, rubric_version, status,
                    options_json, review_required_json, guard_json, generation_run_id
                ) VALUES (?, ?, ?, ?, ?, 'unrated', '[]', ?, ?, ?, 'draft', ?, ?, ?, ?)
                ON CONFLICT(question_id) DO NOTHING
                """,
                (
                    question.question_id,
                    question.task_id,
                    question.kind,
                    question.stem,
                    _answer_json(question),
                    question.max_score,
                    json.dumps(evidence),
                    question.quiz_version,
                    json.dumps(question.options, ensure_ascii=False),
                    json.dumps(question.review_required_fields),
                    json.dumps(guard or {}, ensure_ascii=False),
                    generation_run_id,
                ),
            )
            return cursor.rowcount == 1

    def get(self, question_id: str) -> StoredQuestion:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM question WHERE question_id=?", (question_id,)
            ).fetchone()
        if row is None:
            raise KeyError("문항을 찾을 수 없습니다.")
        return _stored(row)

    def questions(
        self, task_id: str | None = None, status: str | None = None
    ) -> list[StoredQuestion]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM question WHERE (? IS NULL OR task_id=?) "
                "AND (? IS NULL OR status=?) ORDER BY task_id, created_at, question_id",
                (task_id, task_id, status, status),
            ).fetchall()
        return [_stored(row) for row in rows]

    def review(
        self,
        question_id: str,
        action: quiz.ReviewAction,
        *,
        expected_revision: int,
        reviewer: str,
        note: str | None = None,
        confirmed_fields: list[str] = (),
        accepted_answers: list[str] | None = None,
    ) -> StoredQuestion:
        with self.database.connect() as connection:
            # revision 비교와 갱신을 한 쓰기 잠금 안에서 해 동시 검토가 서로 덮어쓰지 않게 한다.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM question WHERE question_id=?", (question_id,)
            ).fetchone()
            if row is None:
                raise KeyError("문항을 찾을 수 없습니다.")
            current = _stored(row)
            if current.revision != expected_revision:
                raise quiz.QuestionConflict("다른 검토가 먼저 저장되었습니다. 새로 불러와 주세요.")
            status = quiz.review_status(current.question, action, list(confirmed_fields))
            question = current.question
            if accepted_answers is not None:
                # 동의 표현은 채점 기준을 바꾸므로 확정 전(승인과 동시)에만 정한다.
                if action != "approve" or question.kind != "short_answer":
                    raise quiz.InvalidReview("동의 표현은 단답 문항을 승인할 때만 정합니다.")
                cleaned = list(dict.fromkeys(a.strip() for a in accepted_answers if a.strip()))
                question = question.model_copy(update={"accepted_answers": cleaned})
            connection.execute(
                "UPDATE question SET status=?, answer_json=?, revision=revision+1, "
                "reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP, review_note=? WHERE question_id=?",
                (status, _answer_json(question), reviewer, note, question_id),
            )
            row = connection.execute(
                "SELECT * FROM question WHERE question_id=?", (question_id,)
            ).fetchone()
        return _stored(row)

    def record_answer(self, question_id: str, employee_id: str, answer, score: quiz.Score) -> str:
        evaluation_id = "EVAL-" + uuid.uuid4().hex
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status FROM question WHERE question_id=?", (question_id,)
            ).fetchone()
            if row is None:
                raise KeyError("문항을 찾을 수 없습니다.")
            if row["status"] != "approved":
                raise quiz.QuestionConflict("확정된 문항에만 답안을 제출할 수 있습니다.")
            connection.execute(
                """
                INSERT INTO evaluation (
                    evaluation_id, question_id, employee_id, answer, python_score,
                    python_score_mode, status, scoring_policy_version
                ) VALUES (?, ?, ?, ?, ?, ?, 'scored', ?)
                """,
                (
                    evaluation_id,
                    question_id,
                    employee_id,
                    json.dumps(answer, ensure_ascii=False),
                    100 * score.score / score.max_score,
                    score.mode,
                    quiz.VERSION,
                ),
            )
        return evaluation_id

    def latest_results(self, employee_id: str) -> list[quiz.AnswerResult]:
        """approved 문항별 가장 최근 답안. 다시 풀면 마지막 결과만 QuizScore에 쓴다."""
        with self.database.connect() as connection:
            # created_at은 초 단위라 같은 초의 답안을 구분하지 못한다. 삽입 순서(rowid)를 쓴다.
            rows = connection.execute(
                """
                SELECT q.task_id, q.question_id, q.max_score, e.python_score
                FROM evaluation e JOIN question q USING(question_id)
                WHERE e.employee_id=? AND q.status='approved' AND e.rowid = (
                    SELECT max(x.rowid) FROM evaluation x
                    WHERE x.employee_id=e.employee_id AND x.question_id=e.question_id
                )
                ORDER BY q.task_id, q.question_id
                """,
                (employee_id,),
            ).fetchall()
        return [
            quiz.AnswerResult(
                row["task_id"],
                row["question_id"],
                row["max_score"] * row["python_score"] / 100,
                row["max_score"],
            )
            for row in rows
        ]

    def quizzed_task_ids(self) -> set[str]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT task_id FROM question WHERE status='approved'"
            ).fetchall()
        return {row["task_id"] for row in rows}

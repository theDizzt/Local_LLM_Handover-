"""업무 적합도·추천·분배·준비도 계산과 API를 확인한다 (develop.md 5.4: 수식, 경계값, 분모 0,
동점, 추천 개수)."""

import importlib.util
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.config import load_settings
from handover_ai.domain import assignment as a
from handover_ai.main import create_app

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("load_workforce", ROOT / "scripts/load_workforce.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


def task(task_id, minutes=100, skills=None, reference=12, level=50):
    return a.TaskInput(
        task_id=task_id,
        title=task_id,
        criticality=level,
        failure_impact=level,
        dependency=level,
        frequency=level,
        urgency=level,
        required_skills=skills if skills is not None else {"s": 4},
        expected_minutes=minutes,
        reference_experience_months=reference,
    )


def person(employee_id="E1", skill=4, months=12, minutes=1000):
    return a.EmployeeInput(employee_id, employee_id, {"s": skill}, months, minutes)


def test_fit_formula_and_caps():
    # SkillMatch 2/4=50, Experience 6/12=50, Availability 50/100=50 → Fit 50
    result = a.score(task("T1"), person(skill=2, months=6), remaining=50)
    assert (result.skill_match, result.experience, result.availability) == (50, 50, 50)
    assert result.fit == pytest.approx(50)
    assert result.impact == pytest.approx(50)
    assert result.assignment_score == pytest.approx(25)
    assert result.risk_gap == pytest.approx(25)

    # 요구보다 많은 역량·경력·시간은 100에서 자른다.
    capped = a.score(task("T1"), person(skill=5, months=99), remaining=10_000)
    assert capped.fit == pytest.approx(100) and capped.risk_gap == pytest.approx(0)


@pytest.mark.parametrize(
    ("item", "employee", "remaining", "reason"),
    [
        (task("T1", skills={}), person(), 1000, "required_skills_missing"),
        (task("T1", reference=0), person(), 1000, "experience_reference_missing"),
        (task("T1"), person(months=None), 1000, "experience_reference_missing"),
        (task("T1"), person(minutes=None), None, "available_minutes_missing"),
    ],
)
def test_missing_inputs_return_none_with_reason(item, employee, remaining, reason):
    result = a.score(item, employee, remaining)
    assert result.fit is None and result.assignment_score is None and result.risk_gap is None
    assert reason in result.reasons


def test_recommend_breaks_ties_by_task_id_and_validates_count():
    tasks = [task("T3"), task("T1"), task("T2")]
    assert [s.task_id for s in a.recommend(tasks, person(), 2)] == ["T1", "T2"]
    for count in (0, a.MAX_RECOMMENDATIONS + 1):
        with pytest.raises(ValueError):
            a.recommend(tasks, person(), count)


def test_recommend_uses_remaining_minutes():
    # 큰 업무를 먼저 맡으면 남은 시간이 줄어 다음 업무의 Availability가 낮아진다.
    tasks = [task("T1", minutes=800, level=90), task("T2", minutes=400, level=80)]
    first, second = a.recommend(tasks, person(minutes=1000), 2)
    assert (first.task_id, first.availability, first.remaining_minutes) == ("T1", 100, 1000)
    assert (second.task_id, second.remaining_minutes) == ("T2", 200)
    assert second.availability == pytest.approx(50)
    # 교육 대상은 일정이 아닌 역량 차이를 보므로 전체 가용 시간 기준이다.
    assert all(s.availability == 100 for s in a.static_scores(tasks, person(minutes=1000)))


def test_readiness_parts_and_missing_quiz():
    tasks = [task("T1", level=90), task("T2", level=40)]
    result = a.readiness(tasks, person(skill=2, months=12))
    # Fit = 0.55*50 + 0.30*100 + 0.15*100 = 72.5, Impact 80 이상은 T1뿐
    assert result.critical_task_count == 1
    assert result.critical_task_fit == pytest.approx(72.5)
    assert result.task_coverage == pytest.approx(100)
    assert result.readiness is None and result.reasons == ("quiz_score_missing",)

    scored = a.readiness(tasks, person(skill=2, months=12), quiz_score=80)
    assert scored.readiness == pytest.approx(0.5 * 80 + 0.35 * 72.5 + 0.15 * 100)

    low = a.readiness([task("T2", level=40)], person(skill=1), quiz_score=80)
    assert low.readiness is None and "no_critical_tasks" in low.reasons


def test_allocate_assigns_each_task_once_within_capacity():
    tasks = [task("T1", minutes=600, level=90), task("T2", minutes=600), task("T3", minutes=600)]
    strong, weak = person("E1", skill=4, minutes=700), person("E2", skill=2, minutes=700)
    result = a.allocate(tasks, [strong, weak])

    assert [(s.task_id, s.employee_id) for s in result.assignments] == [
        ("T1", "E1"),
        ("T2", "E2"),
    ]
    assert result.unassigned == [("T3", "capacity_exceeded")]
    assert result.remaining_minutes == {"E1": 100, "E2": 100}


@pytest.fixture
def client(tmp_path):
    settings = replace(
        load_settings(),
        database_path=tmp_path / "test.db",
        document_store_path=tmp_path / "documents",
        max_upload_bytes=1024,
    )
    loader.main(["--database", str(settings.database_path)])
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


def test_api_on_records_dataset(client):
    employees = client.get("/api/v1/employees").json()
    assert [e["employee_id"] for e in employees] == ["EMP-001", "EMP-002", "EMP-003"]

    body = client.get("/api/v1/employees/EMP-001/recommendations?n=3").json()
    assert [r["task_id"] for r in body["recommendations"]] == ["TASK-001", "TASK-019", "TASK-002"]
    # 재난 피해 보고는 보존처리 담당 이도윤이 더 적합하다는 표시가 붙는다.
    assert body["recommendations"][1]["better_fit_employee_id"] == "EMP-002"
    assert body["training_targets"][0]["task_id"] == "TASK-020"
    assert body["total_expected_minutes"] == sum(
        r["expected_minutes"] for r in body["recommendations"]
    )

    readiness = client.get("/api/v1/employees/EMP-002/readiness").json()
    assert readiness["readiness"] is None and readiness["reasons"] == ["quiz_score_missing"]
    assert readiness["task_coverage"] == 100

    plan = client.get("/api/v1/assignments/plan").json()
    assigned = [row["task_id"] for row in plan["assignments"]]
    assert len(assigned) == len(set(assigned)) == 25 and plan["unassigned"] == []
    assert all(minutes >= 0 for minutes in plan["remaining_minutes"].values())

    assert client.get("/api/v1/employees/EMP-100/recommendations").status_code == 404
    assert client.get("/api/v1/employees/EMP-001/recommendations?n=21").status_code == 422


def test_loader_is_idempotent_and_migrates_old_tables(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        # 열이 추가되기 전 형식의 테이블
        connection.executescript(
            "CREATE TABLE task (task_id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "criticality REAL, failure_impact REAL, dependency_score REAL, frequency REAL, "
            "urgency REAL, required_skills_json TEXT NOT NULL DEFAULT '{}', "
            "expected_minutes INTEGER, created_at TEXT);"
            "CREATE TABLE employee (employee_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "skills_json TEXT NOT NULL DEFAULT '{}', experience_months INTEGER, "
            "available_minutes INTEGER, created_at TEXT);"
        )
    for _ in range(2):
        counts = loader.main(["--database", str(path)])
        assert (counts["tasks"], counts["employees"], counts["linked_documents"]) == (25, 4, 0)
        # 문서를 등록하지 않은 DB이므로 매뉴얼은 모두 미연결로 보고된다.
        assert counts["unmatched_documents"]
    with SQLiteDatabase(path).connect() as connection:
        assert connection.execute("SELECT count(*) FROM task").fetchone()[0] == 25
        assert (
            connection.execute("SELECT count(*) FROM employee WHERE role='receiver'").fetchone()[0]
            == 3
        )

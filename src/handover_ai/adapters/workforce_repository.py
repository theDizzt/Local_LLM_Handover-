"""[읽기 안내] 업무·직원 정답 데이터를 SQLite task/employee 테이블에 저장하고 읽는다."""

import json

from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.domain.assignment import EmployeeInput, TaskInput
from handover_ai.domain.workforce import SyntheticDataset


class SQLiteWorkforceRepository:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def import_dataset(self, dataset: SyntheticDataset) -> dict[str, int]:
        """같은 ID는 갱신한다. 문항 등 다른 테이블이 업무를 참조할 수 있어 삭제하지 않는다."""
        with self.database.connect() as connection:
            for task in dataset.tasks:
                connection.execute(
                    """
                    INSERT INTO task (
                        task_id, title, criticality, failure_impact, dependency_score,
                        frequency, urgency, required_skills_json, expected_minutes,
                        reference_experience_months
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        title=excluded.title, criticality=excluded.criticality,
                        failure_impact=excluded.failure_impact,
                        dependency_score=excluded.dependency_score,
                        frequency=excluded.frequency, urgency=excluded.urgency,
                        required_skills_json=excluded.required_skills_json,
                        expected_minutes=excluded.expected_minutes,
                        reference_experience_months=excluded.reference_experience_months
                    """,
                    (
                        task.task_id,
                        task.title,
                        task.criticality,
                        task.failure_impact,
                        task.dependency_score,
                        task.frequency,
                        task.urgency,
                        json.dumps(task.required_skills),
                        task.expected_minutes,
                        task.reference_experience_months,
                    ),
                )
            for employee in dataset.employees:
                connection.execute(
                    """
                    INSERT INTO employee (
                        employee_id, name, skills_json, experience_months,
                        available_minutes, role
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(employee_id) DO UPDATE SET
                        name=excluded.name, skills_json=excluded.skills_json,
                        experience_months=excluded.experience_months,
                        available_minutes=excluded.available_minutes, role=excluded.role
                    """,
                    (
                        employee.employee_id,
                        employee.name,
                        json.dumps(employee.skills),
                        employee.experience_months,
                        employee.available_minutes,
                        employee.role,
                    ),
                )
        return {"tasks": len(dataset.tasks), "employees": len(dataset.employees)}

    def tasks(self) -> list[TaskInput]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM task ORDER BY task_id").fetchall()
        return [
            TaskInput(
                task_id=row["task_id"],
                title=row["title"],
                criticality=row["criticality"],
                failure_impact=row["failure_impact"],
                dependency=row["dependency_score"],
                frequency=row["frequency"],
                urgency=row["urgency"],
                required_skills=json.loads(row["required_skills_json"]),
                expected_minutes=row["expected_minutes"],
                reference_experience_months=row["reference_experience_months"],
            )
            for row in rows
        ]

    def employees(self, role: str | None = "receiver") -> list[EmployeeInput]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM employee WHERE ? IS NULL OR role = ? ORDER BY employee_id",
                (role, role),
            ).fetchall()
        return [
            EmployeeInput(
                employee_id=row["employee_id"],
                name=row["name"],
                skills=json.loads(row["skills_json"]),
                experience_months=row["experience_months"],
                available_minutes=row["available_minutes"],
            )
            for row in rows
        ]

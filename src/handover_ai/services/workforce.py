"""[읽기 안내] 저장된 업무·인수자로 추천, 교육 대상, 준비도, 전체 분배안을 만든다."""

from dataclasses import asdict

from handover_ai.domain import assignment

NOTICE = "점수는 인수인계 판단을 돕는 참고 지표입니다. 실제 배정과 완료 판단은 담당자가 확정합니다."


def _rounded(value):
    return round(value, 2) if isinstance(value, float) else value


def _row(score: assignment.TaskScore) -> dict:
    return {key: _rounded(value) for key, value in asdict(score).items()}


class WorkforceService:
    def __init__(self, repository):
        self.repository = repository

    def employees(self):
        return [asdict(employee) for employee in self.repository.employees()]

    def _employee(self, employee_id):
        for employee in self.repository.employees():
            if employee.employee_id == employee_id:
                return employee
        raise KeyError(employee_id)

    def recommendations(self, employee_id, n):
        employee, tasks = self._employee(employee_id), self.repository.tasks()
        picked = assignment.recommend(tasks, employee, n)
        best = {}
        # 다른 인수자가 같은 업무에 더 적합하면 함께 알려 준다. 배정 충돌을 사람이 판단하게 한다.
        for other in self.repository.employees():
            for item in assignment.static_scores(tasks, other):
                if item.fit is not None and (
                    item.task_id not in best or item.fit > best[item.task_id].fit
                ):
                    best[item.task_id] = item
        rows = []
        for item in picked:
            row = _row(item)
            top = best.get(item.task_id)
            row["better_fit_employee_id"] = (
                top.employee_id if top and top.employee_id != employee_id else None
            )
            rows.append(row)
        return {
            "policy_version": assignment.POLICY_VERSION,
            "employee": asdict(employee),
            "recommendations": rows,
            "total_expected_minutes": sum(item.expected_minutes for item in picked),
            "training_targets": [
                _row(item) for item in assignment.training_targets(tasks, employee, n)
            ],
            "notice": NOTICE,
        }

    def readiness(self, employee_id):
        employee = self._employee(employee_id)
        result = assignment.readiness(self.repository.tasks(), employee)
        return {
            "policy_version": assignment.POLICY_VERSION,
            **{key: _rounded(value) for key, value in asdict(result).items()},
            "notice": NOTICE,
        }

    def allocation(self):
        employees = self.repository.employees()
        result = assignment.allocate(self.repository.tasks(), employees)
        return {
            "policy_version": assignment.POLICY_VERSION,
            "assignments": [_row(item) for item in result.assignments],
            "unassigned": [
                {"task_id": task_id, "reason": reason} for task_id, reason in result.unassigned
            ],
            "remaining_minutes": result.remaining_minutes,
            "low_fit_task_ids": [
                item.task_id
                for item in result.assignments
                if item.fit is not None and item.fit < assignment.COVERED_FIT
            ],
            "notice": NOTICE,
        }

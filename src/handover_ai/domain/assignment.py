"""[읽기 안내] 업무 적합도·배정·위험·준비도를 계산하는 순수 규칙이다 (develop.md 5.1.1).

Impact·SkillMatch는 scoring.py를 재사용한다. 계획서 수식에 두 가지를 정했다.

- Availability: 업무 하나와 월 가용 시간을 비교하면 거의 항상 100이 되므로, 추천·배정에서는
  앞서 고른 업무의 시간을 뺀 **남은 시간**으로 계산한다(누적 방식).
- RiskGap·준비도는 일정이 아니라 역량 차이를 보는 지표이므로 전체 가용 시간 기준 Fit을 쓴다.

계산할 수 없는 값은 0으로 채우지 않고 None과 사유 코드를 돌려준다.
"""

from dataclasses import dataclass, field

from handover_ai.domain.scoring import ImpactInput, calculate_impact, calculate_skill_match

POLICY_VERSION = "assignment-v1"
FIT_WEIGHTS = {"skill_match": 0.55, "experience": 0.30, "availability": 0.15}
READINESS_WEIGHTS = {"quiz": 0.50, "critical_task_fit": 0.35, "task_coverage": 0.15}
CRITICAL_IMPACT = 80
COVERED_FIT = 60
DEFAULT_RECOMMENDATIONS = 5
MAX_RECOMMENDATIONS = 20


@dataclass(frozen=True, slots=True)
class TaskInput:
    task_id: str
    title: str
    criticality: float
    failure_impact: float
    dependency: float
    frequency: float
    urgency: float
    required_skills: dict[str, float]
    expected_minutes: int
    reference_experience_months: int | None


@dataclass(frozen=True, slots=True)
class EmployeeInput:
    employee_id: str
    name: str
    skills: dict[str, float]
    experience_months: int | None
    available_minutes: int | None


@dataclass(frozen=True, slots=True)
class TaskScore:
    task_id: str
    title: str
    employee_id: str
    impact: float
    skill_match: float | None
    experience: float | None
    availability: float | None
    fit: float | None
    assignment_score: float | None
    risk_gap: float | None
    expected_minutes: int
    remaining_minutes: int | None
    reasons: tuple[str, ...] = field(default=())


def impact(task: TaskInput) -> float:
    return calculate_impact(
        ImpactInput(
            task.criticality, task.failure_impact, task.dependency, task.frequency, task.urgency
        )
    )


def experience_score(months, reference):
    # 기준 경험 기간 대비 비율, 100에서 자른다. 기준이 없거나 0이면 계산하지 않는다.
    if months is None or not reference:
        return None
    return min(months / reference, 1) * 100


def availability_score(remaining, expected):
    if remaining is None or expected <= 0:
        return None
    return min(max(remaining, 0) / expected, 1) * 100


def score(task: TaskInput, employee: EmployeeInput, remaining: int | None) -> TaskScore:
    """remaining: 이 업무를 맡기 전 남은 월 가용 시간(분)."""
    reasons = []
    skill = calculate_skill_match(task.required_skills, employee.skills)
    if skill is None:
        reasons.append("required_skills_missing")
    experience = experience_score(employee.experience_months, task.reference_experience_months)
    if experience is None:
        reasons.append("experience_reference_missing")
    availability = availability_score(remaining, task.expected_minutes)
    if availability is None:
        reasons.append("available_minutes_missing")
    weight = impact(task)
    fit = assignment = risk = None
    if not reasons:
        fit = (
            FIT_WEIGHTS["skill_match"] * skill
            + FIT_WEIGHTS["experience"] * experience
            + FIT_WEIGHTS["availability"] * availability
        )
        assignment = weight * fit / 100
        risk = weight * (1 - fit / 100)
    return TaskScore(
        task.task_id,
        task.title,
        employee.employee_id,
        weight,
        skill,
        experience,
        availability,
        fit,
        assignment,
        risk,
        task.expected_minutes,
        remaining,
        tuple(reasons),
    )


def _best(scores):
    # AssignmentScore 내림차순, 같으면 task_id 오름차순. 계산 불가 업무는 고르지 않는다.
    valid = [s for s in scores if s.assignment_score is not None]
    return min(valid, key=lambda s: (-s.assignment_score, s.task_id, s.employee_id), default=None)


def recommend(tasks, employee: EmployeeInput, n=DEFAULT_RECOMMENDATIONS):
    """한 인수자에게 맡길 업무 n개를 고른다.

    고를 때마다 그 업무의 월 소요 시간을 남은 가용 시간에서 빼고 나머지 업무를 다시 계산한다.
    따라서 시간이 많이 드는 업무를 먼저 맡으면 이후 업무의 Availability가 낮아진다.
    """
    if not 1 <= n <= MAX_RECOMMENDATIONS:
        raise ValueError(f"추천 수는 1~{MAX_RECOMMENDATIONS} 사이여야 합니다.")
    remaining, picked, pool = employee.available_minutes, [], list(tasks)
    while pool and len(picked) < n:
        best = _best(score(task, employee, remaining) for task in pool)
        if best is None:
            break
        picked.append(best)
        pool = [task for task in pool if task.task_id != best.task_id]
        remaining = None if remaining is None else remaining - best.expected_minutes
    return picked


def static_scores(tasks, employee: EmployeeInput):
    """전체 가용 시간 기준 점수. 교육 대상(RiskGap)과 준비도 계산에 쓴다."""
    return [score(task, employee, employee.available_minutes) for task in tasks]


def training_targets(tasks, employee: EmployeeInput, n=DEFAULT_RECOMMENDATIONS):
    valid = [s for s in static_scores(tasks, employee) if s.risk_gap is not None]
    return sorted(valid, key=lambda s: (-s.risk_gap, s.task_id))[:n]


@dataclass(frozen=True, slots=True)
class Readiness:
    employee_id: str
    readiness: float | None
    quiz_score: float | None
    critical_task_fit: float | None
    task_coverage: float | None
    critical_task_count: int
    reasons: tuple[str, ...]


def readiness(tasks, employee: EmployeeInput, quiz_score: float | None = None) -> Readiness:
    scores = static_scores(tasks, employee)
    reasons = []
    if any(s.fit is None for s in scores):
        reasons.append("task_fit_missing")
    critical = [s for s in scores if s.impact >= CRITICAL_IMPACT and s.fit is not None]
    critical_fit = sum(s.fit for s in critical) / len(critical) if critical else None
    if critical_fit is None:
        reasons.append("no_critical_tasks")
    total = sum(s.impact for s in scores)
    coverage = (
        100 * sum(s.impact for s in scores if s.fit is not None and s.fit >= COVERED_FIT) / total
        if total > 0
        else None
    )
    if coverage is None:
        reasons.append("total_impact_zero")
    if quiz_score is None:
        reasons.append("quiz_score_missing")
    value = None
    if not reasons:
        value = (
            READINESS_WEIGHTS["quiz"] * quiz_score
            + READINESS_WEIGHTS["critical_task_fit"] * critical_fit
            + READINESS_WEIGHTS["task_coverage"] * coverage
        )
    return Readiness(
        employee.employee_id,
        value,
        quiz_score,
        critical_fit,
        coverage,
        len(critical),
        tuple(reasons),
    )


@dataclass(frozen=True, slots=True)
class Allocation:
    assignments: list[TaskScore]
    unassigned: list[tuple[str, str]]
    remaining_minutes: dict[str, int | None]


def allocate(tasks, employees) -> Allocation:
    """모든 업무를 인수자 한 명씩에게 나누는 분배안 (계획서 외 확장).

    남은 업무와 인수자의 모든 조합에서 AssignmentScore가 가장 큰 조합부터 확정한다(탐욕법).
    남은 가용 시간이 업무 소요 시간보다 적은 인수자에게는 배정하지 않는다. 최적해를 보장하지
    않지만 같은 입력이면 같은 결과가 나오고 배정 순서를 그대로 설명할 수 있다.
    """
    remaining = {e.employee_id: e.available_minutes for e in employees}
    pool, assignments = list(tasks), []
    while pool:
        candidates = [
            score(task, employee, remaining[employee.employee_id])
            for task in pool
            for employee in employees
            if remaining[employee.employee_id] is not None
            and remaining[employee.employee_id] >= task.expected_minutes
        ]
        best = _best(candidates)
        if best is None:
            break
        assignments.append(best)
        remaining[best.employee_id] -= best.expected_minutes
        pool = [task for task in pool if task.task_id != best.task_id]
    unassigned = []
    for task in sorted(pool, key=lambda t: t.task_id):
        fits = any(r is not None and r >= task.expected_minutes for r in remaining.values())
        unassigned.append((task.task_id, "score_unavailable" if fits else "capacity_exceeded"))
    return Allocation(assignments, unassigned, remaining)

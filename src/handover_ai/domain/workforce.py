# [읽기 안내] 실험용 업무·인계자·인수자 정답 데이터(Ground Truth)의 형식과 참조 규칙이다.
# 점수 계산은 하지 않는다. 0~100 척도와 1~5 역량 수준만 보장하고 계산은 scoring이 맡는다.
from datetime import date
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

Score = Annotated[float, Field(ge=0, le=100)]


class SkillDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(pattern=r"^[a-z_]+$")
    name: str = Field(min_length=1)


class ManualReference(BaseModel):
    # 업무 내용이 실제 매뉴얼의 어느 문장에 근거하는지 기록한다. 페이지는 PDF 물리 페이지다.
    model_config = ConfigDict(extra="forbid")

    document: str = Field(min_length=1)
    pdf_page: int = Field(ge=1)
    quote: str = Field(min_length=1)


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=r"^TASK-\d{3}$")
    handover_id: str
    title: str = Field(min_length=1)
    category: str = Field(min_length=1)
    description: str = Field(min_length=1)
    cycle: str = Field(min_length=1)
    criticality: Score
    failure_impact: Score
    dependency_score: Score
    frequency: Score
    urgency: Score
    # 월 평균 소요 시간이다. 인수자의 available_minutes와 같은 월 단위로 비교한다.
    expected_minutes: int = Field(gt=0)
    # Experience 정규화의 기준값이다. 이 기간 이상이면 경험 점수 상한에 도달한다.
    reference_experience_months: int = Field(gt=0)
    required_skills: dict[str, int] = Field(min_length=1)
    procedure: list[str] = Field(min_length=1)
    cautions: list[str]
    troubleshooting: list[str]
    # 비어 있으면 매뉴얼 근거 없이 가상 기관의 내부 기준으로 작성한 업무다.
    manual_references: list[ManualReference]

    @model_validator(mode="after")
    def validate_skill_levels(self):
        if any(not 1 <= level <= 5 for level in self.required_skills.values()):
            raise ValueError(f"{self.task_id}: 요구 역량 수준은 1~5여야 합니다.")
        return self


class EmployeeRole(StrEnum):
    giver = "giver"
    receiver = "receiver"


class EmployeeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: str = Field(pattern=r"^EMP-\d{3}$")
    name: str = Field(min_length=1)
    role: EmployeeRole
    position: str = Field(min_length=1)
    skills: dict[str, int]
    experience_months: int = Field(ge=0)
    # 월 단위 인계 가능 시간이다.
    available_minutes: int = Field(ge=0)
    previous_tasks: list[str]

    @model_validator(mode="after")
    def validate_skill_levels(self):
        if any(not 0 <= level <= 5 for level in self.skills.values()):
            raise ValueError(f"{self.employee_id}: 보유 역량 수준은 0~5여야 합니다.")
        return self


class HandoverPacket(BaseModel):
    # 업무 묶음 하나가 인수인계서 PDF 한 개가 된다.
    model_config = ConfigDict(extra="forbid")

    handover_id: str = Field(pattern=r"^HO-[A-Z]$")
    title: str = Field(min_length=1)
    giver_id: str
    handover_date: date
    reason: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class SyntheticDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version: str = Field(min_length=1)
    organization: str = Field(min_length=1)
    department: str = Field(min_length=1)
    review_status: str = Field(min_length=1)
    skills: list[SkillDefinition] = Field(min_length=1)
    employees: list[EmployeeRecord] = Field(min_length=1)
    handovers: list[HandoverPacket] = Field(min_length=1)
    tasks: list[TaskRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_references(self):
        errors = []
        for label, ids in [
            ("역량", [item.skill_id for item in self.skills]),
            ("직원", [item.employee_id for item in self.employees]),
            ("인수인계서", [item.handover_id for item in self.handovers]),
            ("업무", [item.task_id for item in self.tasks]),
        ]:
            duplicates = sorted({item for item in ids if ids.count(item) > 1})
            if duplicates:
                errors.append(f"중복 {label} ID: {', '.join(duplicates)}")

        skill_ids = {item.skill_id for item in self.skills}
        task_ids = {item.task_id for item in self.tasks}
        handover_ids = {item.handover_id for item in self.handovers}
        givers = {item.employee_id for item in self.employees if item.role == EmployeeRole.giver}

        for task in self.tasks:
            if unknown := sorted(set(task.required_skills) - skill_ids):
                errors.append(f"{task.task_id}: 정의되지 않은 역량 {', '.join(unknown)}")
            if task.handover_id not in handover_ids:
                errors.append(f"{task.task_id}: 없는 인수인계서 {task.handover_id}")
        for employee in self.employees:
            if unknown := sorted(set(employee.skills) - skill_ids):
                errors.append(f"{employee.employee_id}: 정의되지 않은 역량 {', '.join(unknown)}")
            if unknown := sorted(set(employee.previous_tasks) - task_ids):
                errors.append(f"{employee.employee_id}: 없는 업무 {', '.join(unknown)}")
        for handover in self.handovers:
            if handover.giver_id not in givers:
                errors.append(f"{handover.handover_id}: 인계자가 아닌 직원 {handover.giver_id}")
            if not any(task.handover_id == handover.handover_id for task in self.tasks):
                errors.append(f"{handover.handover_id}: 포함된 업무가 없습니다.")

        if errors:
            raise ValueError("; ".join(errors))
        return self

    def receivers(self) -> list[EmployeeRecord]:
        return [item for item in self.employees if item.role == EmployeeRole.receiver]

    def tasks_for(self, handover_id: str) -> list[TaskRecord]:
        return [item for item in self.tasks if item.handover_id == handover_id]

    def employee(self, employee_id: str) -> EmployeeRecord:
        return next(item for item in self.employees if item.employee_id == employee_id)

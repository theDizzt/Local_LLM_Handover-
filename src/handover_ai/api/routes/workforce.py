"""인수자별 업무 추천·교육 대상·준비도와 전체 분배안 API."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.adapters.workforce_repository import SQLiteWorkforceRepository
from handover_ai.api.dependencies import get_database
from handover_ai.domain.assignment import DEFAULT_RECOMMENDATIONS, MAX_RECOMMENDATIONS
from handover_ai.services.workforce import WorkforceService

router = APIRouter(tags=["workforce"])


def service(database: Annotated[SQLiteDatabase, Depends(get_database)]):
    return WorkforceService(SQLiteWorkforceRepository(database))


Service = Annotated[WorkforceService, Depends(service)]


def call(action):
    try:
        return action()
    except KeyError as exc:
        raise HTTPException(404, "인수자를 찾을 수 없습니다.") from exc


@router.get("/employees")
def employees(workforce: Service):
    return workforce.employees()


@router.get("/employees/{employee_id}/recommendations")
def recommendations(
    employee_id: str,
    workforce: Service,
    n: int = Query(default=DEFAULT_RECOMMENDATIONS, ge=1, le=MAX_RECOMMENDATIONS),
):
    return call(lambda: workforce.recommendations(employee_id, n))


@router.get("/employees/{employee_id}/readiness")
def readiness(employee_id: str, workforce: Service):
    return call(lambda: workforce.readiness(employee_id))


@router.get("/assignments/plan")
def allocation(workforce: Service):
    return workforce.allocation()

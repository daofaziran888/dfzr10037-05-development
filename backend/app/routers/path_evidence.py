"""路径推理边证据门槛与推荐核验专用接口。

只做参数解码与结果封装，全部业务判断在 path_reason / reco_engine 领域服务内完成，
状态流转不经过路由层。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import SERVICES
from app.services.path_reason import PathReasonService
from app.services.reco_engine import RecoEngineService

router = APIRouter(prefix="/api/path-evidence", tags=["路径证据门槛"])


def path_service() -> PathReasonService:
    return SERVICES["path_reason"]  # type: ignore[return-value]


def reco_service() -> RecoEngineService:
    return SERVICES["reco_engine"]  # type: ignore[return-value]


class EdgePayload(BaseModel):
    edge_id: str | None = None
    source: str
    target: str
    relation: str = ""
    evidence: float
    reason: str = ""


class SupplementPayload(BaseModel):
    evidence: float
    reason: str = ""


class CasePayload(BaseModel):
    source: str
    target: str
    max_hops: int = 3
    threshold: float = 0.8


class ClaimPayload(BaseModel):
    path_key: str
    reviewer: str


class ConclusionPayload(BaseModel):
    reviewer: str
    passed: bool = True
    comment: str = ""


def _unwrap(result: tuple[dict[str, Any] | list[Any] | None, str]) -> dict[str, Any]:
    data, message = result
    if data is None:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True, "message": message, "data": data}


# ===== 关系边证据 =====
@router.get("/edges")
def list_edges() -> dict[str, Any]:
    return {"ok": True, "data": path_service().list_edges()}


@router.post("/edges")
def upsert_edge(payload: EdgePayload) -> dict[str, Any]:
    return _unwrap(path_service().upsert_edge(payload.model_dump()))


@router.post("/edges/{edge_id}/supplement")
def supplement_edge(edge_id: str, payload: SupplementPayload) -> dict[str, Any]:
    return _unwrap(path_service().supplement_edge(edge_id, payload.evidence, payload.reason))


# ===== 推理任务与路径评分 =====
@router.get("/cases")
def list_cases() -> dict[str, Any]:
    return {"ok": True, "data": path_service().list_cases()}


@router.post("/cases")
def create_case(payload: CasePayload) -> dict[str, Any]:
    return _unwrap(path_service().create_case(payload.model_dump()))


@router.post("/cases/{case_id}/score")
def score_paths(case_id: int) -> dict[str, Any]:
    return _unwrap(path_service().score_paths(case_id))


@router.get("/cases/{case_id}/scores")
def get_scores(case_id: int, include_history: bool = False) -> dict[str, Any]:
    case = path_service().get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"推理任务 {case_id} 不存在")
    return {
        "ok": True,
        "data": {
            "case": case,
            "scores": path_service().get_scores(case_id, active_only=not include_history),
        },
    }


# ===== 推荐候选：从可用路径评分生成，按目标去重 =====
@router.post("/cases/{case_id}/candidates")
def generate_candidates(case_id: int) -> dict[str, Any]:
    return _unwrap(reco_service().generate_candidates(case_id))


@router.get("/cases/{case_id}/candidates")
def list_candidates(case_id: int) -> dict[str, Any]:
    return {"ok": True, "data": reco_service().list_candidates(case_id)}


# ===== 核验待办：领用 / 冲突 / 唯一结论 / 回查 =====
@router.post("/cases/{case_id}/claims")
def claim_path(case_id: int, payload: ClaimPayload) -> dict[str, Any]:
    return _unwrap(path_service().claim_path(case_id, payload.path_key, payload.reviewer))


@router.post("/verifications/{todo_id}/conclusion")
def submit_conclusion(todo_id: int, payload: ConclusionPayload) -> dict[str, Any]:
    return _unwrap(
        path_service().submit_conclusion(todo_id, payload.reviewer, payload.passed, payload.comment))


@router.get("/cases/{case_id}/verifications")
def list_verifications(case_id: int) -> dict[str, Any]:
    return {"ok": True, "data": path_service().list_verifications(case_id)}


@router.get("/verifications/{todo_id}")
def get_verification(todo_id: int) -> dict[str, Any]:
    todo = path_service().get_verification(todo_id)
    if todo is None:
        raise HTTPException(status_code=404, detail=f"核验待办 {todo_id} 不存在")
    return {"ok": True, "data": todo}

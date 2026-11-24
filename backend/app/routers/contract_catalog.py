"""可用字段契约清单专用路由：清单构建/回查、映射轮次启停与版本重算评估。

通用资源路由（list/create/action/export）仍由 resource.py 承载，
本路由只放契约清单特有的领域动作。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.contract_catalog import ContractCatalogService

router = APIRouter(prefix="/api/contract-catalog", tags=["可用字段契约清单"])


class BuildCatalogPayload(BaseModel):
    window_id: int
    at_ts: float | None = None
    preferred_versions: dict[str, str] = Field(default_factory=dict)


class StartRoundPayload(BaseModel):
    window_id: int
    mapping_name: str
    record_count: int = 10
    source_records: list[dict[str, Any]] | None = None
    at_ts: float | None = None
    preferred_versions: dict[str, str] = Field(default_factory=dict)
    batch_size: int | None = None


class ResumeRoundPayload(BaseModel):
    limit: int | None = None


class RecomputePayload(BaseModel):
    window_id: int
    previous_round_code: str
    at_ts: float | None = None


@router.post("/build")
def build_catalog(payload: BuildCatalogPayload) -> dict[str, Any]:
    """按抽取窗口构建（或复用已冻结的）可用字段契约清单。"""
    try:
        return ContractCatalogService().build_catalog(
            payload.window_id,
            at_ts=payload.at_ts,
            preferred_versions=payload.preferred_versions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/by-window/{window_code}")
def get_catalog(window_code: str) -> dict[str, Any]:
    """按窗口编号回查冻结清单快照。"""
    catalog = ContractCatalogService().get_catalog(window_code)
    if catalog is None:
        raise HTTPException(status_code=404, detail=f"窗口 {window_code} 尚无冻结清单")
    return catalog


@router.post("/rounds/start")
def start_round(payload: StartRoundPayload) -> dict[str, Any]:
    """在两版规则都许可的时段启动 A/B 映射，返回首次结果实际采用的契约版本。"""
    try:
        return ContractCatalogService().start_mapping_round(
            payload.window_id,
            payload.mapping_name,
            record_count=payload.record_count,
            source_records=payload.source_records,
            at_ts=payload.at_ts,
            preferred_versions=payload.preferred_versions,
            batch_size=payload.batch_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/rounds/{round_code}/pause")
def pause_round(round_code: str) -> dict[str, Any]:
    """暂停映射轮次，保留冻结清单与处理水位。"""
    result = ContractCatalogService().pause_mapping_round(round_code)
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@router.post("/rounds/{round_code}/resume")
def resume_round(round_code: str, payload: ResumeRoundPayload) -> dict[str, Any]:
    """恢复暂停轮次：复用同一份冻结清单，不重建、不混入他方专属列。"""
    result = ContractCatalogService().resume_mapping_round(
        round_code, limit=payload.limit
    )
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@router.get("/rounds/{round_code}")
def get_round(round_code: str) -> dict[str, Any]:
    """回查映射轮次状态与逐字段血缘。"""
    state = ContractCatalogService().get_round(round_code)
    if state is None:
        raise HTTPException(status_code=404, detail=f"映射轮次 {round_code} 不存在")
    return state


@router.post("/evaluate-recompute")
def evaluate_recompute(payload: RecomputePayload) -> dict[str, Any]:
    """对比上一轮采用版本与当前清单，判定是否需要按新规则重算。"""
    result = ContractCatalogService().evaluate_recompute(
        payload.window_id,
        payload.previous_round_code,
        at_ts=payload.at_ts,
    )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result

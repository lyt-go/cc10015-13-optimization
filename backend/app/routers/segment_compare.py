"""分群对比接口：整批提交对比任务、读取确认结果表、逐任务独立回执。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import (
    ActionPayload,
    ActionResult,
    BatchResumePayload,
    BatchSubmitPayload,
    PageResult,
)
from app.services.segment_compare import SegmentCompareService

router = APIRouter(prefix="/api/segment_compare", tags=["分群对比"])
service = SegmentCompareService()

LIST_FIELDS = ["任务编号", "分群A", "分群B", "对比指标", "时间范围", "差异阈值", "显著性水平", "比较方法"]
RESULT_FIELDS = [
    "批次号", "任务编号", "分群A", "分群B", "对比指标", "指标编号",
    "指标值", "对比基准", "差异", "p值", "显著性", "样本量A", "样本量B", "status",
]
STATUSES = ["草稿", "计算中", "已完成", "已废弃"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="草稿、计算中、已完成、已废弃"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """任务列表：每行合并该任务的确认结果，列表页直接读这份结果。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/results")
def list_results() -> dict[str, object]:
    """分群对比结果表的全量确认结果，供列表页、详情页与消费端读取。"""
    items = service.list_results()
    return {"items": items, "total": len(items)}


@router.get("/batches")
def list_batches() -> dict[str, object]:
    """批次列表：整组提交的批次状态、完成数与版本。"""
    items = service.list_batches()
    return {"items": items, "total": len(items)}


@router.post("/batch")
def submit_batch(payload: BatchSubmitPayload) -> dict[str, object]:
    """一次提交整组任务：逐条给出结果与独立回执，未完成项随后处理。"""
    return service.submit_batch(
        payload.task_ids,
        strategy=payload.strategy,
        simulate_failures=payload.simulate_failures,
    )


@router.post("/batch/{batch_no}/resume")
def resume_batch(batch_no: str, payload: BatchResumePayload) -> dict[str, object]:
    """批次中断后续算：只重算未完成任务；也可整批回滚后统一重算。"""
    return service.resume_batch(
        batch_no, strategy=payload.strategy, expected_version=payload.expected_version)


@router.get("/export")
def export_results() -> StreamingResponse:
    """下载分群对比结果表的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items = service.list_results()
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *RESULT_FIELDS])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in RESULT_FIELDS)]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="segment_compare.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """详情页：任务明细 + 确认结果 + 完成回执，读的同一份落库数据。"""
    detail = service.get_task_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"对比任务 {entry_id} 不存在或已归档")
    return detail


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条对比任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="对比任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条对比任务执行开始计算、确认完成、废弃任务。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

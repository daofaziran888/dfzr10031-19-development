"""数据审计接口：维护审计记录，覆盖提交审计、标记异常等动作。

同时提供告警事件清单的审计入口：读取变更、登记确认/更正记录、重放判定并核对。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alert_eval import service as alert_eval
from app.services.audit import AuditService

router = APIRouter(prefix="/api/audit", tags=["数据审计"])

service = AuditService()

LIST_FIELDS = ["记录编号", "操作类型", "操作人", "操作对象", "操作时间", "变更内容", "审计结果", "记录状态"]
STATUSES = ["已记录", "已审计", "异常"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据审计清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "audit", "total": total, "items": items}


@router.get("/alert/overview", response_model=dict)
def alert_overview() -> dict[str, Any]:
    """审计首页汇总：告警侧待审计变更、待确认处置数量、需单独确认事件。"""
    return service.alert_overview()


@router.get("/alert/changes", response_model=dict)
def alert_changes(
    pending_only: bool = Query(default=False, description="只看尚未登记审计的变更"),
) -> dict[str, Any]:
    """从告警事件清单读取变更，附带建议的确认记录/更正记录类型。"""
    items = service.alert_changes(pending_only=pending_only)
    return {"items": items, "total": len(items)}


@router.get("/alert/evaluations", response_model=dict)
def alert_evaluations() -> dict[str, Any]:
    """列出可重放的告警判定：审计选择一条后重放同一批观测做核对。"""
    items = alert_eval.list_evaluations()
    return {"items": items, "total": len(items)}


@router.get("/alert/events", response_model=dict)
def alert_events(
    rule_code: str | None = Query(default=None),
    metric_code: str | None = Query(default=None),
    review_only: bool = Query(default=False),
) -> dict[str, Any]:
    """审计侧读取告警事件清单，核对结论、迟到数据影响的处置对象与超限时段。"""
    events = alert_eval.list_events(
        rule_code=rule_code, metric_code=metric_code, review_only=review_only,
    )
    return {
        "items": events,
        "total": len(events),
        "pending_total": sum(1 for event in events if event["状态"] == "待确认"),
        "review_required_total": sum(1 for event in events if event["review_required"]),
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    status: str | None = Query(default=None, description="已记录、已审计、异常"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按记录编号与状态过滤数据审计列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    extra = {k: v for k, v in request.query_params.items() if k not in ("keyword", "status", "page", "size") and v.strip()}
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size, extra=extra)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条审计记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"审计记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条审计记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="审计记录已登记", entry=entry)


@router.post("/alert/changes/{change_code}/audit", response_model=ActionResult)
def audit_alert_change(change_code: str, payload: EntryPayload) -> ActionResult:
    """针对一条告警变更登记确认记录或更正记录。"""
    values = payload.values
    entry, message = service.register_alert_change(
        change_code,
        operator=str(values.get("操作人") or "值班管理员").strip(),
        conclusion=payload.remark,
        record_type=_text(values.get("记录类型")),
        correction_of=_int(values.get("更正记录")),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/alert/evaluations/{evaluation_id}/replay", response_model=ActionResult)
def replay_alert_evaluation(evaluation_id: str, payload: EntryPayload) -> ActionResult:
    """重放同一批观测后核对事件数、覆盖时段与审计对象，并登记一条审计记录。"""
    entry, message, replay = service.register_replay(
        evaluation_id,
        operator=str(payload.values.get("操作人") or "值班管理员").strip(),
        conclusion=payload.remark,
    )
    if entry is None:
        return ActionResult(ok=False, message=message, entry=replay)
    return ActionResult(ok=True, message=message, entry={"审计记录": entry, "重放": replay})


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条审计记录执行提交审计、标记异常；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None

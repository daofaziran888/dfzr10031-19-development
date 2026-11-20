"""智能告警接口：维护告警规则，并对指标观测做阈值判定、生成告警事件清单。

原有规则登记与状态流转保持不变；新增的是「按规则 + 指标编号 + 业务时刻」的判定接口，
以及由判定派生出来的事件、观测台账和重放核对。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alert import AlertService
from app.services.alert_eval import service as evaluation_service

router = APIRouter(prefix="/api/alert", tags=["智能告警"])

service = AlertService()

LIST_FIELDS = ["规则编号", "告警名称", "监控指标", "阈值条件", "告警级别", "通知方式", "静默时段", "规则状态"]
STATUSES = ["草稿", "已配置", "生效中", "已停用"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出智能告警清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "alert", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、已配置、生效中、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤智能告警列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    extra = {k: v for k, v in request.query_params.items() if k not in ("keyword", "status", "page", "size") and v.strip()}
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size, extra=extra)
    return PageResult(items=items, total=total, page=page, size=size)


# ------------------------------------------------------------------ 判定与事件
# 这些路径必须排在 /{entry_id} 之前，避免 "events" 被当成规则 id。


@router.get("/events", response_model=dict)
def list_events(
    rule_code: str | None = Query(default=None, description="规则编号"),
    metric_code: str | None = Query(default=None, description="指标编号"),
    status: str | None = Query(default=None, description="待确认、已确认、已撤销"),
    review_only: bool = Query(default=False, description="只看已确认但仍需单独确认的事件"),
) -> dict[str, Any]:
    """读取告警事件清单：每条事件都带判定依据、规则快照和确认/更正痕迹。"""
    items = evaluation_service.list_events(
        rule_code=rule_code, metric_code=metric_code, status=status, review_only=review_only,
    )
    events = evaluation_service.list_events(rule_code=rule_code, metric_code=metric_code)
    return {
        "items": items,
        "total": len(items),
        "pending_total": sum(1 for event in events if event["状态"] == "待确认"),
        "review_required_total": sum(1 for event in events if event["review_required"]),
    }


@router.get("/observations", response_model=dict)
def list_observations(
    rule_code: str | None = Query(default=None),
    metric_code: str | None = Query(default=None),
) -> dict[str, Any]:
    """读取观测台账：按业务时刻列出每次观测的取值、是否超限与修订历史。"""
    items = evaluation_service.list_observations(rule_code=rule_code, metric_code=metric_code)
    return {"items": items, "total": len(items)}


@router.get("/evaluations", response_model=dict)
def list_evaluations() -> dict[str, Any]:
    """列出判定记录：每次判定都保存了当次的观测快照和规则口径，可供重放。"""
    items = evaluation_service.list_evaluations()
    return {"items": items, "total": len(items)}


@router.get("/evaluations/{evaluation_id}", response_model=dict)
def get_evaluation(evaluation_id: str) -> dict[str, Any]:
    entry = evaluation_service.get_evaluation(evaluation_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"判定 {evaluation_id} 不存在")
    return entry


@router.post("/evaluate", response_model=ActionResult)
def evaluate(payload: EntryPayload) -> ActionResult:
    """按规则、指标编号和观测的业务时刻做阈值判定，返回超限区间与事件变更。"""
    values = payload.values
    try:
        result = evaluation_service.record(
            rule_code=_text(values.get("规则编号")),
            rule_id=_int(values.get("规则ID")),
            metric_code=_text(values.get("指标编号")),
            observations=_observations(values.get("观测")),
            operator=_text(values.get("操作人")) or "值班管理员",
            correction_of=_int(values.get("更正记录")),
            remark=payload.remark,
            merge_override=_text(values.get("合并口径")),
        )
    except LookupError as error:
        return ActionResult(ok=False, message=str(error))
    except ValueError as error:
        return ActionResult(ok=False, message=str(error))
    return ActionResult(ok=True, message=result["说明"], entry=result)


@router.post("/events/{event_id}/actions", response_model=ActionResult)
def run_event_action(event_id: int, payload: EntryPayload) -> ActionResult:
    """对待确认事件执行确认处置、更正处置、撤销待确认。"""
    values = payload.values
    action = _text(values.get("action"))
    rule_code = _text(values.get("规则编号"))
    metric_code = _text(values.get("指标编号"))
    operator = _text(values.get("操作人")) or "值班管理员"
    if not rule_code or not metric_code:
        return ActionResult(ok=False, message="需要同时给出规则编号与指标编号，才能定位事件")
    if action == "确认处置":
        entry, message = evaluation_service.confirm(
            rule_code=rule_code, metric_code=metric_code, event_id=event_id,
            operator=operator, conclusion=payload.remark,
            correction_of=_int(values.get("更正记录")),
            evaluation_no=_text(values.get("判定编号")),
            review_key=_text(values.get("时段")),
        )
    elif action in ("更正处置", "撤销待确认"):
        entry, message = evaluation_service.correct(
            rule_code=rule_code, metric_code=metric_code, event_id=event_id,
            operator=operator, note=payload.remark,
            retract=action == "撤销待确认",
            correction_of=_int(values.get("更正记录")),
            evaluation_no=_text(values.get("判定编号")),
        )
    else:
        return ActionResult(ok=False, message=f"动作「{action}」不属于告警事件可执行范围")
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/evaluations/{evaluation_id}/replay", response_model=ActionResult)
def replay_evaluation(evaluation_id: str) -> ActionResult:
    """重放判定时保存的同一批观测，核对事件数、覆盖时段和处置对象。"""
    try:
        result = evaluation_service.replay(evaluation_id)
    except LookupError as error:
        return ActionResult(ok=False, message=str(error))
    message = (
        f"重放完成：{result['一致性']}（事件数 {result['判定时事件数']}，"
        f"覆盖 {result['判定时覆盖时段'].get('start')} ~ {result['判定时覆盖时段'].get('end')}）"
    )
    return ActionResult(ok=result["一致性"] == "一致", message=message, entry=result)


# ------------------------------------------------- 数据审计侧：变更清单与更正登记


@router.get("/changes", response_model=dict)
def list_changes(
    rule_code: str | None = Query(default=None),
    metric_code: str | None = Query(default=None),
    pending_only: bool = Query(default=False, description="只看尚未登记审计的变更"),
    change_type: str | None = Query(default=None),
) -> dict[str, Any]:
    """读取告警事件清单的变更：审计据此登记确认记录或更正记录。"""
    items = evaluation_service.list_changes(
        rule_code=rule_code, metric_code=metric_code,
        pending_only=pending_only, change_type=change_type,
    )
    return {"items": items, "total": len(items), "pending": sum(1 for item in items if item["待审计"])}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条告警规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条告警规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="告警规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条告警规则执行保存配置、启用规则、停用规则；不允许的动作会被拦下并说明原因。"""
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


def _observations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]

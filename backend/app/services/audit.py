"""数据审计业务规则：状态流转、字段校验与筛选口径都收在这里。

除了原有的审计记录登记，这里还负责从告警事件清单读取变更、登记确认/更正记录，
以及重放告警判定后把核对结果落成审计记录。
"""
from __future__ import annotations

from typing import Any

from app.services.alert_eval import service as alert_eval
from app.store import store

MODULE = "audit"
REQUIRED_FIELDS = ["记录编号", "操作类型", "操作人"]
OPTIONAL_FIELDS = [
    "操作对象", "操作时间", "变更内容", "审计结果", "记录状态",
    "判定编号", "规则编号", "指标编号", "事件", "关联告警变更",
]
STATUS_ORDER = ["已记录", "已审计", "异常"]
ACTION_RULES = {"提交审计": "已审计", "标记异常": "异常"}
NEGATIVE_ACTIONS = []

AUDIT_TYPE_BY_CHANGE = {
    "确认处置": "确认记录",
    "新建事件": "确认记录",
    "追加超限时段": "更正记录",
    "更正处置": "更正记录",
    "撤销处置": "更正记录",
}


class AuditService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        extra: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if extra:
            for field, val in extra.items():
                rows = [row for row in rows if val in str(row.get(field, ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    # ------------------------------------------------------------ 告警变更读取

    def alert_overview(self) -> dict[str, Any]:
        """审计首页要用到的告警侧汇总：待审计变更、待确认处置、需单独确认。"""
        pending_changes = alert_eval.list_changes(pending_only=True)
        events = alert_eval.list_events()
        return {
            "待审计变更": len(pending_changes),
            "待确认项数量": sum(1 for event in events if event["状态"] == "待确认"),
            "需单独确认": sum(1 for event in events if event["review_required"]),
            "事件数": sum(1 for event in events if event["状态"] != "已撤销"),
            "变更": pending_changes,
        }

    def alert_changes(self, *, pending_only: bool = False) -> list[dict[str, Any]]:
        changes = alert_eval.list_changes(pending_only=pending_only)
        for change in changes:
            change["建议记录类型"] = AUDIT_TYPE_BY_CHANGE.get(change["变更类型"], "更正记录")
            evaluation = alert_eval.get_evaluation(change["判定编号"]) if change.get("判定编号") else None
            change["判定摘要"] = {
                "超限时段": evaluation.get("超限时段", []) if evaluation else [],
                "覆盖时段": evaluation.get("覆盖时段") if evaluation else None,
                "事件数": evaluation.get("事件数") if evaluation else None,
            }
        return changes

    # -------------------------------------------------------------- 登记审计记录

    def register_alert_change(
        self,
        change_code: str,
        *,
        operator: str = "值班管理员",
        conclusion: str | None = None,
        record_type: str | None = None,
        correction_of: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        change = alert_eval.find_change(change_code)
        if change is None:
            return None, f"告警变更 {change_code} 不存在"
        if not change.get("待审计"):
            return None, f"告警变更 {change_code} 已登记过审计记录，无需重复登记"
        kind = record_type or AUDIT_TYPE_BY_CHANGE.get(change["变更类型"], "更正记录")
        if kind not in ("确认记录", "更正记录"):
            return None, "审计记录类型只能是「确认记录」或「更正记录」"

        entry = self._new_audit_row(
            operator=operator,
            record_type=kind,
            target=f"告警事件 {change['事件']}（{change['规则编号']} / {change['指标编号']}）",
            content=(
                f"判定 {change['判定编号']} 的变更 {change['变更编号']}："
                f"{change['变更类型']} —— {change['变更摘要']}"
            ),
            result=conclusion or "已核对告警事件清单，结论与变更摘要一致",
            evaluation_no=change.get("判定编号"),
            rule_code=change.get("规则编号"),
            metric_code=change.get("指标编号"),
            event_id=change.get("事件"),
            change_code=change_code,
            correction_of=correction_of,
        )
        alert_eval.mark_changes_audited([change_code], int(entry["id"]))
        return entry, f"{kind}已登记，关联判定 {change.get('判定编号')} 与事件 {change.get('事件')}"

    def register_replay(
        self,
        evaluation_id: str,
        *,
        operator: str = "值班管理员",
        conclusion: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        try:
            replay = alert_eval.replay(evaluation_id)
        except LookupError as error:
            return None, str(error), None
        consistent = replay["一致性"] == "一致"
        entry = self._new_audit_row(
            operator=operator,
            record_type="重放核对",
            target="、".join(
                f"告警事件 {item['事件']}" for item in replay["处置对象"]
            ) or f"判定 {evaluation_id}",
            content=(
                f"重放判定 {evaluation_id} 的同一批 {replay['重放观测数']} 条观测："
                f"事件数 {replay['判定时事件数']}，覆盖 {replay['判定时覆盖时段'].get('start')} ~ "
                f"{replay['判定时覆盖时段'].get('end')}；{replay['差异说明']}"
            ),
            result=conclusion or replay["一致性"],
            evaluation_no=evaluation_id,
            rule_code=replay.get("规则编号"),
            metric_code=replay.get("指标编号"),
            event_id=replay["处置对象"][0]["事件"] if replay["处置对象"] else None,
            change_code=None,
            correction_of=None,
        )
        entry["审计结果"] = conclusion or replay["一致性"]
        if not consistent:
            entry["abnormal"] = True
            entry["status"] = "异常"
        # 重放覆盖到的告警变更一并标记为已审计，避免同一批判定被重复登记。
        covered = [
            change["变更编号"]
            for change in alert_eval.list_changes(pending_only=True)
            if change.get("判定编号") == evaluation_id
        ]
        alert_eval.mark_changes_audited(covered, int(entry["id"]))
        return entry, f"重放核对已登记：{replay['一致性']}", replay

    def _new_audit_row(
        self,
        *,
        operator: str,
        record_type: str,
        target: str,
        content: str,
        result: str,
        evaluation_no: str | None,
        rule_code: str | None,
        metric_code: str | None,
        event_id: int | None,
        change_code: str | None,
        correction_of: int | None,
    ) -> dict[str, Any]:
        rows = store.rows(MODULE)
        entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": "已记录",
            "pending": True,
            "abnormal": False,
            "记录编号": f"AUDI-{len(rows) + 1:04d}",
            "操作类型": record_type,
            "操作人": operator,
            "操作对象": target,
            "操作时间": _now(),
            "变更内容": content,
            "审计结果": result,
            "记录状态": "已记录",
            "判定编号": evaluation_no,
            "规则编号": rule_code,
            "指标编号": metric_code,
            "事件": event_id,
            "关联告警变更": change_code,
            "更正记录": correction_of,
        }
        rows.append(entry)
        return entry

    # ---------------------------------------------------------------- 原有登记

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if values.get(field) not in (None, ""):
                entry[field] = values.get(field)
        entry.setdefault("操作时间", _now())
        entry.setdefault("记录状态", STATUS_ORDER[0])
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"审计记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据审计可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["记录状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"审计记录已{action}"


def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

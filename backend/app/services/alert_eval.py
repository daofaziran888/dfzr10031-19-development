"""告警阈值判定：把一段指标观测按规则算出超限区间，并落成可核对的事件清单。

判定口径（全部按观测的业务时刻排序，与登记顺序无关）：

1. 观测值满足阈值条件 → 超限；不满足 → 恢复。缺测观测没有取值，既不判超限也不判恢复。
2. 相邻两条超限观测之间如果有恢复观测，超限在恢复观测的业务时刻处收口（左闭右开，
   恢复时刻不计入超限时长）；如果中间什么观测都没有，按连续超限处理，缺测/空档会
   单独记在依据的「缺测依据」里，让持续时间可辨。
3. 连续超限观测合成一个超限时段；同一规则、同一指标下，不同超限时段按开始时刻挂到
   事件上。重复提交同一批观测不会新增事件，也不会重复累计时长。

迟到观测按业务时刻插回全量观测历史后重算。已确认的处置保留首次签认（确认人、确认
时间、确认依据不变），发生变化的部分只追加更正记录。

10:00、10:10 超限且已确认，随后补到 10:05 恢复，两种合并口径都支持：

- merge（默认）：保留同一项处置，事件里附两段超限时长（10:00-10:05 与 10:10 起），
  10:05 的恢复段被排除；新出现的未收口超限时段在 review_required 里标出，等待单独确认。
- split：已确认处置保留 10:00-10:05，10:10 起拆成第二项待确认处置，待确认项数量 +1。

两种口径都不改写首次签认；审计侧通过变更清单核对事件数、覆盖时段和待确认项数量。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "alert"
EVENT_MODULE = "_alert_events"
OBSERVATION_MODULE = "_alert_observations"
EVALUATION_MODULE = "_alert_evaluations"
CHANGE_MODULE = "_alert_changes"

OPEN_STATUS = "待确认"
CLOSED_STATUS = "已确认"
STALE_STATUS = "已撤销"
EVENT_STATUSES = [OPEN_STATUS, CLOSED_STATUS, STALE_STATUS]

COMPARE_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}
TIME_FORMAT = "%Y-%m-%d %H:%M"
MERGE_MODES = ("merge", "split")
DEFAULT_MERGE_MODE = "merge"


@dataclass
class Threshold:
    """从规则「阈值条件」里解析出来的判定口径。"""

    op: str = ">"
    value: float = 0.0
    duration_minutes: int = 0
    merge_reopen: str = DEFAULT_MERGE_MODE

    def compare(self, observation: float) -> bool:
        return COMPARE_OPS[self.op](observation, self.value)

    def condition_text(self) -> str:
        text = f"{self.op} {_fmt_number(self.value)}"
        if self.duration_minutes:
            text += f" 且持续 ≥ {self.duration_minutes} 分钟"
        if self.merge_reopen == "split":
            text += "（拆分口径）"
        return text

    def describe(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "value": self.value,
            "duration_minutes": self.duration_minutes,
            "merge_reopen": self.merge_reopen,
            "text": self.condition_text(),
        }


@dataclass
class Segment:
    """连续超限区间：[start, end)。ending 标明这段超限是怎么收口的。"""

    start: datetime
    end: datetime
    ending: str = "open"  # recovered / open
    observations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return _fmt_time(self.start)

    def duration_minutes(self) -> float:
        return round((self.end - self.start).total_seconds() / 60.0, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": _fmt_time(self.start),
            "end": _fmt_time(self.end),
            "duration_minutes": self.duration_minutes(),
            "ending": self.ending,
            "ending_text": "恢复观测收口" if self.ending == "recovered" else "尚未收口",
            "observations": list(self.observations),
        }


@dataclass
class Evaluation:
    """一次纯判定结果：只依赖规则和观测，不读写事件台账。"""

    rule_id: int
    rule_code: str
    metric_code: str
    threshold: Threshold
    observations: list[dict[str, Any]] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_minutes(self) -> float:
        return round(sum(seg.duration_minutes() for seg in self.segments), 2)

    def coverage(self) -> dict[str, Any]:
        if not self.segments:
            return {"start": None, "end": None, "segments": 0}
        return {
            "start": _fmt_time(min(seg.start for seg in self.segments)),
            "end": _fmt_time(max(seg.end for seg in self.segments)),
            "segments": len(self.segments),
        }

    def observations_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return [obs for obs in self.observations if start <= obs["_moment"] <= end]


class AlertEvalService:
    # ---------------------------------------------------------------- 规则解析

    def resolve_rule(self, rule_code: str | None, rule_id: int | None = None) -> dict[str, Any] | None:
        rows = store.rows(MODULE)
        if rule_id is not None:
            for row in rows:
                if int(row.get("id", 0)) == int(rule_id):
                    return row
            return None
        target = str(rule_code or "").strip()
        if not target:
            return None
        for row in rows:
            if str(row.get("规则编号") or "") == target:
                return row
        return None

    def parse_threshold(self, text: Any) -> Threshold:
        """阈值条件支持 `> 90`、`>=90`、`<70 持续30`、`大于 90`、末尾加「拆分」。"""
        raw = str(text or "").strip()
        if not raw:
            raise ValueError("规则「阈值条件」为空，无法判定；请先补充成例如「> 90 持续10」")
        op = _extract_op(raw)
        if op is None:
            raise ValueError(f"阈值条件「{raw}」缺少比较符，支持 > >= < <= 或 大于/小于/不低于/不超过")
        body = _strip_op(raw, op)
        parts = body.split()
        try:
            value = float(parts[0]) if parts else 0.0
        except ValueError:
            raise ValueError(f"阈值条件「{raw}」里的阈值不是数字") from None
        duration = 0
        for token in parts[1:]:
            if token.startswith("持续"):
                digits = "".join(ch for ch in token if ch.isdigit())
                duration = int(digits) if digits else 0
        merge = "split" if ("split" in raw.lower() or "拆分" in raw) else DEFAULT_MERGE_MODE
        return Threshold(op=op, value=value, duration_minutes=duration, merge_reopen=merge)

    def metric_of(self, rule: dict[str, Any]) -> str:
        return str(rule.get("监控指标") or "").strip()

    # ------------------------------------------------------------------ 纯判定

    def evaluate(
        self,
        rule: dict[str, Any],
        metric_code: str,
        observations: list[dict[str, Any]],
    ) -> Evaluation:
        threshold = self.parse_threshold(rule.get("阈值条件"))
        cleaned = self._normalize(observations)
        evaluation = Evaluation(
            rule_id=int(rule.get("id", 0)),
            rule_code=str(rule.get("规则编号") or ""),
            metric_code=metric_code,
            threshold=threshold,
            observations=cleaned,
        )
        if not cleaned:
            return evaluation
        for obs in cleaned:
            obs["_over"] = False if obs["missing"] else threshold.compare(obs["value"])

        current: Segment | None = None
        for obs in cleaned:
            if obs["missing"]:
                # 缺测没有取值，不能当恢复观测把超限收口，只能在依据里标出来。
                continue
            if obs["_over"]:
                if current is None:
                    current = Segment(start=obs["_moment"], end=obs["_moment"])
                current.end = obs["_moment"]
                current.observations.append(_obs_view(obs))
                continue
            if current is not None:
                current.end = obs["_moment"]
                current.ending = "recovered"
                evaluation.segments.append(current)
                current = None
        if current is not None:
            evaluation.segments.append(current)

        if threshold.duration_minutes:
            evaluation.segments = [
                seg for seg in evaluation.segments
                if seg.duration_minutes() >= threshold.duration_minutes
            ]
        evaluation.gaps = self._measure_gaps(cleaned)
        return evaluation

    def _normalize(self, observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按业务时刻排序、同刻去重；同刻后到的观测覆盖先到的。"""
        by_moment: dict[datetime, dict[str, Any]] = {}
        for raw in observations:
            moment_text = str(raw.get("业务时刻") or "").strip()
            if not moment_text:
                continue
            moment = _parse_time(moment_text)
            value = raw.get("观测值")
            missing = bool(raw.get("缺测")) or value in (None, "")
            record = {
                "moment": _fmt_time(moment),
                "_moment": moment,
                "value": None if missing else float(value),
                "missing": missing,
            }
            by_moment[moment] = record
        return [by_moment[moment] for moment in sorted(by_moment)]

    def _measure_gaps(self, observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        valid_diffs = [
            (b["_moment"] - a["_moment"]).total_seconds() / 60.0
            for a, b in zip(observations, observations[1:])
            if not a["missing"] and not b["missing"]
        ]
        typical = sorted(valid_diffs)[len(valid_diffs) // 2] if valid_diffs else 0.0
        gaps: list[dict[str, Any]] = []
        for prev, nxt in zip(observations, observations[1:]):
            minutes = round((nxt["_moment"] - prev["_moment"]).total_seconds() / 60.0, 2)
            if prev["missing"] or nxt["missing"]:
                gaps.append({
                    "start": prev["moment"],
                    "end": nxt["moment"],
                    "minutes": minutes,
                    "reason": "缺测",
                    "note": "缺测观测没有取值，不判恢复也不单独判超限；超限区间由相邻有效观测圈定",
                })
            elif prev["_over"] and nxt["_over"] and typical and minutes > typical * 1.5:
                gaps.append({
                    "start": prev["moment"],
                    "end": nxt["moment"],
                    "minutes": minutes,
                    "reason": "超限期间无新观测",
                    "note": "两段超限观测之间没有收到恢复观测，按连续超限处理，期间时长计入超限",
                })
        return gaps

    # -------------------------------------------------------------- 观测与台账

    def _events(self) -> list[dict[str, Any]]:
        return store.rows(EVENT_MODULE)

    def _observations(self) -> list[dict[str, Any]]:
        return store.rows(OBSERVATION_MODULE)

    def _evaluations(self) -> list[dict[str, Any]]:
        return store.rows(EVALUATION_MODULE)

    def _changes(self) -> list[dict[str, Any]]:
        return store.rows(CHANGE_MODULE)

    @staticmethod
    def _next_id(rows: list[dict[str, Any]]) -> int:
        return max((int(row.get("id", 0)) for row in rows), default=0) + 1

    def events_for(self, rule_code: str, metric_code: str) -> list[dict[str, Any]]:
        rows = [
            row for row in self._events()
            if row.get("规则编号") == rule_code and row.get("指标编号") == metric_code
        ]
        return sorted(rows, key=lambda row: (str(row.get("起始时刻") or ""), int(row.get("id", 0))))

    def _history(self, rule_code: str, metric_code: str) -> list[dict[str, Any]]:
        rows = [
            row for row in self._observations()
            if row.get("规则编号") == rule_code and row.get("指标编号") == metric_code
        ]
        return sorted(rows, key=lambda row: (str(row.get("业务时刻") or ""), int(row.get("id", 0))))

    def _upsert_observations(
        self,
        rule_code: str,
        metric_code: str,
        observations: list[dict[str, Any]],
        operator: str,
    ) -> list[dict[str, Any]]:
        """按「规则 + 指标 + 业务时刻」幂等登记：重复观测不新增行，改值留修订历史。"""
        rows = self._observations()
        created: list[dict[str, Any]] = []
        for obs in observations:
            existing = next(
                (row for row in rows
                 if row.get("规则编号") == rule_code
                 and row.get("指标编号") == metric_code
                 and row.get("业务时刻") == obs["moment"]),
                None,
            )
            if existing is not None:
                if existing.get("观测值") != obs["value"] or bool(existing.get("缺测")) != obs["missing"]:
                    existing.setdefault("修订历史", []).append({
                        "原观测值": existing.get("观测值"),
                        "新观测值": obs["value"],
                        "修订人": operator,
                        "修订时间": _now(),
                    })
                    existing["观测值"] = obs["value"]
                    existing["缺测"] = obs["missing"]
                    existing["修订次数"] = int(existing.get("修订次数", 0)) + 1
                    existing["最近提交"] = _now()
                else:
                    existing["最近提交"] = _now()
                created.append(existing)
                continue
            row = {
                "id": self._next_id(rows),
                "规则编号": rule_code,
                "指标编号": metric_code,
                "业务时刻": obs["moment"],
                "观测值": obs["value"],
                "缺测": obs["missing"],
                "是否超限": None,
                "判定": "待判定",
                "首次登记": _now(),
                "最近提交": _now(),
                "登记人": operator,
                "修订次数": 0,
                "修订历史": [],
                "事件": [],
            }
            rows.append(row)
            created.append(row)
        return created

    # ------------------------------------------------------------------ 判定入口

    def record(
        self,
        *,
        rule_code: str | None = None,
        rule_id: int | None = None,
        metric_code: str | None = None,
        observations: list[dict[str, Any]],
        operator: str = "值班管理员",
        correction_of: int | None = None,
        remark: str | None = None,
        merge_override: str | None = None,
    ) -> dict[str, Any]:
        rule = self.resolve_rule(rule_code, rule_id)
        if rule is None:
            raise LookupError(f"告警规则 {rule_code or rule_id} 不存在，判定未执行")
        metric = str(metric_code or self.metric_of(rule) or "").strip()
        if not metric:
            raise ValueError("缺少指标编号：规则未登记可识别的监控指标，请在提交时显式给出指标编号")

        batch = self._normalize(observations)
        if not batch:
            raise ValueError("至少需要一条带业务时刻的观测，空批次不判定")

        rule_code_value = str(rule.get("规则编号") or "")
        self._upsert_observations(rule_code_value, metric, batch, operator)

        if merge_override in MERGE_MODES:
            # 明确指定口径时只影响本次判定，不改写规则上登记的阈值条件。
            base = self.parse_threshold(rule.get("阈值条件"))
            base.merge_reopen = "merge"
            rule = dict(rule, **{
                "阈值条件": f"{base.condition_text()} {'拆分' if merge_override == 'split' else ''}".strip()
            })

        history = self._history(rule_code_value, metric)
        evaluation = self.evaluate(
            rule,
            metric,
            [{"业务时刻": row["业务时刻"], "观测值": row.get("观测值"), "缺测": row.get("缺测")}
             for row in history],
        )
        # 回填观测登记表上的判定结果，供审计逐条核对。
        by_moment = {obs["moment"]: obs for obs in evaluation.observations}
        for row in history:
            obs = by_moment.get(row["业务时刻"])
            if obs is None:
                continue
            row["是否超限"] = False if obs["missing"] else bool(obs["_over"])
            row["判定"] = "缺测" if obs["missing"] else ("超限" if obs["_over"] else "恢复")

        evaluation_no = f"EVAL-{len(self._evaluations()) + 1:04d}"
        result = self._reconcile(
            evaluation,
            history,
            evaluation_no=evaluation_no,
            operator=operator,
            correction_of=correction_of,
        )
        evaluation_row = self._save_evaluation(
            evaluation,
            history,
            evaluation_no=evaluation_no,
            operator=operator,
            result=result,
            batch=batch,
            remark=remark,
        )

        # 观测行补上涉及的事件编号，审计可以从任一侧串起来。
        for row in history:
            linked = [
                event["id"] for event in self.events_for(rule_code_value, metric)
                if row["业务时刻"] in {item["业务时刻"] for item in event.get("依据观测", [])}
            ]
            row["事件"] = linked

        result["判定编号"] = evaluation_no
        result["判定时间"] = evaluation_row["判定时间"]
        result["观测批次"] = [_obs_registry_view(row) for row in history]
        return result

    # ------------------------------------------------------------ 与已有台账对账

    def _reconcile(
        self,
        evaluation: Evaluation,
        history: list[dict[str, Any]],
        *,
        evaluation_no: str,
        operator: str,
        correction_of: int | None,
    ) -> dict[str, Any]:
        events = self.events_for(evaluation.rule_code, evaluation.metric_code)
        segment_by_key = {seg.key: seg for seg in evaluation.segments}
        affected: list[int] = []
        pending_before = sum(1 for event in events if event.get("状态") == OPEN_STATUS)

        # 1) 已有事件先认领自己原来的超限时段：同一起始时刻视为同一段，迟到数据只改结束时刻。
        for event in events:
            if event.get("状态") == STALE_STATUS:
                continue
            kept: list[dict[str, Any]] = []
            changed = False
            for key in list(event.get("_keys", [])):
                seg = segment_by_key.get(key)
                if seg is None:
                    changed = True
                    continue
                previous = next(
                    (item for item in event.get("_segments", []) if item["start"] == seg.start),
                    None,
                )
                if previous and (
                    previous["end"] != seg.end or previous["ending"] != seg.ending
                ):
                    changed = True
                    acknowledged = event.get("_ack_segments", {}).get(key)
                    if key in event.get("_confirmed_keys", []) and acknowledged and (
                        acknowledged["end"] != _fmt_time(seg.end)
                        or acknowledged["ending"] != seg.ending
                    ):
                        # 确认过的时段被迟到数据改了收口方式，且值班员还没就这次差异表态：
                        # 记为待复核，不改写签认。
                        event["_review_forced"] = (
                            f"已确认的时段 {key} 因迟到观测发生更正，需要复核"
                        )
                kept.append({"start": seg.start, "end": seg.end, "ending": seg.ending})
            if not kept:
                event["_segments"] = []
                event["_keys"] = []
                self._close_event(
                    event, evaluation_no, operator,
                    change_type="撤销处置",
                    summary="重算后该事件已没有超限时段，处置撤销（首次签认保留在记录里）",
                )
                if int(event["id"]) not in affected:
                    affected.append(int(event["id"]))
                continue
            event["_segments"] = kept
            event["_keys"] = [
                key for key in event.get("_keys", []) if key in segment_by_key
            ]
            if changed:
                event["last_change"] = "更正"
                self._change(
                    evaluation_no=evaluation_no,
                    rule_code=evaluation.rule_code,
                    metric_code=evaluation.metric_code,
                    event_id=int(event["id"]),
                    change_type="更正处置",
                    summary="迟到观测按业务时刻插回后，超限时段的结束时刻/收口方式发生更正",
                    operator=operator,
                    correction_of=correction_of,
                )
                affected.append(int(event["id"]))

        # 2) 没有被认领的超限时段：按合并口径决定并入已确认处置，还是新开待确认处置。
        claimed = {
            key
            for event in events
            for key in event.get("_keys", [])
            if event.get("状态") != STALE_STATUS
        }
        unassigned = [seg for seg in evaluation.segments if seg.key not in claimed]
        for seg in sorted(unassigned, key=lambda item: item.start):
            target = self._merge_target(events, seg, evaluation.threshold.merge_reopen) \
                if evaluation.threshold.merge_reopen == "merge" else None
            if target is not None:
                target["_segments"].append(
                    {"start": seg.start, "end": seg.end, "ending": seg.ending}
                )
                target["_keys"].append(seg.key)
                target["_bound"] = max(target.get("_bound") or seg.end, seg.end)
                target["last_change"] = "更正"
                self._change(
                    evaluation_no=evaluation_no,
                    rule_code=evaluation.rule_code,
                    metric_code=evaluation.metric_code,
                    event_id=int(target["id"]),
                    change_type="追加超限时段",
                    summary=(
                        f"补到 {_fmt_time(seg.start)} 起的超限时段，并入已确认处置；"
                        f"该时段 {seg.duration_minutes()} 分钟，恢复段不计入超限"
                    ),
                    operator=operator,
                    correction_of=correction_of,
                )
                if int(target["id"]) not in affected:
                    affected.append(int(target["id"]))
                continue

            event = self._new_event(evaluation, seg, history)
            events = [
                row for row in self._events()
                if row.get("规则编号") == evaluation.rule_code
                and row.get("指标编号") == evaluation.metric_code
            ]
            events.sort(key=lambda row: (str(row.get("起始时刻") or ""), int(row.get("id", 0))))
            self._change(
                evaluation_no=evaluation_no,
                rule_code=evaluation.rule_code,
                metric_code=evaluation.metric_code,
                event_id=int(event["id"]),
                change_type="新建事件",
                summary=(
                    f"识别到 {_fmt_time(seg.start)} ~ {_fmt_time(seg.end)} 的超限时段，"
                    f"登记为待确认处置"
                ),
                operator=operator,
                correction_of=correction_of,
            )
            affected.append(int(event["id"]))

        # 3) 重算每个事件的展示字段：覆盖时段、累计超限分钟、依据观测、缺测说明。
        for event in events:
            if event.get("状态") == STALE_STATUS:
                event["pending"] = False
                event["abnormal"] = True
                continue
            event["review_required"] = False
            event["review_note"] = None
            self._refresh_event(event, evaluation, history)

        pending_after = sum(1 for event in events if event.get("状态") == OPEN_STATUS)
        pending_total = sum(
            1 for event in self._events() if event.get("状态") == OPEN_STATUS
        )
        review_ids = [
            int(event["id"]) for event in events
            if event.get("状态") == CLOSED_STATUS and event.get("review_required")
        ]
        return {
            "规则编号": evaluation.rule_code,
            "指标编号": evaluation.metric_code,
            "阈值判定": evaluation.threshold.describe(),
            "合并口径": evaluation.threshold.merge_reopen,
            "观测数": len(evaluation.observations),
            "超限时段": [seg.as_dict() for seg in evaluation.segments],
            "覆盖时段": evaluation.coverage(),
            "累计超限分钟": evaluation.total_minutes,
            "缺测依据": evaluation.gaps,
            "事件数": len([event for event in events if event.get("状态") != STALE_STATUS]),
            "待确认项数量": pending_after,
            "待确认项总量": pending_total,
            "待确认变化": pending_after - pending_before,
            "需单独确认的事件": review_ids,
            "关联事件": [self._event_view(event) for event in events],
            "受影响事件": sorted(set(affected)),
            "说明": _remark(evaluation, events),
        }

    def _merge_target(
        self,
        events: list[dict[str, Any]],
        seg: Segment,
        merge_mode: str,
    ) -> dict[str, Any] | None:
        """merge 口径：新时段不晚于已确认事件此前的覆盖边界时，并回同一项处置。"""
        if merge_mode != "merge":
            return None
        candidates = [
            event for event in events
            if event.get("状态") == CLOSED_STATUS
            and event.get("_bound") is not None
            and seg.start <= event["_bound"]
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda event: event["_bound"])

    def _new_event(
        self,
        evaluation: Evaluation,
        seg: Segment,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        rows = self._events()
        event = {
            "id": self._next_id(rows),
            "事件序号": "AEVT-{}-{:02d}".format(
                evaluation.rule_code,
                len([
                    row for row in rows
                    if row.get("规则编号") == evaluation.rule_code
                    and row.get("指标编号") == evaluation.metric_code
                ]) + 1,
            ),
            "规则编号": evaluation.rule_code,
            "指标编号": evaluation.metric_code,
            "状态": OPEN_STATUS,
            "pending": True,
            "abnormal": True,
            "起始时刻": _fmt_time(seg.start),
            "结束时刻": _fmt_time(seg.end),
            "_segments": [{"start": seg.start, "end": seg.end, "ending": seg.ending}],
            "_keys": [seg.key],
            "_bound": seg.end,
            "_confirmed_keys": [],
            "_ack_segments": {},
            "累计超限分钟": seg.duration_minutes(),
            "依据观测": [],
            "排除的恢复观测": [],
            "缺测依据": [],
            "规则快照": _rule_snapshot(evaluation),
            "阈值快照": evaluation.threshold.describe(),
            "确认人": None,
            "确认时间": None,
            "确认依据": None,
            "确认时的时段": [],
            "确认时的规则": None,
            "变更记录": [],
            "review_required": False,
            "review_note": None,
            "last_change": "新建",
            "used_observation_ids": [],
        }
        rows.append(event)
        return event

    def _refresh_event(
        self,
        event: dict[str, Any],
        evaluation: Evaluation,
        history: list[dict[str, Any]],
    ) -> None:
        segments = sorted(event.get("_segments", []), key=lambda item: item["start"])
        if not segments:
            return
        start = segments[0]["start"]
        end = max(item["end"] for item in segments)
        event["起始时刻"] = _fmt_time(start)
        event["结束时刻"] = _fmt_time(end)
        event["_bound"] = max(event.get("_bound") or end, end)
        event["累计超限分钟"] = round(
            sum((item["end"] - item["start"]).total_seconds() / 60.0 for item in segments), 2
        )
        event["超限时段"] = [
            {
                "start": _fmt_time(item["start"]),
                "end": _fmt_time(item["end"]),
                "duration_minutes": round(
                    (item["end"] - item["start"]).total_seconds() / 60.0, 2
                ),
                "ending": item["ending"],
                "ending_text": "恢复观测收口" if item["ending"] == "recovered" else "尚未收口",
            }
            for item in segments
        ]
        in_span = evaluation.observations_between(start, end)
        event["依据观测"] = [_obs_view(obs) for obs in in_span]
        event["排除的恢复观测"] = [
            {
                "业务时刻": obs["moment"],
                "观测值": obs["value"],
                "说明": "落在事件覆盖范围内但取值恢复到正常范围，已排除，不计入超限时长",
            }
            for obs in in_span
            if not obs["missing"] and not obs["_over"]
        ]
        event["缺测依据"] = [
            gap for gap in evaluation.gaps
            if _parse_time(gap["start"]) >= start and _parse_time(gap["end"]) <= end
        ]
        event["规则快照"] = _rule_snapshot(evaluation)
        event["阈值快照"] = evaluation.threshold.describe()
        event["_review_forced"] = event.get("_review_forced")
        event["used_observation_ids"] = [
            row["id"] for row in history
            if start <= _parse_time(row["业务时刻"]) <= end
        ]
        event["pending"] = event.get("状态") == OPEN_STATUS
        event["abnormal"] = event.get("状态") != CLOSED_STATUS
        if event.get("状态") == CLOSED_STATUS:
            confirmed_keys = set(event.get("_confirmed_keys", []))
            new_keys = [key for key in event.get("_keys", []) if key not in confirmed_keys]
            notes = []
            if new_keys:
                notes.append(
                    f"确认后新增 {len(new_keys)} 段超限时段（{'、'.join(new_keys)}），需要单独确认"
                )
            forced = event.get("_review_forced")
            if forced:
                notes.append(forced)
                event["_review_forced"] = forced
            event["review_required"] = bool(notes)
            event["review_note"] = "；".join(notes) if notes else None
        else:
            event["review_required"] = False
            event["review_note"] = None

    # ---------------------------------------------------------------- 处置动作

    def confirm(
        self,
        *,
        rule_code: str,
        metric_code: str,
        event_id: int,
        operator: str = "值班管理员",
        conclusion: str | None = None,
        correction_of: int | None = None,
        evaluation_no: str | None = None,
        review_key: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """确认处置。合并口径下的事件若还挂着未单独确认的超限时段，可只确认其中一个时段。

        review_key 是待单独确认时段的开始时刻；不传时确认事件当前的全部超限时段。
        """
        event = self._find_event(rule_code, metric_code, event_id)
        if event is None:
            return None, f"事件 {event_id} 在规则 {rule_code} / 指标 {metric_code} 下不存在"
        if event.get("状态") == STALE_STATUS:
            return None, "该事件已被撤销，不能确认；请先核对新的判定依据"
        already_closed = event.get("状态") == CLOSED_STATUS
        if already_closed and not correction_of and not review_key:
            return None, "该事件已确认；如结论有变请登记更正处置，或指定需单独确认的时段"
        confirmed = list(event.get("_confirmed_keys", []))
        keys = list(event.get("_keys", []))
        if review_key:
            if review_key not in keys:
                return None, f"超限时段 {review_key} 不在事件 {event_id} 里，无法单独确认"
            if review_key in confirmed:
                return None, f"超限时段 {review_key} 已经确认过，无需重复确认"
            confirmed.append(review_key)
        else:
            confirmed = list(keys)
        event["确认人"] = event.get("确认人") or operator
        event["确认时间"] = event.get("确认时间") or _now()
        event["确认依据"] = conclusion or "值班人员核对判定依据（观测、规则快照、超限时段）后签认"
        if not event.get("确认时的规则"):
            # 签认时看到的规则口径单独留档，之后规则再改也不影响已确认结论的可还原性。
            event["确认时的规则"] = event.get("规则快照")
        event["_confirmed_keys"] = confirmed
        # 记下这次签认看到的收口状态；之后迟到观测若改了它，才重新要求复核。
        acks = dict(event.get("_ack_segments", {}))
        for item in event.get("超限时段", []):
            if item.get("start") in confirmed:
                acks[item["start"]] = {"end": item["end"], "ending": item["ending"]}
        event["_ack_segments"] = acks
        event["_review_forced"] = None
        # 首次签认时看到的时段单独留档，之后迟到数据只能追加，不能改写这一段。
        if not event.get("确认时的时段"):
            event["确认时的时段"] = [
                item for item in event.get("超限时段", [])
                if item.get("start") in confirmed
            ]
        event["最近签认时段"] = [
            item for item in event.get("超限时段", [])
            if item.get("start") in confirmed
        ]
        remaining = [key for key in keys if key not in confirmed]
        if remaining:
            event["状态"] = CLOSED_STATUS
            event["review_required"] = True
            event["review_note"] = (
                f"还有 {len(remaining)} 段超限时段待单独确认（{'、'.join(remaining)}）"
            )
        else:
            event["状态"] = CLOSED_STATUS
            event["review_required"] = False
            event["review_note"] = None
        event["last_change"] = "确认"
        event["pending"] = False
        event["abnormal"] = False
        evaluation_no = evaluation_no or self._latest_evaluation_no(rule_code, metric_code)
        self._change(
            evaluation_no=evaluation_no,
            rule_code=rule_code,
            metric_code=metric_code,
            event_id=event_id,
            change_type="确认处置",
            summary=f"确认处置，覆盖时段 {event['起始时刻']} ~ {event['结束时刻']}，"
                    f"累计超限 {event['累计超限分钟']} 分钟",
            operator=operator,
            correction_of=correction_of,
        )
        return self._event_view(event), "事件已确认，首次签认已保留"

    def correct(
        self,
        *,
        rule_code: str,
        metric_code: str,
        event_id: int,
        operator: str = "值班管理员",
        note: str | None = None,
        retract: bool = False,
        correction_of: int | None = None,
        evaluation_no: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        event = self._find_event(rule_code, metric_code, event_id)
        if event is None:
            return None, f"事件 {event_id} 在规则 {rule_code} / 指标 {metric_code} 下不存在"
        if event.get("状态") == STALE_STATUS:
            return None, "该事件已撤销，不需要再更正"
        if retract:
            if event.get("状态") == CLOSED_STATUS:
                return None, "已确认处置不能撤销；首次签认必须保留，请登记更正记录说明差异"
            event["状态"] = STALE_STATUS
            event["review_required"] = False
            event["review_note"] = None
            event["pending"] = False
            event["abnormal"] = True
            event["last_change"] = "撤销"
            change_type = "撤销处置"
        else:
            event["last_change"] = "更正"
            change_type = "更正处置"
            evaluation_no = evaluation_no or self._latest_evaluation_no(rule_code, metric_code)
            # 值班员显式登记更正，视为已复核迟到观测带来的差异。
            event["_review_forced"] = None
            acks = dict(event.get("_ack_segments", {}))
            for item in event.get("超限时段", []):
                acks[item["start"]] = {"end": item["end"], "ending": item["ending"]}
            event["_ack_segments"] = acks
            if event.get("状态") == CLOSED_STATUS:
                confirmed = set(event.get("_confirmed_keys", []))
                remaining = [key for key in event.get("_keys", []) if key not in confirmed]
                event["review_required"] = bool(remaining)
                event["review_note"] = (
                    f"还有 {len(remaining)} 段超限时段待单独确认（{'、'.join(remaining)}）"
                    if remaining else None
                )
        self._change(
            evaluation_no=evaluation_no,
            rule_code=rule_code,
            metric_code=metric_code,
            event_id=event_id,
            change_type=change_type,
            summary=note or ("撤销待确认处置" if retract else "摘要更正，首次签认保持不变"),
            operator=operator,
            correction_of=correction_of,
        )
        return self._event_view(event), (
            "待确认处置已撤销" if retract else "更正记录已登记，首次签认未被覆盖"
        )

    def _latest_evaluation_no(self, rule_code: str, metric_code: str) -> str | None:
        rows = [
            row for row in reversed(self._evaluations())
            if row.get("规则编号") == rule_code and row.get("指标编号") == metric_code
        ]
        return rows[0]["判定编号"] if rows else None

    def _find_event(self, rule_code: str, metric_code: str, event_id: int) -> dict[str, Any] | None:
        for event in self.events_for(rule_code, metric_code):
            if int(event.get("id", 0)) == int(event_id):
                return event
        return None

    def _close_event(
        self,
        event: dict[str, Any],
        evaluation_no: str,
        operator: str,
        *,
        change_type: str,
        summary: str,
    ) -> None:
        event["状态"] = STALE_STATUS
        event["pending"] = False
        event["abnormal"] = True
        event["last_change"] = "撤销"
        self._change(
            evaluation_no=evaluation_no,
            rule_code=str(event.get("规则编号")),
            metric_code=str(event.get("指标编号")),
            event_id=int(event["id"]),
            change_type=change_type,
            summary=summary,
            operator=operator,
        )

    # ---------------------------------------------------------------- 变更清单

    def _change(
        self,
        *,
        evaluation_no: str | None,
        rule_code: str,
        metric_code: str,
        event_id: int,
        change_type: str,
        summary: str,
        operator: str,
        correction_of: int | None = None,
    ) -> dict[str, Any]:
        rows = self._changes()
        change = {
            "id": self._next_id(rows),
            "变更编号": f"CHG-{len(rows) + 1:04d}",
            "判定编号": evaluation_no,
            "规则编号": rule_code,
            "指标编号": metric_code,
            "事件": event_id,
            "变更类型": change_type,
            "变更摘要": summary,
            "操作人": operator,
            "操作时间": _now(),
            "更正记录": correction_of,
            "审计记录": None,
            "待审计": True,
        }
        rows.append(change)
        event = next(
            (row for row in self._events()
             if int(row.get("id", 0)) == int(event_id)
             and row.get("规则编号") == rule_code
             and row.get("指标编号") == metric_code),
            None,
        )
        if event is not None:
            event.setdefault("变更记录", []).append({
                "变更编号": change["变更编号"],
                "变更类型": change_type,
                "变更摘要": summary,
                "操作人": operator,
                "操作时间": change["操作时间"],
            })
        return change

    def list_changes(
        self,
        *,
        rule_code: str | None = None,
        metric_code: str | None = None,
        pending_only: bool = False,
        change_type: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self._changes())
        if rule_code:
            rows = [row for row in rows if row.get("规则编号") == rule_code]
        if metric_code:
            rows = [row for row in rows if row.get("指标编号") == metric_code]
        if pending_only:
            rows = [row for row in rows if row.get("待审计")]
        if change_type:
            rows = [row for row in rows if row.get("变更类型") == change_type]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    def find_change(self, change_code: str) -> dict[str, Any] | None:
        return next(
            (row for row in self._changes() if row.get("变更编号") == change_code),
            None,
        )

    def mark_changes_audited(self, change_ids: list[str], audit_id: int) -> None:
        wanted = set(change_ids)
        for row in self._changes():
            if row.get("变更编号") in wanted:
                row["审计记录"] = audit_id
                row["待审计"] = False

    # ---------------------------------------------------------------- 审计重放

    def replay(self, evaluation_id: str) -> dict[str, Any]:
        """重放判定时保存的同一批观测，核对事件数、覆盖时段与处置对象现状。"""
        row = next(
            (item for item in self._evaluations()
             if item.get("判定编号") == evaluation_id),
            None,
        )
        if row is None:
            raise LookupError(f"判定 {evaluation_id} 不存在")
        rule = self.resolve_rule(row["规则编号"])
        if rule is None:
            raise LookupError(f"判定关联的规则 {row['规则编号']} 已不存在，无法重放")
        snapshot = row.get("观测快照", [])
        evaluation = self.evaluate(
            rule,
            row["指标编号"],
            [{"业务时刻": obs["业务时刻"], "观测值": obs.get("观测值"), "缺测": obs.get("缺测")}
             for obs in snapshot],
        )
        replayed_intervals = [seg.as_dict() for seg in evaluation.segments]
        expected_intervals = row.get("超限时段", [])
        interval_match = _interval_signature(replayed_intervals) == _interval_signature(expected_intervals)
        replayed_events = evaluation.coverage()
        expected_events = row.get("覆盖时段", {})
        coverage_match = replayed_events == expected_events

        current_events = []
        missing_events = []
        for event_id in row.get("事件ID", []):
            event = self._find_event(row["规则编号"], row["指标编号"], int(event_id))
            if event is None:
                missing_events.append(int(event_id))
                continue
            current_events.append(self._event_view(event))
        event_count_match = (
            len([event for event in current_events if event["状态"] != STALE_STATUS])
            == int(row.get("事件数", 0))
        )
        audit_objects = [
            {
                "事件": event["id"],
                "状态": event["状态"],
                "覆盖时段": f"{event['起始时刻']} ~ {event['结束时刻']}",
                "累计超限分钟": event["累计超限分钟"],
                "变更记录": [item.get("变更编号") for item in event.get("变更记录", [])],
            }
            for event in current_events
        ]
        consistent = interval_match and coverage_match and event_count_match and not missing_events
        return {
            "判定编号": evaluation_id,
            "规则编号": row["规则编号"],
            "指标编号": row["指标编号"],
            "重放观测数": len(evaluation.observations),
            "重放时刻区间": replayed_events,
            "重放超限时段": replayed_intervals,
            "判定时事件数": int(row.get("事件数", 0)),
            "判定时覆盖时段": expected_events,
            "判定时超限时段": expected_intervals,
            "事件数一致": event_count_match,
            "覆盖时段一致": coverage_match,
            "时段明细一致": interval_match,
            "处置对象": audit_objects,
            "缺失事件": missing_events,
            "当前待确认项数量": sum(
                1 for item in self._events() if item.get("状态") == OPEN_STATUS
            ),
            "一致性": "一致" if consistent else "存在差异",
            "差异说明": _replay_diff(
                event_count_match, coverage_match, interval_match, missing_events,
            ),
        }

    # ------------------------------------------------------------------ 视图

    def _event_view(self, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": event.get("id"),
            "事件序号": event.get("事件序号") or f"AEVT-{event.get('规则编号')}-{event.get('id')}",
            "规则编号": event.get("规则编号"),
            "指标编号": event.get("指标编号"),
            "状态": event.get("状态"),
            "起始时刻": event.get("起始时刻"),
            "结束时刻": event.get("结束时刻"),
            "累计超限分钟": event.get("累计超限分钟"),
            "超限时段": event.get("超限时段") or [],
            "依据观测": event.get("依据观测") or [],
            "排除的恢复观测": event.get("排除的恢复观测") or [],
            "缺测依据": event.get("缺测依据") or [],
            "规则快照": event.get("规则快照"),
            "阈值快照": event.get("阈值快照"),
            "确认人": event.get("确认人"),
            "确认时间": event.get("确认时间"),
            "确认依据": event.get("确认依据"),
            "确认时的时段": event.get("确认时的时段") or [],
            "最近签认时段": event.get("最近签认时段") or [],
            "确认时的规则": event.get("确认时的规则"),
            "待确认时段": [
                key for key in event.get("_keys", [])
                if event.get("状态") == CLOSED_STATUS
                and key not in event.get("_confirmed_keys", [])
            ],
            "变更记录": event.get("变更记录") or [],
            "review_required": bool(event.get("review_required")),
            "review_note": event.get("review_note"),
            "last_change": event.get("last_change"),
            "used_observation_ids": list(event.get("used_observation_ids") or []),
        }

    def list_events(
        self,
        *,
        rule_code: str | None = None,
        metric_code: str | None = None,
        status: str | None = None,
        review_only: bool = False,
    ) -> list[dict[str, Any]]:
        rows = self._events()
        if rule_code:
            rows = [row for row in rows if row.get("规则编号") == rule_code]
        if metric_code:
            rows = [row for row in rows if row.get("指标编号") == metric_code]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        if review_only:
            rows = [row for row in rows if row.get("review_required")]
        rows = sorted(rows, key=lambda row: (str(row.get("起始时刻") or ""), int(row.get("id", 0))))
        return [self._event_view(row) for row in rows]

    def list_observations(
        self,
        *,
        rule_code: str | None = None,
        metric_code: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._observations()
        if rule_code:
            rows = [row for row in rows if row.get("规则编号") == rule_code]
        if metric_code:
            rows = [row for row in rows if row.get("指标编号") == metric_code]
        rows = sorted(rows, key=lambda row: (str(row.get("业务时刻") or ""), int(row.get("id", 0))))
        return [_obs_registry_view(row) for row in rows]

    def list_evaluations(self) -> list[dict[str, Any]]:
        return [self._evaluation_view(row) for row in reversed(self._evaluations())]

    def get_evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        row = next(
            (item for item in self._evaluations() if item.get("判定编号") == evaluation_id),
            None,
        )
        return self._evaluation_view(row) if row else None

    def _save_evaluation(
        self,
        evaluation: Evaluation,
        history: list[dict[str, Any]],
        *,
        evaluation_no: str,
        operator: str,
        result: dict[str, Any],
        batch: list[dict[str, Any]],
        remark: str | None,
    ) -> dict[str, Any]:
        event_ids = [
            event["id"] for event in self.events_for(evaluation.rule_code, evaluation.metric_code)
            if event.get("状态") != STALE_STATUS
        ]
        row = {
            "判定编号": evaluation_no,
            "规则编号": evaluation.rule_code,
            "指标编号": evaluation.metric_code,
            "阈值快照": evaluation.threshold.describe(),
            "合并口径": evaluation.threshold.merge_reopen,
            "判定人": operator,
            "判定时间": _now(),
            "本次观测": [_obs_view(obs) for obs in batch],
            "观测快照": [_obs_registry_view(item) for item in history],
            "超限时段": [seg.as_dict() for seg in evaluation.segments],
            "覆盖时段": evaluation.coverage(),
            "累计超限分钟": evaluation.total_minutes,
            "缺测依据": evaluation.gaps,
            "事件数": result["事件数"],
            "事件ID": event_ids,
            "待确认项数量": result["待确认项数量"],
            "需单独确认的事件": result["需单独确认的事件"],
            "说明": remark or result["说明"],
        }
        # 覆盖存进判定快照的时段不带观测明细，重放时只比对区间口径。
        self._evaluations().append(row)
        return row

    def _evaluation_view(self, row: dict[str, Any]) -> dict[str, Any]:
        events = []
        for event_id in row.get("事件ID", []):
            event = self._find_event(row["规则编号"], row["指标编号"], int(event_id))
            if event is not None:
                events.append({
                    "id": event["id"],
                    "状态": event["状态"],
                    "起始时刻": event["起始时刻"],
                    "结束时刻": event["结束时刻"],
                    "累计超限分钟": event["累计超限分钟"],
                    "review_required": bool(event.get("review_required")),
                })
        return {
            "判定编号": row["判定编号"],
            "规则编号": row["规则编号"],
            "指标编号": row["指标编号"],
            "阈值快照": row.get("阈值快照"),
            "合并口径": row.get("合并口径"),
            "判定人": row.get("判定人"),
            "判定时间": row.get("判定时间"),
            "本次观测": row.get("本次观测", []),
            "超限时段": [
                {key: value for key, value in item.items() if key != "observations"}
                for item in row.get("超限时段", [])
            ],
            "覆盖时段": row.get("覆盖时段"),
            "累计超限分钟": row.get("累计超限分钟"),
            "缺测依据": row.get("缺测依据", []),
            "事件数": row.get("事件数"),
            "事件ID": row.get("事件ID", []),
            "事件现状": events,
            "待确认项数量": row.get("待确认项数量"),
            "需单独确认的事件": row.get("需单独确认的事件", []),
            "说明": row.get("说明"),
        }


# ------------------------------------------------------------------ 小工具


def _interval_signature(intervals: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return sorted(
        (str(item.get("start")), str(item.get("end")), str(item.get("ending")))
        for item in intervals
    )


def _replay_diff(
    event_count_match: bool,
    coverage_match: bool,
    interval_match: bool,
    missing_events: list[int],
) -> str:
    parts = []
    if not event_count_match:
        parts.append("事件数与判定时不一致")
    if not coverage_match:
        parts.append("覆盖时段与判定时不一致")
    if not interval_match:
        parts.append("超限时段明细与判定时不一致")
    if missing_events:
        parts.append(f"判定对象缺失：{missing_events}")
    return "；".join(parts) or "重放结果与判定快照一致"


def _obs_view(obs: dict[str, Any]) -> dict[str, Any]:
    return {
        "业务时刻": obs["moment"],
        "观测值": obs["value"],
        "缺测": bool(obs["missing"]),
        "是否超限": None if obs["missing"] else bool(obs.get("_over")),
        "判定": "缺测" if obs["missing"] else ("超限" if obs.get("_over") else "恢复"),
    }


def _obs_registry_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "规则编号": row.get("规则编号"),
        "指标编号": row.get("指标编号"),
        "业务时刻": row.get("业务时刻"),
        "观测值": row.get("观测值"),
        "缺测": bool(row.get("缺测")),
        "是否超限": row.get("是否超限"),
        "判定": row.get("判定"),
        "登记人": row.get("登记人"),
        "修订次数": row.get("修订次数", 0),
        "修订历史": row.get("修订历史", []),
        "事件": row.get("事件", []),
    }


def _rule_snapshot(evaluation: Evaluation) -> dict[str, Any]:
    return {
        "规则ID": evaluation.rule_id,
        "规则编号": evaluation.rule_code,
        "指标编号": evaluation.metric_code,
        "阈值条件": evaluation.threshold.condition_text(),
        "合并口径": evaluation.threshold.merge_reopen,
    }


def _remark(evaluation: Evaluation, events: list[dict[str, Any]]) -> str:
    parts = [
        f"判定 {len(evaluation.observations)} 条观测，识别 {len(evaluation.segments)} 段超限，"
        f"累计 {evaluation.total_minutes} 分钟"
    ]
    if evaluation.gaps:
        parts.append(f"依据中含 {len(evaluation.gaps)} 段缺测/无新观测说明")
    changed = [event for event in events if event.get("last_change") in ("更正", "撤销")]
    if changed:
        parts.append("迟到观测已按业务时刻插回并重算：" + "、".join(
            f"事件 {event.get('id')}（{event.get('状态')}）" for event in changed
        ))
    if evaluation.threshold.merge_reopen == "merge":
        parts.append("合并口径：保留同一项处置并附各段实际超限时长")
    else:
        parts.append("拆分口径：新增超限时段单独登记为待确认处置")
    return "；".join(parts)


def _extract_op(raw: str) -> str | None:
    for token, op in (
        (">=", ">="), ("<=", "<="), (">", ">"), ("<", "<"),
        ("大于等于", ">="), ("不低于", ">="), ("小于等于", "<="),
        ("不超过", "<="), ("大于", ">"), ("小于", "<"),
    ):
        if token in raw:
            return op
    return None


def _strip_op(raw: str, op: str) -> str:
    for token in ("大于等于", "不低于", "小于等于", "不超过", "大于", "小于", op):
        raw = raw.replace(token, " ")
    return raw.replace("持续", " 持续").replace("拆分", " ").replace("split", " ").strip()


def _parse_time(text: str) -> datetime:
    candidates = [text, text.replace("T", " "), text.replace("/", "-")]
    for candidate in candidates:
        candidate = str(candidate).strip()
        for fmt in (TIME_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(candidate).replace(tzinfo=None)
        except ValueError:
            continue
    raise ValueError(f"业务时刻「{text}」无法解析，请使用 YYYY-MM-DD HH:MM")


def _fmt_time(moment: datetime) -> str:
    return moment.strftime(TIME_FORMAT)


def _fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


service = AlertEvalService()

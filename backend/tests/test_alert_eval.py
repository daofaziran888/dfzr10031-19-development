"""阈值判定与审计重放的回归测试。

不依赖 pytest：直接 `python -m unittest discover -s backend/tests` 也能跑，
避免额外装测试框架。每个用例都重置内存仓库，互不影响。
"""
from __future__ import annotations

import unittest

from app.seed import SEED_ROWS
from app.services.alert_eval import service as evaluations
from app.services.audit import AuditService
from app.store import Store, store


def reset() -> None:
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}


def obs(pairs: list[tuple[str, float | None]]) -> list[dict[str, object]]:
    return [
        {"业务时刻": moment, "观测值": value} if value is not None else {"业务时刻": moment, "缺测": True}
        for moment, value in pairs
    ]


class MergeScenarioTest(unittest.TestCase):
    """10:00、10:10 超限已确认，补到 10:05 恢复：保留一项处置并附两段超限时长。"""

    def setUp(self) -> None:
        reset()
        first = evaluations.record(
            rule_code="ALER-0001",
            metric_code="METR-0001",
            observations=obs([("2026-09-10 10:00", 95), ("2026-09-10 10:10", 92)]),
        )
        self.first = first
        self.event_id = first["关联事件"][0]["id"]
        evaluations.confirm(
            rule_code="ALER-0001", metric_code="METR-0001", event_id=self.event_id,
            conclusion="确认两次超限",
        )
        self.late = evaluations.record(
            rule_code="ALER-0001",
            metric_code="METR-0001",
            observations=obs([("2026-09-10 10:05", 80)]),
        )

    def test_keeps_single_confirmed_event(self) -> None:
        self.assertEqual(self.late["事件数"], 1)
        self.assertEqual(self.late["待确认项数量"], 0)
        event = self.late["关联事件"][0]
        self.assertEqual(event["状态"], "已确认")
        self.assertEqual(event["确认人"], "值班管理员")

    def test_two_segments_with_recovery_excluded(self) -> None:
        event = self.late["关联事件"][0]
        segments = [(item["start"], item["end"], item["duration_minutes"]) for item in event["超限时段"]]
        self.assertEqual(segments, [("2026-09-10 10:00", "2026-09-10 10:05", 5.0),
                                    ("2026-09-10 10:10", "2026-09-10 10:10", 0.0)])
        self.assertEqual(event["累计超限分钟"], 5.0)
        excluded = [item["业务时刻"] for item in event["排除的恢复观测"]]
        self.assertEqual(excluded, ["2026-09-10 10:05"])

    def test_new_segment_flagged_for_separate_confirmation(self) -> None:
        event = self.late["关联事件"][0]
        self.assertTrue(event["review_required"])
        self.assertIn("2026-09-10 10:10", event["review_note"])
        self.assertEqual(self.late["需单独确认的事件"], [self.event_id])

        confirmed, message = evaluations.confirm(
            rule_code="ALER-0001", metric_code="METR-0001",
            event_id=self.event_id, review_key="2026-09-10 10:10",
        )
        self.assertIsNotNone(confirmed)
        assert confirmed is not None
        self.assertFalse(confirmed["review_required"])
        self.assertEqual(confirmed["确认人"], "值班管理员")

    def test_replaying_same_batch_is_idempotent(self) -> None:
        again = evaluations.record(
            rule_code="ALER-0001",
            metric_code="METR-0001",
            observations=obs([("2026-09-10 10:05", 80)]),
        )
        self.assertEqual(again["事件数"], 1)
        self.assertEqual(again["待确认变化"], 0)
        self.assertEqual(again["累计超限分钟"], 5.0)
        self.assertEqual(
            [event["累计超限分钟"] for event in again["关联事件"]],
            [event["累计超限分钟"] for event in self.late["关联事件"]],
        )


class SplitScenarioTest(unittest.TestCase):
    """同一批数据在拆分口径下：已确认处置只覆盖 10:00-10:05，10:10 另立待确认处置。"""

    def setUp(self) -> None:
        reset()
        first = evaluations.record(
            rule_code="ALER-0003",
            metric_code="METR-0003",
            observations=obs([("2026-09-11 08:00", 95), ("2026-09-11 08:10", 96)]),
        )
        self.event_id = first["关联事件"][0]["id"]
        evaluations.confirm(rule_code="ALER-0003", metric_code="METR-0003", event_id=self.event_id)
        self.late = evaluations.record(
            rule_code="ALER-0003",
            metric_code="METR-0003",
            observations=obs([("2026-09-11 08:05", 50)]),
        )

    def test_second_pending_event_appears(self) -> None:
        self.assertEqual(self.late["事件数"], 2)
        self.assertEqual(self.late["待确认项数量"], 1)
        self.assertEqual(self.late["待确认变化"], 1)
        statuses = {event["状态"] for event in self.late["关联事件"]}
        self.assertEqual(statuses, {"已确认", "待确认"})

    def test_confirmed_event_keeps_first_signoff(self) -> None:
        confirmed = next(event for event in self.late["关联事件"] if event["状态"] == "已确认")
        self.assertEqual(confirmed["id"], self.event_id)
        self.assertEqual(confirmed["确认人"], "值班管理员")
        self.assertEqual(confirmed["超限时段"][0]["end"], "2026-09-11 08:05")

    def test_audit_counts_pending_objects(self) -> None:
        audit = AuditService()
        overview = audit.alert_overview()
        self.assertEqual(overview["待确认项数量"], 1)
        changes = audit.alert_changes(pending_only=True)
        self.assertTrue(any(change["变更类型"] == "新建事件" for change in changes))
        entry, message = audit.register_alert_change(changes[0]["变更编号"])
        self.assertIsNotNone(entry, message)
        assert entry is not None
        self.assertEqual(entry["操作类型"], "确认记录")


class MissingObservationTest(unittest.TestCase):
    def setUp(self) -> None:
        reset()

    def test_missing_does_not_close_segment_and_is_auditable(self) -> None:
        result = evaluations.record(
            rule_code="ALER-0002",
            metric_code="METR-0002",
            observations=[
                {"业务时刻": "2026-09-12 09:00", "观测值": 50},
                {"业务时刻": "2026-09-12 09:10", "缺测": True},
                {"业务时刻": "2026-09-12 09:20", "观测值": 60},
            ],
        )
        event = result["关联事件"][0]
        self.assertEqual(event["结束时刻"], "2026-09-12 09:20")
        self.assertEqual([gap["reason"] for gap in event["缺测依据"]], ["缺测", "缺测"])
        self.assertIn("缺测", [item["判定"] for item in event["依据观测"]])

    def test_bad_rule_and_empty_batch_are_rejected(self) -> None:
        with self.assertRaises(LookupError):
            evaluations.record(rule_code="NOPE", metric_code="X", observations=obs([("2026-09-12 09:00", 1)]))
        with self.assertRaises(ValueError):
            evaluations.record(rule_code="ALER-0002", metric_code="METR-0002", observations=[])
        with self.assertRaises(ValueError):
            evaluations.record(
                rule_code="ALER-0002", metric_code="METR-0002",
                observations=[{"业务时刻": "not-a-time", "观测值": 1}],
            )


class ReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        reset()

    def test_replay_matches_event_count_and_coverage(self) -> None:
        result = evaluations.record(
            rule_code="ALER-0001",
            metric_code="METR-0001",
            observations=obs([("2026-09-13 11:00", 95), ("2026-09-13 11:20", 60), ("2026-09-13 11:30", 99)]),
        )
        replay = evaluations.replay(result["判定编号"])
        self.assertEqual(replay["一致性"], "一致")
        self.assertTrue(replay["事件数一致"])
        self.assertTrue(replay["覆盖时段一致"])
        self.assertEqual(replay["判定时事件数"], result["事件数"])
        self.assertEqual(replay["处置对象"][0]["事件"], result["关联事件"][0]["id"])

    def test_replay_registers_audit_entry(self) -> None:
        result = evaluations.record(
            rule_code="ALER-0001",
            metric_code="METR-0001",
            observations=obs([("2026-09-14 12:00", 95)]),
        )
        audit = AuditService()
        entry, message, replay = audit.register_replay(result["判定编号"])
        self.assertIsNotNone(entry, message)
        assert entry is not None and replay is not None
        self.assertEqual(entry["操作类型"], "重放核对")
        self.assertEqual(entry["判定编号"], result["判定编号"])
        self.assertEqual(entry["审计结果"], "一致")


class IsolatedStoreTest(unittest.TestCase):
    def test_internal_tables_stay_out_of_overview(self) -> None:
        reset()
        evaluations.record(
            rule_code="ALER-0001",
            metric_code="METR-0001",
            observations=obs([("2026-09-15 13:00", 95)]),
        )
        isolated = Store()
        isolated._tables = {name: [dict(row) for row in rows] for name, rows in store._tables.items()}
        names = isolated.module_names()
        self.assertNotIn("_alert_events", names)
        self.assertIn("alert", names)


if __name__ == "__main__":
    unittest.main()


class ReconfirmationTest(unittest.TestCase):
    """复核标记只针对还没被值班员表态的差异，重复重算不应反复要求复核。"""

    def setUp(self) -> None:
        reset()
        first = evaluations.record(
            rule_code="ALER-0001",
            metric_code="METR-0001",
            observations=obs([("2026-10-01 10:00", 95), ("2026-10-01 10:10", 92)]),
        )
        self.event_id = first["关联事件"][0]["id"]
        evaluations.confirm(rule_code="ALER-0001", metric_code="METR-0001", event_id=self.event_id)
        self.late = evaluations.record(
            rule_code="ALER-0001",
            metric_code="METR-0001",
            observations=obs([("2026-10-01 10:05", 80)]),
        )

    def test_review_clears_after_extra_segment_confirmed(self) -> None:
        self.assertTrue(self.late["关联事件"][0]["review_required"])
        confirmed, _ = evaluations.confirm(
            rule_code="ALER-0001", metric_code="METR-0001",
            event_id=self.event_id, review_key="2026-10-01 10:10",
        )
        self.assertFalse(confirmed["review_required"])  # type: ignore[index]

        again = evaluations.record(
            rule_code="ALER-0001", metric_code="METR-0001",
            observations=obs([("2026-10-01 10:05", 80)]),
        )
        event = again["关联事件"][0]
        self.assertFalse(event["review_required"])
        self.assertEqual(again["需单独确认的事件"], [])
        self.assertEqual(again["待确认项数量"], 0)

    def test_first_signoff_is_not_overwritten(self) -> None:
        evaluations.confirm(
            rule_code="ALER-0001", metric_code="METR-0001",
            event_id=self.event_id, review_key="2026-10-01 10:10",
        )
        event = evaluations.list_events(rule_code="ALER-0001")[0]
        self.assertEqual(
            [(item["start"], item["end"]) for item in event["确认时的时段"]],
            [("2026-10-01 10:00", "2026-10-01 10:10")],
        )
        self.assertEqual(event["确认时的规则"], {"规则ID": 1, "规则编号": "ALER-0001",
                                                 "指标编号": "METR-0001",
                                                 "阈值条件": "> 90", "合并口径": "merge"})

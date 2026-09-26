"""端到端场景测试：三异常并发（公路晚到 + 温控异常 + 临时换编）。"""
from __future__ import annotations

import unittest
from datetime import datetime

from src import engine
from src.app import ServiceApp
from src.models import parse_ts
from src.replay import replay
from src.validation import assert_valid
from src.views import FEEDER, RAIL, SHIPPER

AS_OF = "2026-09-26T09:10:00+08:00"


def _decisions(plan: dict) -> dict[str, dict]:
    return {d["batch_id"]: d for d in plan["decisions"]}


class ScenarioPlanningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = ServiceApp(replay())
        self.plan = self.app.make_plan("S100", AS_OF, RAIL, "rail-ops")

    def test_plan_contract(self) -> None:
        assert_valid(self.plan, "plan.schema.json")
        self.assertEqual(self.plan["summary"]["LOAD"]
                         + self.plan["summary"]["ROLLOVER"]
                         + self.plan["summary"]["HOLD"],
                         len(self.plan["decisions"]))

    def test_frozen_batch_loses_car_after_consist_change(self) -> None:
        # B-7003：P1 冻肉，温度正常，但冷藏车被摘下 → HOLD，依据换编/无匹配车厢
        d = _decisions(self.plan)["B-7003"]
        self.assertEqual(d["action"], "HOLD")
        self.assertIn("CONSIST_CHANGED", d["reason_codes"])
        self.assertIn("NO_MATCHING_CAR", d["reason_codes"])
        # 不应占用任何容量
        self.assertIsNone(d["reserve_id"])

    def test_sensor_failure_forces_hold_without_temp_evidence(self) -> None:
        # B-7006：传感器失效，即使读数 3.0°C 看起来正常也不可采信 → HOLD
        d = _decisions(self.plan)["B-7006"]
        self.assertEqual(d["action"], "HOLD")
        self.assertIn("SENSOR_UNVERIFIED", d["reason_codes"])

    def test_capacity_contention_never_overbooks(self) -> None:
        # CHILL 仅 8 位（C1=6 + C-CH2=2）。P1 B-7005(4) + P2 B-7004(6) 占满，
        # P2 B-7001 仅 ETA 未到截关，P3 批次必须被转班或处置；容量账绝不超限
        # capacity 余量不得为负
        remaining = {(c["car_id"], c["zone"]): c["positions_left"]
                     for c in self.plan["capacity"]}
        self.assertTrue(all(v >= 0 for v in remaining.values()))
        total_load = sum(len(d["pallet_ids"]) for d in self.plan["decisions"]
                         if d["action"] == "LOAD")
        self.assertLessEqual(total_load, 8)
        # B-7005 升级为 P1 且先于 B-7004 到站，必须先装
        d5 = _decisions(self.plan)["B-7005"]
        d4 = _decisions(self.plan)["B-7004"]
        self.assertEqual(d5["action"], "LOAD")
        # B-7004（6 托 P2）在仅剩 4 位时装不下本班
        self.assertNotEqual(d4["action"], "LOAD")
        self.assertIn("NO_CAPACITY", d4["reason_codes"])
        self.assertIn("PRIORITY_RANK", d4["reason_codes"])

    def test_late_batch_plans_as_rollover_with_feasible_feeder(self) -> None:
        # 09:10 时刻 B-7001 只有 ETA 09:25（晚于截关操作窗口）→ ROLLOVER 到 S102
        d = _decisions(self.plan)["B-7001"]
        self.assertEqual(d["action"], "ROLLOVER")
        self.assertEqual(d["target_service_id"], "S102")
        self.assertEqual(d["feeder_id"], "F-SHH-2100")
        self.assertGreater(d["basis"]["rollover"]["added_wait_min"], 0)

    def test_late_after_actual_arrival_and_hard_breach_becomes_hold(self) -> None:
        # 重放到 09:42：B-7001 已实际晚到且 8.2°C 硬超限触发隔离 → 必须 HOLD，
        # 即使 09:50 有 3.1°C 补报也不能改变
        plan = self.app.make_plan("S100", "2026-09-26T09:42:00+08:00", RAIL, "r")
        d = _decisions(plan)["B-7001"]
        self.assertEqual(d["action"], "HOLD")
        self.assertIn("LATE_FOR_CUTOFF", d["reason_codes"])
        self.assertIn("HARD_BREACH", d["reason_codes"])

    def test_backfill_does_not_clear_quarantine(self) -> None:
        # 09:50 的 3.1°C 补报后再次规划：硬隔离仍在（点-in-time 口径，
        # 09:55 的人工放行尚不存在）
        from src import snapshot
        plan = self.app.make_plan("S100", "2026-09-26T09:52:00+08:00", RAIL, "r")
        d = _decisions(plan)["B-7001"]
        self.assertEqual(d["action"], "HOLD")
        self.assertIn("HARD_BREACH", d["reason_codes"])
        b = self.app.state.batches["B-7001"]
        open_at_0952 = snapshot.open_quarantines_at(
            b, parse_ts("2026-09-26T09:52:00+08:00"))
        self.assertTrue(any(q["code"] == "HARD_BREACH" for q in open_at_0952))
        # 补报读数被保留并标记，但没有删除/改写任何既有事实
        bf = [o for o in b.obs if o.backfill]
        self.assertEqual(len(bf), 1)

    def test_manual_release_allows_late_batch_to_rollover(self) -> None:
        # 09:55 qc-wang 人工放行硬隔离后：晚到仍成立，转 S102，放行留痕
        plan = self.app.make_plan("S100", "2026-09-26T09:58:00+08:00", RAIL, "r")
        d = _decisions(plan)["B-7001"]
        self.assertEqual(d["action"], "ROLLOVER")
        self.assertIn("MANUAL_RELEASE", d["reason_codes"])
        self.assertTrue(any("人工放行" in w for w in d["warnings"]))

    def test_priority_change_requires_business_reason(self) -> None:
        from src.ledger import LedgerError
        with self.assertRaises(LedgerError):
            self.app.post_event({
                "type": "priority_change",
                "payload": {"event_id": "E-BAD", "occurred_at": AS_OF,
                            "batch_id": "B-7002", "new_priority": "P1"}}, RAIL)
        with self.assertRaises(LedgerError):
            self.app.post_event({
                "type": "priority_change",
                "payload": {"event_id": "E-BAD2", "occurred_at": AS_OF,
                            "batch_id": "B-7002", "new_priority": "P1",
                            "business_reason": "  ", "ref": "X"}}, RAIL)

    def test_priority_history_records_ref(self) -> None:
        b = self.app.state.batches["B-7005"]
        h = next(h for h in b.priority_history if h.get("old") == "P3")
        self.assertEqual(h["new"], "P1")
        self.assertIn("时效赔付", h["business_reason"])
        self.assertEqual(h["ref"], "SO-88421")


class OriginAndRolloverTest(unittest.TestCase):
    def test_split_merge_preserve_origin_batches(self) -> None:
        state = replay()
        from src.ledger import apply_event
        apply_event(state, "batch_split", {
            "event_id": "E-SPLIT1", "occurred_at": "2026-09-26T08:45:00+08:00",
            "batch_id": "B-7002",
            "splits": [{"new_batch_id": "B-7002-A",
                        "pallet_ids": ["P-7002-1"],
                        "reason": "目的仓分流"}],
            "reason": "目的仓分流", "actor": "ops-zhao"})
        self.assertEqual(state.batches["B-7002-A"].origin_batch_ids, ["B-7002"])
        apply_event(state, "batch_merge", {
            "event_id": "E-MRG1", "occurred_at": "2026-09-26T08:50:00+08:00",
            "batch_ids": ["B-7002-A", "B-7006"], "new_batch_id": "B-7099",
            "shipper_id": "S-BERRY", "reason": "同温拼箱"})
        merged = state.batches["B-7099"]
        self.assertEqual(merged.root_batch_ids(state), ["B-7002", "B-7006"])
        # 托盘可追溯到最初批次
        self.assertEqual(merged.pallet_origins["P-7002-1"], "B-7002")
        self.assertEqual(merged.pallet_origins["P-7006-1"], "B-7006")


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_plans_do_not_overbook(self) -> None:
        import threading
        app = ServiceApp(replay())
        errors: list[Exception] = []

        def run() -> None:
            try:
                for _ in range(5):
                    app.make_plan("S100", AS_OF, RAIL, "r")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # 最终预约账：每个 (car,zone) 的非释放占用不得超舱位
        svc = app.state.services["S100"]
        held: dict[tuple[str, str], int] = {}
        for r in app.state.reserves.values():
            if r.service_id == "S100" and r.status in ("HELD", "COMMITTED"):
                held[(r.car_id, r.zone)] = held.get((r.car_id, r.zone), 0) + r.positions
        for (car_id, zone), n in held.items():
            self.assertLessEqual(n, svc.cars[car_id].zones[zone].positions)


class OfflineScanTest(unittest.TestCase):
    def test_scans_corrected_by_field_time_after_clock_sync(self) -> None:
        state = replay()
        s1 = state.scans["SC-1"]
        # 时钟同步前按设备时间入账；同步后设备慢 600 秒，现场真实时间前移
        self.assertTrue(s1.corrected)
        self.assertEqual(
            s1.corrected_at, parse_ts("2026-09-26T09:15:00+08:00"))
        skew = state.clock_skew["D-55"]
        self.assertEqual(skew, -600.0)


class TimelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = ServiceApp(replay())

    def test_timeline_explains_waits_breach_and_manual_release(self) -> None:
        # 走完晚到→硬超限→补报→人工放行→转班
        self.app.make_plan("S100", "2026-09-26T09:42:00+08:00", RAIL, "r")
        self.app.make_plan("S100", "2026-09-26T09:58:00+08:00", RAIL, "r")
        tl = self.app.timeline("B-7001", RAIL, "r")
        assert_valid(tl, "timeline.schema.json")
        kinds = [e["kind"] for e in tl["entries"]]
        self.assertIn("QUARANTINE", kinds)
        self.assertIn("DISPOSITION", kinds)
        self.assertIn("WAIT", kinds)
        # 补报条目标注且明确不撤销隔离
        backfills = [e for e in tl["entries" ] if e.get("backfill")]
        self.assertTrue(backfills)
        self.assertTrue(any("不改变已触发的隔离" in e["detail"] for e in backfills))
        # 贡献说明含公路等待、站场等待、隔离与放行责任
        text = " ".join(c["impact"] for c in tl["contributors"])
        self.assertIn("前序公路等待", " ".join(c["factor"] for c in tl["contributors"]))
        self.assertIn("qc-wang", text)
        self.assertIn("放行", text)
        # 决策版本留痕：首版决定被后续版本取代
        decisions = [e for e in tl["entries"] if e["kind"] == "DECISION"]
        self.assertTrue(any(e.get("superseded_by") for e in decisions))

    def test_timeline_sorted_by_field_time_with_corrected_scan(self) -> None:
        self.app.make_plan("S100", AS_OF, RAIL, "r")
        tl = self.app.timeline("B-7005", RAIL, "r")
        ats = [e["at"] for e in tl["entries"]]
        self.assertEqual(ats, sorted(ats))
        scan_entries = [e for e in tl["entries"] if e["kind"] == "SCAN"]
        self.assertTrue(scan_entries)
        self.assertTrue(any("校正" in e["title"] for e in scan_entries))


class ViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = ServiceApp(replay())

    def test_shipper_sees_only_own_batches(self) -> None:
        view = self.app.make_plan("S100", AS_OF, SHIPPER, "S-FRESH")
        bids = {d["batch_id"] for d in view["decisions"]}
        self.assertEqual(bids, {"B-7001", "B-7005"})
        # 看不到容量余量与竞争批次
        self.assertNotIn("capacity", view)
        for d in view["decisions"]:
            self.assertNotIn("competing_batches", d["basis"])

    def test_shipper_cannot_read_others_timeline(self) -> None:
        from src.views import AccessDenied
        with self.assertRaises(AccessDenied):
            self.app.timeline("B-7003", SHIPPER, "S-FRESH")

    def test_feeder_sees_only_handoffs_without_cargo_identity(self) -> None:
        view = self.app.make_plan("S100", AS_OF, FEEDER, "CARRIER-LU")
        batches_seen = {h["batch_id"] for h in view["handoffs"]}
        # 挂接 S100 的 LOAD 批次在交接清单中
        self.assertTrue(batches_seen)
        raw = json_safe(view)
        # 不出现发货人、货品名、温度、隔离等敏感字段
        for forbidden in ("S-FRESH", "S-MEAT", "冷鲜水产", "冻肉", "HARD_BREACH",
                          "quarantine", "temp_min"):
            self.assertNotIn(forbidden, raw)

    def test_feeder_cannot_write(self) -> None:
        from src.views import AccessDenied
        with self.assertRaises(AccessDenied):
            self.app.post_event({"type": "scan", "payload": {
                "event_id": "X", "occurred_at": AS_OF}}, FEEDER)

    def test_device_role_limited_to_telemetry(self) -> None:
        from src.views import AccessDenied
        with self.assertRaises(AccessDenied):
            self.app.post_event({"type": "batch_tendered", "payload": {
                "event_id": "X", "occurred_at": AS_OF}}, "DEVICE")


class CommitAndCloseTest(unittest.TestCase):
    def test_committed_capacity_survives_replan_and_close(self) -> None:
        app = ServiceApp(replay())
        plan = app.make_plan("S100", AS_OF, RAIL, "r")
        reserve_id = _decisions(plan)["B-7005"]["reserve_id"]
        app.commit(reserve_id, "2026-09-26T09:20:00+08:00", RAIL)
        # 重规划不能挤占已 COMMITTED 的舱位
        plan2 = app.make_plan("S100", AS_OF, RAIL, "r")
        d5 = _decisions(plan2).get("B-7005")
        # 已装车批次不再进入待决定清单
        self.assertIsNone(d5)
        result = app.close("S100", "2026-09-26T10:00:00+08:00", RAIL)
        self.assertIn(reserve_id, result["departed_reserves"])
        b = app.state.batches["B-7005"]
        self.assertEqual(b.final, "DEPARTED")
        self.assertEqual(b.departed_service, "S100")
        # 关闭后不可再规划
        from src.engine import EngineError
        with self.assertRaises(EngineError):
            app.make_plan("S100", AS_OF, RAIL, "r")


def json_safe(obj: object) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


class EventContractTest(unittest.TestCase):
    def test_fixture_envelopes_match_contract(self) -> None:
        import json
        from pathlib import Path
        from src.validation import validate
        schema = json.loads((Path(__file__).resolve().parents[1]
                             / "contracts" / "event.envelope.schema.json")
                            .read_text(encoding="utf-8"))
        envelopes = json.loads((Path(__file__).resolve().parents[1]
                                / "fixtures" / "scenario.json")
                               .read_text(encoding="utf-8"))
        for env in envelopes:
            errs = validate(env, schema)
            self.assertFalse(errs, f"{env.get('type')}: {errs}")


if __name__ == "__main__":
    unittest.main()

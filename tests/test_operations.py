import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.capacity import CapacityPool, OverbookedError
from src.decision import evaluate_loading
from src.dispute import explain_dispute
from src.manifest import build_manifest
from src.models import (
    Batch,
    BatchStatus,
    Carriage,
    CargoProfile,
    ConnectionWindow,
    Decision,
    SensorQuality,
    Train,
)
from src.scenario import build_yard
from src.views import project_batch
from src.yard import Yard

TZ = timezone(timedelta(hours=8))


def t(h: int, m: int = 0, day: int = 26) -> datetime:
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


def make_train(train_id: str, dep_h: int, cutoff_h: int, cutoff_m: int = 30) -> Train:
    departure = t(dep_h)
    return Train(
        train_id, "义乌西", "东莞",
        departure=departure, cutoff=t(cutoff_h, cutoff_m),
        transit=timedelta(hours=12),
        carriages=(Carriage("冷藏A", 20, -25, -10), Carriage("冷藏B", 8, 0, 6)),
        connection=ConnectionWindow(departure + timedelta(hours=10),
                                    departure + timedelta(hours=22)),
    )


def make_batch(batch_id: str, pallets: int = 4, temp_range=(-20, -15)) -> Batch:
    cargo = CargoProfile(temp_range[0], temp_range[1], timedelta(hours=36))
    return Batch(batch_id, "发货人甲", pallets, cargo)


class DecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.train = make_train("K1", 20, 18)
        self.next_train = make_train("K2", 23, 22)
        self.free = {"冷藏A": 20, "冷藏B": 8}

    def test_on_time_batch_loads_current_train(self) -> None:
        batch = make_batch("B1")
        batch.arrived_at = t(17)
        result = evaluate_loading(batch, self.train, self.free)
        self.assertIs(result.decision, Decision.LOAD_CURRENT)
        self.assertEqual(result.carriage_id, "冷藏A")

    def test_late_arrival_transfers_to_next_train(self) -> None:
        batch = make_batch("B1")
        batch.arrived_at = t(18, 45)  # 晚于 18:30 截止
        result = evaluate_loading(batch, self.train, self.free,
                                  self.next_train, self.free)
        self.assertIs(result.decision, Decision.TRANSFER_NEXT)
        self.assertEqual(result.train_id, "K2")
        self.assertTrue(any("晚到" in r for r in result.reasons))

    def test_late_arrival_without_next_train_disposes(self) -> None:
        batch = make_batch("B1")
        batch.arrived_at = t(18, 45)
        result = evaluate_loading(batch, self.train, self.free)
        self.assertIs(result.decision, Decision.DISPOSE_ONSITE)

    def test_quarantined_batch_cannot_load(self) -> None:
        batch = make_batch("B1")
        batch.arrived_at = t(17)
        batch.status = BatchStatus.QUARANTINED
        result = evaluate_loading(batch, self.train, self.free,
                                  self.next_train, self.free)
        self.assertIs(result.decision, Decision.DISPOSE_ONSITE)
        self.assertTrue(any("隔离" in r for r in result.reasons))

    def test_offline_sensor_blocks_loading(self) -> None:
        batch = make_batch("B1")
        batch.arrived_at = t(17)
        batch.sensor = SensorQuality.OFFLINE
        result = evaluate_loading(batch, self.train, self.free)
        self.assertIs(result.decision, Decision.DISPOSE_ONSITE)

    def test_insufficient_capacity_transfers(self) -> None:
        batch = make_batch("B1", pallets=12)
        batch.arrived_at = t(17)
        tight = {"冷藏A": 10, "冷藏B": 8}  # 本班余量不足
        result = evaluate_loading(batch, self.train, tight,
                                  self.next_train, self.free)
        self.assertIs(result.decision, Decision.TRANSFER_NEXT)

    def test_dwell_window_exceeded_disposes(self) -> None:
        batch = make_batch("B1")
        batch.arrived_at = t(2, day=26)  # 耐受窗口 36h，下一班到达也超窗
        batch.cargo = CargoProfile(-20, -15, timedelta(hours=10))
        result = evaluate_loading(batch, self.train, self.free,
                                  self.next_train, self.free)
        self.assertIs(result.decision, Decision.DISPOSE_ONSITE)
        self.assertTrue(any("耐受窗口" in r for r in result.reasons))

    def test_connection_window_missed_transfers(self) -> None:
        train = make_train("K1", 20, 18)
        train = replace(train, connection=ConnectionWindow(t(21), t(23)))  # 到达 08:00 不在窗口
        batch = make_batch("B1")
        batch.arrived_at = t(17)
        result = evaluate_loading(batch, train, self.free,
                                  self.next_train, self.free)
        self.assertIs(result.decision, Decision.TRANSFER_NEXT)


class QuarantineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.yard = Yard()
        self.yard.register_train(make_train("K1", 20, 18))
        self.yard.register_batch(make_batch("B1"), "K1", "调度", t(8))
        self.yard.arrive("B1", t(17), "站场")

    def test_excursion_triggers_quarantine(self) -> None:
        self.yard.report_temperature("B1", -10.0, t(16), "传感器")
        self.assertIs(self.yard.batches["B1"].status, BatchStatus.QUARANTINED)

    def test_late_normal_report_does_not_override_quarantine(self) -> None:
        self.yard.report_temperature("B1", -10.0, t(16), "传感器")
        # 断网恢复后补报正常温度，occurred_at 更晚，仍不得解除隔离
        self.yard.report_temperature("B1", -18.0, t(16, 30), "传感器",
                                     recorded_at=t(17))
        self.assertIs(self.yard.batches["B1"].status, BatchStatus.QUARANTINED)
        events = self.yard.log.for_batch("B1")
        backfill = [e for e in events if e.type == "temperature_reported"][-1]
        self.assertFalse(backfill.payload["applied"])

    def test_release_requires_justification(self) -> None:
        self.yard.report_temperature("B1", -10.0, t(16), "传感器")
        with self.assertRaises(ValueError):
            self.yard.release_quarantine("B1", "", "值班员", t(17))
        self.yard.release_quarantine("B1", "现场复测 -18℃，仪表故障已更换", "值班员", t(17))
        self.assertIs(self.yard.batches["B1"].status, BatchStatus.ARRIVED)

    def test_priority_change_requires_justification(self) -> None:
        with self.assertRaises(ValueError):
            self.yard.change_priority("B1", 9, "", "调度", t(17))
        self.yard.change_priority("B1", 9, "客户时效承诺升级", "调度", t(17))
        self.assertEqual(self.yard.batches["B1"].priority, 9)


class LineageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.yard = Yard()
        self.yard.register_train(make_train("K1", 20, 18))
        self.yard.register_batch(make_batch("B1", pallets=6), "K1", "调度", t(8))

    def test_split_keeps_original_lineage(self) -> None:
        children = self.yard.split("B1", [("B1-a", 2), ("B1-b", 4)], "站场", t(17))
        self.assertEqual([c.lineage for c in children], [("B1",), ("B1",)])
        self.assertIs(self.yard.batches["B1"].status, BatchStatus.CONSUMED)

    def test_split_must_conserve_pallets(self) -> None:
        with self.assertRaises(ValueError):
            self.yard.split("B1", [("B1-a", 2), ("B1-b", 2)], "站场", t(17))

    def test_merge_unions_lineage(self) -> None:
        self.yard.register_batch(make_batch("B2", pallets=3), "K1", "调度", t(8))
        merged = self.yard.merge(["B1", "B2"], "M1", "站场", t(17))
        self.assertEqual(merged.lineage, ("B1", "B2"))
        self.assertEqual(merged.pallets, 9)

    def test_transfer_keeps_lineage(self) -> None:
        self.yard.register_train(make_train("K2", 23, 22))
        self.yard.transfer("B1", "K2", "调度", t(18))
        self.assertEqual(self.yard.batches["B1"].lineage, ("B1",))
        self.assertEqual(self.yard.assignments["B1"], "K2")


class CapacityConcurrencyTest(unittest.TestCase):
    def test_concurrent_holds_never_overbook(self) -> None:
        pool = CapacityPool()
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                pool.hold("冷藏A", key=f"w{i}", pallets=5, limit=20)
            except OverbookedError:
                pass
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertFalse(errors)
        self.assertLessEqual(pool.used("冷藏A"), 20)
        self.assertEqual(pool.used("冷藏A"), 20)  # 恰好 4 个成功，无超装

    def test_same_key_retry_is_idempotent(self) -> None:
        pool = CapacityPool()
        pool.hold("冷藏A", key="B1", pallets=5, limit=20)
        pool.hold("冷藏A", key="B1", pallets=5, limit=20)  # 断网重试
        self.assertEqual(pool.used("冷藏A"), 5)


class ManifestScenarioTest(unittest.TestCase):
    """公路晚到、温控异常、临时换编同时发生时的截关清单。"""

    def test_manifest_resolves_compound_disruption(self) -> None:
        yard, train_id = build_yard()
        manifest = build_manifest(yard, train_id, t(18, 45))
        by_batch = {i["batch_id"]: i for i in manifest["items"]}

        self.assertEqual(by_batch["B-01"]["action"], "上本班")
        self.assertEqual(by_batch["B-02"]["action"], "转下一班")  # 公路晚到
        self.assertEqual(by_batch["B-02"]["train_id"], "K103")
        self.assertEqual(by_batch["B-03"]["action"], "就地处置")  # 隔离未解除
        self.assertEqual(by_batch["B-04"]["action"], "转下一班")  # 容量不足
        self.assertEqual(by_batch["B-05"]["action"], "转下一班")  # 临时换编
        self.assertEqual(manifest["summary"]["上本班"], 1)
        # 每条决定都带理由，不靠电话拍板
        for item in manifest["items"]:
            self.assertTrue(item["reasons"])

    def test_remarshal_with_active_holds_rejected(self) -> None:
        yard, train_id = build_yard()
        yard.decide("B-01", train_id, "调度", t(18, 40))  # 占用冷藏B
        with self.assertRaises(ValueError):
            yard.remarshal(train_id, [], "调度", t(18, 50), "测试")


class DisputeTest(unittest.TestCase):
    def test_explanation_covers_wait_temp_and_release(self) -> None:
        yard, _ = build_yard()
        # B-03 隔离后经人工放行再装车
        yard.release_quarantine("B-03", "现场复测 2.1℃ 合格，传感器已校准",
                                "值班员", t(17, 30))
        yard.decide("B-03", "K101", "调度", t(18, 40))
        report = explain_dispute(yard, "B-03")

        self.assertEqual(report["outcome"], "已装车")
        self.assertTrue(report["temp_excursions"])
        self.assertEqual(report["manual_releases"][0]["justification"],
                         "现场复测 2.1℃ 合格，传感器已校准")
        factors = "；".join(report["factors"])
        self.assertIn("隔离", factors)
        self.assertIn("人工放行", factors)
        # 时间线按现场时间排序：15:40 超限在 16:20 补报之前
        types = [e["type"] for e in report["timeline"]]
        self.assertLess(types.index("quarantine_triggered"),
                        len(types) - 1)

    def test_offline_scan_reorders_by_field_time(self) -> None:
        yard, _ = build_yard()
        events = yard.log.for_batch("B-03")
        times = [e.occurred_at for e in events]
        self.assertEqual(times, sorted(times))


class ViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.yard, _ = build_yard()

    def test_shipper_sees_only_own_batch(self) -> None:
        view = project_batch(self.yard, "B-01", "shipper", requester="发货人甲")
        self.assertNotIn("events", view)
        self.assertNotIn("sensor", view)
        with self.assertRaises(PermissionError):
            project_batch(self.yard, "B-01", "shipper", requester="发货人乙")

    def test_connector_sees_only_handover_essentials(self) -> None:
        view = project_batch(self.yard, "B-01", "connector")
        self.assertEqual(set(view),
                         {"batch_id", "pallets", "temp_min", "temp_max",
                          "destination", "arrival"})

    def test_railway_sees_full_record(self) -> None:
        view = project_batch(self.yard, "B-03", "railway")
        self.assertIn("events", view)
        self.assertIn("sensor", view)


if __name__ == "__main__":
    unittest.main()

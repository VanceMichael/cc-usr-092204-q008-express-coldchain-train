"""复合异常场景：前序公路晚到、温控异常与临时换编同时发生。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.models import (
    Batch,
    Carriage,
    CargoProfile,
    ConnectionWindow,
    SensorQuality,
    Train,
)
from src.yard import Yard

TZ = timezone(timedelta(hours=8))


def _t(h: int, m: int = 0, day: int = 26) -> datetime:
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


FROZEN = CargoProfile(temp_min=-20, temp_max=-15, max_dwell=timedelta(hours=36))
CHILL = CargoProfile(temp_min=0, temp_max=4, max_dwell=timedelta(hours=24))


def _carriages() -> tuple[Carriage, ...]:
    return (
        Carriage("冷藏A", capacity_pallets=20, temp_min=-25, temp_max=-10),
        Carriage("冷藏B", capacity_pallets=8, temp_min=0, temp_max=6),
    )


def _conn(arrival: datetime) -> ConnectionWindow:
    return ConnectionWindow(available_from=arrival - timedelta(hours=2),
                            available_until=arrival + timedelta(hours=6))


def build_yard() -> tuple[Yard, str]:
    yard = Yard()
    t101 = Train("K101", "义乌西", "东莞", departure=_t(20), cutoff=_t(18, 30),
                 transit=timedelta(hours=12), carriages=_carriages(),
                 connection=_conn(_t(20) + timedelta(hours=12)))
    t103 = Train("K103", "义乌西", "东莞", departure=_t(23, 30), cutoff=_t(22),
                 transit=timedelta(hours=12), carriages=_carriages(),
                 connection=_conn(_t(23, 30) + timedelta(hours=12)))
    yard.register_train(t101)
    yard.register_train(t103)

    # B-01 正常批次；B-02 公路晚到；B-03 温控异常被隔离
    yard.register_batch(Batch("B-01", "发货人甲", 6, CHILL), "K101", "调度", _t(8))
    yard.register_batch(Batch("B-02", "发货人甲", 10, FROZEN), "K101", "调度", _t(8))
    yard.register_batch(Batch("B-03", "发货人乙", 4, CHILL), "K101", "调度", _t(8))
    # B-04 容量边缘批次，用于演示并发不超装
    yard.register_batch(Batch("B-04", "发货人甲", 5, CHILL), "K101", "调度", _t(8))
    # B-05 正点冻品批次，因临时换编失去本班冻品车厢
    yard.register_batch(Batch("B-05", "发货人乙", 4, FROZEN), "K101", "调度", _t(8))

    yard.arrive("B-01", _t(17), "站场")
    yard.arrive("B-02", _t(18, 45), "站场")  # 晚于 18:30 截止
    yard.arrive("B-03", _t(16), "站场")
    yard.arrive("B-04", _t(17, 30), "站场")
    yard.arrive("B-05", _t(17, 10), "站场")

    # B-03 在途中温度超限触发隔离；断网恢复后补报的正常温度不得覆盖隔离
    yard.report_temperature("B-03", 7.5, _t(15, 40), "车载传感器",
                            quality=SensorQuality.DEGRADED)
    yard.report_temperature("B-03", 2.5, _t(16, 20), "车载传感器",
                            recorded_at=_t(17))  # 补报，不解除隔离

    # 临时换编：K101 摘下冻品车厢冷藏A，B-05 被迫转下一班
    yard.remarshal("K101", [c for c in t101.carriages if c.carriage_id != "冷藏A"],
                   "调度", _t(18), "机后冷藏车故障，临时摘车")
    return yard, "K101"


if __name__ == "__main__":
    import json

    from src.dispute import explain_dispute
    from src.manifest import build_manifest

    yard, train_id = build_yard()
    print(json.dumps(build_manifest(yard, train_id, _t(18, 45)),
                     ensure_ascii=False, indent=2))
    print(json.dumps(explain_dispute(yard, "B-03"), ensure_ascii=False, indent=2))

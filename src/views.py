"""角色视图：发货人、铁路与接驳商只查看各自必要的数据。"""
from __future__ import annotations

from src.yard import Yard

ROLES = ("shipper", "railway", "connector")


def project_batch(yard: Yard, batch_id: str, role: str, requester: str | None = None) -> dict:
    batch = yard._batch(batch_id)
    if role == "railway":
        # 铁路运营方掌握全量运营数据
        return {
            "batch_id": batch.batch_id,
            "shipper_id": batch.shipper_id,
            "status": batch.status.value,
            "pallets": batch.pallets,
            "priority": batch.priority,
            "sensor": batch.sensor.value,
            "last_temp": batch.last_temp,
            "cargo": {
                "temp_min": batch.cargo.temp_min,
                "temp_max": batch.cargo.temp_max,
                "max_dwell_seconds": batch.cargo.max_dwell.total_seconds(),
            },
            "arrived_at": batch.arrived_at.isoformat() if batch.arrived_at else None,
            "lineage": list(batch.lineage),
            "assigned_train": yard.assignments.get(batch_id),
            "events": [
                {"at": e.occurred_at.isoformat(), "type": e.type,
                 "actor": e.actor, "payload": e.payload,
                 "justification": e.justification}
                for e in yard.log.for_batch(batch_id)
            ],
        }
    if role == "shipper":
        if requester != batch.shipper_id:
            raise PermissionError("发货人仅可查看自有批次")
        return {
            "batch_id": batch.batch_id,
            "status": batch.status.value,
            "pallets": batch.pallets,
            "last_temp": batch.last_temp,
            "lineage": list(batch.lineage),
            "arrived_at": batch.arrived_at.isoformat() if batch.arrived_at else None,
            "decisions": [
                {"at": e.occurred_at.isoformat(),
                 "decision": e.payload.get("decision"),
                 "reasons": e.payload.get("reasons")}
                for e in yard.log.for_batch(batch_id)
                if e.type == "loading_decided"
            ],
        }
    if role == "connector":
        # 接驳商只需知道接什么、何时接、保持什么温区
        train_id = yard.assignments.get(batch_id)
        train = yard.trains.get(train_id) if train_id else None
        return {
            "batch_id": batch.batch_id,
            "pallets": batch.pallets,
            "temp_min": batch.cargo.temp_min,
            "temp_max": batch.cargo.temp_max,
            "destination": train.destination if train else None,
            "arrival": train.arrival.isoformat() if train else None,
        }
    raise ValueError(f"未知角色：{role}")

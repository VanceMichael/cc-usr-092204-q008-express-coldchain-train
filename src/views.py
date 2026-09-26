"""角色视图：发货人、铁路、接驳商只查看完成职责所必需的数据。

- RAIL（铁路运营/站场）：完整计划、容量、隔离与依据；
- SHIPPER（发货人）：仅本发货人批次的决定、截关时点与待办，
  看不到其他发货人批次、容量余量、竞争批次明细；
- FEEDER（接驳商）：仅挂接本承运商接驳班次的交接信息（托数、温层、重量、就绪/交接时刻），
  看不到发货人身份、货品名称、温度序列与隔离细节。
"""
from __future__ import annotations

from typing import Any, Optional

from .models import State


class AccessDenied(PermissionError):
    pass


RAIL = "RAIL"
SHIPPER = "SHIPPER"
FEEDER = "FEEDER"
DEVICE = "DEVICE"

READ_ROLES = {RAIL, SHIPPER, FEEDER}
WRITE_ROLES = {RAIL, DEVICE}


def _decision_for_shipper(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_id": d["batch_id"], "action": d["action"],
        "reason_codes": d["reason_codes"],
        "target_service_id": d["target_service_id"],
        "feeder_id": d["feeder_id"],
        "reserve_id": d["reserve_id"],
        "warnings": d["warnings"],
        "checklist": d["checklist"],
        # 仅暴露与发货人相关的时间依据，剔除竞争批次与容量明细
        "basis": {
            k: d["basis"].get(k) for k in (
                "cutoff", "depart_at", "priority", "arrival", "minutes_late",
                "temperature", "feeder", "rollover", "window_over_min",
                "open_quarantines", "manual_releases", "capacity_note")
            if k in d["basis"]
        },
    }


def _decision_for_feeder(d: dict[str, Any], carrier_id: str) -> Optional[dict[str, Any]]:
    feeder = d.get("basis", {}).get("feeder")
    if not feeder or feeder.get("carrier_id") != carrier_id:
        return None
    # 转班批次的交接随目标班次清单下发，本班只认实际上车的 LOAD
    if d["action"] != "LOAD":
        return None
    return {
        "batch_id": d["batch_id"],
        "pallet_count": len(d["pallet_ids"]),
        "temp_zone": None,  # 由外层从批次补
        "ready_at": feeder["ready_at"],
        "handoff_cutoff": feeder["handoff_cutoff"],
        "feeder_id": feeder["feeder_id"],
        "service_id": d["service_id"],
        "arrive_service_at": None,
    }


def plan_view(state: State, plan: dict[str, Any], role: str,
              actor_id: str) -> dict[str, Any]:
    if role == RAIL:
        return plan
    if role == SHIPPER:
        mine = []
        for d in plan["decisions"]:
            b = state.batches.get(d["batch_id"])
            if b and b.shipper_id == actor_id:
                mine.append(_decision_for_shipper(d))
        return {
            "service_id": plan["service_id"], "plan_version": plan["plan_version"],
            "generated_at": plan["generated_at"], "cutoff": plan["cutoff"],
            "depart_at": plan["depart_at"],
            "summary": {a: sum(1 for d in mine if d["action"] == a)
                        for a in ("LOAD", "ROLLOVER", "HOLD")},
            "decisions": mine,
        }
    if role == FEEDER:
        handoffs = []
        for d in plan["decisions"]:
            view = _decision_for_feeder(d, actor_id)
            if view is None:
                continue
            b = state.batches[d["batch_id"]]
            svc = state.services[d["service_id"]]
            view["temp_zone"] = b.temp_zone
            view["weight_kg"] = b.weight_kg
            view["arrive_service_at"] = svc.arrive_at.isoformat()
            handoffs.append(view)
        return {
            "service_id": plan["service_id"], "generated_at": plan["generated_at"],
            "handoffs": handoffs,
        }
    raise AccessDenied(f"角色无权查看计划: {role}")


def timeline_view(state: State, timeline: dict[str, Any], role: str,
                  actor_id: str) -> dict[str, Any]:
    if role == RAIL:
        return timeline
    if role == SHIPPER:
        b = state.batches[timeline["batch_id"]]
        if b.shipper_id != actor_id:
            raise AccessDenied("只能查看本发货人批次的时间线")
        # 发货人可见完整自身时间线，但竞争方身份不出现在任何条目
        return timeline
    raise AccessDenied(f"角色无权查看争议时间线: {role}")


def require_write(role: str) -> None:
    if role not in WRITE_ROLES:
        raise AccessDenied(f"角色无权写入事件: {role}")

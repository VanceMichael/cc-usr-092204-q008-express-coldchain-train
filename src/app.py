"""应用服务层：事件上报、规划、确认、截关、时间线，统一角色校验。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from . import engine
from .ledger import apply_event
from .models import State, parse_ts
from .views import (
    DEVICE,
    FEEDER,
    RAIL,
    SHIPPER,
    AccessDenied,
    plan_view,
    require_write,
    timeline_view,
)

_DEVICE_EVENTS = {"scan", "clock_sync", "sensor_state", "temp_reading"}


class ServiceApp:
    def __init__(self, state: Optional[State] = None):
        self.state = state or State()

    # ---- 事件 ----
    def post_event(self, envelope: dict[str, Any], role: str) -> dict[str, Any]:
        require_write(role)
        evt_type = envelope.get("type")
        payload = envelope.get("payload")
        if not evt_type or not isinstance(payload, dict):
            raise ValueError("需要 type 与 payload")
        if role == DEVICE and evt_type not in _DEVICE_EVENTS:
            raise AccessDenied("设备角色仅可上报扫描/时钟/传感器类事件")
        rec = apply_event(self.state, evt_type, payload, source=role)
        return {"event_id": rec.event_id, "seq": rec.seq, "type": rec.type,
                "occurred_at": rec.occurred_at.isoformat()}

    # ---- 规划 ----
    def make_plan(self, service_id: str, as_of: str, role: str,
                  actor_id: str) -> dict[str, Any]:
        if role not in (RAIL, SHIPPER, FEEDER):
            raise AccessDenied("仅运营角色可查看装载计划")
        plan = engine.plan(self.state, service_id, parse_ts(as_of))
        return plan_view(self.state, plan, role, actor_id)

    def commit(self, reserve_id: str, at: str, role: str,
               pallet_ids: Optional[list[str]] = None) -> dict[str, Any]:
        if role != RAIL:
            raise AccessDenied("仅铁路运营可确认装车")
        return engine.commit_reserve(self.state, reserve_id, parse_ts(at), pallet_ids)

    def close(self, service_id: str, at: str, role: str) -> dict[str, Any]:
        if role != RAIL:
            raise AccessDenied("仅铁路运营可截关发车")
        return engine.close_service(self.state, service_id, parse_ts(at))

    # ---- 争议时间线 ----
    def timeline(self, batch_id: str, role: str, actor_id: str) -> dict[str, Any]:
        from .timeline import build_timeline
        if role not in (RAIL, SHIPPER):
            raise AccessDenied("仅铁路与发货人可查看争议时间线")
        tl = build_timeline(self.state, batch_id)
        return timeline_view(self.state, tl, role, actor_id)

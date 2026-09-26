"""点-in-time 事实快照。

规划是在某个现场时刻 as_of 做判断，只能采信该时刻之前发生的事实。
状态对象保存完整事件历史，本模块按 as_of 取当时有效视图，避免"未来事件"
（如晚些时候的人工放行）倒灌到早先决定。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .models import Batch, Car, Priority, Service, ZoneCap


def service_view(svc: Service, at: datetime) -> Service:
    """返回 as_of 时刻的编组视图（浅拷贝，车厢按 consist_log 重建）。"""
    entry = None
    for log in svc.consist_log:
        if log["at"] <= at:
            entry = log
    if entry is None:
        entry = {"version": 0, "reason": None, "cars": {}}
    cars: dict[str, Car] = {}
    for cid, cdef in entry["cars"].items():
        cars[cid] = Car(car_id=cid, zones={
            z: ZoneCap(zc["positions"], zc["weight_kg"], zc["volume_m3"])
            for z, zc in cdef["zones"].items()})
    view = Service(
        service_id=svc.service_id, origin=svc.origin, destination=svc.destination,
        depart_at=svc.depart_at, arrive_at=svc.arrive_at,
        load_cutoff=svc.load_cutoff, unload_lead_min=svc.unload_lead_min,
        consist_version=entry["version"], consist_reason=entry["reason"],
        cars=cars, closed=svc.closed, departed_at=svc.departed_at)
    return view


def open_quarantines_at(b: Batch, at: datetime) -> list[dict[str, Any]]:
    out = []
    for q in b.quarantines:
        if q["raised_at"] > at:
            continue
        d = q.get("disposition")
        if d and d["at"] <= at:
            continue
        out.append(q)
    return out


def released_quarantines_at(b: Batch, at: datetime) -> list[dict[str, Any]]:
    out = []
    for q in b.quarantines:
        if q["raised_at"] > at:
            continue
        d = q.get("disposition")
        if d and d["at"] <= at and d["disposition"] == "RELEASED":
            out.append(q)
    return out


def priority_at(b: Batch, at: datetime) -> Priority:
    current = Priority("P3")
    for h in b.priority_history:
        if h["at"] <= at:
            current = Priority(h["new"])
    return current


def arrival_at(b: Batch, at: datetime) -> Optional[datetime]:
    current = None
    for h in b.arrival_history:
        if h["at"] <= at:
            current = h["arrived_at"]
    return current


def eta_at(b: Batch, at: datetime) -> Optional[datetime]:
    current = None
    for h in b.eta_history:
        if h["at"] <= at:
            current = h["eta"]
    return current


def seal_at(b: Batch, at: datetime) -> Optional[bool]:
    current = None
    for h in b.arrival_history:
        if h["at"] <= at:
            current = h.get("seal_ok")
    return current


def exists_at(b: Batch, at: datetime) -> bool:
    return b.tendered_at <= at


def scrapped_or_returned_at(b: Batch, at: datetime) -> bool:
    for q in b.quarantines:
        d = q.get("disposition")
        if d and d["at"] <= at and d["disposition"] in ("SCRAPPED", "RETURNED"):
            return True
    return False


def departed_at(b: Batch, at: datetime) -> bool:
    return b.departed_at is not None and b.departed_at <= at

"""领域值对象与状态模型。

时间一律使用带时区的 datetime；对外序列化为 ISO-8601 字符串。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


def parse_ts(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        ts = value
    else:
        ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError(f"时间必须带时区: {value!r}")
    return ts


def iso(ts: datetime) -> str:
    return ts.isoformat()


class Priority(str, Enum):
    P1 = "P1"  # 最高
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"

    @property
    def rank(self) -> int:
        return int(self.value[1:])


class Action(str, Enum):
    LOAD = "LOAD"            # 上本班
    ROLLOVER = "ROLLOVER"    # 转下一班
    HOLD = "HOLD"            # 就地处置（隔离等待人工）


class SensorQuality(str, Enum):
    OK = "OK"
    SUSPECT = "SUSPECT"      # 校准临期/偏差，数据不可单独作为放行依据
    INVALID = "INVALID"      # 校准失效/故障，温度不可证实


class Disposition(str, Enum):
    RELEASED = "RELEASED"    # 人工放行
    SCRAPPED = "SCRAPPED"
    RETURNED = "RETURNED"
    HELD = "HELD"


# 决策依据码
REASON_ON_TIME = "ON_TIME"
REASON_LATE_FOR_CUTOFF = "LATE_FOR_CUTOFF"
REASON_NO_CAPACITY = "NO_CAPACITY"
REASON_TEMP_NONCOMPLIANT = "TEMP_NONCOMPLIANT"
REASON_TEMP_HARD_BREACH = "TEMP_HARD_BREACH"
REASON_SENSOR_UNVERIFIED = "SENSOR_UNVERIFIED"
REASON_QUARANTINED = "QUARANTINED"
REASON_NO_MATCHING_CAR = "NO_MATCHING_CAR"
REASON_FEEDER_INFEASIBLE = "FEEDER_INFEASIBLE"
REASON_WINDOW_EXCEEDED = "WINDOW_EXCEEDED"
REASON_MANUAL_RELEASE = "MANUAL_RELEASE"
REASON_CONSIST_CHANGED = "CONSIST_CHANGED"
REASON_CAPABILITY_LOST = "CAPABILITY_LOST"
REASON_PRIORITY_RANK = "PRIORITY_RANK"
REASON_NOT_ARRIVED = "NOT_ARRIVED"


@dataclass(frozen=True)
class ZoneCap:
    positions: int
    weight_kg: float
    volume_m3: float


@dataclass
class Car:
    car_id: str
    zones: dict[str, ZoneCap]


@dataclass
class Service:
    service_id: str
    origin: str
    destination: str
    depart_at: datetime
    arrive_at: datetime
    load_cutoff: datetime
    unload_lead_min: int = 40
    consist_version: int = 0
    consist_reason: Optional[str] = None
    cars: dict[str, Car] = field(default_factory=dict)
    consist_log: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False
    departed_at: Optional[datetime] = None


@dataclass
class Feeder:
    feeder_id: str
    carrier_id: str
    station: str
    handoff_cutoff: datetime
    depart_at: datetime


@dataclass
class Pallet:
    pallet_id: str
    weight_kg: float
    volume_m3: float


@dataclass
class TempObs:
    occurred_at: datetime
    sensor_id: str
    temp_c: float
    event_id: str
    backfill: bool = False
    admissible: bool = True
    suspect: bool = False
    received_at: Optional[datetime] = None


@dataclass
class SensorState:
    occurred_at: datetime
    sensor_id: str
    quality: SensorQuality
    calibrated: bool
    calib_until: Optional[datetime]
    max_gap_min: int
    event_id: str


@dataclass
class DecisionRecord:
    service_id: str
    plan_version: int
    batch_id: str
    action: str
    reason_codes: list[str]
    car_id: Optional[str]
    zone: Optional[str]
    pallet_ids: list[str]
    target_service_id: Optional[str]
    feeder_id: Optional[str]
    basis: dict[str, Any]
    checklist: list[dict[str, Any]]
    warnings: list[str]
    reserve_id: Optional[str] = None
    superseded_by: Optional[int] = None


@dataclass
class Batch:
    batch_id: str
    shipper_id: str
    origin: str
    destination: str
    commodity: str
    temp_zone: str
    temp_min: float
    temp_max: float
    weight_kg: float
    volume_m3: float
    pallets: list[Pallet]
    exposure_budget_min: int
    hard_temp_min: Optional[float]
    hard_temp_max: Optional[float]
    priority: Priority
    tendered_at: datetime
    origin_batch_ids: list[str] = field(default_factory=list)
    pallet_origins: dict[str, str] = field(default_factory=dict)
    eta: Optional[datetime] = None
    arrived_at: Optional[datetime] = None
    eta_history: list[dict[str, Any]] = field(default_factory=list)
    arrival_history: list[dict[str, Any]] = field(default_factory=list)
    seal_ok: Optional[bool] = None
    obs: list[TempObs] = field(default_factory=list)
    decisions: list[DecisionRecord] = field(default_factory=list)
    quarantines: list[dict[str, Any]] = field(default_factory=list)
    priority_history: list[dict[str, Any]] = field(default_factory=list)
    reserves: list[dict[str, Any]] = field(default_factory=list)
    departed_service: Optional[str] = None
    departed_at: Optional[datetime] = None
    final: Optional[str] = None

    @property
    def pallet_ids(self) -> list[str]:
        return [p.pallet_id for p in self.pallets]

    @property
    def positions(self) -> int:
        return len(self.pallets)

    def root_batch_ids(self, state: "State") -> list[str]:
        """沿拆分/合并链回溯到最初批次（去重保序）。"""
        roots: list[str] = []
        stack = [self.batch_id]
        seen: set[str] = set()
        while stack:
            bid = stack.pop()
            if bid in seen:
                continue
            seen.add(bid)
            b = state.batches.get(bid)
            if not b or not b.origin_batch_ids:
                if bid not in roots:
                    roots.append(bid)
            else:
                stack.extend(b.origin_batch_ids)
        return sorted(roots)


@dataclass
class Reserve:
    reserve_id: str
    service_id: str
    car_id: str
    zone: str
    batch_id: str
    pallet_ids: list[str]
    positions: int
    weight_kg: float
    volume_m3: float
    created_at: datetime
    plan_version: int
    kind: str = "PLAN"        # PLAN（本班规划）/ ROLLOVER（转入预约）
    source_service_id: Optional[str] = None  # ROLLOVER 预约由哪个班次的规划产生
    status: str = "HELD"  # HELD / COMMITTED / RELEASED


@dataclass
class Scan:
    scan_id: str
    device_id: str
    pallet_id: str
    batch_id: str
    scanned_at: datetime      # 设备现场时间
    received_at: datetime     # 服务端接收时间
    kind: str                 # ARRIVAL / LOAD / UNLOAD
    corrected: bool = False
    corrected_at: Optional[datetime] = None


@dataclass
class EventRecord:
    seq: int
    event_id: str
    type: str
    occurred_at: datetime
    received_at: datetime
    payload: dict[str, Any]
    source: str


@dataclass
class State:
    services: dict[str, Service] = field(default_factory=dict)
    feeders: dict[str, Feeder] = field(default_factory=dict)
    batches: dict[str, Batch] = field(default_factory=dict)
    sensor_states: dict[str, list[SensorState]] = field(default_factory=dict)
    reserves: dict[str, Reserve] = field(default_factory=dict)
    scans: dict[str, Scan] = field(default_factory=dict)
    events: list[EventRecord] = field(default_factory=list)
    plans: dict[str, int] = field(default_factory=dict)  # service -> latest version
    clock_skew: dict[str, float] = field(default_factory=dict)  # device -> 秒偏移
    lock: threading.RLock = field(default_factory=threading.RLock)

"""领域模型：班次、车厢、批次与货品耐受参数。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class SensorQuality(str, Enum):
    GOOD = "良好"
    DEGRADED = "降级"
    OFFLINE = "离线"


class Decision(str, Enum):
    LOAD_CURRENT = "上本班"
    TRANSFER_NEXT = "转下一班"
    DISPOSE_ONSITE = "就地处置"


class BatchStatus(str, Enum):
    EXPECTED = "在途"
    ARRIVED = "已到站"
    QUARANTINED = "隔离待检"
    LOADED = "已装车"
    TRANSFERRED = "已转班"
    DISPOSED = "已就地处置"
    CONSUMED = "已拆并"  # 拆分或合并后的父批次不再参与装载


@dataclass(frozen=True)
class CargoProfile:
    """货品冷链耐受参数。"""

    temp_min: float
    temp_max: float
    max_dwell: timedelta  # 耐受窗口：从到站到目的站交付允许的最长历时


@dataclass(frozen=True)
class Carriage:
    """车厢能力与容量。"""

    carriage_id: str
    capacity_pallets: int
    temp_min: float
    temp_max: float

    def supports(self, cargo: CargoProfile) -> bool:
        return self.temp_min <= cargo.temp_min and self.temp_max >= cargo.temp_max


@dataclass(frozen=True)
class ConnectionWindow:
    """目的站接驳可用窗口。"""

    available_from: datetime
    available_until: datetime

    def covers(self, moment: datetime) -> bool:
        return self.available_from <= moment <= self.available_until


@dataclass(frozen=True)
class Train:
    """客车化班列：稳定时刻、装卸截止、编组与目的站接驳。"""

    train_id: str
    origin: str
    destination: str
    departure: datetime
    cutoff: datetime
    transit: timedelta
    carriages: tuple[Carriage, ...]
    connection: ConnectionWindow

    @property
    def arrival(self) -> datetime:
        return self.departure + self.transit


@dataclass
class Batch:
    """冷链批次。lineage 始终保留原始批次号，拆分、合并、转班不丢失。"""

    batch_id: str
    shipper_id: str
    pallets: int
    cargo: CargoProfile
    priority: int = 0
    status: BatchStatus = BatchStatus.EXPECTED
    arrived_at: datetime | None = None
    sensor: SensorQuality = SensorQuality.GOOD
    last_temp: float | None = None
    lineage: tuple[str, ...] = ()

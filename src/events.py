"""附录式事件日志：所有状态变化留痕。

每个事件同时记录现场时间（occurred_at）与系统落账时间（recorded_at），
断网扫描恢复后按现场时间校正顺序。人工放行与优先级调整必须附业务依据。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

# 必须附业务依据的事件类型
_JUSTIFIED = {
    "priority_changed": "优先级变化必须给出业务依据",
    "quarantine_released": "人工放行必须给出业务依据",
}


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    batch_id: str
    occurred_at: datetime
    recorded_at: datetime
    actor: str
    payload: dict = field(default_factory=dict)
    justification: str | None = None


class EventLog:
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._seq = 0
        self._lock = threading.Lock()

    def append(
        self,
        type: str,
        batch_id: str,
        occurred_at: datetime,
        actor: str,
        payload: dict | None = None,
        justification: str | None = None,
        recorded_at: datetime | None = None,
    ) -> Event:
        if type in _JUSTIFIED and not (justification and justification.strip()):
            raise ValueError(_JUSTIFIED[type])
        with self._lock:
            self._seq += 1
            event = Event(
                seq=self._seq,
                type=type,
                batch_id=batch_id,
                occurred_at=occurred_at,
                recorded_at=recorded_at or datetime.now(timezone.utc),
                actor=actor,
                payload=payload or {},
                justification=justification,
            )
            self._events.append(event)
            return event

    def for_batch(self, batch_id: str) -> list[Event]:
        """按现场时间校正后的事件序列，断网补录的事件回到其真实发生位置。"""
        return sorted(
            (e for e in self._events if e.batch_id == batch_id),
            key=lambda e: (e.occurred_at, e.seq),
        )

    def all(self) -> list[Event]:
        return list(self._events)

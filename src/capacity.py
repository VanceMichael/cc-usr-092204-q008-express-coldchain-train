"""车厢容量占用：站场多人同时办理时，检查与占用在同一临界区内完成，不可超装。"""
from __future__ import annotations

import threading


class OverbookedError(Exception):
    """容量不足，占用被拒绝。"""


class CapacityPool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holds: dict[str, dict[str, int]] = {}  # 车厢号 -> {占用键: 托盘数}

    def hold(self, carriage_id: str, key: str, pallets: int, limit: int) -> None:
        """原子占用。同一占用键重复提交同数量视为幂等成功，防止断网重试造成重复占用。"""
        if pallets <= 0:
            raise ValueError("占用托盘数必须为正")
        with self._lock:
            holds = self._holds.setdefault(carriage_id, {})
            if key in holds:
                if holds[key] == pallets:
                    return
                raise ValueError(f"占用键 {key} 已存在且数量不一致")
            if sum(holds.values()) + pallets > limit:
                raise OverbookedError(
                    f"车厢 {carriage_id} 剩余容量不足，无法占用 {pallets} 托"
                )
            holds[key] = pallets

    def release(self, carriage_id: str, key: str) -> None:
        with self._lock:
            self._holds.get(carriage_id, {}).pop(key, None)

    def used(self, carriage_id: str) -> int:
        with self._lock:
            return sum(self._holds.get(carriage_id, {}).values())

    def free(self, carriage_id: str, limit: int) -> int:
        return limit - self.used(carriage_id)

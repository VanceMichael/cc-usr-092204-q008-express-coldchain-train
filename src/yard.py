"""站场编排：批次生命周期、温控隔离、拆并转班与装载执行。

不变量：
- 温度补报不得覆盖已经触发的隔离，只有附业务依据的人工放行可以解除；
- 拆分、合并、转班都保留原始批次号（lineage）；
- 优先级变化必须给出业务依据；
- 容量占用经 CapacityPool 原子完成，多人同时办理不可超装。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from src.capacity import CapacityPool, OverbookedError
from src.decision import DecisionResult, evaluate_loading
from src.events import EventLog
from src.models import Batch, BatchStatus, Decision, SensorQuality, Train

_TERMINAL = {BatchStatus.LOADED, BatchStatus.DISPOSED, BatchStatus.CONSUMED}
_OPEN = {BatchStatus.EXPECTED, BatchStatus.ARRIVED, BatchStatus.TRANSFERRED, BatchStatus.QUARANTINED}
_SENSOR_RANK = {SensorQuality.GOOD: 0, SensorQuality.DEGRADED: 1, SensorQuality.OFFLINE: 2}


class Yard:
    def __init__(self) -> None:
        self.trains: dict[str, Train] = {}
        self.batches: dict[str, Batch] = {}
        self.assignments: dict[str, str] = {}  # 批次号 -> 班次号
        self.log = EventLog()
        self.pool = CapacityPool()

    # ---- 登记 ----

    def register_train(self, train: Train) -> None:
        self.trains[train.train_id] = train

    def register_batch(self, batch: Batch, train_id: str, actor: str, now: datetime) -> None:
        if batch.batch_id in self.batches:
            raise ValueError(f"批次 {batch.batch_id} 已存在")
        self._train(train_id)
        batch.lineage = batch.lineage or (batch.batch_id,)
        self.batches[batch.batch_id] = batch
        self.assignments[batch.batch_id] = train_id
        self.log.append(
            "batch_registered",
            batch.batch_id,
            occurred_at=now,
            actor=actor,
            payload={"train_id": train_id, "lineage": list(batch.lineage)},
        )

    # ---- 到场与温控 ----

    def arrive(self, batch_id: str, at: datetime, actor: str) -> None:
        batch = self._open_batch(batch_id)
        batch.arrived_at = at
        if batch.status is BatchStatus.EXPECTED:
            batch.status = BatchStatus.ARRIVED
        self.log.append("arrived", batch_id, occurred_at=at, actor=actor)

    def report_temperature(
        self,
        batch_id: str,
        temp: float,
        occurred_at: datetime,
        actor: str,
        quality: SensorQuality | None = None,
        recorded_at: datetime | None = None,
    ) -> None:
        """温度上报。occurred_at 为现场时间，断网恢复补录时按它校正顺序。

        已触发隔离的批次，补报温度只留痕（applied=False），不覆盖隔离状态。
        """
        batch = self._batch(batch_id)
        if quality is not None:
            batch.sensor = quality
        batch.last_temp = temp
        in_range = batch.cargo.temp_min <= temp <= batch.cargo.temp_max
        if batch.status is BatchStatus.QUARANTINED:
            self.log.append(
                "temperature_reported",
                batch_id,
                occurred_at=occurred_at,
                actor=actor,
                payload={"temp": temp, "in_range": in_range, "applied": False,
                         "note": "隔离期间温度补报，不覆盖已触发的隔离"},
                recorded_at=recorded_at,
            )
            return
        self.log.append(
            "temperature_reported",
            batch_id,
            occurred_at=occurred_at,
            actor=actor,
            payload={"temp": temp, "in_range": in_range, "applied": True},
            recorded_at=recorded_at,
        )
        if not in_range and batch.status not in _TERMINAL:
            batch.status = BatchStatus.QUARANTINED
            self.log.append(
                "quarantine_triggered",
                batch_id,
                occurred_at=occurred_at,
                actor=actor,
                payload={"temp": temp,
                         "range": [batch.cargo.temp_min, batch.cargo.temp_max]},
                recorded_at=recorded_at,
            )

    def release_quarantine(self, batch_id: str, justification: str, actor: str, now: datetime) -> None:
        batch = self._batch(batch_id)
        if batch.status is not BatchStatus.QUARANTINED:
            raise ValueError("批次未处于隔离状态")
        self.log.append(
            "quarantine_released", batch_id, occurred_at=now, actor=actor,
            justification=justification,
        )
        batch.status = BatchStatus.ARRIVED

    def change_priority(self, batch_id: str, new_priority: int, justification: str,
                        actor: str, now: datetime) -> None:
        batch = self._open_batch(batch_id)
        old = batch.priority
        self.log.append(
            "priority_changed", batch_id, occurred_at=now, actor=actor,
            payload={"old": old, "new": new_priority},
            justification=justification,
        )
        batch.priority = new_priority

    # ---- 拆分、合并、转班（均保留原始批次） ----

    def split(self, batch_id: str, parts: list[tuple[str, int]], actor: str,
              now: datetime) -> list[Batch]:
        parent = self._open_batch(batch_id)
        if not parts or any(p <= 0 for _, p in parts):
            raise ValueError("拆分份数必须为正")
        if sum(p for _, p in parts) != parent.pallets:
            raise ValueError("拆分托盘数之和必须等于原批次")
        children = []
        for new_id, pallets in parts:
            if new_id in self.batches:
                raise ValueError(f"批次 {new_id} 已存在")
            child = Batch(
                batch_id=new_id,
                shipper_id=parent.shipper_id,
                pallets=pallets,
                cargo=parent.cargo,
                priority=parent.priority,
                status=parent.status,
                arrived_at=parent.arrived_at,
                sensor=parent.sensor,
                last_temp=parent.last_temp,
                lineage=parent.lineage,  # 保留原始批次
            )
            self.batches[new_id] = child
            self.assignments[new_id] = self.assignments[batch_id]
            children.append(child)
        parent.status = BatchStatus.CONSUMED
        del self.assignments[batch_id]
        self.log.append(
            "batch_split", batch_id, occurred_at=now, actor=actor,
            payload={"children": [c.batch_id for c in children],
                     "lineage": list(parent.lineage)},
        )
        return children

    def merge(self, batch_ids: list[str], new_id: str, actor: str, now: datetime) -> Batch:
        if new_id in self.batches:
            raise ValueError(f"批次 {new_id} 已存在")
        parents = [self._open_batch(bid) for bid in batch_ids]
        if len({p.shipper_id for p in parents}) != 1:
            raise ValueError("仅同一发货人的批次可合并")
        if len({p.cargo for p in parents}) != 1:
            raise ValueError("仅同货品耐受参数的批次可合并")
        lineage: tuple[str, ...] = tuple(dict.fromkeys(
            bid for p in parents for bid in p.lineage
        ))  # 保留全部原始批次
        temps = [p.last_temp for p in parents if p.last_temp is not None]
        arrived = [p.arrived_at for p in parents if p.arrived_at is not None]
        child = Batch(
            batch_id=new_id,
            shipper_id=parents[0].shipper_id,
            pallets=sum(p.pallets for p in parents),
            cargo=parents[0].cargo,
            priority=max(p.priority for p in parents),
            status=(BatchStatus.QUARANTINED if any(p.status is BatchStatus.QUARANTINED for p in parents)
                    else parents[0].status),
            arrived_at=max(arrived) if arrived else None,
            sensor=max((p.sensor for p in parents), key=_SENSOR_RANK.__getitem__),
            last_temp=max(temps) if temps else None,
            lineage=lineage,
        )
        self.batches[new_id] = child
        self.assignments[new_id] = self.assignments[parents[0].batch_id]
        for p in parents:
            p.status = BatchStatus.CONSUMED
            del self.assignments[p.batch_id]
        self.log.append(
            "batch_merged", new_id, occurred_at=now, actor=actor,
            payload={"parents": batch_ids, "lineage": list(lineage)},
        )
        return child

    def transfer(self, batch_id: str, to_train_id: str, actor: str, now: datetime) -> None:
        batch = self._open_batch(batch_id)
        self._train(to_train_id)
        from_train = self.assignments.get(batch_id)
        self.assignments[batch_id] = to_train_id
        batch.status = BatchStatus.TRANSFERRED
        self.log.append(
            "batch_transferred", batch_id, occurred_at=now, actor=actor,
            payload={"from_train": from_train, "to_train": to_train_id,
                     "lineage": list(batch.lineage)},
        )

    def remarshal(self, train_id: str, new_carriages, actor: str, now: datetime,
                  reason: str) -> None:
        """临时换编：替换班次编组。被摘车厢不得仍有占用。"""
        train = self._train(train_id)
        old_ids = {c.carriage_id for c in train.carriages}
        new_ids = {c.carriage_id for c in new_carriages}
        for gone in old_ids - new_ids:
            if self.pool.used(self._slot(train_id, gone)):
                raise ValueError(f"车厢 {gone} 仍有容量占用，不可摘车")
        self.trains[train_id] = replace(train, carriages=tuple(new_carriages))
        self.log.append(
            "consist_changed", train_id, occurred_at=now, actor=actor,
            payload={"removed": sorted(old_ids - new_ids),
                     "added": sorted(new_ids - old_ids), "reason": reason},
        )

    # ---- 装载执行 ----

    def decide(self, batch_id: str, train_id: str, actor: str, now: datetime) -> DecisionResult:
        batch = self._batch(batch_id)
        if batch.status in _TERMINAL:
            raise ValueError(f"批次已终结（{batch.status.value}），不可再决策")
        train = self._train(train_id)
        next_train = self._next_train(train)
        for _attempt in (1, 2):
            free = self._free(train)
            next_free = self._free(next_train) if next_train else None
            result = evaluate_loading(batch, train, free, next_train, next_free)
            if result.decision is Decision.DISPOSE_ONSITE:
                break
            target_train = self._train(result.train_id)
            carriage = next(c for c in target_train.carriages
                            if c.carriage_id == result.carriage_id)
            try:
                # 车厢占用按班次命名空间隔离；占用键含批次，断网重试幂等
                self.pool.hold(self._slot(result.train_id, carriage.carriage_id),
                               key=batch_id,
                               pallets=batch.pallets, limit=carriage.capacity_pallets)
                break
            except OverbookedError:
                continue  # 快照与占用之间被他人抢先，按最新余量重估一次
        else:
            raise OverbookedError(f"车厢 {result.carriage_id} 容量竞争失败")

        if result.decision is Decision.LOAD_CURRENT:
            batch.status = BatchStatus.LOADED
        elif result.decision is Decision.TRANSFER_NEXT:
            self.assignments[batch_id] = result.train_id
            batch.status = BatchStatus.TRANSFERRED
        else:
            batch.status = BatchStatus.DISPOSED
        self.log.append(
            "loading_decided", batch_id, occurred_at=now, actor=actor,
            payload={"decision": result.decision.value, "train_id": result.train_id,
                     "carriage_id": result.carriage_id, "reasons": list(result.reasons)},
        )
        return result

    # ---- 内部 ----

    def open_batches(self, train_id: str) -> list[Batch]:
        return [b for b in self.batches.values()
                if self.assignments.get(b.batch_id) == train_id and b.status in _OPEN]

    def _next_train(self, train: Train) -> Train | None:
        later = [t for t in self.trains.values()
                 if t.origin == train.origin and t.destination == train.destination
                 and t.departure > train.departure]
        return min(later, key=lambda t: t.departure, default=None)

    def _free(self, train: Train) -> dict[str, int]:
        return {c.carriage_id: self.pool.free(self._slot(train.train_id, c.carriage_id),
                                              c.capacity_pallets)
                for c in train.carriages}

    @staticmethod
    def _slot(train_id: str, carriage_id: str) -> str:
        return f"{train_id}/{carriage_id}"

    def _train(self, train_id: str) -> Train:
        if train_id not in self.trains:
            raise KeyError(train_id)
        return self.trains[train_id]

    def _batch(self, batch_id: str) -> Batch:
        if batch_id not in self.batches:
            raise KeyError(batch_id)
        return self.batches[batch_id]

    def _open_batch(self, batch_id: str) -> Batch:
        batch = self._batch(batch_id)
        if batch.status in _TERMINAL:
            raise ValueError(f"批次已终结（{batch.status.value}）")
        return batch

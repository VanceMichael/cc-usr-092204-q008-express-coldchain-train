"""装载决策：依据班次时刻、装卸截止、耐受窗口、传感器质量、车厢能力与目的站接驳，
给出上本班、转下一班或就地处置的结论，并保留全部判定理由。"""
from __future__ import annotations

from dataclasses import dataclass

from src.models import Batch, BatchStatus, Carriage, Decision, SensorQuality, Train


@dataclass(frozen=True)
class DecisionResult:
    decision: Decision
    reasons: tuple[str, ...]
    train_id: str
    carriage_id: str | None = None


def evaluate_loading(
    batch: Batch,
    train: Train,
    free_capacity: dict[str, int],
    next_train: Train | None = None,
    next_free: dict[str, int] | None = None,
) -> DecisionResult:
    """对单个批次在给定班次上形成装载决定。free_capacity 为车厢号到剩余托盘位的映射。"""
    blocked = _hard_blockers(batch, train)
    if blocked is not None:
        return blocked

    if batch.arrived_at is None:
        return _transfer_or_dispose(
            batch, train, ("批次尚未送达站台，赶不上本班装卸",), next_train, next_free
        )
    if batch.arrived_at > train.cutoff:
        return _transfer_or_dispose(
            batch,
            train,
            (
                f"前序公路晚到：到站 {_hm(batch.arrived_at)} 晚于装卸截止 {_hm(train.cutoff)}",
            ),
            next_train,
            next_free,
        )

    deadline = batch.arrived_at + batch.cargo.max_dwell
    if train.arrival > deadline:
        return _transfer_or_dispose(
            batch,
            train,
            (
                f"本班到达 {_hm(train.arrival)} 超出货品耐受窗口（最晚交付 {_hm(deadline)}）",
            ),
            next_train,
            next_free,
        )

    if not train.connection.covers(train.arrival):
        return _transfer_or_dispose(
            batch,
            train,
            (f"目的站接驳窗口不覆盖本班到达时刻 {_hm(train.arrival)}",),
            next_train,
            next_free,
        )

    carriage = _pick_carriage(batch, train, free_capacity)
    if carriage is None:
        return _transfer_or_dispose(
            batch,
            train,
            ("本班无温区匹配且容量足够的冷链车厢",),
            next_train,
            next_free,
        )

    reasons = ["装卸截止、耐受窗口、车厢能力与目的站接驳均满足"]
    if batch.sensor is SensorQuality.DEGRADED:
        reasons.append("传感器质量降级，已按保守策略核验在库温度")
    return DecisionResult(Decision.LOAD_CURRENT, tuple(reasons), train.train_id, carriage.carriage_id)


def _hard_blockers(batch: Batch, train: Train) -> DecisionResult | None:
    """隔离、传感器离线与温度超限是一票否决，不进入转班判断。"""
    if batch.status is BatchStatus.QUARANTINED:
        return DecisionResult(
            Decision.DISPOSE_ONSITE,
            ("批次处于隔离待检，未经人工放行不得装车",),
            train.train_id,
        )
    if batch.sensor is SensorQuality.OFFLINE:
        return DecisionResult(
            Decision.DISPOSE_ONSITE,
            ("温度传感器离线，冷链状态不可核验，须就地待检",),
            train.train_id,
        )
    if batch.last_temp is not None and not (
        batch.cargo.temp_min <= batch.last_temp <= batch.cargo.temp_max
    ):
        return DecisionResult(
            Decision.DISPOSE_ONSITE,
            (
                f"最新温度 {batch.last_temp}℃ 超出货品耐受范围 "
                f"[{batch.cargo.temp_min}, {batch.cargo.temp_max}]，须就地处置",
            ),
            train.train_id,
        )
    return None


def _transfer_or_dispose(
    batch: Batch,
    train: Train,
    reasons: tuple[str, ...],
    next_train: Train | None,
    next_free: dict[str, int] | None,
) -> DecisionResult:
    if next_train is not None and next_free is not None:
        alt = evaluate_loading(batch, next_train, next_free)
        if alt.decision is Decision.LOAD_CURRENT:
            return DecisionResult(
                Decision.TRANSFER_NEXT,
                reasons + (f"下一班 {next_train.train_id} 可满足装载条件",),
                next_train.train_id,
                alt.carriage_id,
            )
        reasons += tuple(f"下一班亦不可行：{r}" for r in alt.reasons)
    return DecisionResult(
        Decision.DISPOSE_ONSITE,
        reasons + ("无可行后续班次，须就地处置",),
        train.train_id,
    )


def _pick_carriage(
    batch: Batch, train: Train, free_capacity: dict[str, int]
) -> Carriage | None:
    candidates = [
        c
        for c in train.carriages
        if c.supports(batch.cargo) and free_capacity.get(c.carriage_id, 0) >= batch.pallets
    ]
    if not candidates:
        return None
    # 小车厢优先，减少运力碎片
    return min(candidates, key=lambda c: c.capacity_pallets)


def _hm(moment) -> str:
    return moment.isoformat(timespec="minutes")

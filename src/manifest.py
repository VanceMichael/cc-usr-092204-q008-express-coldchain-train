"""截关可执行清单：按优先级从高到低逐批形成装载决定并占用容量。"""
from __future__ import annotations

from datetime import datetime

from src.models import Decision
from src.yard import Yard


def build_manifest(yard: Yard, train_id: str, now: datetime) -> dict:
    train = yard._train(train_id)
    # 优先级高的批次先占用容量；优先级调整必须已在事件日志中附业务依据
    pending = sorted(
        yard.open_batches(train_id), key=lambda b: (-b.priority, b.batch_id)
    )
    items = []
    for batch in pending:
        result = yard.decide(batch.batch_id, train_id, actor="清单生成", now=now)
        items.append({
            "batch_id": batch.batch_id,
            "lineage": list(batch.lineage),
            "pallets": batch.pallets,
            "action": result.decision.value,
            "train_id": result.train_id,
            "carriage_id": result.carriage_id,
            "reasons": list(result.reasons),
        })
    summary = {d.value: sum(1 for i in items if i["action"] == d.value) for d in Decision}
    return {
        "train_id": train.train_id,
        "destination": train.destination,
        "cutoff": train.cutoff.isoformat(),
        "generated_at": now.isoformat(),
        "items": items,
        "summary": summary,
    }

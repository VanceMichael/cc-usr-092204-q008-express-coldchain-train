"""货损争议说明：按现场时间重建批次时间线，
解释每段等待、温控与人工放行怎样影响最终结果。"""
from __future__ import annotations

from src.events import Event
from src.yard import Yard

_MILESTONES = {
    "arrived", "quarantine_triggered", "quarantine_released",
    "loading_decided", "batch_transferred",
}


def explain_dispute(yard: Yard, batch_id: str) -> dict:
    batch = yard._batch(batch_id)
    events = yard.log.for_batch(batch_id)  # 已按现场时间校正
    return {
        "batch_id": batch_id,
        "lineage": list(batch.lineage),
        "outcome": batch.status.value,
        "timeline": [_describe(e) for e in events],
        "waits": _wait_segments(events),
        "temp_excursions": _temp_excursions(events),
        "manual_releases": _releases(events),
        "factors": _factors(batch, events),
    }


def _describe(event: Event) -> dict:
    at = event.occurred_at.isoformat()
    p = event.payload
    if event.type == "batch_registered":
        text = f"批次登记，原始批次链 {p.get('lineage')}，预配班次 {p.get('train_id')}"
    elif event.type == "arrived":
        text = "前序公路送达站台"
    elif event.type == "temperature_reported":
        text = f"温度上报 {p.get('temp')}℃（{'正常' if p.get('in_range') else '超限'}）"
        if not p.get("applied", True):
            text += "，隔离期间补报不覆盖隔离状态"
    elif event.type == "quarantine_triggered":
        text = f"温度 {p.get('temp')}℃ 超出 {p.get('range')}，触发隔离"
    elif event.type == "quarantine_released":
        text = f"人工放行（{event.actor}）：{event.justification}"
    elif event.type == "priority_changed":
        text = (f"优先级 {p.get('old')} 调整为 {p.get('new')}，"
                f"业务依据：{event.justification}")
    elif event.type == "batch_split":
        text = f"托盘拆分为 {p.get('children')}，保留原始批次 {p.get('lineage')}"
    elif event.type == "batch_merged":
        text = f"由 {p.get('parents')} 合并，保留原始批次 {p.get('lineage')}"
    elif event.type == "batch_transferred":
        text = f"转班 {p.get('from_train')} → {p.get('to_train')}，原始批次保留"
    elif event.type == "loading_decided":
        text = f"装载决定：{p.get('decision')}（班次 {p.get('train_id')}）；{'；'.join(p.get('reasons', []))}"
    else:
        text = event.type
    return {"at": at, "type": event.type, "actor": event.actor, "text": text}


def _wait_segments(events: list[Event]) -> list[dict]:
    milestones = [e for e in events if e.type in _MILESTONES]
    segments = []
    for a, b in zip(milestones, milestones[1:]):
        segments.append({
            "from": a.type,
            "to": b.type,
            "minutes": round((b.occurred_at - a.occurred_at).total_seconds() / 60, 1),
            "note": _segment_note(a, b),
        })
    return segments


def _segment_note(a: Event, b: Event) -> str:
    pair = (a.type, b.type)
    if pair == ("arrived", "quarantine_triggered"):
        return "到站后温控异常，进入隔离等待"
    if pair == ("quarantine_triggered", "quarantine_released"):
        return "隔离期间等待人工放行，此段时间批次不可装车"
    if pair == ("quarantine_released", "loading_decided"):
        return "放行后等待装载决定"
    if a.type == "arrived" and b.type == "loading_decided":
        decision = b.payload.get("decision")
        return {"上本班": "到站后等待装车", "转下一班": "等待后转下一班",
                "就地处置": "等待后就地处置"}.get(decision, "等待装载决定")
    if b.type == "loading_decided":
        return "等待装载决定"
    return "状态间隔"


def _temp_excursions(events: list[Event]) -> list[dict]:
    excursions = []
    current = None
    for e in events:
        if e.type != "temperature_reported":
            continue
        if not e.payload.get("in_range", True):
            if current is None:
                current = {"start": e.occurred_at, "end": e.occurred_at,
                           "count": 0, "peak": e.payload.get("temp")}
            current["end"] = e.occurred_at
            current["count"] += 1
            current["peak"] = max(current["peak"], e.payload.get("temp"))
        elif current is not None:
            excursions.append(current)
            current = None
    if current is not None:
        excursions.append(current)
    return [{
        "start": x["start"].isoformat(),
        "end": x["end"].isoformat(),
        "duration_minutes": round((x["end"] - x["start"]).total_seconds() / 60, 1),
        "reports": x["count"],
        "peak_temp": x["peak"],
    } for x in excursions]


def _releases(events: list[Event]) -> list[dict]:
    return [{
        "at": e.occurred_at.isoformat(),
        "actor": e.actor,
        "justification": e.justification,
    } for e in events if e.type == "quarantine_released"]


def _factors(batch, events: list[Event]) -> list[str]:
    factors = []
    for e in events:
        if e.type == "loading_decided" and e.payload.get("decision") != "上本班":
            factors.append(
                f"{e.payload.get('decision')}的直接原因：{'；'.join(e.payload.get('reasons', []))}"
            )
    if any(e.type == "quarantine_triggered" for e in events):
        factors.append("温控异常触发隔离，隔离期间批次不得装车，后续温度补报不覆盖隔离状态")
    for e in events:
        if e.type == "quarantine_released":
            factors.append(
                f"人工放行（{e.actor}，依据：{e.justification}）使批次恢复可装状态"
            )
        if e.type == "priority_changed":
            factors.append(
                f"优先级调整（依据：{e.justification}）影响截关前的容量分配顺序"
            )
    factors.append(f"最终结果：{batch.status.value}")
    return factors

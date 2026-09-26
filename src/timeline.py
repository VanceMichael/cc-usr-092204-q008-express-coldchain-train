"""货损争议时间线。

沿原始批次链与当前批次，把每段等待、温控证据、隔离与人工放行按现场时间排成一条线，
并给出各因素对最终结果的贡献说明。扫描使用校正后的现场时间；
补报温度标记 backfill，且永远不会显示为"撤销隔离"。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .ledger import eval_exposure
from .models import Batch, State, iso


def _pair_exposure(b: Batch, start: datetime, end: datetime) -> float:
    """把 [start, end] 区间内相邻可采信点之间的超窗暴露分钟按时间夹取归集。"""
    good = [o for o in b.obs if o.admissible and not o.suspect
            and b.tendered_at <= o.occurred_at]
    total = 0.0
    prev = None
    for o in good:
        if prev is not None:
            seg_s = max(prev.occurred_at, start)
            seg_e = min(o.occurred_at, end)
            if seg_e > seg_s:
                bad = (prev.temp_c < b.temp_min or prev.temp_c > b.temp_max
                       or o.temp_c < b.temp_min or o.temp_c > b.temp_max)
                if bad:
                    total += (seg_e - seg_s).total_seconds() / 60.0
        prev = o
    if prev is not None and prev.occurred_at < end:
        seg_e = min(end, datetime.max.replace(tzinfo=prev.occurred_at.tzinfo))
        if prev.temp_c < b.temp_min or prev.temp_c > b.temp_max:
            total += max(0.0, (seg_e - max(prev.occurred_at, start)).total_seconds() / 60.0)
    return round(total, 1)


def build_timeline(state: State, batch_id: str) -> dict[str, Any]:
    with state.lock:
        b = state.batches[batch_id]
        roots = b.root_batch_ids(state)
        entries: list[dict[str, Any]] = []

        def add(at: datetime, kind: str, title: str, detail: str,
                **extra: Any) -> None:
            entries.append({"at": iso(at), "kind": kind, "title": title,
                            "detail": detail, **extra})

        add(b.tendered_at, "TENDER", "托运建立",
            f"批次 {b.batch_id}（{b.commodity}，{b.temp_zone} 温层 "
            f"[{b.temp_min},{b.temp_max}]°C），原始批次 {','.join(roots)}",
            actor=b.shipper_id)

        # 等待分段
        segments = _wait_segments(state, b)
        for seg in segments:
            add(seg["start"], "WAIT", seg["title"], seg["detail"],
                wait_min=seg["wait_min"], segment=seg["name"],
                out_of_window_min=seg["out_min"])

        # 温度读数（现场时间排序，补报与不可采信均标注）
        for o in sorted(b.obs, key=lambda x: x.occurred_at):
            tag = []
            if o.backfill:
                tag.append("补报")
            if not o.admissible:
                tag.append("不可采信")
            if o.suspect:
                tag.append("可疑传感器")
            in_window = b.temp_min <= o.temp_c <= b.temp_max
            note = ""
            if o.backfill and b.quarantines:
                note = "；补报数据不改变已触发的隔离"
            add(o.occurred_at, "TEMP",
                f"温度读数 {o.temp_c}°C{'（' + '、'.join(tag) + '）' if tag else ''}",
                f"传感器 {o.sensor_id}，{'温窗内' if in_window else '超出温窗'}{note}",
                event_id=o.event_id, backfill=o.backfill,
                admissible=o.admissible, received_at=iso(o.received_at))

        # 扫描（校正后现场时间）
        for s in sorted(state.scans.values(), key=lambda x: x.corrected_at or x.scanned_at):
            if s.pallet_id in b.pallet_origins or s.batch_id in roots \
                    or s.batch_id == b.batch_id:
                title = f"扫描 {s.kind}"
                if s.corrected:
                    title += "（断网恢复后按现场时间校正）"
                add(s.corrected_at or s.scanned_at, "SCAN", title,
                    f"托盘 {s.pallet_id}，设备 {s.device_id}，"
                    f"设备时间 {iso(s.scanned_at)}，接收 {iso(s.received_at)}")

        # 隔离与处置
        for q in sorted(b.quarantines, key=lambda x: x["raised_at"]):
            add(q["raised_at"], "QUARANTINE", f"隔离触发：{q['code']}",
                q["detail"], trigger_event_id=q["trigger_event_id"],
                evidence=q.get("evidence", "ADMISSIBLE"))
            d = q.get("disposition")
            if d:
                title = f"人工处置：{q['code']} → {d['disposition']}"
                detail = f"{d['reason']}（责任人 {d.get('actor') or '未记录'}）"
                add(d["at"], "DISPOSITION", title, detail,
                    actor=d.get("actor"), event_id=d["event_id"])

        # 决策（含被取代的历史版本）
        for d in b.decisions:
            add(d.basis.get("as_of") and datetime.fromisoformat(d.basis["as_of"])
                or b.tendered_at, "DECISION",
                f"{d.service_id} v{d.plan_version}：{d.action}",
                "依据：" + ",".join(d.reason_codes)
                + (f"；该决定已被 v{d.superseded_by} 取代" if d.superseded_by else "")
                + (f"；目标 {d.target_service_id}" if d.target_service_id else "")
                + (f"；车厢 {d.car_id}/{d.zone}" if d.car_id else ""),
                action=d.action, reasons=d.reason_codes,
                superseded_by=d.superseded_by, plan_version=d.plan_version)

        # 优先级变更依据
        for h in b.priority_history:
            if h.get("old") is not None:
                add(h["at"], "PRIORITY", f"优先级 {h['old']} → {h['new']}",
                    f"{h['business_reason']}（单据 {h.get('ref')}）", actor=h.get("actor"))

        if b.departed_at:
            add(b.departed_at, "DEPARTURE", f"随班发运 {b.departed_service}",
                f"最终状态 {b.final}")

        entries.sort(key=lambda e: (e["at"], _kind_order(e["kind"])))

        exposure = eval_exposure(state, b, b.departed_at or segments[-1]["end"])
        return {
            "batch_id": b.batch_id,
            "root_batch_ids": roots,
            "final_status": b.final or ("HELD" if any(
                q["status"] == "OPEN" for q in b.quarantines) else "OPEN"),
            "generated_from_events": len(state.events),
            "contributors": _contributors(b, segments, exposure),
            "entries": entries,
        }


def _kind_order(kind: str) -> int:
    return {
        "TENDER": 0, "WAIT": 1, "TEMP": 2, "SCAN": 2, "QUARANTINE": 3,
        "DECISION": 4, "PRIORITY": 4, "DISPOSITION": 5, "DEPARTURE": 9,
    }.get(kind, 8)


def _wait_segments(state: State, b: Batch) -> list[dict[str, Any]]:
    segs: list[dict[str, Any]] = []

    def push(name: str, start: Optional[datetime], end: Optional[datetime],
             title: str, detail: str) -> None:
        if start is None or end is None or end <= start:
            return
        wait = int((end - start).total_seconds() / 60)
        segs.append({
            "name": name, "start": start, "end": end, "wait_min": wait,
            "title": title, "detail": detail,
            "out_min": _pair_exposure(b, start, end),
        })

    # 公路段：托运 → 到站
    push("ROAD", b.tendered_at, b.arrived_at, "前序公路等待",
         "发货人交运至到站（晚到分钟在决策依据中体现）")
    if b.arrived_at is None:
        return segs

    # 站场段：按决策覆盖的班次截关切分（含转班等待）
    timeline_end = b.departed_at
    cur = b.arrived_at
    seen_services: set[str] = set()
    for d in b.decisions:
        svc = state.services.get(d.service_id)
        if not svc or d.service_id in seen_services:
            continue
        seen_services.add(d.service_id)
        push("YARD", cur, svc.load_cutoff,
             f"站场等待（{d.service_id} 截关前）",
             f"决定 {d.action}：{','.join(d.reason_codes)}")
        cur = svc.load_cutoff
        if d.action == "ROLLOVER" and d.target_service_id:
            tgt = state.services.get(d.target_service_id)
            if tgt:
                # 货物若晚于本班截关才到站，转班等待从实际到站起算
                seg_start = max(cur, b.arrived_at) if b.arrived_at else cur
                push("YARD", seg_start, tgt.load_cutoff,
                     f"转班等待（→ {tgt.service_id}）",
                     "暂存冷库并继续温度记录，追加等待计入耐受预算")
                cur = tgt.load_cutoff
        timeline_end = timeline_end or cur

    end = b.departed_at or timeline_end or cur
    if cur < end:
        push("YARD", cur, end, "站场等待（发运前）", "装车与发车复核")
    return segs


def _contributors(b: Batch, segments: list[dict[str, Any]],
                  exposure: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for seg in segments:
        if seg["wait_min"] <= 0:
            continue
        impact = "温窗内等待" if seg["out_min"] <= 0 else (
            f"其中约 {seg['out_min']} 分钟处于超窗状态，计入耐受预算")
        out.append({
            "factor": f"{seg['title']} {seg['wait_min']} 分钟",
            "segment": seg["name"], "wait_min": seg["wait_min"],
            "out_of_window_min": seg["out_min"], "impact": impact,
        })
    for g in exposure["gaps"]:
        out.append({
            "factor": f"温度记录缺口 {round(g['gap_min'])} 分钟",
            "segment": "TEMP_GAP",
            "wait_min": 0, "out_of_window_min": 0,
            "impact": "缺口期温度不可证实，规划按 SENSOR_UNVERIFIED/GAP 拦截，"
                      "须人工复核，不得按合规处理",
        })
    for q in b.quarantines:
        d = q.get("disposition")
        if d and d["disposition"] == "RELEASED":
            impact = (f"人工放行：{d['reason']}（责任人 {d.get('actor')}）；"
                      "放行后装车的温控后果由放行决策承担，补报数据不回溯消除本隔离")
        elif d:
            impact = f"人工处置为 {d['disposition']}：{d['reason']}"
        else:
            impact = "隔离未解除，货物不得装车，等待质检/授权值班处置"
        out.append({
            "factor": f"隔离 {q['code']}", "segment": "QUARANTINE",
            "wait_min": 0, "out_of_window_min": 0,
            "raised_at": iso(q["raised_at"]),
            "trigger_event_id": q["trigger_event_id"],
            "disposition": d["disposition"] if d else None, "impact": impact,
        })
    released = [q for q in b.quarantines if (q.get("disposition") or {}
                                              ).get("disposition") == "RELEASED"]
    if released and b.final == "DEPARTED":
        out.append({
            "factor": "最终结果说明", "segment": "VERDICT",
            "wait_min": 0, "out_of_window_min": 0,
            "impact": "货物在隔离被人工放行后随班发运；公路/站场等待与超窗暴露分钟见上，"
                      "责任分界以放行记录的责任人与单据为准",
        })
    elif b.final in ("SCRAPPED", "RETURNED"):
        out.append({
            "factor": "最终结果说明", "segment": "VERDICT",
            "wait_min": 0, "out_of_window_min": 0,
            "impact": f"货物{b.final == 'SCRAPPED' and '报废' or '退回'}，"
                      "直接原因为未解除/已处置的温控隔离，详见各隔离触发读数与等待暴露",
        })
    return out

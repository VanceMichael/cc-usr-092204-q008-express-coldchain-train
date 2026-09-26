"""事件入账：把外部事件应用到内存状态，并在入账点触发隔离。

关键不变量：
- 隔离一旦触发即落盘到批次上，之后的合规读数或温度补报不得自动清除；
  只有显式的 quarantine_dispose（人工处置）才能终结。
- 优先级变更必须携带业务依据，否则拒绝入账。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .models import (
    Batch,
    Car,
    EventRecord,
    Feeder,
    Pallet,
    Priority,
    Scan,
    SensorQuality,
    SensorState,
    Service,
    State,
    TempObs,
    ZoneCap,
    iso,
    parse_ts,
)


class LedgerError(ValueError):
    pass


# 隔离码
Q_HARD_BREACH = "HARD_BREACH"
Q_EXPOSURE = "EXPOSURE"
Q_GAP = "GAP"
Q_SENSOR_UNVERIFIED = "SENSOR_UNVERIFIED"


def apply_event(state: State, evt_type: str, payload: dict[str, Any],
                source: str = "api") -> EventRecord:
    occurred_at = parse_ts(payload["occurred_at"])
    received_at = parse_ts(payload.get("received_at") or payload["occurred_at"])
    with state.lock:
        seq = len(state.events) + 1
        event_id = payload.get("event_id") or f"EVT-{seq:06d}"
        if any(e.event_id == event_id for e in state.events):
            raise LedgerError(f"事件重复: {event_id}")
        rec = EventRecord(seq, event_id, evt_type, occurred_at, received_at,
                          dict(payload), source)
        handler = _HANDLERS.get(evt_type)
        if handler is None:
            raise LedgerError(f"未知事件类型: {evt_type}")
        handler(state, rec, payload)
        state.events.append(rec)
        return rec


# ---------- 基础资料 ----------

def _service_defined(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    sid = p["service_id"]
    cars: dict[str, Car] = {}
    for c in p.get("cars", []):
        cars[c["car_id"]] = Car(
            car_id=c["car_id"],
            zones={z["zone"]: ZoneCap(z["positions"], z["weight_kg"], z["volume_m3"])
                   for z in c["zones"]},
        )
    state.services[sid] = Service(
        service_id=sid,
        origin=p["origin"],
        destination=p["destination"],
        depart_at=parse_ts(p["depart_at"]),
        arrive_at=parse_ts(p["arrive_at"]),
        load_cutoff=parse_ts(p["load_cutoff"]),
        unload_lead_min=int(p.get("unload_lead_min", 40)),
        consist_version=int(p.get("consist_version", 0)),
        consist_reason=p.get("consist_reason"),
        cars=cars,
    )
    state.services[sid].consist_log.append({
        "at": rec.occurred_at, "version": 0,
        "reason": "初始编组", "cars": _snapshot_cars(cars)})


def _snapshot_cars(cars: dict[str, Car]) -> dict[str, dict[str, Any]]:
    return {cid: {"zones": {z: {"positions": zc.positions,
                                "weight_kg": zc.weight_kg,
                                "volume_m3": zc.volume_m3}
                            for z, zc in c.zones.items()}}
            for cid, c in cars.items()}


def _consist_changed(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    svc = _require_service(state, p["service_id"])
    svc.consist_version += 1
    svc.consist_reason = p.get("reason")
    for c in p.get("cars", []):
        svc.cars[c["car_id"]] = Car(
            car_id=c["car_id"],
            zones={z["zone"]: ZoneCap(z["positions"], z["weight_kg"], z["volume_m3"])
                   for z in c["zones"]},
        )
    for cid in p.get("removed_car_ids", []):
        svc.cars.pop(cid, None)
    svc.consist_log.append({
        "at": rec.occurred_at, "version": svc.consist_version,
        "reason": p.get("reason"), "cars": _snapshot_cars(svc.cars)})


def _feeder_defined(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    state.feeders[p["feeder_id"]] = Feeder(
        feeder_id=p["feeder_id"],
        carrier_id=p["carrier_id"],
        station=p["station"],
        handoff_cutoff=parse_ts(p["handoff_cutoff"]),
        depart_at=parse_ts(p["depart_at"]),
    )


# ---------- 批次与拆分/合并 ----------

_BATCH_FIELDS = (
    "batch_id", "shipper_id", "origin", "destination", "commodity", "temp_zone",
    "temp_min", "temp_max", "weight_kg", "volume_m3", "exposure_budget_min",
    "hard_temp_min", "hard_temp_max", "tendered_at",
)


def _build_batch(p: dict[str, Any], *, priority: Priority,
                 tendered_at: datetime,
                 origin_batch_ids: list[str] | None = None,
                 pallet_origins: dict[str, str] | None = None) -> Batch:
    pallets = [Pallet(pl["pallet_id"], pl["weight_kg"], pl["volume_m3"])
               for pl in p.get("pallets", [])]
    return Batch(
        batch_id=p["batch_id"], shipper_id=p["shipper_id"], origin=p["origin"],
        destination=p["destination"], commodity=p["commodity"],
        temp_zone=p["temp_zone"], temp_min=float(p["temp_min"]),
        temp_max=float(p["temp_max"]), weight_kg=float(p["weight_kg"]),
        volume_m3=float(p["volume_m3"]), pallets=pallets,
        exposure_budget_min=int(p["exposure_budget_min"]),
        hard_temp_min=(float(p["hard_temp_min"]) if p.get("hard_temp_min") is not None else None),
        hard_temp_max=(float(p["hard_temp_max"]) if p.get("hard_temp_max") is not None else None),
        priority=priority, tendered_at=tendered_at,
        origin_batch_ids=origin_batch_ids or [],
        pallet_origins=pallet_origins or {},
    )


def _batch_tendered(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    if p["batch_id"] in state.batches:
        raise LedgerError(f"批次已存在: {p['batch_id']}")
    tendered_at = parse_ts(p["tendered_at"]) if p.get("tendered_at") else rec.occurred_at
    b = _build_batch(p, priority=Priority(p.get("priority", "P3")),
                     tendered_at=tendered_at)
    b.priority_history.append({
        "at": rec.occurred_at, "event_id": rec.event_id,
        "old": None, "new": b.priority.value,
        "business_reason": "初次托运", "ref": p.get("ref"), "actor": p.get("actor"),
    })
    state.batches[b.batch_id] = b


def _inherit_spec(child: dict[str, Any], parents: list[Batch]) -> dict[str, Any]:
    base = parents[0]
    spec = {
        "shipper_id": child.get("shipper_id", base.shipper_id),
        "origin": base.origin, "destination": base.destination,
        "commodity": child.get("commodity", base.commodity),
        "temp_zone": base.temp_zone, "temp_min": base.temp_min,
        "temp_max": base.temp_max, "hard_temp_min": base.hard_temp_min,
        "hard_temp_max": base.hard_temp_max,
        "exposure_budget_min": base.exposure_budget_min,
    }
    for k in ("shipper_id", "commodity"):
        if child.get(k) is not None:
            spec[k] = child[k]
    return spec


def _batch_split(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    parent = _require_batch(state, p["batch_id"])
    by_id = {pl.pallet_id: pl for pl in parent.pallets}
    for sp in p["splits"]:
        if sp["new_batch_id"] in state.batches:
            raise LedgerError(f"批次已存在: {sp['new_batch_id']}")
        missing = [pid for pid in sp["pallet_ids"] if pid not in by_id]
        if missing:
            raise LedgerError(f"托盘不属于母批次: {missing}")
        pallets = [by_id[pid] for pid in sp["pallet_ids"]]
        spec = _inherit_spec(sp, [parent])
        payload = {
            "batch_id": sp["new_batch_id"], "pallets": [
                {"pallet_id": pl.pallet_id, "weight_kg": pl.weight_kg,
                 "volume_m3": pl.volume_m3} for pl in pallets],
            "weight_kg": sum(pl.weight_kg for pl in pallets),
            "volume_m3": sum(pl.volume_m3 for pl in pallets),
            **spec,
        }
        origins = dict(parent.pallet_origins)
        for pl in pallets:
            origins[pl.pallet_id] = parent.pallet_origins.get(pl.pallet_id, parent.batch_id)
        child = _build_batch(
            payload,
            priority=Priority(sp.get("priority", parent.priority.value)),
            tendered_at=rec.occurred_at,
            origin_batch_ids=[parent.batch_id],
            pallet_origins=origins,
        )
        child.arrived_at = parent.arrived_at
        child.eta = parent.eta
        child.seal_ok = parent.seal_ok
        child.priority_history.append({
            "at": rec.occurred_at, "event_id": rec.event_id, "old": None,
            "new": child.priority.value,
            "business_reason": f"由 {parent.batch_id} 拆分：{p.get('reason', '未注明')}",
            "ref": p.get("ref"), "actor": p.get("actor"),
        })
        state.batches[child.batch_id] = child
    moved = {pid for sp in p["splits"] for pid in sp["pallet_ids"]}
    parent.pallets = [pl for pl in parent.pallets if pl.pallet_id not in moved]
    parent.weight_kg = sum(pl.weight_kg for pl in parent.pallets)
    parent.volume_m3 = sum(pl.volume_m3 for pl in parent.pallets)


def _batch_merge(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    parents = [_require_batch(state, bid) for bid in p["batch_ids"]]
    new_id = p["new_batch_id"]
    if new_id in state.batches:
        raise LedgerError(f"批次已存在: {new_id}")
    pallets: list[Pallet] = []
    origins: dict[str, str] = {}
    for b in parents:
        pallets.extend(b.pallets)
        for pl in b.pallets:
            origins[pl.pallet_id] = b.pallet_origins.get(pl.pallet_id, b.batch_id)
    spec = _inherit_spec(p, parents)
    payload = {
        "batch_id": new_id,
        "pallets": [{"pallet_id": pl.pallet_id, "weight_kg": pl.weight_kg,
                     "volume_m3": pl.volume_m3} for pl in pallets],
        "weight_kg": sum(pl.weight_kg for pl in pallets),
        "volume_m3": sum(pl.volume_m3 for pl in pallets),
        **spec,
    }
    merged = _build_batch(
        payload, priority=Priority(p.get("priority", "P2")),
        tendered_at=rec.occurred_at,
        origin_batch_ids=[b.batch_id for b in parents], pallet_origins=origins,
    )
    arrivals = [b.arrived_at for b in parents if b.arrived_at]
    merged.arrived_at = min(arrivals) if arrivals else None
    etas = [b.eta for b in parents if b.eta]
    merged.eta = min(etas) if etas else None
    merged.obs = [o for b in parents for o in b.obs]
    merged.quarantines = [dict(q, inherited_from=b.batch_id)
                          for b in parents for q in b.quarantines]
    for b in parents:
        b.final = "MERGED"
    merged.priority_history.append({
        "at": rec.occurred_at, "event_id": rec.event_id, "old": None,
        "new": merged.priority.value,
        "business_reason": f"合并 {','.join(b.batch_id for b in parents)}：{p.get('reason', '未注明')}",
        "ref": p.get("ref"), "actor": p.get("actor"),
    })
    state.batches[new_id] = merged


# ---------- 到站 / 扫描 / 优先级 ----------

def _batch_eta(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    b = _require_batch(state, p["batch_id"])
    eta = parse_ts(p["eta"])
    b.eta = eta
    b.eta_history.append({"at": rec.occurred_at, "eta": eta})


def _batch_arrived(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    b = _require_batch(state, p["batch_id"])
    arrived_at = parse_ts(p["arrived_at"])
    b.arrived_at = arrived_at
    if "seal_ok" in p:
        b.seal_ok = bool(p["seal_ok"])
    b.arrival_history.append({
        "at": rec.occurred_at, "arrived_at": arrived_at,
        "seal_ok": b.seal_ok})


def _priority_change(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    reason = (p.get("business_reason") or "").strip()
    if not reason:
        raise LedgerError("优先级变更必须给出业务依据 business_reason")
    if not p.get("ref"):
        raise LedgerError("优先级变更必须附带业务单据 ref")
    b = _require_batch(state, p["batch_id"])
    new = Priority(p["new_priority"])
    old = b.priority
    if new == old:
        return
    b.priority = new
    b.priority_history.append({
        "at": rec.occurred_at, "event_id": rec.event_id,
        "old": old.value, "new": new.value, "business_reason": reason,
        "ref": p["ref"], "actor": p.get("actor"),
    })


def _scan(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    sid = p["scan_id"]
    if sid in state.scans:
        raise LedgerError(f"扫描重复: {sid}")
    scanned_at = parse_ts(p.get("scanned_at") or p["occurred_at"])
    skew = state.clock_skew.get(p["device_id"], 0.0)
    corrected_at = scanned_at - timedelta(seconds=skew) if skew else scanned_at
    scan = Scan(
        scan_id=sid, device_id=p["device_id"], pallet_id=p["pallet_id"],
        batch_id=p["batch_id"], scanned_at=scanned_at, received_at=rec.received_at,
        kind=p["kind"], corrected=bool(skew), corrected_at=corrected_at,
    )
    state.scans[sid] = scan
    rec.payload["corrected_at"] = iso(scan.corrected_at)
    rec.payload["clock_skew_sec"] = skew


def _clock_sync(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    device_t = parse_ts(p["device_time"])
    server_t = parse_ts(p["server_time"])
    skew = (device_t - server_t).total_seconds()
    state.clock_skew[p["device_id"]] = skew
    # 断网期间已入账的扫描按新学到的偏移回溯校正（现场时间优先）
    for scan in state.scans.values():
        if scan.device_id != p["device_id"]:
            continue
        scan.corrected = True
        scan.corrected_at = scan.scanned_at - timedelta(seconds=skew)


# ---------- 传感器与温度 ----------

def _sensor_state(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    state.sensor_states.setdefault(p["sensor_id"], []).append(SensorState(
        occurred_at=rec.occurred_at, sensor_id=p["sensor_id"],
        quality=SensorQuality(p.get("quality", "OK")),
        calibrated=bool(p["calibrated"]),
        calib_until=(parse_ts(p["calib_until"]) if p.get("calib_until") else None),
        max_gap_min=int(p.get("max_gap_min", 30)), event_id=rec.event_id,
    ))


def _sensor_at(state: State, sensor_id: str, at: datetime) -> Optional[SensorState]:
    hist = state.sensor_states.get(sensor_id, [])
    current: Optional[SensorState] = None
    for s in hist:
        if s.occurred_at <= at:
            current = s
    return current


def _reading_admissible(state: State, obs: TempObs) -> tuple[bool, bool]:
    """返回 (可作为温控证据, 可疑)。校准失效/故障的读数一律不可采信。"""
    s = _sensor_at(state, obs.sensor_id, obs.occurred_at)
    if s is None:
        return False, True
    if s.quality is SensorQuality.INVALID or not s.calibrated:
        return False, False
    if s.calib_until and s.calib_until < obs.occurred_at:
        return False, False
    if s.quality is SensorQuality.SUSPECT:
        return True, True
    return True, False


def _raise_quarantine(batch: Batch, code: str, at: datetime, trigger_event_id: str,
                      detail: str, evidence: str = "ADMISSIBLE") -> None:
    """触发隔离；已存在同码未处置隔离时不重复、不覆盖。"""
    for q in batch.quarantines:
        if q["code"] == code and q["status"] == "OPEN":
            return
    batch.quarantines.append({
        "code": code, "raised_at": at, "trigger_event_id": trigger_event_id,
        "detail": detail, "evidence": evidence, "status": "OPEN",
        "disposition": None,
    })


def _temp_reading(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    b = _require_batch(state, p["batch_id"])
    obs = TempObs(
        occurred_at=rec.occurred_at, sensor_id=p["sensor_id"],
        temp_c=float(p["temp_c"]), event_id=rec.event_id,
        backfill=bool(p.get("backfill", False)), received_at=rec.received_at,
    )
    admissible, suspect = _reading_admissible(state, obs)
    obs.admissible = admissible
    obs.suspect = suspect
    b.obs.append(obs)
    b.obs.sort(key=lambda o: o.occurred_at)

    if not admissible:
        _raise_quarantine(
            b, Q_SENSOR_UNVERIFIED, rec.occurred_at, rec.event_id,
            f"传感器 {p['sensor_id']} 校准失效/故障，读数不可采信",
            evidence="INADMISSIBLE",
        )
        return

    # 硬性限值：即可疑超限也必须拦下待核，不得自动放行
    hard_hit = (
        (b.hard_temp_min is not None and obs.temp_c < b.hard_temp_min)
        or (b.hard_temp_max is not None and obs.temp_c > b.hard_temp_max)
    )
    if hard_hit:
        _raise_quarantine(
            b, Q_HARD_BREACH, rec.occurred_at, rec.event_id,
            f"读数 {obs.temp_c}°C 超出硬限值 "
            f"[{b.hard_temp_min}, {b.hard_temp_max}]{'（可疑传感器，待复核）' if suspect else ''}",
            evidence="SUSPECT" if suspect else "ADMISSIBLE",
        )
    # 累计超窗暴露在规划时统一复算（需要 as_of 上界），
    # 此处先用当前已有点位即时触发一次。
    eval_exposure(state, b, rec.occurred_at, raise_if_exceeded=True)


def _quarantine_dispose(state: State, rec: EventRecord, p: dict[str, Any]) -> None:
    b = _require_batch(state, p["batch_id"])
    code = p["code"]
    if not (p.get("disposition") or "").strip():
        raise LedgerError("人工处置必须给出 disposition")
    if not (p.get("reason") or "").strip():
        raise LedgerError("人工处置必须记录原因 reason")
    if p["disposition"] == "RELEASED" and not p.get("actor"):
        raise LedgerError("人工放行必须记录责任人 actor")
    open_qs = [q for q in b.quarantines if q["code"] == code and q["status"] == "OPEN"]
    if not open_qs:
        raise LedgerError(f"没有待处置的 {code} 隔离")
    for q in open_qs:
        q["status"] = "CLOSED"
        q["disposition"] = {
            "disposition": p["disposition"], "reason": p["reason"],
            "actor": p.get("actor"), "at": rec.occurred_at,
            "event_id": rec.event_id,
        }
    if p["disposition"] in ("SCRAPPED", "RETURNED"):
        b.final = p["disposition"]


# ---------- 温控评估（暴露积分 / 缺口） ----------

def eval_exposure(state: State, b: Batch, as_of: datetime,
                  raise_if_exceeded: bool = False) -> dict[str, Any]:
    """基于可采信非可疑读数，阶梯积分计算超窗暴露分钟与覆盖缺口。

    可疑读数参与超限拦截，但不能用于证明合规（不作为覆盖证据）。
    只统计 occurred_at <= as_of 的读数（点-in-time 口径）。
    """
    good = [o for o in b.obs if o.admissible and not o.suspect
            and o.occurred_at <= as_of]
    out_min = 0.0
    gaps: list[dict[str, Any]] = []
    window_start = b.tendered_at
    last_ok: Optional[TempObs] = None
    for o in good:
        if last_ok is not None:
            gap = (o.occurred_at - last_ok.occurred_at).total_seconds() / 60.0
            s = _sensor_at(state, o.sensor_id, o.occurred_at)
            max_gap = s.max_gap_min if s else 30
            if gap > max_gap:
                gaps.append({"from": iso(last_ok.occurred_at), "to": iso(o.occurred_at),
                             "gap_min": round(gap, 1), "max_gap_min": max_gap})
            # 区间内任一端超窗即整段计暴露（保守口径）
            if (last_ok.temp_c < b.temp_min or last_ok.temp_c > b.temp_max
                    or o.temp_c < b.temp_min or o.temp_c > b.temp_max):
                out_min += gap
        last_ok = o
    tail = None
    if last_ok is not None:
        tail = (as_of - last_ok.occurred_at).total_seconds() / 60.0
        s = _sensor_at(state, last_ok.sensor_id, last_ok.occurred_at)
        max_gap = s.max_gap_min if s else 30
        if tail > max_gap:
            gaps.append({"from": iso(last_ok.occurred_at), "to": iso(as_of),
                         "gap_min": round(tail, 1), "max_gap_min": max_gap})
        if last_ok.temp_c < b.temp_min or last_ok.temp_c > b.temp_max:
            out_min += max(tail, 0.0)
    unverified = (last_ok is None) or (tail is not None and tail > (
        (_sensor_at(state, last_ok.sensor_id, last_ok.occurred_at).max_gap_min
         if last_ok and _sensor_at(state, last_ok.sensor_id, last_ok.occurred_at) else 30)))

    result = {
        "out_of_window_min": round(out_min, 1),
        "budget_min": b.exposure_budget_min,
        "within_budget": out_min <= b.exposure_budget_min,
        "gaps": gaps,
        "latest_ok_at": iso(last_ok.occurred_at) if last_ok else None,
        "unverified": unverified,
    }
    if raise_if_exceeded:
        if out_min > b.exposure_budget_min:
            _raise_quarantine(
                b, Q_EXPOSURE, as_of, b.obs[-1].event_id if b.obs else "NONE",
                f"累计超窗暴露 {out_min:.0f} 分钟，超过耐受预算 {b.exposure_budget_min} 分钟",
            )
        if gaps:
            _raise_quarantine(
                b, Q_GAP, as_of, b.obs[-1].event_id if b.obs else "NONE",
                f"温度记录存在 {len(gaps)} 处缺口（最大 "
                f"{max(g['gap_min'] for g in gaps):.0f} 分钟）",
            )
    return result


def _require_service(state: State, sid: str) -> Service:
    if sid not in state.services:
        raise LedgerError(f"未知班次: {sid}")
    return state.services[sid]


def _require_batch(state: State, bid: str) -> Batch:
    if bid not in state.batches:
        raise LedgerError(f"未知批次: {bid}")
    return state.batches[bid]


_HANDLERS = {
    "service_defined": _service_defined,
    "service_consist_changed": _consist_changed,
    "feeder_defined": _feeder_defined,
    "batch_tendered": _batch_tendered,
    "batch_split": _batch_split,
    "batch_merge": _batch_merge,
    "batch_eta": _batch_eta,
    "batch_arrived": _batch_arrived,
    "priority_change": _priority_change,
    "scan": _scan,
    "clock_sync": _clock_sync,
    "sensor_state": _sensor_state,
    "temp_reading": _temp_reading,
    "quarantine_dispose": _quarantine_dispose,
}

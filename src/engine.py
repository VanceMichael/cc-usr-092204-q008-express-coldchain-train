"""装载决策引擎。

在截关前对指定班次做整版重规划，输出每个批次的可执行决定：
LOAD（上本班）/ ROLLOVER（转下一班）/ HOLD（就地处置）。

容量安全：
- 所有分配在 state.lock 全局锁下原子完成，站场多人同时触发规划不会超装；
- 预约分 HELD（规划占用）/ COMMITTED（已装车）/ RELEASED（释放）；
- 转班预约直接落在目标班次上（kind=ROLLOVER），目标班次规划时将其并入再分配。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .ledger import eval_exposure
from . import snapshot
from .models import (
    Action,
    Batch,
    DecisionRecord,
    Reserve,
    Service,
    State,
    iso,
)


class EngineError(ValueError):
    pass


HANDLING_LEAD_MIN = 15        # 截关前最小场内操作时间
FEEDER_LEAD_MIN_DEFAULT = 40  # 到达后到接驳交接最小时间（取班次 unload_lead_min）


# ---------------- 容量账本 ----------------
class CapacityLedger:
    def __init__(self, state: State, service: Service):
        self.state = state
        self.service = service
        self.used: dict[tuple[str, str], dict[str, float]] = {}
        for car in service.cars.values():
            for zname, zone in car.zones.items():
                self.used[(car.car_id, zname)] = {
                    "positions": 0.0, "weight_kg": 0.0, "volume_m3": 0.0}

    def consume(self, reserve: Reserve) -> None:
        key = (reserve.car_id, reserve.zone)
        u = self.used[key]
        u["positions"] += reserve.positions
        u["weight_kg"] += reserve.weight_kg
        u["volume_m3"] += reserve.volume_m3

    def fits(self, car_id: str, zone: str, positions: int,
             weight: float, volume: float) -> bool:
        cap = self.service.cars[car_id].zones[zone]
        u = self.used[(car_id, zone)]
        return (u["positions"] + positions <= cap.positions
                and u["weight_kg"] + weight <= cap.weight_kg
                and u["volume_m3"] + volume <= cap.volume_m3)

    def remaining(self) -> list[dict[str, Any]]:
        out = []
        for car in self.service.cars.values():
            for zname, cap in car.zones.items():
                u = self.used[(car.car_id, zname)]
                out.append({
                    "car_id": car.car_id, "zone": zname,
                    "positions_left": cap.positions - int(u["positions"]),
                    "weight_kg_left": round(cap.weight_kg - u["weight_kg"], 1),
                    "volume_m3_left": round(cap.volume_m3 - u["volume_m3"], 2),
                })
        return out

    def find_zone(self, temp_zone: str, positions: int, weight: float,
                  volume: float) -> Optional[tuple[str, str]]:
        for car in self.service.cars.values():
            if temp_zone not in car.zones:
                continue
            if self.fits(car.car_id, temp_zone, positions, weight, volume):
                return car.car_id, temp_zone
        return None


def _active_reserves(state: State, service_id: str, latest_version: int) -> list[Reserve]:
    out = []
    for r in state.reserves.values():
        if r.service_id != service_id or r.status == "RELEASED":
            continue
        if r.status == "COMMITTED":
            out.append(r)
        elif r.kind == "ROLLOVER":
            out.append(r)
        elif r.plan_version == latest_version:
            out.append(r)
    return out


# ---------------- 辅助查询 ----------------

def _later_services(state: State, svc: Service) -> list[Service]:
    return sorted(
        (s for s in state.services.values()
         if s.origin == svc.origin and s.destination == svc.destination
         and s.load_cutoff > svc.load_cutoff and not s.closed),
        key=lambda s: s.load_cutoff,
    )


def _feeder_for(state: State, svc: Service) -> Optional[dict[str, Any]]:
    lead = timedelta(minutes=svc.unload_lead_min)
    ready_at = svc.arrive_at + lead
    candidates = [f for f in state.feeders.values()
                  if f.station == svc.destination and f.handoff_cutoff >= ready_at]
    if not candidates:
        return None
    f = min(candidates, key=lambda x: x.handoff_cutoff)
    return {"feeder_id": f.feeder_id, "carrier_id": f.carrier_id,
            "ready_at": ready_at, "handoff_cutoff": f.handoff_cutoff,
            "feeder_depart_at": f.depart_at}


def _candidate_batches(state: State, svc: Service, at: datetime) -> list[Batch]:
    out = []
    for b in state.batches.values():
        if b.origin != svc.origin or b.destination != svc.destination:
            continue
        if not snapshot.exists_at(b, at):
            continue
        if b.final == "MERGED":
            continue
        if snapshot.scrapped_or_returned_at(b, at):
            continue
        if snapshot.departed_at(b, at):
            continue
        out.append(b)
    return out


# ---------------- 规划主流程 ----------------

def plan(state: State, service_id: str, as_of: datetime) -> dict[str, Any]:
    """对一个班次生成新版装载计划。返回可执行清单（含每批次决定与依据）。

    所有事实按 as_of 做点-in-time 快照：晚于 as_of 的读数、到站、隔离处置、
    换编与优先级变化不得影响本版决定。已 COMMITTED 的物理装车是既成事实，不回退。
    """
    with state.lock:
        current = state.services.get(service_id)
        if current is None:
            raise EngineError(f"未知班次: {service_id}")
        if current.closed:
            raise EngineError(f"班次已截关发车: {service_id}")

        svc = snapshot.service_view(current, as_of)
        version = state.plans.get(service_id, 0) + 1
        state.plans[service_id] = version

        # 释放本班次上一版未装车的规划占用（已 COMMITTED 与外班转入的 ROLLOVER 保留）
        for r in list(state.reserves.values()):
            if (r.service_id == service_id and r.status == "HELD"
                    and r.kind == "PLAN" and r.plan_version < version):
                r.status = "RELEASED"
        # 本班次规划曾给下游班次挂的转班预约：重算前先释放，由新决定重新建立
        for r in list(state.reserves.values()):
            if (r.kind == "ROLLOVER" and r.source_service_id == service_id
                    and r.status == "HELD"):
                r.status = "RELEASED"

        ledger = CapacityLedger(state, svc)
        for r in _active_reserves(state, service_id, state.plans.get(service_id, version)):
            if r.status in ("HELD", "COMMITTED"):
                ledger.consume(r)

        feeder = _feeder_for(state, svc)
        batches = _candidate_batches(state, svc, as_of)
        # 已在本班次装车的批次只出现在清单里，不再参与分配（物理既成事实）
        committed_bids = {r.batch_id for r in _active_reserves(state, service_id, version)
                          if r.status == "COMMITTED"}

        def sort_key(b: Batch) -> tuple[int, datetime, datetime]:
            arrived = snapshot.arrival_at(b, as_of)
            eta = snapshot.eta_at(b, as_of)
            return (snapshot.priority_at(b, as_of).rank,
                    arrived or eta or b.tendered_at, b.tendered_at)

        ordered = sorted(batches, key=sort_key)
        decisions: list[DecisionRecord] = []
        for b in ordered:
            if b.batch_id in committed_bids:
                continue
            dec = _decide(state, svc, b, as_of, ledger, feeder, version,
                          competing=[x.batch_id for x in ordered
                                     if x.batch_id != b.batch_id
                                     and x.batch_id not in committed_bids])
            decisions.append(dec)
            b.decisions.append(dec)
            if len(b.decisions) >= 2:
                prev = b.decisions[-2]
                if prev.superseded_by is None:
                    prev.superseded_by = version

        return _plan_payload(state, svc, version, as_of, ledger, decisions, feeder)


def _decide(state: State, svc: Service, b: Batch, as_of: datetime,
            ledger: CapacityLedger, feeder: Optional[dict[str, Any]],
            version: int, competing: list[str]) -> DecisionRecord:
    reasons: list[str] = []
    warnings: list[str] = []
    prio = snapshot.priority_at(b, as_of)
    basis: dict[str, Any] = {
        "cutoff": iso(svc.load_cutoff), "depart_at": iso(svc.depart_at),
        "as_of": iso(as_of), "priority": prio.value,
        "priority_rank": prio.rank, "consist_version": svc.consist_version,
        "competing_batches": competing,
    }

    # 1) 全部事实先算齐（点-in-time），保证任何决定都附完整业务依据
    open_q = snapshot.open_quarantines_at(b, as_of)
    released_q = snapshot.released_quarantines_at(b, as_of)

    exposure = eval_exposure(state, b, min(as_of, svc.load_cutoff))
    basis["temperature"] = exposure
    arrived = snapshot.arrival_at(b, as_of)
    eta = snapshot.eta_at(b, as_of)
    arrival = arrived or eta
    basis["arrival"] = {"arrived_at": iso(arrived) if arrived else None,
                        "eta": iso(eta) if eta else None}

    can_make_this = arrival is not None and (
        arrival + timedelta(minutes=HANDLING_LEAD_MIN) <= svc.load_cutoff)
    if arrival is None:
        reasons.append("NOT_ARRIVED")
    elif not can_make_this:
        reasons.append("LATE_FOR_CUTOFF")
        basis["minutes_late"] = max(
            0, int((arrival + timedelta(minutes=HANDLING_LEAD_MIN)
                    - svc.load_cutoff).total_seconds() / 60))

    if exposure["unverified"]:
        reasons.append("SENSOR_UNVERIFIED")
    if exposure["out_of_window_min"] > b.exposure_budget_min:
        reasons.append("WINDOW_EXCEEDED")
        basis["window_over_min"] = round(
            exposure["out_of_window_min"] - b.exposure_budget_min, 1)

    if feeder is None:
        reasons.append("FEEDER_INFEASIBLE")
    else:
        basis["feeder"] = {k: (iso(v) if isinstance(v, datetime) else v)
                           for k, v in feeder.items()}

    has_zone_capability = any(b.temp_zone in car.zones for car in svc.cars.values())

    # 2) 未处置隔离：一律就地处置。温度补报不能改变这一结论；
    #    晚到/缺车等其他依据一并列明，便于争议时还原全部因素。
    if open_q:
        codes = sorted({q["code"] for q in open_q})
        reasons = codes + [r for r in reasons if r not in codes]
        basis["open_quarantines"] = [
            {"code": q["code"], "raised_at": iso(q["raised_at"]),
             "trigger_event_id": q["trigger_event_id"], "detail": q["detail"]}
            for q in open_q]
        if not has_zone_capability:
            for code in ("CONSIST_CHANGED", "NO_MATCHING_CAR"):
                if code not in reasons:
                    reasons.append(code)
        return _record(svc, version, b, Action.HOLD, reasons, None, None,
                       None, None, basis, _hold_checklist(b, open_q), warnings)

    if released_q:
        reasons.append("MANUAL_RELEASE")
        warnings.append("存在已人工放行的温控隔离，放行责任与依据随批次留痕")
        basis["manual_releases"] = [
            {"code": q["code"], "disposition": q["disposition"]} for q in released_q]

    # 3) 阻断性依据决定能否上本班
    hard_block = bool([r for r in reasons
                       if r in ("NOT_ARRIVED", "LATE_FOR_CUTOFF",
                                "SENSOR_UNVERIFIED", "WINDOW_EXCEEDED",
                                "FEEDER_INFEASIBLE")])

    # 4) 车厢能力与容量
    target = _try_reserve(state, svc, b, ledger, version) if not hard_block else None
    if not hard_block and target is None:
        if not has_zone_capability:
            if svc.consist_version > 0:
                reasons.append("CONSIST_CHANGED")
            reasons.append("NO_MATCHING_CAR")
            basis["capacity_note"] = (
                f"当前编组（版本 {svc.consist_version}）无 {b.temp_zone} 温层车厢")
        else:
            reasons.append("NO_CAPACITY")
            basis["capacity_remaining"] = ledger.remaining()
            basis["capacity_note"] = ("同温层容量被更高优先级或更早到站批次占用，"
                                      "排序依据 PRIORITY_RANK（优先级→到站时刻→托运时刻）")
            reasons.append("PRIORITY_RANK")

    if target is not None:
        car_id, zone, reserve = target
        reasons = reasons or ["ON_TIME"]
        checklist = _load_checklist(b, svc, feeder, reserve, exposure)
        if svc.consist_version > 0:
            warnings.append(f"本班次经历 {svc.consist_version} 次临时换编，"
                            f"已按当前编组 {','.join(svc.cars)} 复核能力")
        return _record(svc, version, b, Action.LOAD, reasons, car_id, zone,
                       None, feeder["feeder_id"] if feeder else None,
                       basis, checklist, warnings, reserve_id=reserve.reserve_id)

    # 6) 不能上本班：尝试转下一班
    roll = _try_rollover(state, svc, b, as_of, exposure)
    if roll is not None:
        target_svc, reserve, target_feeder, added_wait_min = roll
        basis["rollover"] = {
            "target_service_id": target_svc.service_id,
            "target_cutoff": iso(target_svc.load_cutoff),
            "added_wait_min": added_wait_min,
            "feeder_id": target_feeder["feeder_id"],
        }
        reasons = reasons or ["LATE_FOR_CUTOFF"]
        checklist = _rollover_checklist(b, svc, target_svc, reserve, target_feeder)
        return _record(svc, version, b, Action.ROLLOVER, reasons, reserve.car_id,
                       reserve.zone, target_svc.service_id,
                       target_feeder["feeder_id"], basis, checklist, warnings,
                       reserve_id=reserve.reserve_id)

    # 7) 无可行班次：就地处置
    if not any(r in reasons for r in
               ("NO_CAPACITY", "FEEDER_INFEASIBLE", "NO_MATCHING_CAR",
                "CONSIST_CHANGED", "WINDOW_EXCEEDED", "SENSOR_UNVERIFIED")):
        reasons.append("NO_FORWARD_OPTION")
    basis.setdefault("capacity_remaining", ledger.remaining())
    return _record(svc, version, b, Action.HOLD, reasons, None, None, None, None,
                   basis, _hold_checklist(b, open_q or [{"code": "NO_FORWARD_OPTION"}]),
                   warnings)


def _try_reserve(state: State, svc: Service, b: Batch, ledger: CapacityLedger,
                 version: int) -> Optional[tuple[str, str, Reserve]]:
    # 上游班次规划已为本批建立的转入预约直接复用（其容量已计入账本，不可二次占用）
    inbound = next(
        (r for r in state.reserves.values()
         if r.service_id == svc.service_id and r.batch_id == b.batch_id
         and r.kind == "ROLLOVER" and r.status == "HELD"), None)
    if inbound is not None:
        return inbound.car_id, inbound.zone, inbound
    found = ledger.find_zone(b.temp_zone, b.positions, b.weight_kg, b.volume_m3)
    if found is None:
        return None
    car_id, zone = found
    rid = f"RSV-{svc.service_id}-{version}-{b.batch_id}"
    reserve = Reserve(
        reserve_id=rid, service_id=svc.service_id, car_id=car_id, zone=zone,
        batch_id=b.batch_id, pallet_ids=list(b.pallet_ids),
        positions=b.positions, weight_kg=b.weight_kg, volume_m3=b.volume_m3,
        created_at=svc.load_cutoff, plan_version=version, kind="PLAN",
    )
    state.reserves[rid] = reserve
    ledger.consume(reserve)
    b.reserves.append(_reserve_summary(reserve))
    return car_id, zone, reserve


def _latest_reading_out_of_window(state: State, b: Batch, at: datetime) -> bool:
    good = [o for o in b.obs if o.admissible and not o.suspect
            and o.occurred_at <= at]
    if not good:
        return False
    last = max(good, key=lambda o: o.occurred_at)
    return last.temp_c < b.temp_min or last.temp_c > b.temp_max


def _try_rollover(state: State, svc: Service, b: Batch, as_of: datetime,
                  exposure: dict[str, Any]) -> Optional[tuple[
        Service, Reserve, dict[str, Any], int]]:
    arrived = snapshot.arrival_at(b, as_of)
    eta = snapshot.eta_at(b, as_of)
    arrival = arrived or eta
    if arrival is None:
        return None
    # 未证实/超窗/硬隔离在未人工放行前不允许"转班了事"
    if exposure["unverified"] or not exposure["within_budget"]:
        return None
    for nxt0 in _later_services(state, svc):
        nxt = snapshot.service_view(nxt0, as_of)
        if arrival + timedelta(minutes=HANDLING_LEAD_MIN) > nxt.load_cutoff:
            continue
        nxt_feeder = _feeder_for(state, nxt)
        if nxt_feeder is None:
            continue
        # 耐受窗口：站场冷库按温层暂存，仅当最新可采信读数已超窗时，
        # 保守地把追加等待计入超窗暴露
        added_wait = int((nxt.load_cutoff - svc.load_cutoff).total_seconds() / 60)
        latest_bad = bool(exposure["latest_ok_at"]) and _latest_reading_out_of_window(
            state, b, as_of)
        projected_out = exposure["out_of_window_min"] + (added_wait if latest_bad else 0)
        if projected_out > b.exposure_budget_min:
            continue
        nxt_version = state.plans.get(nxt.service_id, 0)
        nxt_ledger = CapacityLedger(state, nxt)
        for r in _active_reserves(state, nxt.service_id, nxt_version):
            nxt_ledger.consume(r)
        found = nxt_ledger.find_zone(b.temp_zone, b.positions, b.weight_kg, b.volume_m3)
        if found is None:
            continue
        car_id, zone = found
        rid = f"RSV-{nxt.service_id}-R-{b.batch_id}"
        existing = state.reserves.get(rid)
        if existing and existing.status == "HELD":
            existing.status = "RELEASED"
        reserve = Reserve(
            reserve_id=rid, service_id=nxt.service_id, car_id=car_id, zone=zone,
            batch_id=b.batch_id, pallet_ids=list(b.pallet_ids),
            positions=b.positions, weight_kg=b.weight_kg, volume_m3=b.volume_m3,
            created_at=as_of, plan_version=nxt_version, kind="ROLLOVER",
            source_service_id=svc.service_id,
        )
        state.reserves[rid] = reserve
        b.reserves.append(_reserve_summary(reserve))
        return nxt, reserve, nxt_feeder, added_wait
    return None


# ---------------- 清单与序列化 ----------------

def _reserve_summary(r: Reserve) -> dict[str, Any]:
    return {"reserve_id": r.reserve_id, "service_id": r.service_id,
            "car_id": r.car_id, "zone": r.zone, "status": r.status,
            "kind": r.kind, "plan_version": r.plan_version,
            "pallet_ids": list(r.pallet_ids)}


def _record(svc: Service, version: int, b: Batch, action: Action,
            reason_codes: list[str], car_id: Optional[str], zone: Optional[str],
            target_service_id: Optional[str], feeder_id: Optional[str],
            basis: dict[str, Any], checklist: list[dict[str, Any]],
            warnings: list[str], reserve_id: Optional[str] = None) -> DecisionRecord:
    return DecisionRecord(
        service_id=svc.service_id, plan_version=version, batch_id=b.batch_id,
        action=action.value, reason_codes=reason_codes, car_id=car_id, zone=zone,
        pallet_ids=list(b.pallet_ids), target_service_id=target_service_id,
        feeder_id=feeder_id, basis=basis, checklist=checklist,
        warnings=warnings, reserve_id=reserve_id)


def _load_checklist(b: Batch, svc: Service, feeder: Optional[dict[str, Any]],
                    reserve: Reserve, exposure: dict[str, Any]) -> list[dict[str, Any]]:
    items = [
        {"step": "到站核验", "deadline": iso(b.arrived_at or svc.load_cutoff),
         "owner": "站场", "detail": f"核到 {b.positions} 托，铅封状态 "
         f"{'完好' if b.seal_ok else '待核'}"},
        {"step": "温控证据核验", "deadline": iso(svc.load_cutoff), "owner": "冷链值班",
         "detail": f"超窗暴露 {exposure['out_of_window_min']}/"
                   f"{b.exposure_budget_min} 分钟，缺口 {len(exposure['gaps'])} 处"},
        {"step": "车厢容量确认", "deadline": iso(svc.load_cutoff), "owner": "调度",
         "detail": f"{reserve.car_id}/{reserve.zone} 已预约 {reserve.reserve_id}"},
        {"step": "装车扫描", "deadline": iso(svc.load_cutoff), "owner": "装卸班",
         "detail": "逐托扫描，断网点位恢复后按现场时间校正"},
        {"step": "铅封与发车复核", "deadline": iso(svc.depart_at), "owner": "站场",
         "detail": "扫码 COMMIT 后舱位锁定"},
    ]
    if feeder:
        items.append({
            "step": "接驳预约", "deadline": iso(feeder["ready_at"]),
            "owner": f"接驳商 {feeder['carrier_id']}",
            "detail": f"{feeder['feeder_id']} 交接截关 "
                      f"{iso(feeder['handoff_cutoff'])}",
        })
    return items


def _rollover_checklist(b: Batch, svc: Service, nxt: Service, reserve: Reserve,
                        feeder: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"step": "转班通知", "deadline": iso(svc.depart_at), "owner": "运营",
         "detail": f"通知发货人 {b.shipper_id}，说明转班业务依据"},
        {"step": "冷库暂存", "deadline": iso(svc.load_cutoff), "owner": "站场",
         "detail": f"{b.temp_zone} 温层暂存，继续温度记录"},
        {"step": "下一班容量确认", "deadline": iso(nxt.load_cutoff), "owner": "调度",
         "detail": f"{nxt.service_id} {reserve.car_id}/{reserve.zone} 已预约 "
                   f"{reserve.reserve_id}"},
        {"step": "接驳改约", "deadline": iso(feeder["ready_at"]), "owner": "运营",
         "detail": f"改约 {feeder['feeder_id']}，交接截关 "
                   f"{iso(feeder['handoff_cutoff'])}"},
    ]


def _hold_checklist(b: Batch, quarantines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"step": "移入隔离区", "deadline": None, "owner": "站场",
         "detail": f"{b.positions} 托移至 {b.temp_zone} 隔离位，禁止装车"},
        {"step": "人工处置", "deadline": None, "owner": "质检/授权值班",
         "detail": "隔离码：" + ",".join(sorted({q["code"] for q in quarantines}))
                   + "；放行须记录责任人与依据，报废/退回同步发货人"},
    ]


def _decision_dict(d: DecisionRecord) -> dict[str, Any]:
    return {
        "service_id": d.service_id, "plan_version": d.plan_version,
        "batch_id": d.batch_id, "action": d.action,
        "reason_codes": d.reason_codes, "car_id": d.car_id, "zone": d.zone,
        "pallet_ids": d.pallet_ids, "target_service_id": d.target_service_id,
        "feeder_id": d.feeder_id, "reserve_id": d.reserve_id,
        "superseded_by": d.superseded_by, "basis": d.basis,
        "checklist": d.checklist, "warnings": d.warnings,
    }


def _plan_payload(state: State, svc: Service, version: int, as_of: datetime,
                  ledger: CapacityLedger, decisions: list[DecisionRecord],
                  feeder: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        "service_id": svc.service_id, "plan_version": version,
        "generated_at": iso(as_of), "cutoff": iso(svc.load_cutoff),
        "depart_at": iso(svc.depart_at),
        "consist_version": svc.consist_version,
        "feeder": ({k: (iso(v) if isinstance(v, datetime) else v)
                    for k, v in feeder.items()} if feeder else None),
        "summary": {a.value: sum(1 for d in decisions if d.action == a.value)
                    for a in Action},
        "capacity": ledger.remaining(),
        "decisions": [_decision_dict(d) for d in decisions],
    }


# ---------------- 装车确认与截关 ----------------

def commit_reserve(state: State, reserve_id: str, at: datetime,
                   pallet_ids: Optional[list[str]] = None) -> dict[str, Any]:
    """装车扫描完成后人工/自动确认，HELD -> COMMITTED，此后容量不可被重规划挤占。"""
    with state.lock:
        r = state.reserves.get(reserve_id)
        if r is None:
            raise EngineError(f"未知预约: {reserve_id}")
        if r.status != "HELD":
            raise EngineError(f"预约状态 {r.status} 不可确认")
        if pallet_ids is not None:
            missing = set(pallet_ids) - set(r.pallet_ids)
            if missing:
                raise EngineError(f"托盘不在预约内: {sorted(missing)}")
            r.pallet_ids = list(pallet_ids)
            r.positions = len(pallet_ids)
        r.status = "COMMITTED"
        b = state.batches[r.batch_id]
        for item in b.reserves:
            if item["reserve_id"] == r.reserve_id:
                item["status"] = "COMMITTED"
        return _reserve_summary(r)


def close_service(state: State, service_id: str, at: datetime) -> dict[str, Any]:
    """截关发车：未装车的 HELD 规划占用释放；COMMITTED 批次标记发运。"""
    with state.lock:
        svc = state.services.get(service_id)
        if svc is None:
            raise EngineError(f"未知班次: {service_id}")
        if svc.closed:
            raise EngineError("班次已关闭")
        svc.closed = True
        svc.departed_at = at
        released, departed = [], []
        for r in state.reserves.values():
            if r.service_id != service_id:
                continue
            b = state.batches.get(r.batch_id)
            if r.status == "COMMITTED":
                if b and not b.departed_service:
                    b.departed_service = service_id
                    b.departed_at = at
                    b.final = "DEPARTED"
                departed.append(r.reserve_id)
            elif r.status == "HELD":
                r.status = "RELEASED"
                released.append(r.reserve_id)
                if b:
                    for item in b.reserves:
                        if item["reserve_id"] == r.reserve_id:
                            item["status"] = "RELEASED"
        return {"service_id": service_id, "closed_at": iso(at),
                "departed_reserves": departed, "released_reserves": released}

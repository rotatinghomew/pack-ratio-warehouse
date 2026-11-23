"""门店补货申请的分配与承诺引擎。

规则要点：

* 申请带交付窗口、可接受包装版、是否允许拆箱；
* 不允许拆箱时，必须以完整整箱满足申请所需的完整包装数量；
* 允许拆箱时优先消化已拆开的散件，再考虑拆整箱，且仓库要有拆装权限；
* 在途调拨只有在 ETA 不晚于窗口时才可承诺，且只能作整箱来源；
* 计划先落 provisional 占用；拣货确认时才真正拆箱并转为 picked；
* 已确认拣出的整箱在账本层面被占用，任何再拆/再分配都会被拒绝；
* 更正、调拨延迟等事件触发受影响申请的联合重核，同批货不会被重复承诺。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ledger import LedgerError
from .models import CommitState, Form, Lot, LotState


@dataclass(frozen=True)
class Request:
    request_id: str
    store: str
    sku: str
    units: int
    window: str
    allowed_revisions: frozenset[str] = frozenset()  # 空集合表示不限制版次
    allow_split: bool = False
    priority: int = 0
    created_day: str | None = None

    def accepts(self, revision: str) -> bool:
        return not self.allowed_revisions or revision in self.allowed_revisions


@dataclass
class CommitmentLine:
    warehouse: str
    lot_id: str
    revision: str
    form: Form
    qty: int                  # 整箱数或散件件数
    units: int                # 折算件数
    requires_unpack: bool = False
    transfer_id: str | None = None
    provisional: bool = True

    @property
    def in_transit(self) -> bool:
        return self.transfer_id is not None


@dataclass
class Plan:
    request_id: str
    lines: list[CommitmentLine] = field(default_factory=list)
    short_units: int = 0
    change_note: str = ""

    @property
    def feasible(self) -> bool:
        return self.short_units == 0

    @property
    def total_units(self) -> int:
        return sum(line.units for line in self.lines)


class Allocator:
    def __init__(self, ledger):
        self.ledger = ledger
        self.requests: dict[str, Request] = {}
        self.plans: dict[str, Plan] = {}
        # provisional 预留由分配器自行记账：(仓, 批次, 形态) -> 件数
        self._reservations: dict[tuple[str, str, Form], int] = {}

    # ------------------------------------------------------------ 候选池
    def _reserved(self, warehouse: str, lot_id: str, form: Form) -> int:
        return self._reservations.get((warehouse, lot_id, form), 0)

    def _physical_pools(self, req: Request):
        """返回 (整箱池, 散件池)。可用量 = 账本自由量 - 其它申请的 provisional 预留。"""
        case_pool: list[tuple[str, Lot, int]] = []
        loose_pool: list[tuple[str, Lot, int]] = []
        for lot in self.ledger.lots(req.sku):
            if lot.state is not LotState.NORMAL:
                continue
            if not req.accepts(lot.revision):
                continue
            free_cases = (
                self.ledger.free_cases(lot.warehouse, lot.lot_id)
                - self._reserved(lot.warehouse, lot.lot_id, Form.CASE)
            )
            if free_cases > 0:
                case_pool.append((lot.warehouse, lot, free_cases))
            free_loose = (
                self.ledger.free_loose(lot.warehouse, lot.lot_id)
                - self._reserved(lot.warehouse, lot.lot_id, Form.LOOSE)
            )
            if free_loose > 0:
                loose_pool.append((lot.warehouse, lot, free_loose))
        case_pool.sort(key=lambda x: (x[0], x[1].lot_id))
        loose_pool.sort(key=lambda x: (x[0], x[1].lot_id))
        return case_pool, loose_pool

    def _transit_pool(self, req: Request):
        # 已用量只统计其它申请；当前申请重算时会先释放自己的预留，
        # 把自己算进去会导致在途容量被凭空占用。
        used_cases: dict[str, int] = {}
        for plan_id, plan in self.plans.items():
            if plan_id == req.request_id:
                continue
            for line in plan.lines:
                if line.transfer_id is not None:
                    used_cases[line.transfer_id] = (
                        used_cases.get(line.transfer_id, 0) + line.qty
                    )
        pool = []
        for tr in self.ledger.transfers:
            if tr.state != "in-transit" or tr.sku != req.sku:
                continue
            if tr.eta > req.window:
                continue
            if not req.accepts(tr.revision):
                continue
            remaining = tr.cases - used_cases.get(tr.transfer_id, 0)
            if remaining > 0:
                pool.append((tr, remaining))
        pool.sort(key=lambda x: (x[0].eta, x[0].transfer_id))
        return pool

    def _can_unpack(self, warehouse: str) -> bool:
        wh = self.ledger._warehouses.get(warehouse)  #  noqa: SLF001
        return wh is None or wh.can_unpack

    # ------------------------------------------------------------ 计划
    def add_request(self, req: Request) -> Plan:
        self.requests[req.request_id] = req
        return self.plan(req.request_id)

    def plan(self, request_id: str) -> Plan:
        req = self.requests[request_id]
        plan = Plan(request_id=request_id)
        case_pool, loose_pool = self._physical_pools(req)
        transit_pool = self._transit_pool(req)
        need = req.units

        if not req.allow_split:
            # 只要完整整箱：在仓整箱优先，在途整箱其次
            for wh, lot, free in case_pool:
                if need <= 0:
                    break
                take = min(free, need // lot.units_per_case)
                if take:
                    plan.lines.append(
                        CommitmentLine(wh, lot.lot_id, lot.revision, Form.CASE,
                                       take, take * lot.units_per_case)
                    )
                    need -= take * lot.units_per_case
            for tr, remaining in transit_pool:
                if need <= 0:
                    break
                take = min(remaining, need // tr.units_per_case)
                if take:
                    plan.lines.append(
                        CommitmentLine(tr.destination, tr.source_lot, tr.revision, Form.CASE,
                                       take, take * tr.units_per_case,
                                       transfer_id=tr.transfer_id)
                    )
                    need -= take * tr.units_per_case
            plan.short_units = max(need, 0)
        else:
            # 先消化散件余量（按实际包装形态留给后续请求的部分）
            for wh, lot, free in loose_pool:
                if need <= 0:
                    break
                take = min(free, need)
                plan.lines.append(
                    CommitmentLine(wh, lot.lot_id, lot.revision, Form.LOOSE,
                                   take, take)
                )
                need -= take
            # 再用在仓整箱：能整箱满足就整箱，否则需拆箱（校验拆装权限）
            for wh, lot, free in case_pool:
                if need <= 0:
                    break
                whole = min(free, need // lot.units_per_case)
                if whole:
                    plan.lines.append(
                        CommitmentLine(wh, lot.lot_id, lot.revision, Form.CASE,
                                       whole, whole * lot.units_per_case)
                    )
                    need -= whole * lot.units_per_case
                if need > 0 and self._can_unpack(wh):
                    # 向上取整得到“至少覆盖剩余需求”的箱数，但不得超过自由箱数；
                    # 拆出余量保留为散件，供后续符合条件的申请使用
                    take_cases = min(
                        free - whole,
                        (need + lot.units_per_case - 1) // lot.units_per_case,
                    )
                    if take_cases:
                        plan.lines.append(
                            CommitmentLine(wh, lot.lot_id, lot.revision, Form.CASE,
                                           take_cases, take_cases * lot.units_per_case,
                                           requires_unpack=True)
                        )
                        need -= take_cases * lot.units_per_case
            # 最后才动用在途整箱（在途无法提前拆，只能整箱）
            for tr, remaining in transit_pool:
                if need <= 0:
                    break
                take = min(remaining, (need + tr.units_per_case - 1) // tr.units_per_case)
                plan.lines.append(
                    CommitmentLine(tr.destination, tr.source_lot, tr.revision, Form.CASE,
                                   take, take * tr.units_per_case,
                                   transfer_id=tr.transfer_id)
                )
                need -= take * tr.units_per_case
            plan.short_units = max(need, 0)

        self._reserve(plan)
        return plan

    def _reserve(self, plan: Plan) -> None:
        """把计划落成 provisional 占用（拆箱行只占整箱，确认时再拆）。

        预留表始终由所有 provisional 计划重建，避免逐单加减产生累计误差。
        先把新计划登记进 plans，重建时才不会漏掉它自己。
        """
        self.plans[plan.request_id] = plan
        self.ledger.release_provisional(plan.request_id)
        for line in plan.lines:
            if line.in_transit:
                continue  # 在途由调拨实体本身承载，不能在仓位上重复占用
            self.ledger.hold(
                plan.request_id, line.warehouse, line.lot_id, line.form, line.qty,
                CommitState.PROVISIONAL,
            )
        self._rebuild_reservations()

    def _release_reservation(self, request_id: str) -> None:
        self.ledger.release_provisional(request_id)
        self._rebuild_reservations()

    def _rebuild_reservations(self) -> None:
        self._reservations = {}
        for plan in self.plans.values():
            for line in plan.lines:
                if line.in_transit or not line.provisional:
                    continue
                key = (line.warehouse, line.lot_id, line.form)
                self._reservations[key] = self._reservations.get(key, 0) + line.qty

    # ------------------------------------------------------------ 拣货确认
    def confirm_pick(self, request_id: str, day: str) -> Plan:
        plan = self.plans.get(request_id)
        if plan is None or not plan.feasible:
            raise LedgerError(f"申请 {request_id} 无可行计划，不能确认拣货")
        for line in plan.lines:
            if line.in_transit:
                raise LedgerError(
                    f"申请 {request_id} 含在途承诺 {line.transfer_id}，到货后才能确认"
                )
        # 先解除本单全部 provisional，账本确认 free_cases 足以真实拆箱
        self.ledger.release_provisional(request_id)
        self._rebuild_reservations()
        for line in plan.lines:
            if line.requires_unpack:
                # 真正拆箱：账本校验自由整箱与拆装权限，拆完以散件重新占用
                self.ledger.unpack(day, line.lot_id, line.qty,
                                   reason=f"拣货确认拆箱，服务 {request_id}")
                line.form = Form.LOOSE
                line.qty = line.units
                line.requires_unpack = False
        # 按最终形态重新占用并整体转为已拣出（账本将硬冻结，任何再拆都会被拒）
        for line in plan.lines:
            self.ledger.hold(request_id, line.warehouse, line.lot_id, line.form,
                             line.qty, CommitState.PROVISIONAL)
        self.ledger.mark_picked(request_id)
        self._rebuild_reservations()
        for line in plan.lines:
            line.provisional = False
        return plan

    def picked_lot_ids(self) -> set[str]:
        return {
            h.lot_id
            for req_id in self.plans
            for h in self.ledger.committed(req_id)
            if h.state is CommitState.PICKED
        }

    # ------------------------------------------------------------ 联合重核
    def replan_affected(self, lot_ids: set[str], transfer_ids: set[str] | None = None,
                        note: str = "") -> dict[str, Plan]:
        """更正/延迟后重核所有受影响的申请；picked 行不参与重排。

        先统一释放受影响的 provisional 占用，再按优先级重新规划，
        因此单张都能满足的申请也不会重复使用同一批货。
        """
        transfer_ids = transfer_ids or set()
        affected: list[Request] = []
        for req_id, req in self.requests.items():
            plan = self.plans.get(req_id)
            if plan is None:
                continue
            touches_lot = any(line.lot_id in lot_ids for line in plan.lines)
            touches_transfer = any(
                line.transfer_id in transfer_ids for line in plan.lines
            )
            if touches_lot or touches_transfer:
                affected.append(req)
        if not affected:
            return {}
        for req in affected:
            self._release_reservation(req.request_id)
            self.ledger.release_provisional(req.request_id)
        # 已确认拣货的整箱仍被账本 hold 住，规划器自然看不到它们，杜绝重复分配
        affected.sort(key=lambda r: (-r.priority, r.request_id))
        results: dict[str, Plan] = {}
        for req in affected:
            new_plan = self.plan(req.request_id)
            new_plan.change_note = note or "包装更正/调拨变动后联合重核"
            results[req.request_id] = new_plan
        return results

    def mark_shipped(self, request_id: str) -> None:
        """发货后清掉预留表中的本单记录（实物已离仓）。"""
        plan = self.plans.get(request_id)
        if plan is not None:
            for line in plan.lines:
                line.provisional = False
        self._rebuild_reservations()

    def request_status(self, request_id: str) -> dict:
        plan = self.plans[request_id]
        req = self.requests[request_id]
        return {
            "request_id": request_id,
            "store": req.store,
            "sku": req.sku,
            "window": req.window,
            "requested_units": req.units,
            "feasible": plan.feasible,
            "short_units": plan.short_units,
            "needs_new_commitment": bool(plan.change_note) or not plan.feasible,
            "lines": [
                {
                    "warehouse": line.warehouse,
                    "lot_id": line.lot_id,
                    "revision": line.revision,
                    "form": line.form.value,
                    "qty": line.qty,
                    "units": line.units,
                    "requires_unpack": line.requires_unpack,
                    "in_transit": line.in_transit,
                    "transfer_id": line.transfer_id,
                    "provisional": line.provisional,
                }
                for line in plan.lines
            ],
        }

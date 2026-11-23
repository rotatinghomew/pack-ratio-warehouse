"""事件溯源库存账本。

所有库存变动都是只追加事件：收货、包装确认、拆箱、换包装、返厂、
调拨、规格更正。设计约束：

* 拆箱/换包装/调拨严格实物守恒，件数一个不多一个不少；
* 返厂只能按生产商允许的退货形态（旧箱 case-only 不能拆退）；
* 已确认拣出的整箱被 Hold 占用，账本拒绝再次拆用；
* 期末锁定后发生的任何数量变化，逐笔形成带事件号的调整记录；
* 不认识标签的批次进隔离，包装确认前不参与可用量。
"""
from __future__ import annotations

from typing import Iterable

from .catalog import PackagingCatalog, PackagingError, ReturnPolicy
from .models import (
    Adjustment,
    CommitState,
    Event,
    Form,
    Hold,
    Lot,
    LotState,
    PendingCorrection,
    PeriodLock,
    Position,
    Transfer,
    Warehouse,
)


class LedgerError(RuntimeError):
    """账本操作被业务规则拒绝。"""


class InventoryLedger:
    def __init__(self, catalog: PackagingCatalog, warehouses: Iterable[Warehouse] = ()):
        self.catalog = catalog
        self._warehouses: dict[str, Warehouse] = {w.code: w for w in warehouses}
        self._lots: dict[str, Lot] = {}
        self._pos: dict[tuple[str, str, Form], int] = {}
        self._events: list[Event] = []
        self._holds: list[Hold] = []
        self._transfers: dict[str, Transfer] = {}
        self._pending: dict[str, PendingCorrection] = {}
        self._locks: dict[str, PeriodLock] = {}
        self._lock_views: dict[str, dict[tuple[str, str, Form], int]] = {}
        self._adjustments: list[Adjustment] = []
        self._returned: dict[str, int] = {}
        self._lot_seq = 0
        self._event_seq = 0

    # ------------------------------------------------------------------ 基础
    def _emit(self, kind: str, day: str, payload: dict) -> Event:
        self._event_seq += 1
        event = Event(seq=self._event_seq, kind=kind, day=day, payload=dict(payload))
        self._events.append(event)
        return event

    def _new_lot_id(self, prefix: str) -> str:
        self._lot_seq += 1
        return f"{prefix}-{self._lot_seq:04d}"

    def _key(self, warehouse: str, lot_id: str, form: Form) -> tuple[str, str, Form]:
        return (warehouse, lot_id, form)

    def _add(self, key: tuple[str, str, Form], qty: int) -> None:
        self._pos[key] = self._pos.get(key, 0) + qty

    def _take(self, key: tuple[str, str, Form], qty: int) -> None:
        have = self._pos.get(key, 0)
        if have < qty:
            raise LedgerError(f"仓位 {key} 存量 {have} 不足，无法扣减 {qty}")
        self._pos[key] = have - qty

    def _unit_total(self, sku: str) -> int:
        """某 SKU 全部实物件数：在仓（整箱折算+散件）+ 在途 + 已返厂。"""
        total = 0
        for (_, lot_id, form), qty in self._pos.items():
            lot = self._lots[lot_id]
            if lot.sku != sku:
                continue
            total += qty * (lot.units_per_case if form is Form.CASE else 1)
        for tr in self._transfers.values():
            if tr.sku == sku and tr.state == "in-transit":
                total += tr.cases * tr.units_per_case
        for lot_id, qty in self._returned.items():
            if self._lots[lot_id].sku == sku:
                total += qty
        return total

    def _adjust_against_locks(
        self, event: Event, affected: Iterable[tuple[str, str, Form]]
    ) -> list[Adjustment]:
        """事件落账后，对每个已锁定期末比对增量，留下可追溯调整。"""
        made: list[Adjustment] = []
        for period_end in self._locks:
            # 锁定时点之后（含锁定当日之后）发生的数量变化都要对该期留痕；
            # 远在锁定之前的历史事件不影响该期期末，跳过。
            if event.day < period_end:
                continue
            view = self._lock_views[period_end]
            for key in affected:
                new_qty = self._pos.get(key, 0)
                old_qty = view.get(key, 0)
                # 快照里没有的键视为 0（锁后才出现的仓位同样要留痕）
                view[key] = new_qty
                delta = new_qty - old_qty
                if delta == 0:
                    continue
                adj = Adjustment(
                    period_end=period_end,
                    event_seq=event.seq,
                    kind=event.kind,
                    key=key,
                    delta=delta,
                    reason=event.payload.get("reason", event.kind),
                )
                made.append(adj)
                self._adjustments.append(adj)
        return made

    # ------------------------------------------------------------------ 收货
    def receive(
        self,
        receipt_id: str,
        day: str,
        warehouse: str,
        sku: str,
        revision: str | None,
        cases: int,
        label: str | None = None,
    ) -> Lot:
        """登记收货。revision 无法识别（标签陌生）时整批隔离。"""
        if cases <= 0:
            raise LedgerError("收货箱数必须为正")
        before = self._unit_total(sku)
        unknown = revision is None or revision not in self.catalog.known_revisions(sku)
        if unknown:
            upc = 0  # 未知版次没有可信换算，隔离期不折算件数
            state = LotState.QUARANTINED
            rev = revision or "UNKNOWN"
        else:
            spec = self.catalog.spec_at(sku, revision, day)
            upc = spec.units_per_case
            state = LotState.NORMAL
            rev = revision
        lot = Lot(
            lot_id=self._new_lot_id("LOT"),
            sku=sku,
            revision=rev,
            units_per_case=upc,
            warehouse=warehouse,
            state=state,
            source_event=receipt_id,
            note=label or "",
        )
        self._lots[lot.lot_id] = lot
        self._add(self._key(warehouse, lot.lot_id, Form.CASE), cases)
        event = self._emit(
            "receive",
            day,
            {
                "receipt_id": receipt_id,
                "warehouse": warehouse,
                "sku": sku,
                "revision": rev,
                "cases": cases,
                "units_per_case": upc,
                "quarantined": unknown,
                "label": label,
            },
        )
        if not unknown:
            assert self._unit_total(sku) - before == cases * upc
        self._adjust_against_locks(event, [self._key(warehouse, lot.lot_id, Form.CASE)])
        return lot

    def confirm_packaging(self, day: str, lot_id: str, revision: str) -> Lot:
        """后补包装确认：隔离批次释放为已知版次的新批次（血缘相连）。"""
        lot = self.lot(lot_id)
        if lot.state is not LotState.QUARANTINED:
            raise LedgerError(f"批次 {lot_id} 不在隔离中，无需包装确认")
        spec = self.catalog.spec_at(lot.sku, revision, day)
        key = self._key(lot.warehouse, lot_id, Form.CASE)
        cases = self._pos.get(key, 0)
        before = self._unit_total(lot.sku)
        # 隔离批次 upc=0 不计件数；释放后按确认版次恢复口径，增量来自口径恢复而非新增实物
        restored_units = cases * spec.units_per_case
        new_lot = Lot(
            lot_id=self._new_lot_id("LOT"),
            sku=lot.sku,
            revision=revision,
            units_per_case=spec.units_per_case,
            warehouse=lot.warehouse,
            state=LotState.NORMAL,
            parent_lot=lot_id,
            source_event=f"confirm:{lot_id}",
        )
        self._lots[new_lot.lot_id] = new_lot
        self._take(key, cases)
        self._lots[lot_id] = Lot(**{**lot.__dict__, "state": LotState.RESOLVED})
        self._add(self._key(lot.warehouse, new_lot.lot_id, Form.CASE), cases)
        event = self._emit(
            "confirm_packaging",
            day,
            {
                "quarantine_lot": lot_id,
                "released_lot": new_lot.lot_id,
                "sku": lot.sku,
                "revision": revision,
                "units_per_case": spec.units_per_case,
                "cases": cases,
                "reason": "后补包装确认，隔离释放",
            },
        )
        assert self._unit_total(lot.sku) - before == restored_units
        self._adjust_against_locks(
            event, [key, self._key(lot.warehouse, new_lot.lot_id, Form.CASE)]
        )
        return new_lot

    # ------------------------------------------------------------------ 拆装
    def unpack(self, day: str, lot_id: str, cases: int, reason: str = "门店拆箱替代") -> None:
        """整箱拆成散件。件数守恒，且只能动未被占用的自由整箱。"""
        lot = self.lot(lot_id)
        self._require_normal(lot)
        wh = self._require_unpack_permission(lot)
        if cases <= 0:
            raise LedgerError("拆箱数必须为正")
        case_key = self._key(wh, lot_id, Form.CASE)
        loose_key = self._key(wh, lot_id, Form.LOOSE)
        if self.free_cases(wh, lot_id) < cases:
            raise LedgerError(
                f"批次 {lot_id} 自由整箱不足，占用方: {self._blockers(case_key)}"
            )
        before = self._unit_total(lot.sku)
        self._take(case_key, cases)
        self._add(loose_key, cases * lot.units_per_case)
        event = self._emit(
            "unpack",
            day,
            {
                "lot_id": lot_id,
                "warehouse": wh,
                "cases": cases,
                "units_freed": cases * lot.units_per_case,
                "units_per_case": lot.units_per_case,
                "reason": reason,
            },
        )
        assert self._unit_total(lot.sku) == before  # 拆箱不增不减实物
        self._adjust_against_locks(event, [case_key, loose_key])

    def repack(
        self,
        day: str,
        source_lot_id: str,
        target_revision: str,
        new_cases: int,
        reason: str = "供应商换包装",
    ) -> Lot:
        """用散件按另一版口径重新装箱。新箱件数必须等于消耗散件，守恒。"""
        source = self.lot(source_lot_id)
        self._require_normal(source)
        wh = self._require_unpack_permission(source)
        target_spec = self.catalog.spec_at(source.sku, target_revision, day)
        need_loose = new_cases * target_spec.units_per_case
        loose_key = self._key(wh, source_lot_id, Form.LOOSE)
        if self.free_loose(wh, source_lot_id) < need_loose:
            raise LedgerError(
                f"换包装需要 {need_loose} 件自由散件，批次 {source_lot_id} 不足"
            )
        before = self._unit_total(source.sku)
        new_lot = Lot(
            lot_id=self._new_lot_id("LOT"),
            sku=source.sku,
            revision=target_revision,
            units_per_case=target_spec.units_per_case,
            warehouse=wh,
            parent_lot=source_lot_id,
            source_event=f"repack:{source_lot_id}",
        )
        self._lots[new_lot.lot_id] = new_lot
        self._take(loose_key, need_loose)
        case_key = self._key(wh, new_lot.lot_id, Form.CASE)
        self._add(case_key, new_cases)
        event = self._emit(
            "repack",
            day,
            {
                "source_lot": source_lot_id,
                "new_lot": new_lot.lot_id,
                "warehouse": wh,
                "sku": source.sku,
                "target_revision": target_revision,
                "new_cases": new_cases,
                "loose_consumed": need_loose,
                "reason": reason,
            },
        )
        assert self._unit_total(source.sku) == before  # 换包装不得凭空增减
        self._adjust_against_locks(event, [loose_key, case_key])
        return new_lot

    # ------------------------------------------------------------------ 返厂
    def return_to_vendor(self, day: str, lot_id: str, form: Form, qty: int) -> None:
        lot = self.lot(lot_id)
        self._require_normal(lot)
        spec = self.catalog.get(lot.sku, lot.revision)
        if spec.return_policy is ReturnPolicy.CASE_ONLY and form is not Form.CASE:
            raise LedgerError(f"版次 {lot.revision} 只允许整箱退回供货商，拒收散件")
        if spec.return_policy is ReturnPolicy.NONE:
            raise LedgerError(f"版次 {lot.revision} 生产商不允许退货")
        key = self._key(lot.warehouse, lot_id, form)
        free = (
            self.free_cases(lot.warehouse, lot_id)
            if form is Form.CASE
            else self.free_loose(lot.warehouse, lot_id)
        )
        if free < qty:
            raise LedgerError(f"批次 {lot_id} 自由存量不足，已占用部分不能返厂")
        before = self._unit_total(lot.sku)
        self._take(key, qty)
        units = qty * lot.units_per_case if form is Form.CASE else qty
        self._returned[lot_id] = self._returned.get(lot_id, 0) + units
        if self.on_hand_lot(lot_id) == 0:
            self._lots[lot_id] = Lot(**{**lot.__dict__, "state": LotState.RETURNED})
        event = self._emit(
            "return_to_vendor",
            day,
            {
                "lot_id": lot_id,
                "warehouse": lot.warehouse,
                "form": form.value,
                "qty": qty,
                "units": units,
                "return_policy": spec.return_policy.value,
                "reason": "按生产商退货政策返厂",
            },
        )
        assert self._unit_total(lot.sku) == before  # 实物只是离仓返厂，全局件数仍守恒
        self._adjust_against_locks(event, [key])

    # ------------------------------------------------------------------ 调拨
    def initiate_transfer(
        self,
        transfer_id: str,
        day: str,
        source: str,
        destination: str,
        lot_id: str,
        cases: int,
        eta: str,
    ) -> Transfer:
        lot = self.lot(lot_id)
        self._require_normal(lot)
        if cases <= 0:
            raise LedgerError("调拨箱数必须为正")
        if self.free_cases(source, lot_id) < cases:
            raise LedgerError(f"批次 {lot_id} 自由整箱不足，无法调出 {cases} 箱")
        before = self._unit_total(lot.sku)
        key = self._key(source, lot_id, Form.CASE)
        self._take(key, cases)
        tr = Transfer(
            transfer_id=transfer_id,
            sku=lot.sku,
            revision=lot.revision,
            units_per_case=lot.units_per_case,
            source=source,
            destination=destination,
            cases=cases,
            eta=eta,
            source_lot=lot_id,
        )
        event = self._emit(
            "transfer_out",
            day,
            {
                "transfer_id": transfer_id,
                "source": source,
                "destination": destination,
                "lot_id": lot_id,
                "revision": lot.revision,
                "cases": cases,
                "eta": eta,
                "reason": "仓间调拨，在途单列",
            },
        )
        tr.event_seq = event.seq
        self._transfers[transfer_id] = tr
        assert self._unit_total(lot.sku) == before
        self._adjust_against_locks(event, [key])
        return tr

    def arrive_transfer(self, day: str, transfer_id: str) -> Lot:
        tr = self._transfers.get(transfer_id)
        if tr is None or tr.state != "in-transit":
            raise LedgerError(f"调拨 {transfer_id} 不在途")
        before = self._unit_total(tr.sku)
        new_lot = Lot(
            lot_id=self._new_lot_id("LOT"),
            sku=tr.sku,
            revision=tr.revision,
            units_per_case=tr.units_per_case,
            warehouse=tr.destination,
            parent_lot=tr.source_lot,
            source_event=f"transfer:{transfer_id}",
        )
        self._lots[new_lot.lot_id] = new_lot
        key = self._key(tr.destination, new_lot.lot_id, Form.CASE)
        self._add(key, tr.cases)
        tr.state = "arrived"
        event = self._emit(
            "transfer_in",
            day,
            {
                "transfer_id": transfer_id,
                "warehouse": tr.destination,
                "lot_id": new_lot.lot_id,
                "revision": tr.revision,
                "cases": tr.cases,
                "reason": "调拨到货",
            },
        )
        # 在途件转入在仓：在途池减一件、仓位加一件，全局件数守恒
        assert self._unit_total(tr.sku) == before
        self._adjust_against_locks(event, [key])
        return new_lot

    def delay_transfer(self, day: str, transfer_id: str, new_eta: str) -> None:
        tr = self._transfers.get(transfer_id)
        if tr is None or tr.state != "in-transit":
            raise LedgerError(f"调拨 {transfer_id} 不可改期")
        old_eta = tr.eta
        tr.eta = new_eta
        self._emit(
            "transfer_delay",
            day,
            {
                "transfer_id": transfer_id,
                "old_eta": old_eta,
                "new_eta": new_eta,
                "reason": "调拨未到，承诺需重排",
            },
        )

    # ------------------------------------------------------------------ 更正
    def register_correction(
        self,
        day: str,
        correction_id: str,
        receipt_id: str,
        lot_id: str,
        cases: int,
        actual_revision: str,
    ) -> PendingCorrection:
        """登记待执行更正，不动实物；门店查询时可见 pending。"""
        lot = self.lot(lot_id)
        if actual_revision not in self.catalog.known_revisions(lot.sku):
            raise PackagingError(f"更正目标版次未登记: {actual_revision}")
        event = self._emit(
            "correction_registered",
            day,
            {
                "correction_id": correction_id,
                "receipt_id": receipt_id,
                "lot_id": lot_id,
                "cases": cases,
                "actual_revision": actual_revision,
                "reason": "旧箱规格待更正，先挂起不动物料",
            },
        )
        pc = PendingCorrection(
            correction_id=correction_id,
            receipt_id=receipt_id,
            warehouse=lot.warehouse,
            lot_id=lot_id,
            cases=cases,
            actual_revision=actual_revision,
            event_seq=event.seq,
        )
        self._pending[correction_id] = pc
        return pc

    def apply_correction(self, day: str, correction_id: str) -> Lot:
        """执行规格更正：旧批次的箱转到新版次子批次，件数差异显式留痕。"""
        pc = self._pending.pop(correction_id, None)
        if pc is None:
            raise LedgerError(f"更正单 {correction_id} 不存在或已执行")
        old_lot = self.lot(pc.lot_id)
        if self.free_cases(pc.warehouse, pc.lot_id) < pc.cases:
            raise LedgerError(
                f"更正被占用阻断，请先重核相关申请: "
                f"{self._blockers(self._key(pc.warehouse, pc.lot_id, Form.CASE))}"
            )
        spec = self.catalog.spec_at(old_lot.sku, pc.actual_revision, day)
        before = self._unit_total(old_lot.sku)
        old_key = self._key(pc.warehouse, pc.lot_id, Form.CASE)
        self._take(old_key, pc.cases)
        new_lot = Lot(
            lot_id=self._new_lot_id("LOT"),
            sku=old_lot.sku,
            revision=pc.actual_revision,
            units_per_case=spec.units_per_case,
            warehouse=pc.warehouse,
            parent_lot=pc.lot_id,
            source_event=f"correction:{pc.receipt_id}",
            note=f"receipt {pc.receipt_id} corrected",
        )
        self._lots[new_lot.lot_id] = new_lot
        new_key = self._key(pc.warehouse, new_lot.lot_id, Form.CASE)
        self._add(new_key, pc.cases)
        unit_delta = pc.cases * (spec.units_per_case - old_lot.units_per_case)
        event = self._emit(
            "correction_applied",
            day,
            {
                "correction_id": correction_id,
                "receipt_id": pc.receipt_id,
                "old_lot": pc.lot_id,
                "new_lot": new_lot.lot_id,
                "warehouse": pc.warehouse,
                "cases": pc.cases,
                "old_revision": old_lot.revision,
                "old_units_per_case": old_lot.units_per_case,
                "actual_revision": pc.actual_revision,
                "new_units_per_case": spec.units_per_case,
                "unit_delta": unit_delta,
                "reason": "旧箱规格更正，件数差异显式调整而非按新比例重算",
            },
        )
        assert self._unit_total(old_lot.sku) - before == unit_delta
        self._adjust_against_locks(event, [old_key, new_key])
        return new_lot

    @property
    def pending_corrections(self) -> list[PendingCorrection]:
        return list(self._pending.values())

    # ------------------------------------------------------------------ 锁定
    def lock_period(self, period_end: str) -> PeriodLock:
        if period_end in self._locks:
            raise LedgerError(f"期间 {period_end} 已锁定")
        snapshot = {key: qty for key, qty in self._pos.items() if qty}
        lock = PeriodLock(period_end=period_end, snapshot=snapshot)
        self._locks[period_end] = lock
        self._lock_views[period_end] = dict(snapshot)
        self._emit("period_locked", period_end, {"period_end": period_end})
        return lock

    @property
    def adjustments(self) -> list[Adjustment]:
        return list(self._adjustments)

    # ------------------------------------------------------------------ 占用
    def hold(
        self,
        request_id: str,
        warehouse: str,
        lot_id: str,
        form: Form,
        qty: int,
        state: CommitState = CommitState.PROVISIONAL,
    ) -> None:
        lot = self.lot(lot_id)
        if lot.state is LotState.QUARANTINED:
            raise LedgerError("隔离批次不能承诺")
        free = (
            self.free_cases(warehouse, lot_id)
            if form is Form.CASE
            else self.free_loose(warehouse, lot_id)
        )
        if free < qty:
            raise LedgerError(f"可用量不足，无法为 {request_id} 占用 {form.value} x{qty}")
        self._holds.append(
            Hold(
                request_id=request_id,
                warehouse=warehouse,
                lot_id=lot_id,
                form=form,
                qty=qty,
                state=state,
            )
        )

    def release_provisional(self, request_id: str) -> None:
        self._holds = [
            h
            for h in self._holds
            if not (h.request_id == request_id and h.state is CommitState.PROVISIONAL)
        ]

    def mark_picked(self, request_id: str) -> None:
        for h in self._holds:
            if h.request_id == request_id and h.state is CommitState.PROVISIONAL:
                h.state = CommitState.PICKED

    def ship(self, day: str, request_id: str) -> None:
        picked = [
            h
            for h in self._holds
            if h.request_id == request_id and h.state is CommitState.PICKED
        ]
        if not picked:
            raise LedgerError(f"申请 {request_id} 没有已确认拣货，不能发货")
        affected: list[tuple[str, str, Form]] = []
        for h in picked:
            key = self._key(h.warehouse, h.lot_id, h.form)
            self._take(key, h.qty)
            affected.append(key)
        self._holds = [h for h in self._holds if h not in picked]
        event = self._emit(
            "ship",
            day,
            {
                "request_id": request_id,
                "lines": [
                    {
                        "warehouse": h.warehouse,
                        "lot_id": h.lot_id,
                        "form": h.form.value,
                        "qty": h.qty,
                    }
                    for h in picked
                ],
                "reason": "门店补货发货",
            },
        )
        self._adjust_against_locks(event, affected)

    # ------------------------------------------------------------------ 查询
    def lot(self, lot_id: str) -> Lot:
        try:
            return self._lots[lot_id]
        except KeyError:
            raise LedgerError(f"未知批次 {lot_id}") from None

    def lots(self, sku: str | None = None) -> list[Lot]:
        return [l for l in self._lots.values() if sku is None or l.sku == sku]

    def positions(self) -> list[Position]:
        return [
            Position(warehouse=wh, lot_id=lot_id, form=form, qty=qty)
            for (wh, lot_id, form), qty in sorted(self._pos.items(), key=str)
            if qty
        ]

    def on_hand(self, warehouse: str, lot_id: str, form: Form) -> int:
        return self._pos.get(self._key(warehouse, lot_id, form), 0)

    def on_hand_lot(self, lot_id: str) -> int:
        lot = self.lot(lot_id)
        return (
            self.on_hand(lot.warehouse, lot_id, Form.CASE) * lot.units_per_case
            + self.on_hand(lot.warehouse, lot_id, Form.LOOSE)
        )

    def _held(self, warehouse: str, lot_id: str, form: Form) -> int:
        """已确认拣出（picked）才硬性冻结；provisional 由分配器自行管理，

        这样联合重核时先释放 provisional 再重算不会把自己算成占用。
        """
        return sum(
            h.qty
            for h in self._holds
            if h.warehouse == warehouse
            and h.lot_id == lot_id
            and h.form == form
            and h.state is CommitState.PICKED
        )

    def free_cases(self, warehouse: str, lot_id: str) -> int:
        lot = self.lot(lot_id)
        if lot.state is LotState.QUARANTINED:
            return 0
        return self.on_hand(warehouse, lot_id, Form.CASE) - self._held(
            warehouse, lot_id, Form.CASE
        )

    def free_loose(self, warehouse: str, lot_id: str) -> int:
        lot = self.lot(lot_id)
        if lot.state is LotState.QUARANTINED:
            return 0
        return self.on_hand(warehouse, lot_id, Form.LOOSE) - self._held(
            warehouse, lot_id, Form.LOOSE
        )

    def committed(self, request_id: str) -> list[Hold]:
        return [h for h in self._holds if h.request_id == request_id]

    def holds_on(self, lot_id: str) -> list[Hold]:
        return [h for h in self._holds if h.lot_id == lot_id]

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    def events_of_receipt(self, receipt_id: str) -> list[Event]:
        """旧签收单按原始口径回查：收货事件固化了当时的 units_per_case。"""
        return [
            e
            for e in self._events
            if e.payload.get("receipt_id") == receipt_id
        ]

    @property
    def transfers(self) -> list[Transfer]:
        return list(self._transfers.values())

    def transfer(self, transfer_id: str) -> Transfer:
        return self._transfers[transfer_id]

    @property
    def locks(self) -> list[PeriodLock]:
        return list(self._locks.values())

    # ------------------------------------------------------------------ 辅助
    def _require_normal(self, lot: Lot) -> None:
        if lot.state is LotState.QUARANTINED:
            raise LedgerError(f"隔离批次 {lot.lot_id} 须先完成包装确认")
        if lot.state in (LotState.RETURNED, LotState.RESOLVED):
            raise LedgerError(f"批次 {lot.lot_id} 状态 {lot.state.value}，不可操作")

    def _require_unpack_permission(self, lot: Lot) -> str:
        wh = self._warehouses.get(lot.warehouse)
        if wh is not None and not wh.can_unpack:
            raise LedgerError(f"仓库 {lot.warehouse} 无拆装权限")
        return lot.warehouse

    def _blockers(self, key: tuple[str, str, Form]) -> list[str]:
        return sorted(
            {
                h.request_id
                for h in self._holds
                if (h.warehouse, h.lot_id, h.form) == key
            }
        )

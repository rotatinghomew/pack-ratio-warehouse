"""对账、可交付量查询与批次流转追溯。"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Form, LotState, ReconciliationLine


@dataclass
class DeliverableRow:
    warehouse: str
    lot_id: str
    revision: str
    form: Form
    available_qty: int
    units: int
    note: str


def deliverable(ledger, sku: str, window: str) -> list[DeliverableRow]:
    """门店视角：可交付的数量与包装形态（隔离批次不可见、在途按 ETA 提示）。"""
    rows: list[DeliverableRow] = []
    for lot in ledger.lots(sku):
        if lot.state is LotState.QUARANTINED:
            rows.append(
                DeliverableRow(lot.warehouse, lot.lot_id, lot.revision, Form.CASE,
                               0, 0, "标签待确认，批次隔离中，暂不可承诺")
            )
            continue
        free_cases = ledger.free_cases(lot.warehouse, lot.lot_id)
        if free_cases:
            rows.append(
                DeliverableRow(lot.warehouse, lot.lot_id, lot.revision, Form.CASE,
                               free_cases, free_cases * lot.units_per_case,
                               "完整整箱/整盒")
            )
        free_loose = ledger.free_loose(lot.warehouse, lot.lot_id)
        if free_loose:
            rows.append(
                DeliverableRow(lot.warehouse, lot.lot_id, lot.revision, Form.LOOSE,
                               free_loose, free_loose, "拆箱余量（散件）")
            )
    for tr in ledger.transfers:
        if tr.sku != sku or tr.state != "in-transit":
            continue
        note = f"在途 {tr.source}->{tr.destination}，ETA {tr.eta}"
        if tr.eta > window:
            note += "，晚于交付窗口，本窗口不可承诺"
        rows.append(
            DeliverableRow(tr.destination, tr.source_lot, tr.revision, Form.CASE,
                           tr.cases, tr.cases * tr.units_per_case, note)
        )
    for pc in ledger.pending_corrections:
        rows.append(
            DeliverableRow(pc.warehouse, pc.lot_id, ledger.lot(pc.lot_id).revision,
                           Form.CASE, 0, 0,
                           f"存在待执行规格更正 {pc.correction_id}，承诺可能变化")
        )
    return rows


def reconciliation(ledger, period_end: str | None = None) -> list[ReconciliationLine]:
    """按仓、商品、原包装版本核对账面与实物，并解释在途与拆箱余量。

    本系统账面即由实物事件累积，正常情况下 variance 为 0；
    若期末锁定后发生过调整，调整记录附在对应行的 notes 里。
    """
    grouped: dict[tuple[str, str, str, Form], ReconciliationLine] = {}
    for pos in ledger.positions():
        lot = ledger.lot(pos.lot_id)
        key = (pos.warehouse, lot.sku, lot.revision, pos.form)
        line = grouped.setdefault(
            key,
            ReconciliationLine(
                warehouse=pos.warehouse, sku=lot.sku, revision=lot.revision,
                form=pos.form, book_qty=0, physical_qty=0,
            ),
        )
        line.book_qty += pos.qty
        line.physical_qty += pos.qty
        if lot.state is LotState.QUARANTINED:
            line.quarantined_qty += pos.qty
            line.notes.append("批次隔离，等待包装确认")
        if pos.form is Form.LOOSE:
            line.notes.append(
                f"拆箱剩余散件 {pos.qty} 件（原箱 {lot.units_per_case} 件/箱）"
            )
    # 在途单独成行解释：既不在发出仓也不在接收仓的实物
    for tr in ledger.transfers:
        if tr.state != "in-transit":
            continue
        key = (f"{tr.source}->{tr.destination}(在途)", tr.sku, tr.revision, Form.CASE)
        grouped[key] = ReconciliationLine(
            warehouse=key[0], sku=tr.sku, revision=tr.revision, form=Form.CASE,
            book_qty=tr.cases, physical_qty=tr.cases,
            notes=[f"在途调拨 {tr.transfer_id}，ETA {tr.eta}，{tr.cases} 整箱"],
        )
    # 锁定期调整挂到对应行
    for adj in ledger.adjustments:
        wh, lot_id, form = adj.key
        lot = ledger.lot(lot_id)
        key = (wh, lot.sku, lot.revision, form)
        line = grouped.get(key)
        if line is None:
            line = ReconciliationLine(wh, lot.sku, lot.revision, form, 0, 0)
            grouped[key] = line
        line.notes.append(
            f"锁定 {adj.period_end} 后事件#{adj.event_seq}({adj.kind}) "
            f"调整 {adj.delta:+d}：{adj.reason}"
        )
        line.locked_qty += abs(adj.delta)
    return sorted(grouped.values(), key=lambda x: (x.warehouse, x.sku, x.revision, x.form.value))


def trace_lot(ledger, lot_id: str) -> dict:
    """按原单回查一批箱件在仓间怎样流转：血缘链 + 相关事件 + 占用/发货。"""
    root = ledger.lot(lot_id)
    # 向上找根批次
    root_id = lot_id
    while ledger.lot(root_id).parent_lot:
        root_id = ledger.lot(root_id).parent_lot

    lineage: list[dict] = []
    stack = [root_id]
    while stack:
        current = stack.pop(0)
        lot = ledger.lot(current)
        children = [
            l.lot_id for l in ledger.lots(lot.sku) if l.parent_lot == current
        ]
        stack.extend(children)
        lineage.append({
            "lot_id": current,
            "revision": lot.revision,
            "units_per_case": lot.units_per_case,
            "warehouse": lot.warehouse,
            "state": lot.state.value,
            "parent_lot": lot.parent_lot,
            "source_event": lot.source_event,
            "children": children,
            "on_hand_units": ledger.on_hand_lot(current),
        })

    related_events = [
        {
            "seq": e.seq,
            "kind": e.kind,
            "day": e.day,
            "payload": e.payload,
        }
        for e in ledger.events
        if _event_touches_lot(e, root_id, ledger)
    ]
    # 根批次与所有子批次的占用/拣货状态一并呈现
    lineage_ids = {node["lot_id"] for node in lineage}
    holds = [
        {
            "request_id": h.request_id,
            "warehouse": h.warehouse,
            "lot_id": h.lot_id,
            "form": h.form.value,
            "qty": h.qty,
            "state": h.state.value,
        }
        for lot_id in lineage_ids
        for h in ledger.holds_on(lot_id)
    ]
    return {
        "root_lot": root_id,
        "queried_lot": lot_id,
        "sku": root.sku,
        "lineage": lineage,
        "events": related_events,
        "holds": holds,
    }


def _event_touches_lot(event, root_lot_id: str, ledger) -> bool:
    payload = event.payload
    lot_fields = ("lot_id", "old_lot", "new_lot", "source_lot", "quarantine_lot",
                  "released_lot")
    for field_name in lot_fields:
        value = payload.get(field_name)
        if value and _is_related(value, root_lot_id, ledger):
            return True
    for line in payload.get("lines", []):
        if _is_related(line.get("lot_id", ""), root_lot_id, ledger):
            return True
    return False


def _is_related(lot_id: str, root_lot_id: str, ledger) -> bool:
    if not lot_id:
        return False
    current = lot_id
    seen = set()
    while current and current not in seen:
        if current == root_lot_id:
            return True
        seen.add(current)
        try:
            current = ledger.lot(current).parent_lot
        except Exception:
            return False
    return False

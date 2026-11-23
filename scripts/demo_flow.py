"""端到端业务场景脚本：隔离释放、锁定调整、更正重核、延迟重排、追溯。

运行：python3 -I scripts/demo_flow.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stockchain.catalog import PackagingCatalog, PackagingSpec, ReturnPolicy
from stockchain.ledger import InventoryLedger
from stockchain.allocation import Allocator, Request
from stockchain.models import Form, Warehouse
from stockchain.reports import deliverable, reconciliation, trace_lot

catalog = PackagingCatalog([
    PackagingSpec("SEAL-9", "R1", 12, ReturnPolicy.CASE_ONLY, valid_to="2026-11-01"),
    PackagingSpec("SEAL-9", "R2", 10, ReturnPolicy.EACH, valid_from="2026-11-01"),
])
ledger = InventoryLedger(catalog, [Warehouse("W1"), Warehouse("W2", can_unpack=False)])

r1 = ledger.receive("RC1", "2026-10-20", "W1", "SEAL-9", "R1", 4)
r2 = ledger.receive("RC2", "2026-11-03", "W1", "SEAL-9", "R2", 4)
unknown = ledger.receive("RC3", "2026-11-05", "W1", "SEAL-9", None, 1, label="UNREAD")
ledger.lock_period("2026-11-15")

allocator = Allocator(ledger)
# 高优先级申请吃掉本地 R1 整箱，第二张只能靠在途
allocator.add_request(Request("REQ-HI", "S8", "SEAL-9", 24, "2026-11-25",
                              frozenset({"R1"}), False, priority=100))
ledger.initiate_transfer("T1", "2026-11-16", "W1", "W2", r1.lot_id, 1, "2026-11-24")
plan0 = allocator.add_request(
    Request("REQ0", "S9", "SEAL-9", 24, "2026-11-25", frozenset({"R1"}), False, priority=99)
)
assert any(line.in_transit for line in plan0.lines) and plan0.feasible
print("REQ0 初始承诺含在途:", [(l.transfer_id, l.qty) for l in plan0.lines if l.in_transit])

# 调拨延迟超过窗口 -> 联合重排，在途承诺撤回，出现缺口
ledger.delay_transfer("2026-11-19", "T1", "2026-11-28")
replanned = allocator.replan_affected(set(), {"T1"}, note="T1 延迟超出交付窗口")
assert not replanned["REQ0"].feasible
print("延迟后 REQ0 缺口:", replanned["REQ0"].short_units, replanned["REQ0"].change_note)

# 隔离批次确认释放，锁后事件留调整痕
released = ledger.confirm_packaging("2026-11-17", unknown.lot_id, "R2")
print("隔离释放:", unknown.lot_id, "->", released.lot_id)
print("锁后调整:", [(a.kind, a.delta) for a in ledger.adjustments])

# 旧箱规格更正：件数差异显式记录，并触发占用申请的联合重核
ledger.register_correction("2026-11-18", "C1", "RC1", r1.lot_id, 1, "R2")
new_lot = ledger.apply_correction("2026-11-18", "C1")
print("更正子批次:", new_lot.lot_id, "血缘:", new_lot.parent_lot,
      "口径 12 ->", new_lot.units_per_case)

# 对账与追溯
for line in reconciliation(ledger):
    print("对账:", line.warehouse, line.revision, line.form.value,
          line.book_qty, line.physical_qty, line.notes)
trace = trace_lot(ledger, r1.lot_id)
print("批次血缘节点:", [(n["lot_id"], n["revision"]) for n in trace["lineage"]])
print("可交付行数:", len(deliverable(ledger, "SEAL-9", "2026-11-25")))
print("DEMO OK")

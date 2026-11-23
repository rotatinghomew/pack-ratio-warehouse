"""门店可交付查询、对账解释与批次流转追溯测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stockchain.catalog import PackagingCatalog, PackagingSpec, ReturnPolicy
from stockchain.ledger import InventoryLedger
from stockchain.models import Form, Warehouse
from stockchain.reports import deliverable, reconciliation, trace_lot


def build_world():
    catalog = PackagingCatalog([
        PackagingSpec("SEAL-9", "R1", 12, ReturnPolicy.CASE_ONLY, valid_to="2026-11-01"),
        PackagingSpec("SEAL-9", "R2", 10, ReturnPolicy.EACH, valid_from="2026-11-01"),
    ])
    ledger = InventoryLedger(catalog, [Warehouse("W1"), Warehouse("W2", can_unpack=False)])
    r1 = ledger.receive("RC1", "2026-10-20", "W1", "SEAL-9", "R1", 3)
    r2 = ledger.receive("RC2", "2026-11-03", "W1", "SEAL-9", "R2", 4)
    unknown = ledger.receive("RC3", "2026-11-05", "W1", "SEAL-9", None, 1)
    ledger.unpack("2026-11-08", r2.lot_id, 1)
    ledger.initiate_transfer("T1", "2026-11-10", "W1", "W2", r1.lot_id, 1, "2026-11-24")
    return ledger, r1, r2, unknown


class DeliverableTests(unittest.TestCase):
    def test_rows_explain_form_quarantine_and_in_transit(self):
        ledger, r1, r2, unknown = build_world()
        rows = deliverable(ledger, "SEAL-9", "2026-11-25")
        notes = "|".join(n.note for n in rows)
        self.assertIn("完整整箱", notes)
        self.assertIn("拆箱余量", notes)
        self.assertIn("隔离", notes)
        self.assertIn("在途", notes)
        # 隔离批次可用量必须为 0
        self.assertTrue(all(n.available_qty == 0 for n in rows
                            if n.lot_id == unknown.lot_id))

    def test_late_transit_is_marked_unpromisable_for_window(self):
        ledger, *_ = build_world()
        ledger.delay_transfer("2026-11-19", "T1", "2026-11-28")
        rows = deliverable(ledger, "SEAL-9", "2026-11-25")
        transit = [n for n in rows if "在途" in n.note][0]
        self.assertIn("晚于交付窗口", transit.note)


class ReconciliationTests(unittest.TestCase):
    def test_in_transit_and_loose_are_explained_not_lost(self):
        ledger, r1, r2, unknown = build_world()
        lines = reconciliation(ledger)
        by_wh = {l.warehouse: l for l in lines}
        self.assertIn("W1->W2(在途)", by_wh)
        loose = [l for l in lines if l.form is Form.LOOSE][0]
        self.assertIn("拆箱剩余散件", loose.notes[0])
        quarantined = [l for l in lines if l.quarantined_qty][0]
        self.assertIn("隔离", quarantined.notes[0])
        # 账面与实物一致（本系统账面即实物事件累积）
        self.assertTrue(all(l.variance == 0 for l in lines))

    def test_locked_period_adjustments_attach_to_reconciliation_notes(self):
        ledger, r1, r2, unknown = build_world()
        ledger.lock_period("2026-11-15")
        ledger.confirm_packaging("2026-11-17", unknown.lot_id, "R2")
        lines = reconciliation(ledger)
        notes = "|".join(n for l in lines for n in l.notes)
        self.assertIn("confirm_packaging", notes)
        self.assertIn("2026-11-15", notes)


class TraceTests(unittest.TestCase):
    def test_lineage_follows_transfer_and_correction_across_warehouses(self):
        ledger, r1, r2, unknown = build_world()
        arrived = ledger.arrive_transfer("2026-11-24", "T1")
        ledger.register_correction("2026-11-25", "C1", "RC1",
                                   arrived.lot_id, 1, "R2")
        corrected = ledger.apply_correction("2026-11-26", "C1")
        trace = trace_lot(ledger, corrected.lot_id)
        self.assertEqual(trace["root_lot"], r1.lot_id)
        node_ids = {n["lot_id"] for n in trace["lineage"]}
        self.assertIn(arrived.lot_id, node_ids)
        self.assertIn(corrected.lot_id, node_ids)
        kinds = {e["kind"] for e in trace["events"]}
        self.assertIn("transfer_out", kinds)
        self.assertIn("transfer_in", kinds)
        self.assertIn("correction_applied", kinds)


if __name__ == "__main__":
    unittest.main()

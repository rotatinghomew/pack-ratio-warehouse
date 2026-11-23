"""账本事件规则：实物守恒、隔离释放、返厂政策、锁定调整、更正留痕、占用冻结。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stockchain.catalog import PackagingCatalog, PackagingSpec, ReturnPolicy
from stockchain.ledger import InventoryLedger, LedgerError
from stockchain.models import CommitState, Form, Warehouse


def build_ledger() -> InventoryLedger:
    catalog = PackagingCatalog([
        PackagingSpec("SEAL-9", "R1", 12, ReturnPolicy.CASE_ONLY, valid_to="2026-11-01"),
        PackagingSpec("SEAL-9", "R2", 10, ReturnPolicy.EACH, valid_from="2026-11-01"),
    ])
    return InventoryLedger(catalog, [Warehouse("W1"), Warehouse("W2", can_unpack=False)])


class ConservationTests(unittest.TestCase):
    def setUp(self):
        self.ledger = build_ledger()
        self.r2 = self.ledger.receive("RC2", "2026-11-03", "W1", "SEAL-9", "R2", 4)

    def test_unpack_preserves_unit_count(self):
        before = self.ledger._unit_total("SEAL-9")
        self.ledger.unpack("2026-11-08", self.r2.lot_id, 2)
        self.assertEqual(self.ledger.on_hand("W1", self.r2.lot_id, Form.CASE), 2)
        self.assertEqual(self.ledger.on_hand("W1", self.r2.lot_id, Form.LOOSE), 20)
        self.assertEqual(self.ledger._unit_total("SEAL-9"), before)

    def test_repack_consumes_exact_loose_units_and_creates_lineage(self):
        self.ledger.unpack("2026-11-08", self.r2.lot_id, 2)  # 20 散件
        before = self.ledger._unit_total("SEAL-9")
        first = self.ledger.repack("2026-11-09", self.r2.lot_id, "R2", 1)
        self.assertEqual(first.parent_lot, self.r2.lot_id)
        self.assertEqual(self.ledger._unit_total("SEAL-9"), before)
        # 剩 10 散件正好再装一箱；第三箱没有散件，必须失败
        self.ledger.repack("2026-11-09", self.r2.lot_id, "R2", 1)
        with self.assertRaises(LedgerError):
            self.ledger.repack("2026-11-09", self.r2.lot_id, "R2", 1)

    def test_transfer_keeps_total_units_visible_as_in_transit(self):
        before = self.ledger._unit_total("SEAL-9")
        tr = self.ledger.initiate_transfer(
            "T1", "2026-11-10", "W1", "W2", self.r2.lot_id, 1, "2026-11-20"
        )
        self.assertEqual(self.ledger._unit_total("SEAL-9"), before)
        arrived = self.ledger.arrive_transfer("2026-11-20", "T1")
        self.assertEqual(arrived.parent_lot, tr.source_lot)
        self.assertEqual(self.ledger._unit_total("SEAL-9"), before)


class QuarantineTests(unittest.TestCase):
    def setUp(self):
        self.ledger = build_ledger()

    def test_unknown_label_quarantines_and_excludes_from_availability(self):
        lot = self.ledger.receive("RC3", "2026-11-05", "W1", "SEAL-9", None, 1,
                                  label="UNREAD")
        self.assertTrue(lot.quarantined)
        self.assertEqual(self.ledger.free_cases("W1", lot.lot_id), 0)
        with self.assertRaises(LedgerError):
            self.ledger.unpack("2026-11-06", lot.lot_id, 1)

    def test_late_packaging_confirmation_releases_batch_with_lineage(self):
        lot = self.ledger.receive("RC3", "2026-11-05", "W1", "SEAL-9", None, 1)
        released = self.ledger.confirm_packaging("2026-11-07", lot.lot_id, "R2")
        self.assertEqual(released.parent_lot, lot.lot_id)
        self.assertEqual(released.units_per_case, 10)
        self.assertEqual(self.ledger.free_cases("W1", released.lot_id), 1)
        self.assertEqual(self.ledger.lot(lot.lot_id).state.value, "resolved")


class ReturnPolicyTests(unittest.TestCase):
    def test_case_only_revision_rejects_loose_return(self):
        ledger = build_ledger()
        r1 = ledger.receive("RC1", "2026-10-20", "W1", "SEAL-9", "R1", 3)
        with self.assertRaises(LedgerError):
            ledger.return_to_vendor("2026-11-11", r1.lot_id, Form.LOOSE, 1)
        ledger.return_to_vendor("2026-11-11", r1.lot_id, Form.CASE, 1)
        self.assertEqual(ledger.on_hand("W1", r1.lot_id, Form.CASE), 2)

    def test_each_revision_allows_loose_return(self):
        ledger = build_ledger()
        r2 = ledger.receive("RC2", "2026-11-03", "W1", "SEAL-9", "R2", 2)
        ledger.unpack("2026-11-08", r2.lot_id, 1)
        ledger.return_to_vendor("2026-11-09", r2.lot_id, Form.LOOSE, 5)
        self.assertEqual(ledger.on_hand("W1", r2.lot_id, Form.LOOSE), 5)


class UnpackPermissionTests(unittest.TestCase):
    def test_warehouse_without_unpack_right_cannot_open_cases(self):
        ledger = build_ledger()
        lot = ledger.receive("RC4", "2026-11-06", "W2", "SEAL-9", "R2", 2)
        with self.assertRaises(LedgerError):
            ledger.unpack("2026-11-07", lot.lot_id, 1)


class LockAdjustmentTests(unittest.TestCase):
    def test_changes_after_lock_leave_traceable_adjustments(self):
        ledger = build_ledger()
        r2 = ledger.receive("RC2", "2026-11-03", "W1", "SEAL-9", "R2", 4)
        ledger.lock_period("2026-11-15")
        ledger.unpack("2026-11-16", r2.lot_id, 1)
        kinds = [(a.kind, a.delta, a.event_seq) for a in ledger.adjustments]
        self.assertTrue(any(kind == "unpack" and delta == -1 for kind, delta, _ in kinds))
        self.assertTrue(any(kind == "unpack" and delta == 10 for kind, delta, _ in kinds))
        for adj in ledger.adjustments:
            self.assertEqual(adj.period_end, "2026-11-15")
            self.assertGreater(adj.event_seq, 0)

    def test_events_before_lock_do_not_create_adjustments(self):
        ledger = build_ledger()
        r2 = ledger.receive("RC2", "2026-11-03", "W1", "SEAL-9", "R2", 4)
        ledger.unpack("2026-11-08", r2.lot_id, 1)
        ledger.lock_period("2026-11-15")
        self.assertEqual(ledger.adjustments, [])


class CorrectionTests(unittest.TestCase):
    def test_correction_derives_new_lot_and_records_unit_delta_explicitly(self):
        ledger = build_ledger()
        r1 = ledger.receive("RC1", "2026-10-20", "W1", "SEAL-9", "R1", 3)
        ledger.register_correction("2026-11-16", "C1", "RC1", r1.lot_id, 1, "R2")
        # 待执行阶段实物不动
        self.assertEqual(ledger.on_hand("W1", r1.lot_id, Form.CASE), 3)
        new_lot = ledger.apply_correction("2026-11-17", "C1")
        self.assertEqual(new_lot.revision, "R2")
        self.assertEqual(new_lot.units_per_case, 10)
        self.assertEqual(new_lot.parent_lot, r1.lot_id)
        event = [e for e in ledger.events if e.kind == "correction_applied"][0]
        self.assertEqual(event.payload["unit_delta"], -2)  # 12 -> 10

    def test_correction_blocked_by_picked_hold_names_the_blocker(self):
        ledger = build_ledger()
        r1 = ledger.receive("RC1", "2026-10-20", "W1", "SEAL-9", "R1", 3)
        ledger.hold("REQ9", "W1", r1.lot_id, Form.CASE, 1, CommitState.PICKED)
        # 更正全部 3 箱，但其中 1 箱已确认拣出，自由量只有 2 箱，必须阻断
        ledger.register_correction("2026-11-16", "C1", "RC1", r1.lot_id, 3, "R2")
        with self.assertRaises(LedgerError) as ctx:
            ledger.apply_correction("2026-11-17", "C1")
        self.assertIn("REQ9", str(ctx.exception))


class HoldFreezeTests(unittest.TestCase):
    def test_picked_cases_cannot_be_unpacked_again(self):
        ledger = build_ledger()
        r2 = ledger.receive("RC2", "2026-11-03", "W1", "SEAL-9", "R2", 3)
        ledger.hold("REQ2", "W1", r2.lot_id, Form.CASE, 2, CommitState.PICKED)
        # 只剩 1 个自由箱，试图拆 2 箱必须被拒
        with self.assertRaises(LedgerError):
            ledger.unpack("2026-11-20", r2.lot_id, 2)
        ledger.unpack("2026-11-20", r2.lot_id, 1)  # 自由箱可拆
        self.assertEqual(ledger._held("W1", r2.lot_id, Form.CASE), 2)


if __name__ == "__main__":
    unittest.main()

"""包装版次目录与旧签收单口径冻结的测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stockchain.catalog import PackagingCatalog, PackagingError, PackagingSpec, ReturnPolicy
from stockchain.ledger import InventoryLedger


def build_catalog() -> PackagingCatalog:
    return PackagingCatalog([
        PackagingSpec("SEAL-9", "R1", 12, ReturnPolicy.CASE_ONLY, valid_to="2026-11-01"),
        PackagingSpec("SEAL-9", "R2", 10, ReturnPolicy.EACH, valid_from="2026-11-01"),
    ])


class PackagingCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = build_catalog()

    def test_revisions_have_distinct_effective_windows(self):
        self.assertEqual(self.catalog.spec_at("SEAL-9", "R1", "2026-10-31").units_per_case, 12)
        self.assertEqual(self.catalog.spec_at("SEAL-9", "R2", "2026-11-01").units_per_case, 10)

    def test_expired_and_not_yet_effective_revisions_are_rejected_by_date(self):
        with self.assertRaises(PackagingError):
            self.catalog.spec_at("SEAL-9", "R1", "2026-11-01")
        with self.assertRaises(PackagingError):
            self.catalog.spec_at("SEAL-9", "R2", "2026-10-31")

    def test_duplicate_revision_registration_is_rejected(self):
        with self.assertRaises(PackagingError):
            PackagingCatalog([
                PackagingSpec("X", "R1", 1, ReturnPolicy.NONE),
                PackagingSpec("X", "R1", 2, ReturnPolicy.NONE),
            ])

    def test_receipt_freezes_original_caliber_even_after_table_changes(self):
        """旧箱按收货时点口径固化，之后用当前换算表重算也不会变件数。"""
        ledger = InventoryLedger(self.catalog)
        lot = ledger.receive("RC1", "2026-10-20", "W1", "SEAL-9", "R1", 5)
        self.assertEqual(lot.units_per_case, 12)
        self.assertEqual(ledger.on_hand_lot(lot.lot_id), 5 * 12)
        event = [e for e in ledger.events_of_receipt("RC1") if e.kind == "receive"][0]
        # 11 月 R1 已失效，但旧签收事件仍保留 12 件/箱的原始口径
        self.assertEqual(event.payload["units_per_case"], 12)
        with self.assertRaises(PackagingError):
            self.catalog.spec_at("SEAL-9", "R1", "2026-11-20")


if __name__ == "__main__":
    unittest.main()

"""分配引擎测试：完整包装、拆箱替代、散件余量、联合重核、拣货冻结、在途窗口。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stockchain.allocation import Allocator, Request
from stockchain.catalog import PackagingCatalog, PackagingSpec, ReturnPolicy
from stockchain.ledger import InventoryLedger, LedgerError
from stockchain.models import Form, Warehouse


def build_world():
    catalog = PackagingCatalog([
        PackagingSpec("SEAL-9", "R1", 12, ReturnPolicy.CASE_ONLY, valid_to="2026-11-01"),
        PackagingSpec("SEAL-9", "R2", 10, ReturnPolicy.EACH, valid_from="2026-11-01"),
    ])
    ledger = InventoryLedger(catalog, [Warehouse("W1"), Warehouse("W2", can_unpack=False)])
    r1 = ledger.receive("RC1", "2026-10-20", "W1", "SEAL-9", "R1", 3)
    r2 = ledger.receive("RC2", "2026-11-03", "W1", "SEAL-9", "R2", 4)
    w2 = ledger.receive("RC4", "2026-11-06", "W2", "SEAL-9", "R2", 2)
    ledger.unpack("2026-11-08", r2.lot_id, 1)  # 10 件散件余量
    return ledger, Allocator(ledger), r1, r2, w2


def req(rid, units, *, allow_split, revisions=(), priority=0, window="2026-11-25"):
    return Request(rid, f"S-{rid}", "SEAL-9", units, window,
                   frozenset(revisions), allow_split, priority)


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.ledger, self.alloc, self.r1, self.r2, self.w2 = build_world()

    def test_no_split_requires_whole_cases_and_respects_allowed_revisions(self):
        plan = self.alloc.add_request(req("REQ1", 24, allow_split=False,
                                          revisions=("R1", "R2")))
        self.assertTrue(plan.feasible)
        self.assertTrue(all(line.form is Form.CASE for line in plan.lines))
        # 只接受 R2 时，R1 箱不能参与
        plan_r2 = self.alloc.add_request(req("REQ2", 10, allow_split=False,
                                             revisions=("R2",)))
        self.assertTrue(all(line.revision == "R2" for line in plan_r2.lines))

    def test_split_request_consumes_loose_remainder_before_opening_cases(self):
        plan = self.alloc.add_request(req("REQ3", 6, allow_split=True,
                                          revisions=("R2",)))
        self.assertEqual([(l.form, l.qty) for l in plan.lines],
                         [(Form.LOOSE, 6)])
        self.assertFalse(any(l.requires_unpack for l in plan.lines))

    def test_split_requires_unpack_flag_and_real_unpack_on_confirmation(self):
        # 散件 10 件先被高优先级小请求吃掉，新请求只能拆整箱
        self.alloc.add_request(req("REQ-A", 10, allow_split=True,
                                   revisions=("R2",), priority=10))
        plan = self.alloc.add_request(req("REQ-B", 12, allow_split=True,
                                          revisions=("R2",)))
        self.assertTrue(any(l.requires_unpack for l in plan.lines))
        self.alloc.confirm_pick("REQ-B", "2026-11-20")
        # 确认后账本上是 picked 散件占用，而非整箱
        holds = self.ledger.committed("REQ-B")
        self.assertTrue(all(h.state.value == "picked" for h in holds))
        self.assertTrue(any(h.form is Form.LOOSE for h in holds))

    def test_no_unpack_right_warehouse_cannot_serve_split_via_its_cases(self):
        # W2 无拆装权限：只能整箱供应，拆箱替代不允许使用 W2 的箱
        plan = self.alloc.add_request(
            Request("REQ4", "S4", "SEAL-9", 15, "2026-11-25",
                    frozenset(("R2",)), True))
        for line in plan.lines:
            if line.requires_unpack:
                self.assertNotEqual(line.warehouse, "W2")

    def test_confirmed_picked_cases_are_not_reallocated_to_other_stores(self):
        plan = self.alloc.add_request(req("REQ5", 10, allow_split=False,
                                          revisions=("R2",)))
        self.alloc.confirm_pick("REQ5", "2026-11-20")
        picked_lot = plan.lines[0].lot_id
        with self.assertRaises(LedgerError):
            self.ledger.unpack("2026-11-21", picked_lot,
                               self.ledger.on_hand("W1", picked_lot, Form.CASE))

    def test_transfer_only_promised_when_eta_meets_window(self):
        # 高优先级申请先占满本地 R1（3 整箱），REQ6 只能依赖在途的 1 箱
        self.alloc.add_request(req("REQ-HI", 36, allow_split=False,
                                   revisions=("R1",), priority=100))
        self.ledger.initiate_transfer(
            "T1", "2026-11-16", "W1", "W2", self.r1.lot_id, 1, "2026-11-24"
        )
        plan = self.alloc.add_request(req("REQ6", 12, allow_split=False,
                                          revisions=("R1",)))
        self.assertTrue(any(l.transfer_id == "T1" for l in plan.lines))
        # 延迟到窗口之后：重排必须撤回在途承诺
        self.ledger.delay_transfer("2026-11-19", "T1", "2026-11-28")
        replanned = self.alloc.replan_affected(
            set(), {"T1"}, note="T1 延迟超出窗口"
        )
        self.assertFalse(replanned["REQ6"].feasible)
        self.assertFalse(any(l.transfer_id == "T1"
                             for l in replanned["REQ6"].lines))
        self.assertTrue(replanned["REQ6"].change_note)

    def test_joint_replan_never_double_books_the_same_batch(self):
        # 两张都能单独满足的申请，联合重核后总预留仍不得超过实物
        a = self.alloc.add_request(req("REQ7", 10, allow_split=True,
                                       revisions=("R2",), priority=5))
        b = self.alloc.add_request(req("REQ8", 20, allow_split=True,
                                       revisions=("R2",), priority=4))
        self.ledger.register_correction(
            "2026-11-16", "C1", "RC2", self.r2.lot_id, 1, "R2"
        )
        results = self.alloc.replan_affected({self.r2.lot_id}, note="更正联合重核")
        self.assertEqual(set(results), {"REQ7", "REQ8"})
        reserved_cases = sum(
            qty for (wh, lot, form), qty in self.alloc._reservations.items()
            if form is Form.CASE
        )
        physical_cases = sum(
            p.qty for p in self.ledger.positions() if p.form is Form.CASE
        )
        self.assertLessEqual(reserved_cases, physical_cases)

    def test_short_request_is_flagged_and_not_confirmable(self):
        plan = self.alloc.add_request(req("REQ9", 999, allow_split=True))
        self.assertFalse(plan.feasible)
        with self.assertRaises(LedgerError):
            self.alloc.confirm_pick("REQ9", "2026-11-20")


if __name__ == "__main__":
    unittest.main()

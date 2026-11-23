"""基于 data/facts.json 合成资料的端到端测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stockchain.fixture import build
from stockchain.models import Form, LotState
from stockchain.reports import deliverable, reconciliation, trace_lot


class FixtureScenarioTests(unittest.TestCase):
    def setUp(self):
        self.data, self.ledger, self.alloc = build()

    def test_fixture_builds_quarantine_transfer_lock_and_pending_correction(self):
        states = {l.lot_id: l.state for l in self.ledger.lots("SEAL-9")}
        self.assertIn(LotState.QUARANTINED, states.values())
        self.assertEqual(self.ledger.transfers[0].state, "in-transit")
        self.assertEqual(len(self.ledger.pending_corrections), 1)
        self.assertEqual(self.ledger.locks[0].period_end, "2026-11-15")

    def test_fixture_requests_are_feasible_with_complete_packaging(self):
        for rid in ("REQ1", "REQ2"):
            plan = self.alloc.plans[rid]
            self.assertTrue(plan.feasible, f"{rid} 应可满足")
        # REQ1 不允许拆箱，全部行必须是完整整箱
        self.assertTrue(all(line.form is Form.CASE
                            for line in self.alloc.plans["REQ1"].lines))
        # REQ2 先消化散件余量
        self.assertEqual(self.alloc.plans["REQ2"].lines[0].form, Form.LOOSE)

    def test_confirmation_releases_quarantine_and_records_lock_adjustment(self):
        q_lot = next(l for l in self.ledger.lots("SEAL-9") if l.quarantined)
        released = self.ledger.confirm_packaging("2026-11-17", q_lot.lot_id, "R2")
        self.assertEqual(self.ledger.free_cases("W1", released.lot_id), 1)
        self.assertTrue(any(a.kind == "confirm_packaging"
                            for a in self.ledger.adjustments))

    def test_applying_correction_triggers_joint_replan_without_double_booking(self):
        pc = self.ledger.pending_corrections[0]
        new_lot = self.ledger.apply_correction("2026-11-18", pc.correction_id)
        results = self.alloc.replan_affected(
            {pc.lot_id}, note="C1 旧箱规格更正后联合重核"
        )
        self.assertIn("REQ1", results)
        # 所有在仓整箱预留不得超过实物
        for (wh, lot_id, form), qty in self.alloc._reservations.items():
            if form is Form.CASE:
                self.assertLessEqual(
                    qty, self.ledger.on_hand(wh, lot_id, Form.CASE)
                )
        # 更正差异显式留痕：R1 12 件/箱 -> R2 10 件/箱
        correction_events = [e for e in self.ledger.events
                             if e.kind == "correction_applied"]
        self.assertEqual(correction_events[-1].payload["unit_delta"], -2)
        self.assertEqual(new_lot.parent_lot, pc.lot_id)

    def test_reports_run_against_fixture_and_trace_covers_lineage(self):
        rows = deliverable(self.ledger, "SEAL-9", "2026-11-25")
        self.assertTrue(any("在途" in r.note for r in rows))
        lines = reconciliation(self.ledger)
        self.assertTrue(any("在途" in " ".join(l.notes) for l in lines))
        r1_lot = next(l for l in self.ledger.lots("SEAL-9") if l.revision == "R1")
        trace = trace_lot(self.ledger, r1_lot.lot_id)
        self.assertEqual(trace["root_lot"], r1_lot.lot_id)
        self.assertIn("transfer_out", {e["kind"] for e in trace["events"]})


if __name__ == "__main__":
    unittest.main()

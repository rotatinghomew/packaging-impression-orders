"""委印服务核心口径测试。"""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import json
import unittest

from service import CommissionPrintingService, load_service

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "inputs"


def data(name):
    return json.loads((INPUTS / f"{name}.json").read_text(encoding="utf-8"))


class SampleScenario(unittest.TestCase):
    def setUp(self):
        self.svc = load_service(INPUTS)
        self.report = self.svc.report()

    def test_purchaser_sees_stop_and_inbound(self):
        view = self.svc.purchasing_report("WL7")
        self.assertEqual(view["affected_sites"], ["A", "C"])
        states = {row["site"]: row["stop_state"] for row in view["sites"]}
        self.assertEqual(states["C"], "confirmed_stopped")
        self.assertIn("stop_failed", states["A"])
        a = next(row for row in view["sites"] if row["site"] == "A")
        self.assertEqual([x["run"] for x in a["unstopped_reported_batches"]], ["I-27"])
        self.assertEqual(a["unstopped_reported_batches"][0]["printed_qty"], 120)
        pending = {(x["run"], x["site"]): x["outstanding_receipt_qty"] for x in view["will_receive"]}
        self.assertEqual(pending[("I-26", "C")], 80)
        self.assertEqual(pending[("I-26", "A")], 0)
        self.assertEqual(pending[("I-27", "A")], 0)

    def test_warehouse_batches_and_basis(self):
        rows = {(r["run"], r["site"]): r for r in self.svc.warehouse_report()["batches"]}
        a26 = rows[("I-26", "A")]
        self.assertEqual(a26["received_qty"], 160)
        self.assertEqual(a26["issued_qty"], 65)
        self.assertEqual(a26["delivered_qty"], 40)
        self.assertEqual(a26["warehouse_on_hand_qty"], 95)
        self.assertEqual(a26["line_side_open_qty"], 25)
        self.assertEqual(a26["valid_overlaid_qty"], 23)
        self.assertEqual(a26["available_qty"], 23)
        self.assertEqual(a26["pending_overlay_qty"], 72)
        self.assertEqual(a26["consumed_qty"], 65)
        self.assertEqual(a26["pending_overlay_including_line_side_qty"], 97)
        a27 = rows[("I-27", "A")]
        self.assertEqual((a27["received_qty"], a27["available_qty"], a27["pending_overlay_qty"]), (120, 0, 120))
        totals = self.svc.warehouse_report()["totals"]
        self.assertEqual(totals["warehouse_on_hand_qty"], 215)
        self.assertEqual(totals["available_qty"], 23)
        self.assertEqual(totals["pending_overlay_qty"], 192)
        self.assertEqual(totals["consumed_qty"], 65)

    def test_finance_payable_items(self):
        fin = self.svc.finance_report()
        self.assertEqual(fin["printed_qualified_qty"], 500)
        self.assertEqual(fin["printed_subtotal"], Decimal("120.00"))
        self.assertEqual(fin["cancellation_subtotal"], Decimal("18.00"))
        self.assertEqual(fin["payable_total"], Decimal("138.00"))
        self.assertEqual(len(fin["payable_items"]), 6)

    def test_certificate_uses_old_revision(self):
        certs = self.svc.certificates_report()
        self.assertEqual(len(certs), 1)
        cert = certs[0]
        self.assertTrue(cert["certifiable"])
        self.assertEqual((cert["plate"], cert["revision"], cert["run"], cert["quantity"]), ("IP28", 1, "I-26", 40))
        self.assertEqual(cert["label_claims_at_time"], ["PLANT", "LOCAL"])

    def test_sales_order_only_gets_completed_overlay_stock(self):
        avail = self.svc.order_availability(
            {"id": "T1", "market": "N", "sku": "P1", "delivery_date": "2026-12-10", "quantity": 100}
        )
        self.assertEqual(avail["currently_available_qty"], 23)
        self.assertEqual(avail["shortfall_qty"], 77)
        self.assertEqual(avail["allocation"][0]["run"], "I-26")
        self.assertEqual(avail["allocation"][0]["allocate_qty"], 23)


class IdempotencyAndRules(unittest.TestCase):
    def setUp(self):
        self.design = data("designs")
        self.press = data("press")
        self.stock = data("stock")

    def test_duplicate_press_batch_is_not_double_billed(self):
        press = deepcopy(self.press)
        press["reports"].append(
            {"id": "P-DUP", "kind": "printed", "run": "I-26", "count": {"A": 160}, "at": "2026-12-03T12:00:00+08:00"}
        )
        svc = CommissionPrintingService(self.design, press, self.stock)
        self.assertEqual(svc.finance_report()["printed_qualified_qty"], 500)
        self.assertEqual(svc.finance_report()["payable_total"], Decimal("138.00"))

    def test_exact_duplicate_stock_event_does_not_form_stock_twice(self):
        stock = self.stock + [deepcopy(self.stock[0])]
        svc = CommissionPrintingService(self.design, self.press, stock)
        rows = {(r["run"], r["site"]): r for r in svc.warehouse_report()["batches"]}
        self.assertEqual(rows[("I-26", "A")]["received_qty"], 160)
        self.assertEqual(len(svc.stock_events), len(self.stock))

    def test_rejected_pieces_are_not_paid(self):
        press = deepcopy(self.press)
        press["reports"].append(
            {"id": "P-WASTE", "kind": "rejected", "run": "I-27", "count": {"A": 20}, "at": "2026-12-03T11:20:00+08:00"}
        )
        svc = CommissionPrintingService(self.design, press, self.stock)
        fin = svc.finance_report()
        # I-27 A qualified drops 120 -> 100, reducing 4.80.
        self.assertEqual(fin["printed_qualified_qty"], 480)
        self.assertEqual(fin["payable_total"], Decimal("133.20"))

    def test_wrong_overlay_approval_does_not_transform_label(self):
        stock = deepcopy(self.stock)
        for row in stock:
            if row.get("kind") == "overlaid":
                row["approval"] = "FAKE"
        svc = CommissionPrintingService(self.design, self.press, stock)
        a26 = next(r for r in svc.warehouse_report()["batches"] if (r["run"], r["site"]) == ("I-26", "A"))
        self.assertEqual(a26["valid_overlaid_qty"], 0)
        self.assertEqual(a26["available_qty"], 0)
        self.assertEqual(a26["invalid_overlay_qty"], 23)

    def test_unknown_revision_is_not_payable_or_usable_as_known_proof(self):
        press = deepcopy(self.press)
        press["revision"] = 2
        svc = CommissionPrintingService(self.design, press, self.stock)
        with self.assertRaises(Exception):
            svc.purchasing_report()
        fin = svc.finance_report()
        self.assertEqual(fin["payable_total"], Decimal("0.00"))
        self.assertTrue(any(e["type"] == "unknown_plate_revision" for e in svc.errors))

    def test_permit_expiry_blocks_order(self):
        svc = load_service(INPUTS)
        expired = svc.order_availability(
            {"id": "OLD", "market": "N", "sku": "P1", "delivery_date": "2026-12-21", "quantity": 1}
        )
        # PLANT valid_to 2026-12-20; no valid PLANT permit for delivery on 12-21.
        self.assertEqual(expired["currently_available_qty"], 0)


if __name__ == "__main__":
    unittest.main()

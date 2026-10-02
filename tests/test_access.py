import unittest
from datetime import timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from referral_packet import (
    AccessDenied,
    Receipt,
    ReceiptLedger,
    check_read_timeouts,
    open_item,
    seal_packet,
)

ROLE_KEYS = {
    "receiving_physician": "key-doc",
    "receiving_nurse": "key-nurse",
    "medical_records_clerk": "key-clerk",
}
ITEMS = (
    ("vs-001", "vital_signs", b"HR 110, BP 90/60"),
    ("ecg-001", "ecg", b"ecg-bytes"),
    ("att-001", "attachment", b"scan-bytes"),
)


def make_packet():
    return seal_packet(
        "ref-1",
        1,
        "transfer_receiving",
        ITEMS,
        role_keys=ROLE_KEYS,
        nonce="n-1",
    )


class RoleDecryptionTest(unittest.TestCase):
    def test_physician_reads_clinical_items(self):
        packet = make_packet()
        self.assertEqual(
            open_item(packet, "vs-001", role="receiving_physician", role_keys=ROLE_KEYS),
            b"HR 110, BP 90/60",
        )

    def test_nurse_cannot_read_ecg(self):
        packet = make_packet()
        with self.assertRaises(AccessDenied):
            open_item(packet, "ecg-001", role="receiving_nurse", role_keys=ROLE_KEYS)

    def test_clerk_reads_attachment_but_not_vitals(self):
        packet = make_packet()
        self.assertEqual(
            open_item(packet, "att-001", role="medical_records_clerk", role_keys=ROLE_KEYS),
            b"scan-bytes",
        )
        with self.assertRaises(AccessDenied):
            open_item(packet, "vs-001", role="medical_records_clerk", role_keys=ROLE_KEYS)

    def test_qc_role_reads_no_clinical_content(self):
        packet = make_packet()
        with self.assertRaises(AccessDenied):
            open_item(packet, "vs-001", role="qc_reviewer", role_keys=ROLE_KEYS)


class ReceiptAndEscalationTest(unittest.TestCase):
    def test_receipt_records_seen_version(self):
        ledger = ReceiptLedger()
        ledger.record(
            Receipt(
                referral_id="ref-1",
                version=2,
                user="dr-王",
                role="receiving_physician",
                purpose="transfer_receiving",
                item_ids=("vs-001",),
                at="2026-09-20T09:30:00+08:00",
            )
        )
        self.assertTrue(ledger.has_read("ref-1", 2))
        self.assertFalse(ledger.has_read("ref-1", 1))

    def test_timeout_escalates_without_expanding_scope(self):
        receipts = ReceiptLedger()
        pending = (
            ("ref-1", 1, "2026-09-20T09:00:00+08:00"),
            ("ref-2", 1, "2026-09-20T09:00:00+08:00"),
        )
        receipts.record(
            Receipt(
                referral_id="ref-2",
                version=1,
                user="dr-李",
                role="receiving_physician",
                purpose="transfer_receiving",
                item_ids=("vs-001",),
                at="2026-09-20T09:20:00+08:00",
            )
        )
        escalations = check_read_timeouts(
            pending,
            receipts,
            now="2026-09-20T10:00:00+08:00",
            timeout=timedelta(minutes=30),
        )
        self.assertEqual(len(escalations), 1)
        self.assertEqual(escalations[0].referral_id, "ref-1")
        self.assertFalse(escalations[0].scope_expanded)  # 升级不扩大资料范围


if __name__ == "__main__":
    unittest.main()

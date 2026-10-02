import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from referral_packet import ItemDeclaration, LedgerError, ManifestLedger

DECL = ItemDeclaration(
    item_id="vs-001",
    category="vital_signs",
    source="卫生院A",
    collected_at="2026-09-20T08:40:00+08:00",
    completeness="complete",
    consent=True,
    content_hash="h1",
)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.ledger = ManifestLedger()
        self.v1 = self.ledger.append_version(
            "ref-1",
            reason="initial",
            created_at="2026-09-20T09:00:00+08:00",
            item_ids=("vs-001", "att-001"),
            declarations=(DECL,),
        )

    def test_all_change_kinds_append_new_version(self):
        for reason in ("correction", "withdrawal", "remeasure", "escalation"):
            v = self.ledger.append_version(
                "ref-1",
                reason=reason,
                created_at="2026-09-20T10:00:00+08:00",
                item_ids=("vs-001",),
                declarations=(DECL,),
            )
        self.assertEqual(v.version, 5)
        self.assertEqual(self.ledger.latest("ref-1").version, 5)

    def test_unknown_reason_rejected(self):
        with self.assertRaises(LedgerError):
            self.ledger.append_version(
                "ref-1",
                reason="delete",
                created_at="2026-09-20T10:00:00+08:00",
                item_ids=(),
                declarations=(),
            )

    def test_signed_version_stays_auditable_after_supersede(self):
        self.ledger.sign_off("ref-1", 1, by="县医院接诊医生", at="2026-09-20T09:30:00+08:00")
        self.ledger.append_version(
            "ref-1",
            reason="withdrawal",
            created_at="2026-09-20T10:00:00+08:00",
            item_ids=("vs-001",),  # 撤回非必要附件 att-001
            declarations=(DECL,),
        )
        old = self.ledger.get("ref-1", 1)
        self.assertIn("att-001", old.item_ids)  # 旧版本内容未被改写
        self.assertTrue(self.ledger.is_signed("ref-1", 1))
        self.assertEqual(
            self.ledger.sign_offs("ref-1", 1)[0]["by"], "县医院接诊医生"
        )

    def test_versions_are_immutable_snapshots(self):
        self.ledger.append_version(
            "ref-1",
            reason="correction",
            created_at="2026-09-20T10:00:00+08:00",
            item_ids=("vs-001",),
            declarations=(DECL,),
        )
        self.assertEqual(self.ledger.get("ref-1", 1).item_ids, ("vs-001", "att-001"))


if __name__ == "__main__":
    unittest.main()

import json
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from referral_packet import (
    ItemDeclaration,
    ManifestLedger,
    build_packet,
    validate,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "referral_journey.sample.json"


def load_sample():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def make_ledger_with_sample():
    sample = load_sample()
    ledger = ManifestLedger()
    decls = tuple(ItemDeclaration(**d) for d in sample["declarations"])
    version = ledger.append_version(
        sample["referral_id"],
        reason="initial",
        created_at="2026-09-20T09:00:00+08:00",
        item_ids=tuple(d.item_id for d in decls),
        declarations=decls,
    )
    return sample, ledger, version


class BuildPacketTest(unittest.TestCase):
    def test_packet_is_purpose_limited(self):
        sample, _, version = make_ledger_with_sample()
        packet = build_packet(
            version,
            disease_code=sample["disease_code"],
            risk_level=sample["risk_level"],
            purpose="emergency_escalation",  # 该用途不允许 lab_results/attachment
            packet_id="pk-1",
        )
        self.assertEqual(set(packet.item_ids), {"vs-001", "ecg-001", "med-001"})

    def test_unknown_purpose_allows_nothing(self):
        sample, _, version = make_ledger_with_sample()
        packet = build_packet(
            version,
            disease_code=sample["disease_code"],
            risk_level=sample["risk_level"],
            purpose="marketing",
            packet_id="pk-2",
        )
        self.assertEqual(packet.item_ids, ())

    def test_unconsented_item_excluded(self):
        sample, _, version = make_ledger_with_sample()
        decls = tuple(
            ItemDeclaration(**{**d, "consent": False}) if d["item_id"] == "ecg-001"
            else ItemDeclaration(**d)
            for d in sample["declarations"]
        )
        ledger = ManifestLedger()
        v = ledger.append_version(
            sample["referral_id"],
            reason="initial",
            created_at="2026-09-20T09:00:00+08:00",
            item_ids=tuple(d.item_id for d in decls),
            declarations=decls,
        )
        packet = build_packet(
            v,
            disease_code=sample["disease_code"],
            risk_level=sample["risk_level"],
            purpose="transfer_receiving",
            packet_id="pk-3",
        )
        self.assertNotIn("ecg-001", packet.item_ids)
        self.assertTrue(any(f.kind == "consent_absent" for f in packet.findings))


class ValidateTest(unittest.TestCase):
    def test_missing_required_category_flagged_with_stage(self):
        sample, _, version = make_ledger_with_sample()
        findings = validate(
            version,
            disease_code=sample["disease_code"],
            risk_level=sample["risk_level"],
            purpose="transfer_receiving",
        )
        missing = [f for f in findings if f.kind == "missing"]
        # 样例缺 lab_results（chest_pain/high 必需）
        self.assertTrue(any(f.item_id == "lab_results" for f in missing))
        self.assertTrue(all(f.stage == "declare" for f in missing))

    def test_conflicting_declarations_flagged(self):
        sample = load_sample()
        decls = [ItemDeclaration(**d) for d in sample["declarations"]]
        dup = ItemDeclaration(
            **{**sample["declarations"][0], "content_hash": "h-different"}
        )
        ledger = ManifestLedger()
        v1 = ledger.append_version(
            sample["referral_id"],
            reason="initial",
            created_at="2026-09-20T09:00:00+08:00",
            item_ids=tuple(d.item_id for d in decls),
            declarations=tuple(decls),
        )
        v2 = ledger.append_version(
            sample["referral_id"],
            reason="correction",
            created_at="2026-09-20T10:00:00+08:00",
            item_ids=tuple(d.item_id for d in decls),
            declarations=tuple(decls) + (dup,),
        )
        findings = validate(
            v2,
            disease_code=sample["disease_code"],
            risk_level=sample["risk_level"],
            purpose="transfer_receiving",
        )
        self.assertTrue(
            any(f.kind == "conflict" and f.item_id == "vs-001" for f in findings)
        )

    def test_collected_after_version_flagged(self):
        sample = load_sample()
        late = dict(sample["declarations"][0])
        late["collected_at"] = "2026-09-21T08:00:00+08:00"
        decls = [ItemDeclaration(**late)] + [
            ItemDeclaration(**d) for d in sample["declarations"][1:]
        ]
        ledger = ManifestLedger()
        v = ledger.append_version(
            sample["referral_id"],
            reason="initial",
            created_at="2026-09-20T09:00:00+08:00",
            item_ids=tuple(d.item_id for d in decls),
            declarations=tuple(decls),
        )
        findings = validate(
            v,
            disease_code=sample["disease_code"],
            risk_level=sample["risk_level"],
            purpose="transfer_receiving",
        )
        self.assertTrue(
            any(f.kind == "conflict" and f.item_id == "vs-001" for f in findings)
        )


if __name__ == "__main__":
    unittest.main()

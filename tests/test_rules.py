"""规则目录与清单提示：系统只提示缺失/冲突，不做病情裁决。"""

import unittest

from referral_packet.catalog import (
    Duty,
    ItemCode,
    Purpose,
    RiskLevel,
    required_items,
    duty_visible_codes,
)
from referral_packet.manifest import (
    ChangeReason,
    Completeness,
    FindingKind,
    ItemDeclaration,
    build_version,
    evaluate,
    GENESIS_HASH,
)
from tests._helpers import decl


class CatalogTest(unittest.TestCase):
    def test_chest_pain_routine_requires_core_items(self):
        req = required_items("chest_pain", RiskLevel.ROUTINE)
        self.assertIn(ItemCode.VITALS, req.item_codes)
        self.assertIn(ItemCode.ECG, req.item_codes)
        self.assertIn(ItemCode.LAB, req.item_codes)
        self.assertIn(ItemCode.PREAUTH, req.item_codes)
        self.assertNotIn(ItemCode.INFUSION, req.item_codes)

    def test_urgent_adds_infusion_without_judging_values(self):
        routine = required_items("chest_pain", RiskLevel.ROUTINE)
        urgent = required_items("chest_pain", RiskLevel.URGENT)
        self.assertEqual(urgent.item_codes, routine.item_codes | {ItemCode.INFUSION})

    def test_unknown_condition_falls_back_to_general(self):
        req = required_items("mystery_dx", RiskLevel.ROUTINE)
        self.assertEqual(req.condition, "general_transfer")

    def test_purpose_scope_smaller_than_emergency(self):
        triage = duty_visible_codes(Duty.TRIAGE_DESK, Purpose.TRIAGE)
        self.assertNotIn(ItemCode.LAB, triage)
        self.assertNotIn(ItemCode.MEDICATION, triage)
        emergency = duty_visible_codes(Duty.EMERGENCY, Purpose.EMERGENCY_RX)
        self.assertIn(ItemCode.MEDICATION, emergency)

    def test_qc_and_patient_have_no_item_scope(self):
        self.assertEqual(duty_visible_codes(Duty.QC_OFFICER, Purpose.QC), frozenset())
        self.assertEqual(duty_visible_codes(Duty.PATIENT, Purpose.PATIENT_VIEW), frozenset())


class FindingTest(unittest.TestCase):
    def _req(self, risk=RiskLevel.URGENT):
        return required_items("chest_pain", risk)

    def test_missing_is_flagged_not_raised(self):
        # 只有生命体征与授权：胸痛所需其余条目缺失，但 evaluate 不抛错。
        findings = evaluate(self._req(), [
            decl(ItemCode.VITALS), decl(ItemCode.ALLERGIES), decl(ItemCode.PREAUTH),
        ])
        missing = {f.item for f in findings if f.kind is FindingKind.MISSING}
        self.assertIn(ItemCode.ECG, missing)
        self.assertIn(ItemCode.LAB, missing)

    def test_unavailable_is_a_hint_not_a_blocker(self):
        items = [
            decl(c, completeness=Completeness.UNAVAILABLE)
            for c in self._req().item_codes
        ]
        findings = evaluate(self._req(), items)
        self.assertTrue(any(f.kind is FindingKind.UNAVAILABLE for f in findings))
        # unavailable 是显式声明，不再重复报 missing。
        self.assertFalse(any(f.kind is FindingKind.MISSING for f in findings))

    def test_conflicting_sources_are_flagged_without_resolution(self):
        findings = evaluate(self._req(), [
            decl(ItemCode.VITALS, source="监护仪A", collected_at="2026-09-20T08:40:00+08:00"),
            decl(ItemCode.VITALS, source="监护仪B", collected_at="2026-09-20T09:05:00+08:00"),
        ])
        self.assertTrue(any(f.kind is FindingKind.CONFLICT
                            and f.item is ItemCode.VITALS for f in findings))

    def test_exact_duplicate_is_informational(self):
        findings = evaluate(self._req(), [
            decl(ItemCode.VITALS, source="监护仪A"),
            decl(ItemCode.VITALS, source="监护仪A"),
        ])
        self.assertTrue(any(f.kind is FindingKind.DUPLICATE for f in findings))
        self.assertFalse(any(f.kind is FindingKind.CONFLICT for f in findings))

    def test_partial_and_unauthorized_hints(self):
        findings = evaluate(self._req(), [
            decl(ItemCode.INFUSION, completeness=Completeness.PARTIAL, note="缺剂量页",
                 content_ref="art-x"),
            decl(ItemCode.ECG, authorized=False, content_ref="art-y"),
        ])
        kinds = {f.kind for f in findings}
        self.assertIn(FindingKind.PARTIAL, kinds)
        self.assertIn(FindingKind.UNAUTHORIZED, kinds)

    def test_extra_scope_item_flagged(self):
        req = required_items("general_transfer", RiskLevel.ROUTINE)
        findings = evaluate(req, [
            decl(c) for c in req.item_codes
        ] + [decl(ItemCode.IMAGING)])
        extra = [f for f in findings if f.kind is FindingKind.EXTRA_SCOPE]
        self.assertEqual([f.item for f in extra], [ItemCode.IMAGING])


class VersionHashTest(unittest.TestCase):
    def test_hash_chain_and_determinism(self):
        req = required_items("chest_pain", RiskLevel.URGENT)
        items = tuple(decl(c) for c in req.item_codes)
        v1 = build_version(revision=1, created_at="2026-09-20T09:00:00+08:00",
                           declared_by="d1", facility="f1", condition="chest_pain",
                           risk="urgent", change_reason=ChangeReason.INITIAL,
                           required=req, items=items, parent_hash=GENESIS_HASH)
        v1_again = build_version(revision=1, created_at="2026-09-20T09:00:00+08:00",
                                 declared_by="d1", facility="f1", condition="chest_pain",
                                 risk="urgent", change_reason=ChangeReason.INITIAL,
                                 required=req, items=items, parent_hash=GENESIS_HASH)
        self.assertEqual(v1.version_hash, v1_again.version_hash)
        v2 = build_version(revision=2, created_at="2026-09-20T10:00:00+08:00",
                           declared_by="d1", facility="f1", condition="chest_pain",
                           risk="urgent", change_reason=ChangeReason.CORRECTION,
                           required=req, items=items, parent_hash=v1.version_hash)
        self.assertEqual(v2.parent_hash, v1.version_hash)
        self.assertNotEqual(v2.version_hash, v1.version_hash)

    def test_invalid_time_rejected(self):
        with self.assertRaises(ValueError):
            decl(ItemCode.VITALS, collected_at="not-a-time")


if __name__ == "__main__":
    unittest.main()

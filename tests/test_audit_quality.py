"""患者访问查询与双方质控（交接环节缺项定位、范围隔离）。"""

import unittest

from referral_packet.catalog import (
    Duty, ItemCode, Purpose, RiskLevel,
)
from referral_packet.errors import AccessDenied
from referral_packet.manifest import ChangeReason, Completeness

from tests._helpers import decl, make_service, sha, upload_item


class PatientAuditTest(unittest.TestCase):
    def test_patient_sees_who_why_which_fields(self):
        svc, _ = make_service()
        ref = upload_item(svc, referral_key="K1", patient_id="P-9",
                          code=ItemCode.VITALS, blob=b"v")
        res = svc.push_manifest(
            referral_key="K1", patient_id="P-9", declared_by="d", facility="f",
            condition="general_transfer", risk=RiskLevel.ROUTINE, idempotency_key="1",
            items=[
                decl(ItemCode.VITALS, content_ref=ref,
                     content_sha256=sha(b"v")),
                decl(ItemCode.ALLERGIES), decl(ItemCode.PREAUTH),
                decl(ItemCode.CHIEF_COMPLAINT),
            ],
        )
        d = svc.deliver(referral_id=res.referral_id,
                        purpose=Purpose.TRIAGE, duty=Duty.TRIAGE_DESK)
        svc.sign_receipt(delivery_id=d.delivery_id, actor="nurse-1",
                         duty=Duty.TRIAGE_DESK)

        report = svc.patient_access_report(patient_id="P-9", actor="P-9")
        opens = [e for e in report.events if e["action"] == "open"]
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0]["actor"], "nurse-1")
        self.assertEqual(opens[0]["purpose"], "triage")
        self.assertIn("vitals", opens[0]["field_codes"])
        self.assertIn("vitals", report.fields_seen)

    def test_patient_cannot_see_other_patients_records(self):
        svc, _ = make_service()
        ref = upload_item(svc, referral_key="K2", patient_id="P-A",
                          code=ItemCode.VITALS, blob=b"a")
        res = svc.push_manifest(
            referral_key="K2", patient_id="P-A", declared_by="d", facility="f",
            condition="general_transfer", risk=RiskLevel.ROUTINE, idempotency_key="1",
            items=[decl(ItemCode.VITALS, content_ref=ref, content_sha256=sha(b"a")),
                   decl(ItemCode.ALLERGIES), decl(ItemCode.PREAUTH),
                   decl(ItemCode.CHIEF_COMPLAINT)],
        )
        d = svc.deliver(referral_id=res.referral_id,
                        purpose=Purpose.TRIAGE, duty=Duty.TRIAGE_DESK)
        svc.sign_receipt(delivery_id=d.delivery_id, actor="n", duty=Duty.TRIAGE_DESK)
        other = svc.patient_access_report(patient_id="P-B", actor="P-B")
        self.assertEqual(other.events, ())


class QualityControlTest(unittest.TestCase):
    def _journey_with_gap_then_correction(self, svc):
        # 首版缺 ECG（显式 unavailable）。
        items = [
            decl(ItemCode.VITALS),
            decl(ItemCode.ALLERGIES),
            decl(ItemCode.PREAUTH),
            decl(ItemCode.CHIEF_COMPLAINT),
            decl(ItemCode.MEDICATION),
            decl(ItemCode.ECG, completeness=Completeness.UNAVAILABLE, note="设备故障"),
            decl(ItemCode.LAB, completeness=Completeness.UNAVAILABLE),
        ]
        r1 = svc.push_manifest(
            referral_key="QC-1", patient_id="P-Q", declared_by="doc",
            facility="基层", condition="chest_pain", risk=RiskLevel.URGENT,
            items=items, idempotency_key="v1",
        )
        # 更正版补齐 ECG；LAB 仍无法取得。
        items_v2 = []
        for code in (
            ItemCode.VITALS, ItemCode.ALLERGIES, ItemCode.PREAUTH,
            ItemCode.CHIEF_COMPLAINT, ItemCode.MEDICATION,
            ItemCode.ECG, ItemCode.INFUSION,
        ):
            items_v2.append(decl(code))
        items_v2.append(decl(ItemCode.LAB, completeness=Completeness.UNAVAILABLE))
        svc.push_manifest(
            referral_key="QC-1", patient_id="P-Q", declared_by="doc",
            facility="基层", condition="chest_pain", risk=RiskLevel.URGENT,
            change_reason=ChangeReason.CORRECTION, items=items_v2,
            idempotency_key="v2",
        )
        return r1.referral_id

    def test_gap_located_between_handoff_stages(self):
        svc, _ = make_service()
        ref_id = self._journey_with_gap_then_correction(svc)
        svc.grant_qc_scope("qc-li", [ref_id])
        report = svc.qc_gap_report(referral_id=ref_id, officer="qc-li")

        gap = report.item_gaps["ecg_report"]
        self.assertEqual(gap["missing_from_revision"], "1")
        self.assertEqual(gap["resolved_at_revision"], "2")
        self.assertEqual(gap["resolved_via"], "correction")
        # LAB 到最新版仍缺（仍为 unavailable）。
        self.assertIn("lab_summary", report.still_missing)
        # 时间线包含推送与更正两个环节。
        stages = [e["stage"] for e in report.timeline if e["kind"] == "handoff"]
        self.assertIn("primary_push", stages)
        self.assertIn("correction", stages)

    def test_qc_cannot_browse_unrelated_referrals(self):
        svc, _ = make_service()
        ref_id = self._journey_with_gap_then_correction(svc)
        # 未授权 -> 拒绝并留痕。
        with self.assertRaises(AccessDenied):
            svc.qc_gap_report(referral_id=ref_id, officer="qc-wang")
        denied = [e for e in svc.audit_log.all_events()
                  if e.decision.value == "deny"]
        self.assertTrue(any(e.actor == "qc-wang" for e in denied))

    def test_qc_scope_does_not_leak_across_referrals(self):
        svc, _ = make_service()
        id_a = self._journey_with_gap_then_correction(svc)
        # 另一条无关旅程。
        items = [decl(ItemCode.VITALS), decl(ItemCode.ALLERGIES),
                 decl(ItemCode.PREAUTH), decl(ItemCode.CHIEF_COMPLAINT)]
        r_b = svc.push_manifest(
            referral_key="QC-2", patient_id="P-R", declared_by="d", facility="f",
            condition="general_transfer", risk=RiskLevel.ROUTINE,
            items=items, idempotency_key="b1",
        )
        svc.grant_qc_scope("qc-li", [id_a])
        with self.assertRaises(AccessDenied):
            svc.qc_gap_report(referral_id=r_b.referral_id, officer="qc-li")


if __name__ == "__main__":
    unittest.main()

"""旅程幂等、用途受限资料包、按职责解密、回执与超时升级。"""

import unittest
from datetime import timedelta

from referral_packet.catalog import Duty, ItemCode, Purpose, RiskLevel
from referral_packet.delivery import DeliveryStatus
from referral_packet.errors import AccessDenied, ImmutableViolation, RuleViolation
from referral_packet.manifest import ChangeReason, Completeness

from tests._helpers import decl, make_service, sha, upload_item


def _push_chest_pain(service, *, risk=RiskLevel.URGENT, key="REF-1",
                     patient="P-1", idem="push-1", items=None):
    codes = {
        ItemCode.VITALS, ItemCode.ALLERGIES, ItemCode.PREAUTH,
        ItemCode.CHIEF_COMPLAINT, ItemCode.MEDICATION,
        ItemCode.ECG, ItemCode.LAB, ItemCode.INFUSION,
    }
    if items is None:
        items = []
        for code in codes:
            ref = upload_item(service, referral_key=key, patient_id=patient,
                              code=code, blob=f"content-{code.value}".encode())
            items.append(decl(code, content_ref=ref, content_sha256=sha(f"content-{code.value}".encode())))
    return service.push_manifest(
        referral_key=key, patient_id=patient, declared_by="doc-primary",
        facility="基层卫生院", condition="chest_pain", risk=risk,
        items=items, idempotency_key=idem,
    )


class IdempotencyTest(unittest.TestCase):
    def test_repeated_push_is_one_journey_one_revision(self):
        svc, _ = make_service()
        r1 = _push_chest_pain(svc, idem="same-key")
        r2 = _push_chest_pain(svc, idem="same-key")
        self.assertEqual(r1.referral_id, r2.referral_id)
        self.assertEqual(r1.revision, r2.revision)
        self.assertTrue(r2.deduped)
        # 不带幂等键的「首次推送」第二次必须说明变更原因。
        with self.assertRaises(RuleViolation):
            _push_chest_pain(svc, idem=None)

    def test_referral_key_binds_one_patient(self):
        svc, _ = make_service()
        _push_chest_pain(svc, key="REF-X", patient="P-1", idem="a")
        with self.assertRaises(RuleViolation):
            _push_chest_pain(svc, key="REF-X", patient="P-2", idem="b")


class PacketScopeTest(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make_service()
        self.result = _push_chest_pain(self.svc)
        self.ref_id = self.result.referral_id

    def test_triage_packet_excludes_lab_and_medication(self):
        delivery = self.svc.deliver(referral_id=self.ref_id,
                                    purpose=Purpose.TRIAGE, duty=Duty.TRIAGE_DESK)
        codes = {c.value for c in delivery.scope_codes}
        self.assertIn("vitals", codes)
        self.assertIn("chief_complaint", codes)
        self.assertNotIn("lab_summary", codes)
        self.assertNotIn("current_medication", codes)
        self.assertEqual(delivery.excluded.get("lab_summary"),
                         "outside_purpose_or_duty_scope")

    def test_emergency_duty_gets_full_emergency_scope(self):
        delivery = self.svc.deliver(referral_id=self.ref_id,
                                    purpose=Purpose.EMERGENCY_RX, duty=Duty.EMERGENCY)
        codes = set(delivery.scope_codes)
        self.assertIn(ItemCode.LAB, codes)
        self.assertIn(ItemCode.MEDICATION, codes)

    def test_duty_cannot_serve_wrong_purpose(self):
        with self.assertRaises(AccessDenied):
            self.svc.deliver(referral_id=self.ref_id,
                             purpose=Purpose.INPATIENT, duty=Duty.TRIAGE_DESK)

    def test_unauthorized_item_never_leaves(self):
        svc, _ = make_service()
        ref = upload_item(svc, referral_key="K", patient_id="P",
                          code=ItemCode.VITALS, blob=b"vitals-secret")
        items = [
            decl(ItemCode.VITALS, content_ref=ref, authorized=False),
            decl(ItemCode.ALLERGIES),
            decl(ItemCode.PREAUTH),
            decl(ItemCode.CHIEF_COMPLAINT),
        ]
        res = svc.push_manifest(
            referral_key="K", patient_id="P", declared_by="d", facility="f",
            condition="general_transfer", risk=RiskLevel.ROUTINE,
            items=items, idempotency_key="k1",
        )
        delivery = svc.deliver(referral_id=res.referral_id,
                               purpose=Purpose.TRIAGE, duty=Duty.TRIAGE_DESK)
        self.assertNotIn(ItemCode.VITALS, delivery.scope_codes)
        self.assertEqual(delivery.excluded["vitals"], "unauthorized")


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        result = _push_chest_pain(self.svc)
        self.ref_id = result.referral_id
        self.version_hash = result.version_hash

    def test_decrypt_by_duty_and_receipt_pins_version(self):
        delivery = self.svc.deliver(referral_id=self.ref_id,
                                    purpose=Purpose.TRIAGE, duty=Duty.TRIAGE_DESK)
        receipt, plaintexts = self.svc.sign_receipt(
            delivery_id=delivery.delivery_id, actor="nurse-7", duty=Duty.TRIAGE_DESK)
        self.assertEqual(receipt.status, DeliveryStatus.READ)
        self.assertEqual(receipt.seen_version_hash, self.version_hash)
        self.assertEqual(plaintexts[ItemCode.VITALS], b"content-vitals")

    def test_other_duty_cannot_open(self):
        delivery = self.svc.deliver(referral_id=self.ref_id,
                                    purpose=Purpose.TRIAGE, duty=Duty.TRIAGE_DESK)
        with self.assertRaises(AccessDenied):
            self.svc.sign_receipt(delivery_id=delivery.delivery_id,
                                  actor="x", duty=Duty.EMERGENCY)

    def test_signed_receipt_is_immutable(self):
        delivery = self.svc.deliver(referral_id=self.ref_id,
                                    purpose=Purpose.TRIAGE, duty=Duty.TRIAGE_DESK)
        self.svc.sign_receipt(delivery_id=delivery.delivery_id,
                              actor="n", duty=Duty.TRIAGE_DESK)
        with self.assertRaises(ImmutableViolation):
            self.svc.sign_receipt(delivery_id=delivery.delivery_id,
                                  actor="n2", duty=Duty.TRIAGE_DESK)

    def test_signed_old_revision_stays_auditable(self):
        delivery = self.svc.deliver(referral_id=self.ref_id,
                                    purpose=Purpose.TRIAGE, duty=Duty.TRIAGE_DESK)
        self.svc.sign_receipt(delivery_id=delivery.delivery_id,
                              actor="n", duty=Duty.TRIAGE_DESK)
        # 新版本推送后，revision 1 的审计投影仍可取。
        self.svc.push_manifest(
            referral_key="REF-1", patient_id="P-1", declared_by="doc-primary",
            facility="基层卫生院", condition="chest_pain", risk=RiskLevel.URGENT,
            change_reason=ChangeReason.CORRECTION,
            items=[decl(c) for c in delivery.scope_codes],
            idempotency_key="push-2",
        )
        view = self.svc.view_signed_revision(
            referral_id=self.ref_id, revision=1, actor="auditor",
            duty=Duty.QC_OFFICER)
        self.assertEqual(view["version_hash"], self.version_hash)
        self.assertEqual(view["revision"], 1)

    def test_unsigned_revision_audit_view_denied(self):
        from referral_packet.errors import ImmutableViolation as IV
        with self.assertRaises(IV):
            self.svc.view_signed_revision(
                referral_id=self.ref_id, revision=1, actor="a",
                duty=Duty.QC_OFFICER)


class SupersedeAndTimeoutTest(unittest.TestCase):
    def test_new_version_supersedes_unread_only(self):
        svc, _ = make_service()
        result = _push_chest_pain(svc)
        d1 = svc.deliver(referral_id=result.referral_id,
                         purpose=Purpose.TRIAGE, duty=Duty.TRIAGE_DESK)
        svc.sign_receipt(delivery_id=d1.delivery_id, actor="n", duty=Duty.TRIAGE_DESK)

        d2 = svc.deliver(referral_id=result.referral_id,
                         purpose=Purpose.EMERGENCY_RX, duty=Duty.EMERGENCY)
        # 更正版本：未读的 d2 作废，已读的 d1 保持 read。
        push2 = svc.push_manifest(
            referral_key="REF-1", patient_id="P-1", declared_by="doc",
            facility="f", condition="chest_pain", risk=RiskLevel.URGENT,
            change_reason=ChangeReason.CORRECTION,
            items=[decl(c) for c in (
                ItemCode.VITALS, ItemCode.ALLERGIES, ItemCode.PREAUTH,
                ItemCode.CHIEF_COMPLAINT, ItemCode.MEDICATION,
                ItemCode.ECG, ItemCode.LAB, ItemCode.INFUSION)],
            idempotency_key="push-2",
            deliver_to=((Duty.EMERGENCY, Purpose.EMERGENCY_RX),
                        (Duty.TRIAGE_DESK, Purpose.TRIAGE)),
        )
        self.assertIn(d2.delivery_id, push2.superseded_deliveries)
        self.assertEqual(svc.delivery(d1.delivery_id).status, DeliveryStatus.READ)
        self.assertEqual(svc.delivery(d2.delivery_id).status, DeliveryStatus.SUPERSEDED)
        with self.assertRaises(AccessDenied):
            svc.sign_receipt(delivery_id=d2.delivery_id, actor="e", duty=Duty.EMERGENCY)

    def test_delivering_new_revision_supersedes_stale_unread(self):
        # 先推送 v2、再发起交付时，v1 的未读交付仍必须自动作废，
        # 否则接收方可能签收旧版（正是「看到的是哪一版」无确认的风险）。
        svc, _ = make_service()
        result = _push_chest_pain(svc, idem="p1")
        stale = svc.deliver(referral_id=result.referral_id,
                            purpose=Purpose.TRIAGE, duty=Duty.TRIAGE_DESK)
        svc.push_manifest(
            referral_key="REF-1", patient_id="P-1", declared_by="doc",
            facility="f", condition="chest_pain", risk=RiskLevel.URGENT,
            change_reason=ChangeReason.CORRECTION,
            items=[decl(c) for c in (
                ItemCode.VITALS, ItemCode.ALLERGIES, ItemCode.PREAUTH,
                ItemCode.CHIEF_COMPLAINT, ItemCode.MEDICATION,
                ItemCode.ECG, ItemCode.LAB, ItemCode.INFUSION)],
            idempotency_key="p2",
        )
        fresh = svc.deliver(referral_id=result.referral_id,
                            purpose=Purpose.TRIAGE, duty=Duty.TRIAGE_DESK)
        self.assertEqual(fresh.revision, 2)
        self.assertEqual(svc.delivery(stale.delivery_id).status,
                         DeliveryStatus.SUPERSEDED)
        with self.assertRaises(AccessDenied):
            svc.sign_receipt(delivery_id=stale.delivery_id, actor="n",
                             duty=Duty.TRIAGE_DESK)

    def test_timeout_escalation_keeps_scope(self):
        svc, clock = make_service()
        result = _push_chest_pain(svc)
        d1 = svc.deliver(referral_id=result.referral_id,
                         purpose=Purpose.TRIAGE, duty=Duty.TRIAGE_DESK)
        original_scope = set(d1.scope_codes)
        self.assertEqual(d1.status, DeliveryStatus.DELIVERED)

        clock.advance(timedelta(minutes=16))
        followups = svc.sweep_timeouts(result.referral_id)
        self.assertEqual(len(followups), 1)
        followup = followups[0]
        # 备份职责（急诊科）收到同用途、不扩大的范围。
        self.assertEqual(followup.duty, Duty.EMERGENCY)
        self.assertEqual(followup.purpose, Purpose.TRIAGE)
        self.assertTrue(set(followup.scope_codes) <= original_scope)
        self.assertEqual(svc.delivery(d1.delivery_id).status, DeliveryStatus.TIMED_OUT)

        # 即使清单后来多出条目，催办交付仍受 scope_ceiling 限制。
        receipt, plaintexts = svc.sign_receipt(
            delivery_id=followup.delivery_id, actor="er-doc", duty=Duty.EMERGENCY)
        self.assertEqual(set(plaintexts), original_scope)

    def test_escalation_does_not_automatically_widen_delivery(self):
        svc, clock = make_service()
        result = _push_chest_pain(svc, risk=RiskLevel.URGENT)
        d1 = svc.deliver(referral_id=result.referral_id,
                         purpose=Purpose.TRIAGE, duty=Duty.TRIAGE_DESK)
        triage_scope = set(d1.scope_codes)
        # 临床紧急升级（emergency 新版本）。
        svc.escalate(referral_id=result.referral_id, declared_by="doc-primary",
                     items=[decl(c) for c in triage_scope],
                     idempotency_key="esc-1")
        # 超时催办仍按原交付范围，不因为 emergency 规则而扩大。
        clock.advance(timedelta(minutes=16))
        followups = svc.sweep_timeouts(result.referral_id)
        self.assertEqual(len(followups), 1)
        self.assertTrue(set(followups[0].scope_codes) <= triage_scope)


if __name__ == "__main__":
    unittest.main()

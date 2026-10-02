"""端到端场景：对照 fixtures/packet_manifest.json 描述的演进。

首版胸痛（urgent）：检验 unavailable、处置 partial；分诊台签收 revision 1；
更正 revision 2（补齐检验结论）；撤回附件 revision 3；到院复测 revision 4；
紧急升级 revision 5。校验：已签收版本冻结可审计、撤回墓碑、升级不扩大范围、
患者审计与质控定位一致。
"""

import json
import unittest
from datetime import timedelta
from pathlib import Path

from referral_packet.catalog import Duty, ItemCode, Purpose, RiskLevel
from referral_packet.delivery import DeliveryStatus
from referral_packet.manifest import ChangeReason, Completeness

from tests._helpers import decl, make_service, sha, upload_item


class EndToEndScenarioTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        self.key = "REF-20260920-019"
        self.patient = "PSEUDO-7A21"
        fixture = json.loads(
            (Path(__file__).parents[1] / "fixtures" / "packet_manifest.json")
            .read_text(encoding="utf-8")
        )
        self.fixture = fixture

    def _artifact(self, code: ItemCode, text: str) -> tuple[str, str]:
        blob = text.encode("utf-8")
        ref = upload_item(self.svc, referral_key=self.key,
                          patient_id=self.patient, code=code, blob=blob,
                          chunk_size=40, fail_chunk=1)
        return ref, sha(blob)

    def _push_v1(self):
        items = []
        for code, text in [
            (ItemCode.VITALS, "T36.8 P92 R20 BP118/76 SpO2 96%"),
            (ItemCode.ALLERGIES, "否认药物及食物过敏"),
            (ItemCode.CHIEF_COMPLAINT, "胸痛 2 小时"),
            (ItemCode.MEDICATION, "阿司匹林既往口服"),
            (ItemCode.ECG, "窦性心律，II/III/aVF ST 段轻度抬高"),
        ]:
            ref, digest = self._artifact(code, text)
            items.append(decl(code, content_ref=ref, content_sha256=digest))
        items.append(decl(ItemCode.LAB, completeness=Completeness.UNAVAILABLE,
                          note="肌钙蛋白外送未回"))
        ref, digest = self._artifact(ItemCode.INFUSION, "硝酸甘油静滴（剂量页缺）")
        items.append(decl(ItemCode.INFUSION, content_ref=ref,
                          content_sha256=digest, completeness=Completeness.PARTIAL,
                          note="剂量页缺页"))
        ref, digest = self._artifact(ItemCode.PREAUTH, "电子授权书 AUTH-019")
        items.append(decl(ItemCode.PREAUTH, content_ref=ref, content_sha256=digest))
        return self.svc.push_manifest(
            referral_key=self.key, patient_id=self.patient,
            declared_by="doctor-prim-04", facility="某基层卫生院（脱敏）",
            condition="chest_pain", risk=RiskLevel.URGENT,
            items=items, idempotency_key="v1",
        )

    def test_full_scenario(self):
        r1 = self._push_v1()
        # 首版应提示 lab 无法取得、infusion 不完整。
        kinds = {(f["item"], f["kind"]) for f in r1.findings}
        self.assertIn(("lab_summary", "unavailable"), kinds)
        self.assertIn(("infusion_record", "partial"), kinds)

        # 分诊台只收生命体征/主诉/过敏/授权/氧疗（无检验、无用药）。
        d_triage = self.svc.deliver(
            referral_id=r1.referral_id, purpose=Purpose.TRIAGE,
            duty=Duty.TRIAGE_DESK)
        self.assertNotIn(ItemCode.LAB, d_triage.scope_codes)
        receipt, plaintexts = self.svc.sign_receipt(
            delivery_id=d_triage.delivery_id, actor="triage-nurse",
            duty=Duty.TRIAGE_DESK)
        self.assertEqual(receipt.seen_version_hash, r1.version_hash)
        self.assertIn(b"BP118/76", plaintexts[ItemCode.VITALS])

        # ---- revision 2：更正检验结论。
        items2 = []
        for code, text in [
            (ItemCode.VITALS, "T36.8 P92 R20 BP118/76 SpO2 96%"),
            (ItemCode.ALLERGIES, "否认药物及食物过敏"),
            (ItemCode.CHIEF_COMPLAINT, "胸痛 2 小时"),
            (ItemCode.MEDICATION, "阿司匹林既往口服"),
            (ItemCode.ECG, "窦性心律，II/III/aVF ST 段轻度抬高"),
            (ItemCode.LAB, "肌钙蛋白 I 0.09 ng/mL（轻度升高）"),
            (ItemCode.INFUSION, "硝酸甘油静滴 10 ug/min"),
            (ItemCode.PREAUTH, "电子授权书 AUTH-019"),
        ]:
            ref, digest = self._artifact(code, text)
            items2.append(decl(code, content_ref=ref, content_sha256=digest))
        r2 = self.svc.push_manifest(
            referral_key=self.key, patient_id=self.patient,
            declared_by="doctor-prim-04", facility="某基层卫生院（脱敏）",
            condition="chest_pain", risk=RiskLevel.URGENT,
            change_reason=ChangeReason.CORRECTION, items=items2,
            idempotency_key="v2")
        self.assertEqual(r2.revision, 2)

        # revision 1 已签收版本继续可审计，哈希不变。
        view1 = self.svc.view_signed_revision(
            referral_id=r1.referral_id, revision=1, actor="auditor",
            duty=Duty.QC_OFFICER)
        self.assertEqual(view1["version_hash"], r1.version_hash)

        # ---- revision 3：撤回一张非必要照片（墓碑）。
        items3 = list(items2)
        items3.append(decl(ItemCode.IMAGING, withdrawn=True,
                           source="患者自行拍摄的手腕照片",
                           note="撤回与本次用途无关附件"))
        r3 = self.svc.push_manifest(
            referral_key=self.key, patient_id=self.patient,
            declared_by="doctor-prim-04", facility="某基层卫生院（脱敏）",
            condition="chest_pain", risk=RiskLevel.URGENT,
            change_reason=ChangeReason.WITHDRAWAL, items=items3,
            idempotency_key="v3")
        self.assertEqual(r3.revision, 3)
        # 撤回墓碑使该条目不在版本有效集合中。
        v3 = self.svc._journeys.get_by_id(r1.referral_id).revision(3)
        self.assertNotIn(ItemCode.IMAGING, v3.effective_codes())
        tombstone = next(i for i in v3.items if i.code is ItemCode.IMAGING)
        self.assertTrue(tombstone.withdrawn)
        # 撤回条目不进入任何资料包。
        d_em = self.svc.deliver(
            referral_id=r1.referral_id, purpose=Purpose.EMERGENCY_RX,
            duty=Duty.EMERGENCY)
        self.assertNotIn(ItemCode.IMAGING, d_em.scope_codes)

        # ---- revision 4：到院复测生命体征（来源为县医院急诊）。
        items4 = list(items2)
        items4 = [i for i in items4 if i.code is not ItemCode.VITALS]
        ref, digest = self._artifact(ItemCode.VITALS, "复测 T36.9 P110 BP126/80")
        items4.append(decl(ItemCode.VITALS, source="县医院急诊监护床 2026-09-20 09:31",
                           collected_at="2026-09-20T09:31:00+08:00",
                           content_ref=ref, content_sha256=digest))
        r4 = self.svc.push_manifest(
            referral_key=self.key, patient_id=self.patient,
            declared_by="er-doctor-2", facility="县医院急诊科",
            condition="chest_pain", risk=RiskLevel.URGENT,
            change_reason=ChangeReason.RECHECK, items=items4,
            idempotency_key="v4")
        self.assertEqual(r4.revision, 4)

        # ---- revision 5：紧急升级；升级本身不自动扩大已交付范围。
        r5 = self.svc.escalate(
            referral_id=r1.referral_id, declared_by="er-doctor-2",
            items=items4, idempotency_key="v5")
        # emergency 规则新增氧疗要求，清单没有 -> 只提示缺失，不拦截。
        self.assertIn(("oxygen_therapy", "missing"),
                      {(f["item"], f["kind"]) for f in r5.findings})
        self.clock.advance(timedelta(minutes=16))
        # d_em 是 emergency_rx 用途：没有任何其他职责能承接，只能记超时事件，
        # 不产生放宽职责边界的派生交付。
        followups = self.svc.sweep_timeouts(r1.referral_id)
        self.assertEqual(followups, [])
        self.assertEqual(self.svc.delivery(d_em.delivery_id).status,
                         DeliveryStatus.TIMED_OUT)

        # 幂等：再次提交 v5 不产生 revision 6。
        again = self.svc.escalate(
            referral_id=r1.referral_id, declared_by="er-doctor-2",
            items=items4, idempotency_key="v5")
        self.assertTrue(again.deduped)
        self.assertEqual(again.revision, 5)

        # 患者访问报告：能看到分诊台与急诊的解密访问及字段。
        report = self.svc.patient_access_report(
            patient_id=self.patient, actor=self.patient)
        actors = {e["actor"] for e in report.events if e["action"] == "open"}
        self.assertIn("triage-nurse", actors)
        self.assertIn("vitals", report.fields_seen)

        # 质控：检验缺项首现于基层推送 revision 1，在更正 revision 2 补齐。
        self.svc.grant_qc_scope("qc-zhang", [r1.referral_id])
        gaps = self.svc.qc_gap_report(
            referral_id=r1.referral_id, officer="qc-zhang")
        self.assertEqual(gaps.item_gaps["lab_summary"]["missing_from_revision"], "1")
        self.assertEqual(gaps.item_gaps["lab_summary"]["resolved_at_revision"], "2")
        self.assertEqual(gaps.item_gaps["lab_summary"]["resolved_via"], "correction")

        # 样例文件与场景的业务键一致（文档与代码同源）。
        self.assertEqual(
            self.fixture["scenario"]["referral_key"], self.key)
        self.assertEqual(
            [x["revision"] for x in self.fixture["scenario"]["later_revisions"]],
            [2, 3, 4])

        # 旅程只有一条。
        self.assertEqual(
            len(self.svc._journeys.all_for_patient(self.patient)), 1)
        # 首版交付始终 read，未被破坏。
        self.assertEqual(
            self.svc.delivery(d_triage.delivery_id).status, DeliveryStatus.READ)


if __name__ == "__main__":
    unittest.main()

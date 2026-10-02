import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from referral_packet import (
    AccessEvent,
    AuditDenied,
    AuditLog,
    Finding,
)


def make_log() -> AuditLog:
    log = AuditLog()
    log.register_referral("ref-1", parties=("乡镇卫生院A", "县人民医院B"))
    log.register_referral("ref-2", parties=("乡镇卫生院C", "县人民医院B"))
    log.record_access(
        AccessEvent(
            referral_id="ref-1",
            patient_id="pat-1",
            actor="dr-王",
            role="receiving_physician",
            purpose="transfer_receiving",
            fields=("vital_signs", "ecg"),
            version=1,
            at="2026-09-20T09:30:00+08:00",
        )
    )
    log.record_access(
        AccessEvent(
            referral_id="ref-2",
            patient_id="pat-2",
            actor="dr-李",
            role="receiving_physician",
            purpose="transfer_receiving",
            fields=("imaging",),
            version=1,
            at="2026-09-20T10:00:00+08:00",
        )
    )
    log.record_findings(
        "ref-1",
        version=1,
        findings=(
            Finding("missing", "declare", "lab_results", "必需类目 lab_results 无完整声明"),
            Finding("conflict", "package", "att-009", "类目 attachment 超出用途允许范围，未入包"),
        ),
    )
    return log


class PatientViewTest(unittest.TestCase):
    def test_patient_sees_who_why_which_fields(self):
        log = make_log()
        events = log.patient_view("pat-1")
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e.actor, "dr-王")
        self.assertEqual(e.purpose, "transfer_receiving")
        self.assertEqual(e.fields, ("vital_signs", "ecg"))

    def test_patient_view_is_scoped_to_self(self):
        log = make_log()
        self.assertEqual(log.patient_view("pat-3"), ())


class QcTraceTest(unittest.TestCase):
    def test_qc_locates_gap_stage(self):
        log = make_log()
        gaps = log.qc_trace("ref-1", requester_org="乡镇卫生院A", requester_role="qc_reviewer")
        stages = {g.stage for g in gaps}
        self.assertEqual(stages, {"declare", "package"})
        self.assertTrue(any(g.item_id == "lab_results" for g in gaps))

    def test_both_party_orgs_can_trace(self):
        log = make_log()
        gaps = log.qc_trace("ref-1", requester_org="县人民医院B", requester_role="qc_reviewer")
        self.assertEqual(len(gaps), 2)

    def test_unrelated_org_cannot_trace(self):
        log = make_log()
        with self.assertRaises(AuditDenied):
            log.qc_trace("ref-1", requester_org="乡镇卫生院C", requester_role="qc_reviewer")

    def test_non_qc_role_cannot_trace(self):
        log = make_log()
        with self.assertRaises(AuditDenied):
            log.qc_trace("ref-1", requester_org="乡镇卫生院A", requester_role="receiving_physician")

    def test_no_browse_all_api(self):
        # 合同层面：AuditLog 不提供列举全部转诊的方法
        self.assertFalse(hasattr(AuditLog, "list_referrals"))
        self.assertFalse(hasattr(AuditLog, "all_events"))


if __name__ == "__main__":
    unittest.main()

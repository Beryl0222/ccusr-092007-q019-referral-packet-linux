"""用例门面：转诊资料最小交付服务的唯一编排入口。

方法对应业务动作，不做任何临床解读；所有判断都来自
:mod:`referral_packet.catalog` 的制度规则表与医生的逐项声明。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from referral_packet.audit import AccessEvent, Action, AuditLog, Decision
from referral_packet.catalog import (
    Duty,
    ItemCode,
    Purpose,
    RiskLevel,
    required_items,
)
from referral_packet.clock import Clock, SystemClock
from referral_packet.delivery import Delivery, DeliveryStatus, DeliveryStore
from referral_packet.envelope import DutyKeyring
from referral_packet.errors import AccessDenied, RuleViolation
from referral_packet.journey import (
    HandoffStage,
    JourneyEvent,
    JourneyRepository,
    ReferralJourney,
)
from referral_packet.manifest import (
    GENESIS_HASH,
    ChangeReason,
    ItemDeclaration,
    build_version,
)
from referral_packet.quality import GapReport, QcScope, locate_gaps
from referral_packet.uploads import Artifact, UploadManager


@dataclass(frozen=True)
class PushResult:
    referral_id: str
    revision: int
    version_hash: str
    deduped: bool                  # True = 幂等命中，未产生新版本
    findings: tuple[dict, ...]
    superseded_deliveries: tuple[str, ...]


@dataclass(frozen=True)
class PatientAccessView:
    patient_id: str
    events: tuple[dict, ...]
    fields_seen: tuple[str, ...]   # 被访问过的字段去重列表


class ReferralPacketService:
    def __init__(self, *, clock: Clock | None = None,
                 keyring: DutyKeyring | None = None) -> None:
        self._clock = clock or SystemClock()
        self._seq = itertools.count(1)
        self._journeys = JourneyRepository(self._new_id)
        self._uploads = UploadManager()
        self._keyring = keyring or self._default_keyring()
        self._deliveries = DeliveryStore(
            keyring=self._keyring, uploads=self._uploads, id_generator=self._new_id
        )
        self._audit = AuditLog()
        self._qc = QcScope(self._audit)

    @staticmethod
    def _default_keyring() -> DutyKeyring:
        keyring = DutyKeyring()
        for duty in Duty:
            keyring.issue(duty)
        return keyring

    def _new_id(self) -> str:
        return f"id-{next(self._seq):08d}"

    def _now_text(self) -> str:
        return self._clock.now().isoformat()

    def _now(self) -> datetime:
        return self._clock.now()

    # ============================================================= 旅程定位

    def _journey(self, referral_id: str) -> ReferralJourney:
        try:
            return self._journeys.get_by_id(referral_id)
        except KeyError:
            from referral_packet.errors import NotFound

            raise NotFound("转诊不存在", referral_id=referral_id) from None

    # ============================================================= 分片上传

    def open_upload(self, *, referral_key: str, patient_id: str, item_hint: str,
                    filename: str, total_size: int, chunk_size: int,
                    whole_sha256: str) -> dict:
        journey = self._journeys.get_or_create(
            referral_key=referral_key, patient_id=patient_id, now=self._now_text()
        )
        session = self._uploads.open_session(
            upload_id=self._new_id(),
            referral_id=journey.referral_id,
            item_hint=item_hint, filename=filename,
            total_size=total_size, chunk_size=chunk_size,
            whole_sha256=whole_sha256, created_at=self._now_text(),
        )
        return self._uploads.session_status(session.upload_id)

    def upload_chunk(self, upload_id: str, index: int, data: bytes,
                     chunk_sha256: str | None = None) -> dict:
        self._uploads.put_chunk(upload_id, index, data, chunk_sha256)
        return self._uploads.session_status(upload_id)

    def upload_status(self, upload_id: str) -> dict:
        return self._uploads.session_status(upload_id)

    def assemble_upload(self, upload_id: str) -> Artifact:
        return self._uploads.assemble(upload_id, self._now_text())

    # ============================================================= 清单推送

    def push_manifest(
        self,
        *,
        referral_key: str,
        patient_id: str,
        declared_by: str,
        facility: str,
        condition: str,
        risk: RiskLevel,
        items: Iterable[ItemDeclaration],
        change_reason: ChangeReason = ChangeReason.INITIAL,
        idempotency_key: str | None = None,
        deliver_to: tuple[tuple[Duty, Purpose], ...] = (),
    ) -> PushResult:
        """推送（或更正/撤回/复测/升级）清单。

        同一 ``idempotency_key`` 重放只返回首次结果——重复推送不会形成两条旅程，
        也不会形成两个版本。
        """

        now = self._now_text()
        journey = self._journeys.get_or_create(
            referral_key=referral_key, patient_id=patient_id, now=now
        )

        if idempotency_key is not None:
            hit = journey.idempotent_result(idempotency_key)
            if hit is not None:
                version = journey.revision(hit)
                return PushResult(
                    referral_id=journey.referral_id, revision=hit,
                    version_hash=version.version_hash, deduped=True,
                    findings=tuple(f.as_dict() for f in version.findings),
                    superseded_deliveries=(),
                )

        required = required_items(condition, risk)
        parent = journey.latest.version_hash if journey.latest else GENESIS_HASH
        if journey.latest is None and change_reason is not ChangeReason.INITIAL:
            raise RuleViolation(
                "旅程首个版本必须是 initial", change_reason=change_reason.value
            )
        if journey.latest is not None and change_reason is ChangeReason.INITIAL:
            raise RuleViolation(
                "旅程已存在；再次推送须说明变更原因（correction/withdrawal/recheck/escalation）"
            )
        revision = len(journey.versions) + 1
        version = build_version(
            revision=revision, created_at=now,
            declared_by=declared_by, facility=facility,
            condition=required.condition, risk=risk.value,
            change_reason=change_reason, required=required,
            items=tuple(items), parent_hash=parent,
            supersedes=revision - 1 if revision > 1 else None,
        )
        journey.append_version(version, idempotency_key)
        stage = {
            ChangeReason.INITIAL: HandoffStage.PRIMARY_PUSH,
            ChangeReason.CORRECTION: HandoffStage.CORRECTION,
            ChangeReason.WITHDRAWAL: HandoffStage.WITHDRAWAL,
            ChangeReason.RECHECK: HandoffStage.RECHECK,
            ChangeReason.ESCALATION: HandoffStage.CLINICAL_ESCALATION,
        }[change_reason]
        journey.record(JourneyEvent(
            at=now, stage=stage, actor=declared_by, revision=revision,
            detail=f"revision={revision} reason={change_reason.value} "
                   f"hash={version.version_hash[:12]} "
                   f"findings={[f.kind.value for f in version.findings]}",
        ))

        # 新版本使同职责/用途的未读交付作废（已签收的不受影响）。
        superseded: list[str] = []
        for duty, purpose in deliver_to or ():
            superseded.extend(
                self._deliveries.supersede_unread(journey, duty=duty, purpose=purpose)
            )
        return PushResult(
            referral_id=journey.referral_id, revision=revision,
            version_hash=version.version_hash, deduped=False,
            findings=tuple(f.as_dict() for f in version.findings),
            superseded_deliveries=tuple(superseded),
        )

    def escalate(self, *, referral_id: str, declared_by: str,
                 items: Iterable[ItemDeclaration],
                 idempotency_key: str | None = None,
                 ) -> PushResult:
        """临床紧急升级：新版本（风险=emergency），系统不自动扩大资料范围。"""
        journey = self._journey(referral_id)
        latest = journey.latest
        if latest is None:
            raise RuleViolation("尚未推送过清单，无从升级")
        return self.push_manifest(
            referral_key=journey.referral_key, patient_id=journey.patient_id,
            declared_by=declared_by, facility=latest.facility,
            condition=latest.condition, risk=RiskLevel.EMERGENCY,
            items=items, change_reason=ChangeReason.ESCALATION,
            idempotency_key=idempotency_key,
        )

    # ============================================================= 交付/签收

    def deliver(self, *, referral_id: str, purpose: Purpose, duty: Duty,
                revision: int | None = None) -> Delivery:
        journey = self._journey(referral_id)
        if revision is None:
            version = journey.latest
            if version is None:
                raise RuleViolation("旅程尚无清单版本")
        else:
            try:
                version = journey.revision(revision)
            except KeyError:
                from referral_packet.errors import NotFound

                raise NotFound("版本不存在", revision=revision) from None
        delivery = self._deliveries.build(
            journey=journey, version=version, purpose=purpose, duty=duty,
            now=self._now_text(),
        )
        self._audit.append(AccessEvent(
            at=self._now_text(), actor=f"system:{duty.value}", duty=duty,
            action=Action.DELIVER, purpose=purpose,
            referral_id=journey.referral_id, patient_id=journey.patient_id,
            decision=Decision.ALLOW,
            field_codes=tuple(c.value for c in delivery.scope_codes),
            reason=purpose.value,
            detail=f"delivery={delivery.delivery_id} revision={version.revision}",
        ))
        return delivery

    def sign_receipt(self, *, delivery_id: str, actor: str, duty: Duty
                     ) -> tuple[Delivery, dict[str, bytes]]:
        """接收科室按职责解密并签收，回执锁定所见版本哈希。"""
        delivery = self._deliveries.get(delivery_id)
        journey = self._journey(delivery.referral_id)
        result, plaintexts = self._deliveries.open(
            delivery_id, actor=actor, duty=duty, journey=journey,
            now=self._now_text(),
        )
        self._audit.append(AccessEvent(
            at=self._now_text(), actor=actor, duty=duty, action=Action.OPEN,
            purpose=delivery.purpose,
            referral_id=journey.referral_id, patient_id=journey.patient_id,
            decision=Decision.ALLOW,
            field_codes=tuple(c.value for c in delivery.scope_codes),
            reason=delivery.purpose.value,
            detail=f"delivery={delivery_id} seen={delivery.seen_version_hash[:12]}",
        ))
        return result, plaintexts

    def sweep_timeouts(self, referral_id: str) -> list[Delivery]:
        """超时未读 -> 同范围升级催办。不会新增条目、不扩大用途。"""
        journey = self._journey(referral_id)
        latest = journey.latest
        if latest is None:
            return []
        return self._deliveries.sweep_timeouts(
            journey=journey, version=latest,
            now_text=self._now_text(), moment=self._now(),
        )

    # ===================================================== 历史版本（可审计）

    def view_signed_revision(self, *, referral_id: str, revision: int,
                             actor: str, duty: Duty) -> dict:
        """已签收版本继续可审计：任何持有职责者可看*审计投影*（无临床正文）。"""
        journey = self._journey(referral_id)
        version = journey.revision(revision)
        signed = any(
            d.revision == revision and d.status is DeliveryStatus.READ
            for d in self._deliveries.for_referral(journey)
        )
        from referral_packet.errors import ImmutableViolation

        if not signed:
            raise ImmutableViolation("仅已签收版本提供审计投影", revision=revision)
        self._audit.append(AccessEvent(
            at=self._now_text(), actor=actor, duty=duty, action=Action.HISTORY_VIEW,
            purpose=None, referral_id=journey.referral_id,
            patient_id=journey.patient_id, decision=Decision.ALLOW,
            field_codes=tuple(version.required_codes),
            reason="audit_signed_revision",
            detail=f"revision={revision}",
        ))
        return version.to_audit_dict()

    # ============================================================= 患者查询

    def patient_access_report(self, *, patient_id: str, actor: str
                              ) -> PatientAccessView:
        """患者查询：谁、因何用途、访问过哪些字段（含被拒绝的尝试）。"""
        events = self._audit.for_patient(patient_id)
        self._audit.append(AccessEvent(
            at=self._now_text(), actor=actor, duty=Duty.PATIENT,
            action=Action.PATIENT_VIEW, purpose=Purpose.PATIENT_VIEW,
            referral_id="-", patient_id=patient_id,
            decision=Decision.ALLOW, field_codes=(),
            reason="patient_view", detail="患者查询自身访问记录",
        ))
        fields = tuple(sorted({
            code for e in events for code in e.field_codes
        }))
        return PatientAccessView(
            patient_id=patient_id,
            events=tuple(e.as_dict() for e in events),
            fields_seen=fields,
        )

    # ============================================================= 质控

    def grant_qc_scope(self, officer: str, referral_ids: Iterable[str]) -> None:
        self._qc.grant(officer, referral_ids)

    def qc_gap_report(self, *, referral_id: str, officer: str) -> GapReport:
        journey = self._journey(referral_id)
        if not self._qc.check(officer, journey, now=self._now_text()):
            raise AccessDenied(
                "质控人员只能查看被指派的转诊，不能浏览无关历史",
                officer=officer, referral_id=referral_id,
            )
        report = locate_gaps(journey, self._now_text())
        self._audit.append(AccessEvent(
            at=self._now_text(), actor=officer, duty=Duty.QC_OFFICER,
            action=Action.QC_VIEW, purpose=Purpose.QC,
            referral_id=journey.referral_id, patient_id=journey.patient_id,
            decision=Decision.ALLOW,
            field_codes=tuple(sorted(report.item_gaps.keys())),
            reason="quality_control",
            detail="交接环节缺项定位报告",
        ))
        return report

    # ============================================================= 只读访问

    @property
    def audit_log(self) -> AuditLog:
        return self._audit

    def delivery(self, delivery_id: str) -> Delivery:
        return self._deliveries.get(delivery_id)

    def uploads(self) -> UploadManager:
        return self._uploads

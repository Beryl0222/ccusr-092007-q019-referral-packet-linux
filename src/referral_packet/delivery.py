"""用途受限资料包、按职责解密的交付与回执。

资料包不是「整份病历」：每次交付的条目集合 =

    清单有效条目 ∩ 用途白名单 ∩ 接收职责可见集 ∩ 已获患者授权

更正/撤回产生新版本后，旧的未读交付标记为 :attr:`DeliveryStatus.SUPERSEDED`，
接收方只能打开最新交付；**已经签收的交付永久冻结**，回执记录所见版本哈希，
旧版本仍可审计。超时未读只按同一范围升级催办，绝不自动扩大条目范围。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import hashlib
from typing import Callable, Mapping

from referral_packet import catalog
from referral_packet.catalog import Duty, ItemCode, Purpose
from referral_packet.envelope import DutyKeyring, SealedEnvelope, open_envelope, seal
from referral_packet.errors import AccessDenied, ImmutableViolation, NotFound, RuleViolation
from referral_packet.journey import HandoffStage, JourneyEvent, ReferralJourney
from referral_packet.manifest import ManifestVersion
from referral_packet.uploads import UploadManager


class DeliveryStatus(str, Enum):
    DELIVERED = "delivered"
    READ = "read"                       # 已签收（不可变）
    SUPERSEDED = "superseded"           # 未读即被新版本取代
    TIMED_OUT = "timed_out"             # 曾超时升级，之后仍可被签收


@dataclass(frozen=True)
class PacketItem:
    code: ItemCode
    envelope_id: str
    content_sha256: str


@dataclass
class Delivery:
    delivery_id: str
    referral_id: str
    revision: int
    version_hash: str
    purpose: Purpose
    duty: Duty
    items: list[PacketItem]
    created_at: str
    status: DeliveryStatus = DeliveryStatus.DELIVERED
    read_by: str | None = None
    read_at: str | None = None
    seen_version_hash: str | None = None
    escalated_from: str | None = None     # 超时升级产生的交付指向原交付
    excluded: dict[str, str] = field(default_factory=dict)  # code -> 剔除原因

    @property
    def scope_codes(self) -> tuple[ItemCode, ...]:
        return tuple(i.code for i in self.items)


#: 各用途的未读超时（分钟）。制度参数，调参不改状态机。
READ_SLA_MINUTES: Mapping[Purpose, int] = {
    Purpose.TRIAGE: 15,
    Purpose.EMERGENCY_RX: 10,
    Purpose.INPATIENT: 60,
}

#: 超时后的催办接收职责（用途与范围保持不变）。
#: None 表示没有任何其他职责能服务同一用途 —— 只记录超时升级事件、
#: 通过制度规定的其他渠道催办，绝不为此放宽用途或职责边界。
TIMEOUT_BACKUP: Mapping[Duty, Duty | None] = {
    Duty.TRIAGE_DESK: Duty.EMERGENCY,  # 急诊科可承接分诊用途
    Duty.EMERGENCY: None,              # emergency_rx 无其他职责可承接，只催办
    Duty.INPATIENT: None,              # inpatient 同理，禁止放宽职责边界
}


def select_codes(version: ManifestVersion, purpose: Purpose, duty: Duty,
                 scope_ceiling: frozenset[ItemCode] | None = None
                 ) -> tuple[list[ItemCode], dict[str, str]]:
    """纯函数：按 用途∩职责∩授权 挑条目，同时返回被剔除条目及原因。"""

    if not catalog.duty_may_serve(duty, purpose):
        raise AccessDenied("该职责不允许此用途",
                           duty=duty.value, purpose=purpose.value)
    allowed = catalog.duty_visible_codes(duty, purpose)
    if scope_ceiling is not None:
        # 升级催办的硬约束：新交付范围不得超过原交付。
        allowed = allowed & scope_ceiling
    chosen: list[ItemCode] = []
    excluded: dict[str, str] = {}
    for code in sorted(version.effective_codes(), key=lambda c: c.value):
        decl = version.declaration_for(code)
        if decl is None:
            continue
        if code not in allowed:
            excluded[code.value] = "outside_purpose_or_duty_scope"
            continue
        if not decl.patient_authorized:
            excluded[code.value] = "unauthorized"
            continue
        if decl.content_ref is None:
            excluded[code.value] = "declared_without_content"
            continue
        chosen.append(code)
    return chosen, excluded


class DeliveryStore:
    def __init__(self, *, keyring: DutyKeyring, uploads: UploadManager,
                 id_generator: Callable[[], str]) -> None:
        self._keyring = keyring
        self._uploads = uploads
        self._id = id_generator
        self._deliveries: dict[str, Delivery] = {}
        self._envelopes: dict[str, SealedEnvelope] = {}

    # -------------------------------------------------------------- 打包/交付

    def build(
        self,
        *,
        journey: ReferralJourney,
        version: ManifestVersion,
        purpose: Purpose,
        duty: Duty,
        now: str,
        scope_ceiling: frozenset[ItemCode] | None = None,
        escalated_from: str | None = None,
    ) -> Delivery:
        codes, excluded = select_codes(version, purpose, duty, scope_ceiling)
        key_id = self._keyring.active_key_id(duty)
        items: list[PacketItem] = []
        for code in codes:
            decl = version.declaration_for(code)
            assert decl is not None and decl.content_ref is not None
            blob = self._uploads.read_blob(decl.content_ref)
            if decl.content_sha256 is not None and \
                    decl.content_sha256 != _sha(blob):
                raise RuleViolation(
                    "条目正文哈希与声明不符，拒绝封装", code=code.value
                )
            envelope: SealedEnvelope = seal(
                blob, purpose=purpose,
                recipients={duty: key_id}, keyring=self._keyring,
            )
            items.append(PacketItem(code=code, envelope_id=envelope.envelope_id,
                                    content_sha256=envelope.content_sha256))
            # envelope 与解封所需会话密钥在此实现里随条目临时保存（见 _envelopes）。
            self._envelopes[envelope.envelope_id] = envelope
        delivery = Delivery(
            delivery_id=self._id(),
            referral_id=journey.referral_id,
            revision=version.revision,
            version_hash=version.version_hash,
            purpose=purpose,
            duty=duty,
            items=items,
            created_at=now,
            escalated_from=escalated_from,
            excluded=excluded,
        )
        self._deliveries[delivery.delivery_id] = delivery
        # 同一渠道（职责+用途）上指向旧版本的未读交付立即作废：
        # 接收方不可能再签收旧版，避免「看到的是哪一版」无确认。
        for prior in self._deliveries.values():
            if (prior is not delivery
                    and prior.referral_id == journey.referral_id
                    and prior.duty is duty and prior.purpose is purpose
                    and prior.revision < version.revision
                    and prior.status is DeliveryStatus.DELIVERED):
                prior.status = DeliveryStatus.SUPERSEDED
        journey.record(JourneyEvent(
            at=now, stage=HandoffStage.DELIVERY, actor=f"system:{duty.value}",
            revision=version.revision,
            detail=f"delivery={delivery.delivery_id} purpose={purpose.value} "
                   f"items={[c.value for c in delivery.scope_codes]}",
        ))
        return delivery

    def supersede_unread(self, journey: ReferralJourney, *, duty: Duty,
                         purpose: Purpose) -> list[str]:
        """新版本推送后：同职责同用途的未读交付作废，强制接收方看最新版。"""
        changed = []
        for delivery in self._deliveries.values():
            if delivery.referral_id != journey.referral_id:
                continue
            if delivery.duty is duty and delivery.purpose is purpose and \
                    delivery.status is DeliveryStatus.DELIVERED:
                delivery.status = DeliveryStatus.SUPERSEDED
                changed.append(delivery.delivery_id)
        return changed

    # -------------------------------------------------------------- 签收/解封

    def open(self, delivery_id: str, *, actor: str, duty: Duty,
             journey: ReferralJourney, now: str) -> tuple[Delivery, dict[ItemCode, bytes]]:
        """接收科室解封签收。返回所见条目明文；调用方负责写访问审计。"""

        delivery = self.get(delivery_id)
        if delivery.referral_id != journey.referral_id:
            raise AccessDenied("交付不属于该转诊旅程")
        if duty is not delivery.duty:
            raise AccessDenied("职责与交付接收者不符", duty=duty.value)
        if delivery.status is DeliveryStatus.SUPERSEDED:
            raise AccessDenied(
                "该交付已被新版本取代，未签收；请打开最新交付",
                delivery_id=delivery_id,
            )
        if delivery.status is DeliveryStatus.READ:
            # 重复打开：交付不可变，返回审计性拒绝，由服务层改走「查回执」。
            raise ImmutableViolation(
                "交付已签收，回执不可更改；如需复核请凭回执查历史版本",
                delivery_id=delivery_id,
            )
        plaintexts: dict[ItemCode, bytes] = {}
        for item in delivery.items:
            envelope = self._envelopes[item.envelope_id]
            # key_id 记录在封装头中（构建时的密钥版本），不能假设当前活跃密钥未轮换。
            wrapped = envelope.wrapped_keys[duty]
            key_id = wrapped.split(":", 1)[0]
            plaintexts[item.code] = open_envelope(
                envelope, duty=duty, key_id=key_id,
                purpose=delivery.purpose, keyring=self._keyring,
            )
        delivery.status = DeliveryStatus.READ
        delivery.read_by = actor
        delivery.read_at = now
        delivery.seen_version_hash = delivery.version_hash
        journey.record(JourneyEvent(
            at=now, stage=HandoffStage.READ_RECEIPT, actor=actor,
            revision=delivery.revision,
            detail=f"delivery={delivery.delivery_id} seen_version_hash={delivery.version_hash}",
        ))
        return delivery, plaintexts

    # -------------------------------------------------------------- 超时升级

    def sweep_timeouts(self, *, journey: ReferralJourney, version: ManifestVersion,
                       now_text: str, moment: datetime) -> list[Delivery]:
        """找出超时未读交付并产生*同范围*催办交付。不新增任何条目。"""

        escalated: list[Delivery] = []
        for delivery in list(self._deliveries.values()):
            if delivery.referral_id != journey.referral_id:
                continue
            if delivery.status is not DeliveryStatus.DELIVERED or delivery.escalated_from:
                continue
            sla = timedelta(minutes=READ_SLA_MINUTES.get(delivery.purpose, 15))
            if datetime.fromisoformat(delivery.created_at) + sla <= moment:
                backup = TIMEOUT_BACKUP.get(delivery.duty)
                delivery.status = DeliveryStatus.TIMED_OUT
                journey.record(JourneyEvent(
                    at=now_text, stage=HandoffStage.TIMEOUT_ESCALATION,
                    actor="system:sla-monitor", revision=delivery.revision,
                    detail=(
                        f"delivery={delivery.delivery_id} 超时未读，"
                        f"范围保持 {[c.value for c in delivery.scope_codes]}"
                        + ("，无同用途备用职责，仅升级催办" if backup is None else "")
                    ),
                ))
                if backup is not None:
                    ceiling = frozenset(delivery.scope_codes)
                    followup = self.build(
                        journey=journey, version=version,
                        purpose=delivery.purpose, duty=backup, now=now_text,
                        scope_ceiling=ceiling, escalated_from=delivery.delivery_id,
                    )
                    # 硬不变量：升级催办范围只能等于/小于原范围。
                    if not frozenset(followup.scope_codes) <= ceiling:
                        raise RuleViolation("升级交付范围超过原交付，已中止")
                    escalated.append(followup)
        return escalated

    # ------------------------------------------------------------------ 查询

    def get(self, delivery_id: str) -> Delivery:
        try:
            return self._deliveries[delivery_id]
        except KeyError:
            raise NotFound("交付不存在", delivery_id=delivery_id) from None

    def for_referral(self, journey: ReferralJourney) -> list[Delivery]:
        return [d for d in self._deliveries.values()
                if d.referral_id == journey.referral_id]

    def latest_for(self, journey: ReferralJourney, *, duty: Duty,
                   purpose: Purpose) -> Delivery | None:
        candidates = [
            d for d in self.for_referral(journey)
            if d.duty is duty and d.purpose is purpose
            and d.status is not DeliveryStatus.SUPERSEDED
        ]
        return candidates[-1] if candidates else None


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()

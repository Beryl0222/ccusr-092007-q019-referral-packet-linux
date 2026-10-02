"""转诊旅程聚合与其仓储。

*同一转诊的重复推送不能形成两条旅程* —— 旅程按稳定业务键 ``referral_key``
去重，``referral_id`` 只是首次落库后分配的内部标识；推送还可携带
``idempotency_key``，键相同则返回首次产生的版本，不产生新版本。

旅程同时保存**交接事件时间线**：推送、交付、签收、超时升级、复测各自留痕，
质控据此定位「缺项发生在哪个交接环节」（见 :mod:`referral_packet.quality`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from referral_packet.errors import IntegrityError, RuleViolation
from referral_packet.manifest import GENESIS_HASH, ManifestVersion


class HandoffStage(str, Enum):
    PRIMARY_PUSH = "primary_push"          # 基层推送
    DELIVERY = "delivery"                  # 资料包送达接收科室
    READ_RECEIPT = "read_receipt"          # 县医院签收所见版本
    TIMEOUT_ESCALATION = "timeout_escalation"  # 超时未读触发升级
    CLINICAL_ESCALATION = "clinical_escalation"  # 临床紧急升级
    RECHECK = "recheck"                    # 到院复测
    WITHDRAWAL = "withdrawal"              # 撤回附件
    CORRECTION = "correction"              # 更正
    ACCESS_DENIED = "access_denied"        # 越权访问尝试（质控可见）


@dataclass(frozen=True)
class JourneyEvent:
    at: str
    stage: HandoffStage
    actor: str
    revision: int | None
    detail: str = ""


@dataclass
class ReferralJourney:
    referral_id: str
    referral_key: str
    patient_id: str
    created_at: str
    versions: list[ManifestVersion] = field(default_factory=list)
    events: list[JourneyEvent] = field(default_factory=list)
    # 幂等键 -> 首次产生的版本号。
    _idempotency: dict[str, int] = field(default_factory=dict)

    @property
    def latest(self) -> ManifestVersion | None:
        return self.versions[-1] if self.versions else None

    def revision(self, revision: int) -> ManifestVersion:
        for version in self.versions:
            if version.revision == revision:
                return version
        raise KeyError(revision)

    def idempotent_result(self, key: str) -> Optional[int]:
        return self._idempotency.get(key)

    def append_version(self, version: ManifestVersion, idempotency_key: str | None) -> None:
        expected_revision = len(self.versions) + 1
        if version.revision != expected_revision:
            raise RuleViolation(
                "版本号必须严格递增",
                expected=expected_revision, got=version.revision,
            )
        expected_parent = self.versions[-1].version_hash if self.versions else GENESIS_HASH
        if version.parent_hash != expected_parent:
            raise IntegrityError(
                "版本哈希链断裂：parent_hash 不指向上一版本",
                revision=version.revision,
            )
        self.versions.append(version)
        if idempotency_key is not None:
            self._idempotency[idempotency_key] = version.revision

    def record(self, event: JourneyEvent) -> None:
        self.events.append(event)


class JourneyRepository:
    """内存仓储；所有取旅程的入口都只认业务键或内部 id 二者之一，绝不双开旅程。"""

    def __init__(self, id_generator) -> None:
        self._id_generator = id_generator
        self._by_key: dict[str, ReferralJourney] = {}
        self._by_id: dict[str, str] = {}  # referral_id -> referral_key

    def get_or_create(self, *, referral_key: str, patient_id: str, now: str) -> ReferralJourney:
        journey = self._by_key.get(referral_key)
        if journey is None:
            referral_id = self._id_generator()
            journey = ReferralJourney(
                referral_id=referral_id,
                referral_key=referral_key,
                patient_id=patient_id,
                created_at=now,
            )
            self._by_key[referral_key] = journey
            self._by_id[referral_id] = referral_key
        elif journey.patient_id != patient_id:
            # 同一转诊键绑定不同患者属于严重配置/冒用错误，拒绝。
            raise RuleViolation(
                "转诊业务键已绑定其他患者",
                referral_key=referral_key,
            )
        return journey

    def get_by_key(self, referral_key: str) -> ReferralJourney:
        try:
            return self._by_key[referral_key]
        except KeyError:
            raise KeyError(referral_key) from None

    def get_by_id(self, referral_id: str) -> ReferralJourney:
        key = self._by_id.get(referral_id)
        if key is None:
            raise KeyError(referral_id)
        return self._by_key[key]

    def all_for_patient(self, patient_id: str) -> list[ReferralJourney]:
        """患者只能枚举自己的旅程；质控的横向访问在 quality 模块单独授权。"""
        return [j for j in self._by_key.values() if j.patient_id == patient_id]

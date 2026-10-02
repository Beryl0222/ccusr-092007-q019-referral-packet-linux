"""访问审计：谁、因何用途、访问过哪些字段。

每一次 *尝试* 都留痕——包括被拒绝的访问（:data:`Decision.DENIED`），
质控才能发现越权浏览意图；患者能按字段级别查到访问明细。
审计记录只增不改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from referral_packet.catalog import Duty, Purpose


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class Action(str, Enum):
    DELIVER = "deliver"          # 生成用途受限资料包
    OPEN = "open"                # 解密查看条目正文（签收）
    QC_VIEW = "qc_view"          # 质控查看元数据/时间线
    PATIENT_VIEW = "patient_view"  # 患者查看访问记录
    HISTORY_VIEW = "history_view"  # 查看已签收历史版本（审计投影）


@dataclass(frozen=True)
class AccessEvent:
    at: str
    actor: str
    duty: Duty | None
    action: Action
    purpose: Purpose | None
    referral_id: str
    patient_id: str
    decision: Decision
    field_codes: tuple[str, ...]   # 字段级：本次触及的条目编码
    reason: str                    # 用途说明（因何用途）
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "at": self.at,
            "actor": self.actor,
            "duty": self.duty.value if self.duty else None,
            "action": self.action.value,
            "purpose": self.purpose.value if self.purpose else None,
            "referral_id": self.referral_id,
            "decision": self.decision.value,
            "field_codes": list(self.field_codes),
            "reason": self.reason,
            "detail": self.detail,
        }


class AuditLog:
    def __init__(self) -> None:
        self._events: list[AccessEvent] = []

    def append(self, event: AccessEvent) -> None:
        self._events.append(event)

    def for_patient(self, patient_id: str) -> list[AccessEvent]:
        """患者视角：只看与自己有关的记录，拒绝原因也如实展示。"""
        return [e for e in self._events if e.patient_id == patient_id]

    def for_referral(self, referral_id: str) -> list[AccessEvent]:
        return [e for e in self._events if e.referral_id == referral_id]

    def denied_events(self) -> list[AccessEvent]:
        return [e for e in self._events if e.decision is Decision.DENY]

    def all_events(self) -> Sequence[AccessEvent]:
        return tuple(self._events)

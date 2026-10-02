"""双方质控：交接环节缺项定位。

质控人员只能针对**被指定的转诊**工作；服务层在调用前必须登记质控授权范围，
访问范围外转诊直接拒绝并写拒绝审计——质控不能横向浏览无关历史记录。
质控视图只含元数据、声明要素与交接时间线，不含条目正文。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from referral_packet.audit import AccessEvent, Action, AuditLog, Decision
from referral_packet.catalog import Duty, ItemCode, Purpose, required_items
from referral_packet.journey import HandoffStage, ReferralJourney
from referral_packet.manifest import FindingKind


@dataclass(frozen=True)
class GapReport:
    referral_id: str
    generated_at: str
    # 条目 -> 缺项跨越的环节（首现环节 -> 补齐环节）
    item_gaps: dict[str, dict[str, str]]
    still_missing: tuple[str, ...]
    timeline: tuple[dict, ...]

    def as_dict(self) -> dict:
        return {
            "referral_id": self.referral_id,
            "generated_at": self.generated_at,
            "item_gaps": self.item_gaps,
            "still_missing": list(self.still_missing),
            "timeline": list(self.timeline),
        }


_STAGE_ORDER = [
    HandoffStage.PRIMARY_PUSH,
    HandoffStage.DELIVERY,
    HandoffStage.TIMEOUT_ESCALATION,
    HandoffStage.CLINICAL_ESCALATION,
    HandoffStage.READ_RECEIPT,
    HandoffStage.RECHECK,
    HandoffStage.CORRECTION,
    HandoffStage.WITHDRAWAL,
    HandoffStage.ACCESS_DENIED,
]


def build_timeline(journey: ReferralJourney) -> tuple[dict, ...]:
    """把版本节点与交接事件合并为单条时间线（按发生顺序，流程序号兜底）。"""
    entries: list[tuple[str, int, dict]] = []
    for version in journey.versions:
        entries.append((version.created_at, 0, {
            "kind": "manifest_revision",
            "at": version.created_at,
            "revision": version.revision,
            "change_reason": version.change_reason.value,
            "version_hash": version.version_hash[:12],
            "risk": version.risk,
            "findings": [f.as_dict() for f in version.findings],
        }))
    for seq, event in enumerate(journey.events):
        entries.append((event.at, 1 + seq, {
            "kind": "handoff",
            "at": event.at,
            "stage": event.stage.value,
            "actor": event.actor,
            "revision": event.revision,
            "detail": event.detail,
        }))
    entries.sort(key=lambda e: (e[0], e[1]))
    return tuple(e[2] for e in entries)


def locate_gaps(journey: ReferralJourney, generated_at: str) -> GapReport:
    """定位缺项发生/延续在哪些交接环节。

    判定方式：逐版本按当时的病种/风险重算要求集，记录每个要求条目首次缺失的
    版本（=基层推送环节的缺项），以及它首次变为有效声明的版本与变更原因
    （更正/复测/升级环节补齐）。到最新版仍缺的进入 ``still_missing``。
    """

    item_gaps: dict[str, dict[str, str]] = {}
    missing_since: dict[ItemCode, int] = {}
    latest_required: frozenset[ItemCode] = frozenset()

    for version in journey.versions:
        from referral_packet.catalog import RiskLevel

        required = required_items(version.condition, RiskLevel(version.risk))
        latest_required = required.item_codes
        effective = version.effective_codes()
        missing_kinds = {
            f.item for f in version.findings
            if f.kind in (FindingKind.MISSING, FindingKind.UNAVAILABLE) and f.item
        }
        for code in required.item_codes:
            if code in missing_kinds or code not in effective:
                missing_since.setdefault(code, version.revision)
            elif code in missing_since:
                # 本版补齐：记录跨越的环节。
                item_gaps[code.value] = {
                    "missing_from_revision": str(missing_since.pop(code)),
                    "resolved_at_revision": str(version.revision),
                    "resolved_via": version.change_reason.value,
                }

    still_missing = tuple(sorted(c.value for c in missing_since))
    for code, revision in list(missing_since.items()):
        item_gaps[code.value] = {
            "missing_from_revision": str(revision),
            "status": "unresolved",
        }
    return GapReport(
        referral_id=journey.referral_id,
        generated_at=generated_at,
        item_gaps=item_gaps,
        still_missing=still_missing,
        timeline=build_timeline(journey),
    )


class QcScope:
    """质控授权范围登记。范围外访问一律拒绝（拒绝事件也进审计）。"""

    def __init__(self, audit: AuditLog) -> None:
        self._audit = audit
        self._grants: dict[str, set[str]] = {}  # officer -> {referral_id}

    def grant(self, officer: str, referral_ids: Iterable[str]) -> None:
        self._grants.setdefault(officer, set()).update(referral_ids)

    def check(self, officer: str, journey: ReferralJourney, *, now: str) -> bool:
        allowed = journey.referral_id in self._grants.get(officer, set())
        if not allowed:
            self._audit.append(AccessEvent(
                at=now, actor=officer, duty=Duty.QC_OFFICER,
                action=Action.QC_VIEW, purpose=Purpose.QC,
                referral_id=journey.referral_id, patient_id=journey.patient_id,
                decision=Decision.DENY, field_codes=(),
                reason="quality_control",
                detail="质控人员访问未授权转诊，已拒绝",
            ))
        return allowed

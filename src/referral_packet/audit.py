"""审计日志与限定范围的查询。

- 患者视图：患者可查询谁、因何用途、访问过自己哪些字段。
- 质控视图：双方质控人员可按转诊定位缺项发生在哪个交接环节；
  接口强制要求 referral_id 且校验质控方是否为该转诊当事机构，
  不提供任何"列出全部转诊"的浏览入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Finding


class AuditDenied(PermissionError):
    pass


@dataclass(frozen=True)
class AccessEvent:
    referral_id: str
    patient_id: str
    actor: str
    role: str
    purpose: str
    fields: tuple[str, ...]
    version: int
    at: str


@dataclass(frozen=True)
class StageGap:
    """某交接环节上的一次缺项/冲突记录。"""

    stage: str
    kind: str
    item_id: str
    detail: str
    version: int


class AuditLog:
    def __init__(self) -> None:
        self._events: list[AccessEvent] = []
        # referral_id -> 参与机构（转出方、接收方），用于质控越权校验
        self._parties: dict[str, frozenset[str]] = {}
        # referral_id -> 各版本的缺项定位
        self._gaps: dict[str, list[StageGap]] = {}

    def register_referral(self, referral_id: str, *, parties: tuple[str, ...]) -> None:
        self._parties[referral_id] = frozenset(parties)

    def record_access(self, event: AccessEvent) -> None:
        self._events.append(event)

    def record_findings(
        self, referral_id: str, *, version: int, findings: tuple[Finding, ...]
    ) -> None:
        """把校验器输出的缺失/冲突按交接环节登记，供质控追溯。"""
        self._gaps.setdefault(referral_id, []).extend(
            StageGap(
                stage=f.stage,
                kind=f.kind,
                item_id=f.item_id,
                detail=f.detail,
                version=version,
            )
            for f in findings
        )

    # ---- 患者视图 ----

    def patient_view(self, patient_id: str) -> tuple[AccessEvent, ...]:
        """患者查询：谁、因何用途、在何时访问过自己哪些字段。"""
        return tuple(e for e in self._events if e.patient_id == patient_id)

    # ---- 质控视图 ----

    def qc_trace(
        self, referral_id: str, *, requester_org: str, requester_role: str
    ) -> tuple[StageGap, ...]:
        """质控追溯：仅限该转诊当事机构的质控角色，按转诊定位缺项环节。"""
        if requester_role != "qc_reviewer":
            raise AuditDenied("仅质控角色可查询交接缺项")
        parties = self._parties.get(referral_id)
        if parties is None or requester_org not in parties:
            raise AuditDenied("该机构不是此转诊的当事方，无权查看")
        return tuple(self._gaps.get(referral_id, ()))

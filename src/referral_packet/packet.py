"""资料包生成与结构校验。

校验器只输出缺失与冲突提示（Finding），并把问题定位到交接环节；
它不评估病情轻重、不建议补做什么检查——那些决定属于临床人员。
"""

from __future__ import annotations

from datetime import datetime

from .models import Finding, ItemDeclaration, ManifestVersion, Packet
from .rules import allowed_categories, packet_categories, required_categories


def build_packet(
    manifest: ManifestVersion,
    *,
    disease_code: str,
    risk_level: str,
    purpose: str,
    packet_id: str,
) -> Packet:
    """按用途受限生成资料包。

    入包条目 = 本版本清单条目 ∩（病情必需 ∩ 用途允许）的类目，
    且剔除患者未授权的条目。撤回版本中被移除的条目自然不再出现。
    """
    scope = packet_categories(disease_code, risk_level, purpose)
    declared = {d.item_id: d for d in manifest.declarations}
    items = tuple(
        item_id
        for item_id in manifest.item_ids
        if item_id in declared
        and declared[item_id].category in scope
        and declared[item_id].consent
    )
    findings = validate(
        manifest,
        disease_code=disease_code,
        risk_level=risk_level,
        purpose=purpose,
    )
    return Packet(
        packet_id=packet_id,
        referral_id=manifest.referral_id,
        version=manifest.version,
        purpose=purpose,
        item_ids=items,
        findings=findings,
    )


def validate(
    manifest: ManifestVersion,
    *,
    disease_code: str,
    risk_level: str,
    purpose: str,
) -> tuple[Finding, ...]:
    """结构校验：缺失、授权缺位、声明冲突。只提示，不判断。"""
    findings: list[Finding] = []
    declared = {d.item_id: d for d in manifest.declarations}

    # 1) 必需类目在本版本清单中无完整声明 → 缺失（定位到声明环节）
    required = required_categories(disease_code, risk_level)
    covered = {
        declared[i].category
        for i in manifest.item_ids
        if i in declared and declared[i].completeness == "complete"
    }
    for category in sorted(required - covered):
        findings.append(
            Finding(
                kind="missing",
                stage="declare",
                item_id=category,
                detail=f"必需类目 {category} 无完整声明",
            )
        )

    # 2) 清单条目缺声明或缺患者授权 → 缺失/授权缺位
    for item_id in manifest.item_ids:
        decl = declared.get(item_id)
        if decl is None:
            findings.append(
                Finding("missing", "declare", item_id, "清单条目无任何声明")
            )
        elif not decl.consent:
            findings.append(
                Finding(
                    "consent_absent", "declare", item_id, "患者未授权该条目"
                )
            )

    # 3) 声明冲突：同一条目出现在多版本间内容摘要不一致，或采集时间晚于版本生成时间
    for decl in manifest.declarations:
        if _parse(decl.collected_at) > _parse(manifest.created_at):
            findings.append(
                Finding(
                    "conflict",
                    "declare",
                    decl.item_id,
                    "采集时间晚于清单版本生成时间",
                )
            )
    if manifest.parent_version is not None:
        findings.extend(_cross_version_conflicts(manifest))

    # 4) 用途外条目提示（不拦截，只提示打包环节存在超用途内容）
    allowed = allowed_categories(purpose)
    for item_id in manifest.item_ids:
        decl = declared.get(item_id)
        if decl is not None and decl.category not in allowed:
            findings.append(
                Finding(
                    "conflict",
                    "package",
                    item_id,
                    f"类目 {decl.category} 超出用途 {purpose} 允许范围，未入包",
                )
            )
    return tuple(findings)


def _cross_version_conflicts(manifest: ManifestVersion) -> list[Finding]:
    """同一版本内同一条目出现两份内容摘要不同的声明。"""
    findings: list[Finding] = []
    seen: dict[str, str] = {}
    for decl in manifest.declarations:
        prev = seen.get(decl.item_id)
        if prev is not None and prev != decl.content_hash:
            findings.append(
                Finding(
                    "conflict",
                    "declare",
                    decl.item_id,
                    "同一条目存在内容摘要不一致的多份声明",
                )
            )
        seen[decl.item_id] = decl.content_hash
    return findings


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)

"""转诊清单及其版本链。

基层医生对每个条目逐项声明四要素：**来源、采集时间、完整性、患者授权**。
系统把条目集与制度要求集（:mod:`referral_packet.catalog`）比对，产出
:class:`Finding` —— 只提示缺失与冲突，绝不替临床人员判断病情，也不阻止
紧急转诊（缺失是提示，不是闸门）。

更正报告、撤回非必要附件、到院复测、紧急升级一律 *追加新版本*；
旧版本内容由 ``parent_hash → version_hash`` 哈希链冻结，签收后仍可审计。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

from referral_packet.catalog import ItemCode, RequiredSet

#: 旅程与版本哈希链的共同根（首版 parent_hash）。
GENESIS_HASH = "0" * 64


class Completeness(str, Enum):
    COMPLETE = "complete"     # 医生声明：该条目内容完整
    PARTIAL = "partial"       # 医生声明：只拿到部分（如照片缺页）
    UNAVAILABLE = "unavailable"  # 医生声明：客观上无法取得


class ChangeReason(str, Enum):
    INITIAL = "initial"          # 首次推送
    CORRECTION = "correction"    # 更正报告
    WITHDRAWAL = "withdrawal"    # 撤回非必要附件
    RECHECK = "recheck"          # 到院复测
    ESCALATION = "escalation"    # 紧急升级


class FindingKind(str, Enum):
    MISSING = "missing"              # 制度要求但清单无有效声明
    CONFLICT = "conflict"            # 同一条目声明互相矛盾
    UNAUTHORIZED = "unauthorized"    # 有内容但缺患者授权
    PARTIAL = "partial"              # 医生自声明不完整
    UNAVAILABLE = "unavailable"      # 医生显式声明无法取得（提示，非阻塞）
    EXTRA_SCOPE = "extra_scope"      # 清单有、本次用途不需要（打包时剔除）
    DUPLICATE = "duplicate"          # 完全重复的声明（信息级）


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    message: str
    item: ItemCode | None = None

    def as_dict(self) -> dict:
        return {"kind": self.kind.value, "item": self.item.value if self.item else None,
                "message": self.message}


@dataclass(frozen=True)
class ItemDeclaration:
    """一个条目的逐项声明。内容本体不进清单，只有指向组装产物的引用与哈希。"""

    code: ItemCode
    source: str                       # 来源（病历页码/设备/患者口述/到院复测）
    collected_at: str                 # 采集时间 ISO-8601
    completeness: Completeness
    patient_authorized: bool          # 患者授权逐项声明
    content_ref: str | None = None    # 组装产物标识（uploads 产物 id）
    content_sha256: str | None = None
    withdrawn: bool = False           # True = 撤回墓碑（撤回非必要附件）
    note: str = ""

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("条目来源不能为空")
        # 仅校验时间可解析，不做任何「过旧/过期」的临床判断。
        _parse_iso(self.collected_at)

    def canonical(self) -> dict:
        return {
            "code": self.code.value,
            "source": self.source,
            "collected_at": self.collected_at,
            "completeness": self.completeness.value,
            "patient_authorized": self.patient_authorized,
            "content_ref": self.content_ref,
            "content_sha256": self.content_sha256,
            "withdrawn": self.withdrawn,
            "note": self.note,
        }


@dataclass(frozen=True)
class ManifestVersion:
    revision: int
    created_at: str
    declared_by: str                  # 声明医生标识
    facility: str                     # 声明机构
    condition: str                    # 临床声明的病种（规则解析结果另存）
    risk: str                         # 临床声明的风险等级
    change_reason: ChangeReason
    ruleset_version: int
    required_codes: tuple[str, ...]   # 生成时的制度要求快照
    items: tuple[ItemDeclaration, ...]
    parent_hash: str
    version_hash: str
    findings: tuple[Finding, ...] = field(default=())
    supersedes: int | None = None

    def effective_codes(self) -> frozenset[ItemCode]:
        """当前版本里仍有效（未撤回）的条目。"""
        return frozenset(i.code for i in self.items if not i.withdrawn)

    def declaration_for(self, code: ItemCode) -> ItemDeclaration | None:
        for item in self.items:
            if item.code == code and not item.withdrawn:
                return item
        return None

    def to_audit_dict(self) -> dict:
        """审计/质控投影：结构与哈希，不含临床正文（正文引用仍是 ref）。"""
        return {
            "revision": self.revision,
            "created_at": self.created_at,
            "declared_by": self.declared_by,
            "facility": self.facility,
            "condition": self.condition,
            "risk": self.risk,
            "change_reason": self.change_reason.value,
            "ruleset_version": self.ruleset_version,
            "required_codes": list(self.required_codes),
            "version_hash": self.version_hash,
            "parent_hash": self.parent_hash,
            "supersedes": self.supersedes,
            "findings": [f.as_dict() for f in self.findings],
            "items": [
                {
                    "code": i.code.value,
                    "source": i.source,
                    "collected_at": i.collected_at,
                    "completeness": i.completeness.value,
                    "patient_authorized": i.patient_authorized,
                    "withdrawn": i.withdrawn,
                    "has_content": i.content_ref is not None,
                }
                for i in self.items
            ],
        }


def _parse_iso(value: str) -> None:
    from datetime import datetime

    datetime.fromisoformat(value)


def canonical_hash(payload: dict | list | str) -> str:
    """领域统一哈希：规范化 JSON（键排序、无空白）后取 SHA-256。"""
    if not isinstance(payload, str):
        payload = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate(required: RequiredSet, items: Sequence[ItemDeclaration]) -> tuple[Finding, ...]:
    """把声明与制度要求比对，生成提示。纯函数，无副作用。

    注意缺失/无法取得 *不* 抛错：紧急救治不能被表单阻塞。
    """

    findings: list[Finding] = []
    seen: dict[ItemCode, list[ItemDeclaration]] = {}
    for item in items:
        seen.setdefault(item.code, []).append(item)

    # 1) 同条目多声明：完全一致记重复；来源或时间不一致记冲突（系统不裁决）。
    for code, decls in seen.items():
        active = [d for d in decls if not d.withdrawn]
        if len(active) > 1:
            sigs = {(d.source, d.collected_at, d.content_sha256) for d in active}
            if len(sigs) == 1:
                findings.append(Finding(FindingKind.DUPLICATE,
                                        f"条目 {code.value} 存在完全重复的声明", code))
            else:
                findings.append(Finding(
                    FindingKind.CONFLICT,
                    f"条目 {code.value} 的来源/采集时间声明不一致，需临床人员核实",
                    code,
                ))

    # 冲突时 active_map 只保留最后一条用于后续检查；冲突本身已经提示。
    active_map: dict[ItemCode, ItemDeclaration] = {}
    for item in items:
        if not item.withdrawn:
            active_map[item.code] = item

    # 2) 逐项授权 / 完整性提示。
    for code, decl in active_map.items():
        if decl.content_ref is not None and not decl.patient_authorized:
            findings.append(Finding(
                FindingKind.UNAUTHORIZED,
                f"条目 {code.value} 缺少患者授权声明，不能对外交付",
                code,
            ))
        if decl.completeness is Completeness.PARTIAL:
            findings.append(Finding(
                FindingKind.PARTIAL,
                f"条目 {code.value} 由声明医生标记为不完整（{decl.note or '无说明'}）",
                code,
            ))
        if decl.completeness is Completeness.UNAVAILABLE:
            findings.append(Finding(
                FindingKind.UNAVAILABLE,
                f"条目 {code.value} 经医生声明无法取得",
                code,
            ))

    # 3) 制度要求集核对：撤回墓碑与无声明都按缺失提示。
    for code in required.item_codes:
        decl = active_map.get(code)
        if decl is None:
            tombstone = next((d for d in seen.get(code, ()) if d.withdrawn), None)
            if tombstone is not None:
                findings.append(Finding(
                    FindingKind.MISSING,
                    f"制度要求条目 {code.value} 已在本版撤回，交付前需确认",
                    code,
                ))
            else:
                findings.append(Finding(
                    FindingKind.MISSING,
                    f"按 {required.condition}/{required.risk.value} 规则缺少 {code.value}",
                    code,
                ))
        elif decl.completeness is Completeness.UNAVAILABLE:
            # 已有显式 unavailable 提示，不再重复 missing。
            continue

    # 4) 超范围条目（清单里有、制度本版未要求）——信息级，打包按用途再过滤。
    for code in active_map:
        if code not in required.item_codes:
            findings.append(Finding(
                FindingKind.EXTRA_SCOPE,
                f"条目 {code.value} 不在本版最小要求集内，将按用途白名单决定是否交付",
                code,
            ))

    return tuple(findings)


def build_version(
    *,
    revision: int,
    created_at: str,
    declared_by: str,
    facility: str,
    condition: str,
    risk: str,
    change_reason: ChangeReason,
    required: RequiredSet,
    items: Iterable[ItemDeclaration],
    parent_hash: str,
    supersedes: int | None = None,
) -> ManifestVersion:
    """构造新版本并计算哈希链。校验序号连续性是旅程仓储的职责。"""

    items = tuple(items)
    findings = evaluate(required, items)
    body = {
        "revision": revision,
        "declared_by": declared_by,
        "facility": facility,
        "condition": condition,
        "risk": risk,
        "change_reason": change_reason.value,
        "ruleset_version": required.ruleset_version,
        "required_codes": list(required.requirement_hash_parts()),
        "items": [i.canonical() for i in items],
        "parent_hash": parent_hash,
        "supersedes": supersedes,
    }
    version_hash = canonical_hash(body)
    return ManifestVersion(
        revision=revision,
        created_at=created_at,
        declared_by=declared_by,
        facility=facility,
        condition=condition,
        risk=risk,
        change_reason=change_reason,
        ruleset_version=required.ruleset_version,
        required_codes=tuple(required.requirement_hash_parts()),
        items=items,
        parent_hash=parent_hash,
        version_hash=version_hash,
        findings=findings,
        supersedes=supersedes,
    )

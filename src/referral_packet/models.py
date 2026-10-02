"""转诊最小交付的领域模型。

状态迁移说明（新增状态的约定）：
- 清单版本 ``reason`` 取值：``initial`` / ``correction`` / ``withdrawal`` /
  ``remeasure`` / ``escalation``。任何更正、撤回、到院复测、紧急升级都只追加
  新版本，旧版本（尤其已签收版本）原地保留、可审计，不做就地修改。
- 条目完整性 ``completeness`` 取值：``complete`` / ``partial``。缺失不编码进
  条目，而是由校验器以"缺失提示"的形式输出，避免把系统判断伪装成临床声明。
- 交接环节 ``STAGES`` 用于质控定位缺项发生位置，顺序固定，新增环节只能追加。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 交接环节：声明 → 打包 → 传输 → 接收 → 阅读
STAGES: tuple[str, ...] = ("declare", "package", "transmit", "receive", "read")

# 清单版本产生原因
REASONS: tuple[str, ...] = (
    "initial",
    "correction",
    "withdrawal",
    "remeasure",
    "escalation",
)


@dataclass(frozen=True)
class ItemDeclaration:
    """基层医生对单个资料条目的逐项声明。

    系统只记录声明内容并做结构校验，不据声明推断病情。
    """

    item_id: str
    category: str  # 如 vital_signs / ecg / medications / lab_results / imaging / attachment
    source: str  # 采集来源（机构或设备标识）
    collected_at: str  # 采集时间，ISO 8601 带时区
    completeness: str  # complete / partial
    consent: bool  # 患者授权该条目用于本次转诊
    content_hash: str  # 内容摘要，用于冲突检测与完整性核对
    size: int = 0  # 字节数，大附件据此走分片传输


@dataclass(frozen=True)
class ManifestVersion:
    """一次清单版本。版本之间只追加、不回改。"""

    referral_id: str
    version: int
    reason: str  # 见 REASONS
    created_at: str
    item_ids: tuple[str, ...]  # 本版本实际交付的条目（撤回后不再出现）
    declarations: tuple[ItemDeclaration, ...]  # 全量声明，含已撤回条目，供审计
    parent_version: int | None
    note: str = ""


@dataclass(frozen=True)
class Finding:
    """系统给出的缺失/冲突提示。只提示，不替临床下结论。"""

    kind: str  # missing / conflict / consent_absent
    stage: str  # 见 STAGES，定位缺项发生的交接环节
    item_id: str
    detail: str


@dataclass(frozen=True)
class Packet:
    """按用途受限生成的资料包。"""

    packet_id: str
    referral_id: str
    version: int  # 生成时基于的清单版本
    purpose: str  # 用途，决定可见类目范围
    item_ids: tuple[str, ...]
    findings: tuple[Finding, ...] = field(default=())

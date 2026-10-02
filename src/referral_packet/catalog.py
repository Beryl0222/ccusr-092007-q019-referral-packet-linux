"""病种 / 风险规则目录 —— 最小交付策略，不是临床判断。

目录回答的唯一问题是：*按医联体既有制度*，某病种在某风险等级下、
为某用途交付时，清单里至少应出现哪些条目。

它不解读数值、不判断病情轻重；风险等级与升级由临床人员在动作里声明，
系统只据此查表并提示「缺失 / 冲突」。规则随制度版本化（:data:`RULESET_VERSION`），
制度改版时提升版本号，旧清单仍按生成时的规则版本可审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence


class ItemCode(str, Enum):
    """最小交付条目编码。新增编码只能追加；复用既有编码不得改变含义。"""

    VITALS = "vitals"                 # 生命体征（体温/脉搏/呼吸/血压/血氧）
    CHIEF_COMPLAINT = "chief_complaint"
  # 主诉与现病史
    ALLERGIES = "allergies"           # 过敏史（可为「否认」的显式声明）
    MEDICATION = "current_medication"  # 当前用药
    ECG = "ecg_report"                # 心电图结论
    LAB = "lab_summary"               # 检验结论（非原始流水）
    IMAGING = "imaging_report"        # 影像检查结论
    OXYGEN = "oxygen_therapy"         # 氧疗记录
    INFUSION = "infusion_record"      # 处置/输液记录
    CONSCIOUSNESS = "consciousness"   # 意识评分
    PREAUTH = "patient_authorization"  # 患者授权


class RiskLevel(str, Enum):
    ROUTINE = "routine"   # 常规转诊
    URGENT = "urgent"     # 急重倾向
    EMERGENCY = "emergency"  # 紧急升级


class Purpose(str, Enum):
    """资料用途。授权与审计均以此为准，禁止「其他」泛化用途。"""

    TRIAGE = "triage"            # 接诊分诊
    EMERGENCY_RX = "emergency_rx"  # 紧急救治
    INPATIENT = "inpatient"      # 住院收治
    QC = "quality_control"       # 双方质控核查
    PATIENT_VIEW = "patient_view"  # 患者本人查阅


class Duty(str, Enum):
    """县医院接收端职责。资料包按职责最小解密，见 :mod:`referral_packet.packet`。"""

    TRIAGE_DESK = "triage_desk"     # 分诊台：生命体征/主诉/过敏
    EMERGENCY = "emergency_dept"    # 急诊科：救治所需全部
    INPATIENT = "inpatient_dept"    # 住院科室：收治所需
    QC_OFFICER = "qc_officer"       # 质控：元数据/交接时间线，不读临床原文
    PATIENT = "patient"             # 患者本人


# ---------------------------------------------------------------- 制度规则集

#: 规则集版本（机构制度版本）。规则含义变化时 +1。
RULESET_VERSION = 1

#: 任何病种、任何风险、任何交付都必须存在的条目。
UNIVERSAL_ITEMS: frozenset[ItemCode] = frozenset(
    {ItemCode.VITALS, ItemCode.ALLERGIES, ItemCode.PREAUTH}
)

#: 各病种的基线条目（routine）。
CONDITION_BASELINE: Mapping[str, frozenset[ItemCode]] = {
    "chest_pain": frozenset(
        {ItemCode.CHIEF_COMPLAINT, ItemCode.MEDICATION, ItemCode.ECG, ItemCode.LAB}
    ),
    "stroke_screen": frozenset(
        {
            ItemCode.CHIEF_COMPLAINT,
            ItemCode.CONSCIOUSNESS,
            ItemCode.MEDICATION,
            ItemCode.IMAGING,
            ItemCode.LAB,
        }
    ),
    "respiratory_distress": frozenset(
        {
            ItemCode.CHIEF_COMPLAINT,
            ItemCode.OXYGEN,
            ItemCode.LAB,
            ItemCode.IMAGING,
        }
    ),
    "general_transfer": frozenset({ItemCode.CHIEF_COMPLAINT, ItemCode.MEDICATION}),
}

#: 风险升级时在基线之上追加的条目；紧急升级不再扩充检查类条目
#: （紧急时缺检查只提示，不以表单阻塞救治）——但救治用途需要的处置记录必带。
RISK_ADDITIONS: Mapping[RiskLevel, frozenset[ItemCode]] = {
    RiskLevel.ROUTINE: frozenset(),
    RiskLevel.URGENT: frozenset({ItemCode.INFUSION}),
    RiskLevel.EMERGENCY: frozenset({ItemCode.INFUSION, ItemCode.OXYGEN}),
}

#: 用途 -> 该用途允许交付的条目白名单（用途受限，即使清单里有也不超发）。
PURPOSE_SCOPE: Mapping[Purpose, frozenset[ItemCode]] = {
    Purpose.TRIAGE: frozenset(
        {
            ItemCode.VITALS,
            ItemCode.CHIEF_COMPLAINT,
            ItemCode.ALLERGIES,
            ItemCode.CONSCIOUSNESS,
            ItemCode.OXYGEN,
            ItemCode.PREAUTH,
        }
    ),
    Purpose.EMERGENCY_RX: frozenset(
        {
            ItemCode.VITALS,
            ItemCode.CHIEF_COMPLAINT,
            ItemCode.ALLERGIES,
            ItemCode.MEDICATION,
            ItemCode.ECG,
            ItemCode.LAB,
            ItemCode.IMAGING,
            ItemCode.OXYGEN,
            ItemCode.INFUSION,
            ItemCode.CONSCIOUSNESS,
            ItemCode.PREAUTH,
        }
    ),
    Purpose.INPATIENT: frozenset(
        {
            ItemCode.VITALS,
            ItemCode.CHIEF_COMPLAINT,
            ItemCode.ALLERGIES,
            ItemCode.MEDICATION,
            ItemCode.ECG,
            ItemCode.LAB,
            ItemCode.IMAGING,
            ItemCode.OXYGEN,
            ItemCode.INFUSION,
            ItemCode.CONSCIOUSNESS,
            ItemCode.PREAUTH,
        }
    ),
    # 质控与患者查阅走独立投影（元数据 / 审计记录），不取条目原文。
    Purpose.QC: frozenset(),
    Purpose.PATIENT_VIEW: frozenset(),
}

#: 职责 -> 默认可服务的用途。
DUTY_PURPOSES: Mapping[Duty, frozenset[Purpose]] = {
    Duty.TRIAGE_DESK: frozenset({Purpose.TRIAGE}),
    Duty.EMERGENCY: frozenset({Purpose.TRIAGE, Purpose.EMERGENCY_RX}),
    Duty.INPATIENT: frozenset({Purpose.INPATIENT}),
    Duty.QC_OFFICER: frozenset({Purpose.QC}),
    Duty.PATIENT: frozenset({Purpose.PATIENT_VIEW}),
}

#: 职责在各用途内可见的条目（职责最小化：分诊台即使走紧急用途也看不到检验原文）。
DUTY_ITEM_VIEW: Mapping[Duty, frozenset[ItemCode]] = {
    Duty.TRIAGE_DESK: frozenset(
        {
            ItemCode.VITALS,
            ItemCode.CHIEF_COMPLAINT,
            ItemCode.ALLERGIES,
            ItemCode.CONSCIOUSNESS,
            ItemCode.OXYGEN,
            ItemCode.PREAUTH,
        }
    ),
    Duty.EMERGENCY: frozenset(set(PURPOSE_SCOPE[Purpose.EMERGENCY_RX])),
    Duty.INPATIENT: frozenset(set(PURPOSE_SCOPE[Purpose.INPATIENT])),
    Duty.QC_OFFICER: frozenset(),
    Duty.PATIENT: frozenset(),
}


@dataclass(frozen=True)
class RequiredSet:
    """一次交付的「制度要求集」。"""

    condition: str
    risk: RiskLevel
    ruleset_version: int
    item_codes: frozenset[ItemCode] = field(hash=False)

    def requirement_hash_parts(self) -> tuple[str, ...]:
        return tuple(sorted(c.value for c in self.item_codes))


def required_items(condition: str, risk: RiskLevel) -> RequiredSet:
    """按病种 + 风险等级求最小条目集。

    未知病种回落 ``general_transfer`` —— 回落本身是制度策略，调用方可据返回的
    ``condition`` 记录实际命中的规则；系统不推断病种。
    """

    base = CONDITION_BASELINE.get(condition)
    resolved = condition
    if base is None:
        base = CONDITION_BASELINE["general_transfer"]
        resolved = "general_transfer"
    items = UNIVERSAL_ITEMS | base | RISK_ADDITIONS[risk]
    return RequiredSet(
        condition=resolved,
        risk=risk,
        ruleset_version=RULESET_VERSION,
        item_codes=items,
    )


def purpose_allows(purpose: Purpose, code: ItemCode) -> bool:
    return code in PURPOSE_SCOPE[purpose]


def duty_may_serve(duty: Duty, purpose: Purpose) -> bool:
    return purpose in DUTY_PURPOSES.get(duty, frozenset())


def duty_visible_codes(duty: Duty, purpose: Purpose) -> frozenset[ItemCode]:
    """职责 ∩ 用途的条目可见集（两者白名单的交集）。"""

    return DUTY_ITEM_VIEW.get(duty, frozenset()) & PURPOSE_SCOPE.get(purpose, frozenset())


def all_purposes() -> Sequence[Purpose]:
    return tuple(Purpose)

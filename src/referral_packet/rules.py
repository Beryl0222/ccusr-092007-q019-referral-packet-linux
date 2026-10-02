"""病种与风险规则：决定某用途下资料包应包含的最小条目集合。

规则只做结构匹配（病种、风险等级、用途三者查表），输出"应当交付哪些类目"
与"该用途允许看哪些类目"。规则本身不包含、也不输出任何病情判断；
未知病种/风险组合返回空集，由临床人员人工决定，系统不猜。
"""

from __future__ import annotations

# (病种编码, 风险等级) -> 必需资料类目
REQUIRED_BY_CONDITION: dict[tuple[str, str], frozenset[str]] = {
    ("chest_pain", "high"): frozenset(
        {"vital_signs", "ecg", "medications", "lab_results"}
    ),
    ("chest_pain", "medium"): frozenset({"vital_signs", "ecg", "medications"}),
    ("stroke", "high"): frozenset(
        {"vital_signs", "imaging", "medications", "lab_results"}
    ),
    ("fracture", "medium"): frozenset({"vital_signs", "imaging"}),
    ("fracture", "low"): frozenset({"imaging"}),
}

# 用途 -> 允许访问的类目（用途受限：即使清单里有，超出用途也不进包）
PURPOSE_ALLOWLIST: dict[str, frozenset[str]] = {
    "transfer_receiving": frozenset(
        {
            "vital_signs",
            "ecg",
            "medications",
            "lab_results",
            "imaging",
            "attachment",
        }
    ),
    "emergency_escalation": frozenset({"vital_signs", "ecg", "medications"}),
    "follow_up": frozenset({"vital_signs", "lab_results"}),
    # 质控只看元数据与交接轨迹，不看任何临床内容
    "qc_review": frozenset(),
}


def required_categories(disease_code: str, risk_level: str) -> frozenset[str]:
    """该病种+风险等级下应当交付的类目。未知组合返回空集。"""
    return REQUIRED_BY_CONDITION.get((disease_code, risk_level), frozenset())


def allowed_categories(purpose: str) -> frozenset[str]:
    """该用途允许访问的类目。未知用途一律不允许。"""
    return PURPOSE_ALLOWLIST.get(purpose, frozenset())


def packet_categories(
    disease_code: str, risk_level: str, purpose: str
) -> frozenset[str]:
    """本次打包的类目范围 = 病情必需 ∩ 用途允许。"""
    return required_categories(disease_code, risk_level) & allowed_categories(purpose)

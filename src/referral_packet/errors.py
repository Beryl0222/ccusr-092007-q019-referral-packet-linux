"""领域异常。所有失败都带结构化上下文，便于调用方按码处理与审计。"""

from __future__ import annotations


class ReferralError(Exception):
    """全部领域错误的基类，``code`` 稳定可用于判定分支。"""

    code = "referral_error"

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.context = context


class RuleViolation(ReferralError):
    """请求违反业务约束（非鉴权类），如重复版本号、条目未获授权。"""

    code = "rule_violation"


class ManifestConflict(RuleViolation):
    """同一事实存在互相矛盾的来源/时间声明。系统只提示，不裁决。"""

    code = "manifest_conflict"


class IntegrityError(RuleViolation):
    """分片哈希不符、清单哈希链断裂等完整性问题。"""

    code = "integrity_error"


class AccessDenied(ReferralError):
    """职责或用途不允许访问该资料包/字段/历史转诊。"""

    code = "access_denied"


class NotFound(ReferralError):
    """转诊、版本、上传会话或分片不存在。"""

    code = "not_found"


class ImmutableViolation(RuleViolation):
    """试图修改已签收（或已撤回）的不可变版本。"""

    code = "immutable_violation"

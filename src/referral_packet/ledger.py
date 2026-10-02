"""清单版本台账。

更正报告、撤回非必要附件、到院复测、紧急升级都通过 append_version 产生
新版本；旧版本永不修改。已签收版本（sign_off 记录过回执的版本）继续
完整保留，审计时可按版本号原样取回。
"""

from __future__ import annotations

from .models import REASONS, ItemDeclaration, ManifestVersion


class LedgerError(ValueError):
    pass


class ManifestLedger:
    def __init__(self) -> None:
        # referral_id -> 按版本号升序的不可变清单
        self._versions: dict[str, list[ManifestVersion]] = {}
        # (referral_id, version) -> 签收记录列表
        self._sign_offs: dict[tuple[str, int], list[dict]] = {}

    def append_version(
        self,
        referral_id: str,
        *,
        reason: str,
        created_at: str,
        item_ids: tuple[str, ...],
        declarations: tuple[ItemDeclaration, ...],
        note: str = "",
    ) -> ManifestVersion:
        """追加新版本。reason 必须是已登记的产生原因之一。"""
        if reason not in REASONS:
            raise LedgerError(f"未知版本原因: {reason}")
        history = self._versions.setdefault(referral_id, [])
        version = ManifestVersion(
            referral_id=referral_id,
            version=len(history) + 1,
            reason=reason,
            created_at=created_at,
            item_ids=tuple(item_ids),
            declarations=tuple(declarations),
            parent_version=len(history) or None,
            note=note,
        )
        history.append(version)
        return version

    def get(self, referral_id: str, version: int) -> ManifestVersion:
        """按版本号取回任意历史版本，包括已被取代的已签收版本。"""
        history = self._versions.get(referral_id, [])
        if not 1 <= version <= len(history):
            raise LedgerError(f"转诊 {referral_id} 不存在版本 {version}")
        return history[version - 1]

    def latest(self, referral_id: str) -> ManifestVersion:
        history = self._versions.get(referral_id, [])
        if not history:
            raise LedgerError(f"转诊 {referral_id} 尚无清单版本")
        return history[-1]

    def sign_off(
        self, referral_id: str, version: int, *, by: str, at: str
    ) -> None:
        """登记某版本的签收回执。签收不修改版本内容，只追加回执记录。"""
        self.get(referral_id, version)  # 确认版本存在
        self._sign_offs.setdefault((referral_id, version), []).append(
            {"by": by, "at": at}
        )

    def sign_offs(self, referral_id: str, version: int) -> tuple[dict, ...]:
        return tuple(self._sign_offs.get((referral_id, version), ()))

    def is_signed(self, referral_id: str, version: int) -> bool:
        return bool(self._sign_offs.get((referral_id, version)))

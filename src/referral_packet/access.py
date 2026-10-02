"""按职责解密、所见版本回执与超时升级。

加密在此处是合同级模拟：用角色密钥派生密钥流做异或封装，演示"按职责
分发可读范围"的访问结构；生产实现应替换为 KMS 托管密钥 + AEAD。
超时升级只产生通知事件，绝不自动扩大资料范围（scope_expanded 恒为 False，
扩大范围只能由临床人员通过新的清单版本显式完成）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# 角色 -> 可解密的资料类目
ROLE_CATEGORY_POLICY: dict[str, frozenset[str]] = {
    "receiving_physician": frozenset(
        {"vital_signs", "ecg", "medications", "lab_results", "imaging"}
    ),
    "receiving_nurse": frozenset({"vital_signs", "medications"}),
    "medical_records_clerk": frozenset({"attachment"}),
    "qc_reviewer": frozenset(),  # 只看元数据
}


class AccessDenied(PermissionError):
    pass


@dataclass(frozen=True)
class SealedItem:
    item_id: str
    category: str
    ciphertext: bytes
    nonce: str


@dataclass(frozen=True)
class SealedPacket:
    referral_id: str
    version: int
    purpose: str
    items: tuple[SealedItem, ...]


def _keystream(role_key: str, nonce: str, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(
            hashlib.sha256(f"{role_key}:{nonce}:{counter}".encode()).digest()
        )
        counter += 1
    return bytes(out[:length])


def seal_packet(
    referral_id: str,
    version: int,
    purpose: str,
    items: tuple[tuple[str, str, bytes], ...],  # (item_id, category, plaintext)
    *,
    role_keys: dict[str, str],
    nonce: str,
) -> SealedPacket:
    """按类目对应的角色集合封装资料包。每个类目用其可访问角色的派生密钥加密。"""
    sealed: list[SealedItem] = []
    for item_id, category, plaintext in items:
        roles = [
            role
            for role, cats in ROLE_CATEGORY_POLICY.items()
            if category in cats and role in role_keys
        ]
        if not roles:
            raise AccessDenied(f"类目 {category} 没有任何在册角色可解密")
        # 以可解密角色集合派生封装密钥（模拟信封加密中的按角色包裹）
        wrap_key = hashlib.sha256(
            ("|".join(sorted(roles)) + nonce).encode()
        ).hexdigest()
        stream = _keystream(wrap_key, nonce, len(plaintext))
        ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream))
        sealed.append(
            SealedItem(item_id=item_id, category=category, ciphertext=ciphertext, nonce=nonce)
        )
    return SealedPacket(
        referral_id=referral_id, version=version, purpose=purpose, items=tuple(sealed)
    )


def open_item(packet: SealedPacket, item_id: str, *, role: str, role_keys: dict[str, str]) -> bytes:
    """按职责解密单个条目：角色不在该类目政策内即拒绝。"""
    for item in packet.items:
        if item.item_id == item_id:
            allowed = ROLE_CATEGORY_POLICY.get(role, frozenset())
            if item.category not in allowed or role not in role_keys:
                raise AccessDenied(f"角色 {role} 无权解密类目 {item.category}")
            roles = [
                r
                for r, cats in ROLE_CATEGORY_POLICY.items()
                if item.category in cats and r in role_keys
            ]
            wrap_key = hashlib.sha256(
                ("|".join(sorted(roles)) + item.nonce).encode()
            ).hexdigest()
            stream = _keystream(wrap_key, item.nonce, len(item.ciphertext))
            return bytes(a ^ b for a, b in zip(item.ciphertext, stream))
    raise AccessDenied(f"资料包中不存在条目 {item_id}")


@dataclass(frozen=True)
class Receipt:
    """接收方回执：谁、以什么角色、看到了哪个版本、哪些条目。"""

    referral_id: str
    version: int
    user: str
    role: str
    purpose: str
    item_ids: tuple[str, ...]
    at: str


@dataclass
class ReceiptLedger:
    receipts: list[Receipt] = field(default_factory=list)

    def record(self, receipt: Receipt) -> None:
        self.receipts.append(receipt)

    def has_read(self, referral_id: str, version: int) -> bool:
        return any(
            r.referral_id == referral_id and r.version == version
            for r in self.receipts
        )


@dataclass(frozen=True)
class Escalation:
    """超时未读升级事件。只通知，不扩大资料范围。"""

    referral_id: str
    version: int
    triggered_at: str
    notify_roles: tuple[str, ...]
    scope_expanded: bool = False  # 合同约束：升级永不自动扩范围


def check_read_timeouts(
    pending: tuple[tuple[str, int, str], ...],  # (referral_id, version, delivered_at)
    receipts: ReceiptLedger,
    *,
    now: str,
    timeout: timedelta,
    notify_roles: tuple[str, ...] = ("receiving_physician", "duty_manager"),
) -> tuple[Escalation, ...]:
    """对已送达但超时未读的版本生成升级事件。"""
    now_dt = datetime.fromisoformat(now)
    escalations: list[Escalation] = []
    for referral_id, version, delivered_at in pending:
        if receipts.has_read(referral_id, version):
            continue
        if now_dt - datetime.fromisoformat(delivered_at) >= timeout:
            escalations.append(
                Escalation(
                    referral_id=referral_id,
                    version=version,
                    triggered_at=now,
                    notify_roles=notify_roles,
                )
            )
    return tuple(escalations)

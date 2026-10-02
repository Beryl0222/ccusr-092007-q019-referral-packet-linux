"""按职责封装的加密封装（仿真实现）。

.. warning::
    本模块的密码学部分是**流程仿真**（SHA-256 派生的异或流），用于在无外部
    KMS 的环境里验证「按职责封装、按用途解封、明文不落日志」的合同；生产部署
    必须替换为机构 KMS / 国密合规实现，并保持 :class:`SealedEnvelope` 的字段不变。

封装头绑定用途（purpose）：即便拿错密钥，用途不符也拒绝解封。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Mapping

from referral_packet.catalog import Duty, Purpose
from referral_packet.errors import AccessDenied

CIPHER_SUITE = "SIMULATED-XOR-SHA256/v1"  # 仿真标识，禁止在生产当作真实算法


class DutyKeyring:
    """各职责的密钥注册表。每个职责独立密钥，科室只能解自己那一份封装密钥。"""

    def __init__(self) -> None:
        self._keys: dict[Duty, dict[str, bytes]] = {}

    def issue(self, duty: Duty, key_id: str | None = None) -> str:
        key_id = key_id or f"k-{duty.value}-{len(self._keys.setdefault(duty, {})) + 1}"
        self._keys.setdefault(duty, {})[key_id] = os.urandom(32)
        return key_id

    def key(self, duty: Duty, key_id: str) -> bytes:
        try:
            return self._keys[duty][key_id]
        except KeyError:
            raise AccessDenied("该职责无匹配密钥", duty=duty.value, key_id=key_id) from None

    def active_key_id(self, duty: Duty) -> str:
        ids = self._keys.get(duty, {})
        if not ids:
            raise AccessDenied("职责尚未签发密钥", duty=duty.value)
        return sorted(ids)[-1]


@dataclass(frozen=True)
class SealedEnvelope:
    envelope_id: str
    purpose: Purpose
    content_sha256: str
    wrapped_keys: Mapping[Duty, str]   # duty -> 封装后的会话密钥（含 key_id 前缀）
    ciphertext: bytes
    suite: str = CIPHER_SUITE

    def recipient_duties(self) -> frozenset[Duty]:
        return frozenset(self.wrapped_keys)


# ----------------------------------------------------------------- 仿真原语

def _keystream(session_key: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hashlib.sha256(session_key + counter.to_bytes(8, "big")).digest())
        counter += 1
    return bytes(out[:length])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def _wrap(session_key: bytes, kek: bytes) -> str:
    stream = hashlib.sha256(kek + b"wrap").digest()
    return _xor(session_key, _keystream(stream, len(session_key))).hex()


def _unwrap(wrapped: bytes, kek: bytes) -> bytes:
    stream = hashlib.sha256(kek + b"wrap").digest()
    return _xor(wrapped, _keystream(stream, len(wrapped)))


def seal(plaintext: bytes, *, purpose: Purpose,
         recipients: Mapping[Duty, str], keyring: DutyKeyring) -> SealedEnvelope:
    """对一个条目正文封装；``recipients`` 为 duty -> key_id 的最小接收者集合。"""

    if not recipients:
        raise AccessDenied("封装至少需要一个接收职责")
    content_hash = hashlib.sha256(plaintext).hexdigest()
    session_key = os.urandom(32)
    wrapped: dict[Duty, str] = {}
    for duty, key_id in recipients.items():
        kek = keyring.key(duty, key_id)
        wrapped[duty] = key_id + ":" + _wrap(session_key, kek)
    ciphertext = _xor(plaintext, _keystream(session_key, len(plaintext)))
    # 每次封装生成随机 nonce：即便内容/用途/接收者完全相同，两次封装也不撞 id。
    nonce = os.urandom(16)
    envelope_id = hashlib.sha256(
        content_hash.encode() + b"|" + purpose.value.encode()
        + b"|" + b",".join(sorted(d.value.encode() for d in wrapped))
        + b"|" + nonce
    ).hexdigest()[:32]
    return SealedEnvelope(
        envelope_id=envelope_id,
        purpose=purpose,
        content_sha256=content_hash,
        wrapped_keys=wrapped,
        ciphertext=ciphertext,
    )


def open_envelope(envelope: SealedEnvelope, *, duty: Duty, key_id: str,
                  purpose: Purpose, keyring: DutyKeyring) -> bytes:
    """接收科室按职责解封。三重检查：接收职责、用途、密钥。"""

    if purpose is not envelope.purpose:
        raise AccessDenied(
            "封装用途与本次访问用途不符",
            envelope_purpose=envelope.purpose.value, requested=purpose.value,
        )
    wrapped = envelope.wrapped_keys.get(duty)
    if wrapped is None:
        raise AccessDenied("该职责不是此封装的接收者", duty=duty.value)
    if not wrapped.startswith(key_id + ":"):
        raise AccessDenied("封装密钥版本与科室密钥不匹配", duty=duty.value)
    kek = keyring.key(duty, key_id)
    session_key = _unwrap(bytes.fromhex(wrapped.split(":", 1)[1]), kek)
    plaintext = _xor(envelope.ciphertext, _keystream(session_key, len(envelope.ciphertext)))
    if hashlib.sha256(plaintext).hexdigest() != envelope.content_sha256:
        raise AccessDenied("解封后完整性校验失败")
    return plaintext

"""大文件分片续传与转诊推送去重。

- ChunkedUpload：分片失败后可按 missing() 续传，已收分片不重复接收；
  收齐后按内容摘要整体核对。
- PushGateway：旅程以 referral_id 为键。同一转诊的重复推送（无论是否
  携带相同幂等键）都并入既有旅程，绝不生成第二条旅程。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


class TransferError(ValueError):
    pass


class ChunkedUpload:
    def __init__(
        self,
        upload_id: str,
        referral_id: str,
        *,
        total_chunks: int,
        expected_hash: str,
    ) -> None:
        if total_chunks <= 0:
            raise TransferError("分片数必须为正")
        self.upload_id = upload_id
        self.referral_id = referral_id
        self.total_chunks = total_chunks
        self.expected_hash = expected_hash
        self._chunks: dict[int, bytes] = {}

    def receive(self, index: int, data: bytes) -> None:
        """接收一个分片；重复投递同一序号视为幂等成功。"""
        if not 0 <= index < self.total_chunks:
            raise TransferError(f"分片序号越界: {index}")
        self._chunks.setdefault(index, data)

    def missing(self) -> tuple[int, ...]:
        """尚未收到的分片序号，续传方据此只补传缺口。"""
        return tuple(i for i in range(self.total_chunks) if i not in self._chunks)

    @property
    def complete(self) -> bool:
        return not self.missing()

    def assemble(self) -> bytes:
        """收齐后拼装并核对整体摘要，不一致则拒绝交付。"""
        if not self.complete:
            raise TransferError(f"分片未收齐，缺 {self.missing()}")
        blob = b"".join(self._chunks[i] for i in range(self.total_chunks))
        digest = hashlib.sha256(blob).hexdigest()
        if digest != self.expected_hash:
            raise TransferError("整体内容摘要不一致")
        return blob


@dataclass
class Journey:
    """一条转诊旅程：同一转诊的所有推送都归并到这里。"""

    referral_id: str
    journey_id: str
    pushes: list[dict] = field(default_factory=list)
    duplicate_attempts: int = 0


class PushGateway:
    def __init__(self) -> None:
        self._journeys: dict[str, Journey] = {}
        self._seen_keys: set[tuple[str, str]] = set()

    def push(
        self,
        referral_id: str,
        *,
        idempotency_key: str,
        packet_id: str,
        version: int,
        at: str,
    ) -> Journey:
        """推送资料包。同一转诊重复推送返回既有旅程，不新建。"""
        journey = self._journeys.get(referral_id)
        if journey is None:
            journey = Journey(referral_id=referral_id, journey_id=f"jr-{referral_id}")
            self._journeys[referral_id] = journey
        key = (referral_id, idempotency_key)
        if key in self._seen_keys:
            journey.duplicate_attempts += 1
            return journey
        self._seen_keys.add(key)
        journey.pushes.append(
            {
                "idempotency_key": idempotency_key,
                "packet_id": packet_id,
                "version": version,
                "at": at,
            }
        )
        return journey

    def journey(self, referral_id: str) -> Journey | None:
        return self._journeys.get(referral_id)

    def journey_count(self) -> int:
        return len(self._journeys)

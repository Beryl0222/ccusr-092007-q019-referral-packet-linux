"""测试公用构造。"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import hashlib

from referral_packet.clock import FixedClock
from referral_packet.manifest import Completeness, ItemDeclaration
from referral_packet.catalog import ItemCode
from referral_packet.service import ReferralPacketService


def make_service(start: str = "2026-09-20T09:00:00+08:00") -> tuple[ReferralPacketService, FixedClock]:
    clock = FixedClock(datetime.fromisoformat(start))
    return ReferralPacketService(clock=clock), clock


def decl(
    code: ItemCode,
    *,
    source: str = "门诊病历",
    collected_at: str = "2026-09-20T08:40:00+08:00",
    completeness: Completeness = Completeness.COMPLETE,
    authorized: bool = True,
    content_ref: str | None = None,
    content_sha256: str | None = None,
    withdrawn: bool = False,
    note: str = "",
) -> ItemDeclaration:
    return ItemDeclaration(
        code=code,
        source=source,
        collected_at=collected_at,
        completeness=completeness,
        patient_authorized=authorized,
        content_ref=content_ref,
        content_sha256=content_sha256,
        withdrawn=withdrawn,
        note=note,
    )


def sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def upload_item(
    service: ReferralPacketService,
    *,
    referral_key: str,
    patient_id: str,
    code: ItemCode,
    blob: bytes,
    chunk_size: int = 64,
    fail_chunk: int | None = None,
) -> str:
    """分片上传一个条目，返回 artifact_id；fail_chunk 模拟该分片首次失败后续传。"""
    whole = sha(blob)
    status = service.open_upload(
        referral_key=referral_key,
        patient_id=patient_id,
        item_hint=code.value,
        filename=f"{code.value}.bin",
        total_size=len(blob),
        chunk_size=chunk_size,
        whole_sha256=whole,
    )
    upload_id = status["upload_id"]
    total = (len(blob) + chunk_size - 1) // chunk_size
    for index in range(total):
        piece = blob[index * chunk_size:(index + 1) * chunk_size]
        if index == fail_chunk:
            # 申报错误哈希触发失败：分片被拒收，session_status 里该序号仍缺失。
            try:
                service.upload_chunk(upload_id, index, piece, chunk_sha256="0" * 64)
            except Exception:
                pass
            status = service.upload_status(upload_id)
            assert index in status["missing"]
        service.upload_chunk(upload_id, index, piece, chunk_sha256=sha(piece))
    artifact = service.assemble_upload(upload_id)
    return artifact.artifact_id

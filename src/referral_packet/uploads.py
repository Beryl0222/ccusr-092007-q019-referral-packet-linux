"""大文件分片上传：失败可续传、内容寻址。

一个上传会话把文件切成定长分片；任一分片失败（网络中断或哈希不符）后，
客户端凭 :meth:`UploadManager.session_status` 得到缺失分片序号，只需重传缺失部分。
全部到齐后组装，按整体 SHA-256 校验生成**内容寻址**产物（artifact），
清单条目只引用 artifact id —— 同一文件被多次推送不会产生两份正文。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum

from referral_packet.errors import IntegrityError, NotFound, RuleViolation


class UploadStatus(str, Enum):
    OPEN = "open"
    ASSEMBLED = "assembled"
    ABORTED = "aborted"


@dataclass(frozen=True)
class ChunkRecord:
    index: int
    sha256: str
    size: int


@dataclass(frozen=True)
class Artifact:
    artifact_id: str   # 即整体内容 SHA-256
    size: int
    item_hint: str     # 该产物预期对应的条目编码（由声明环节最终确认）
    created_at: str


@dataclass
class UploadSession:
    upload_id: str
    referral_id: str
    item_hint: str
    filename: str
    total_size: int
    chunk_size: int
    total_chunks: int
    whole_sha256: str                 # 客户端申报的整体哈希，组装时复核
    created_at: str
    chunks: dict[int, ChunkRecord] = field(default_factory=dict)
    status: UploadStatus = UploadStatus.OPEN
    artifact_id: str | None = None

    def missing_indexes(self) -> list[int]:
        return [i for i in range(self.total_chunks) if i not in self.chunks]


class UploadManager:
    """内存态会话与产物库；接口即存储合同，替换实现时保持幂等语义。"""

    def __init__(self) -> None:
        self._sessions: dict[str, UploadSession] = {}
        self._blobs: dict[str, bytes] = {}      # artifact_id -> bytes
        self._artifacts: dict[str, Artifact] = {}
        # (upload_id, index) -> 分片正文；组装后可随会话清理，保留至 abort 以便重试。
        self._chunk_bytes: dict[tuple[str, int], bytes] = {}

    # ------------------------------------------------------------- 会话生命周期

    def open_session(
        self,
        *,
        upload_id: str,
        referral_id: str,
        item_hint: str,
        filename: str,
        total_size: int,
        chunk_size: int,
        whole_sha256: str,
        created_at: str,
    ) -> UploadSession:
        if upload_id in self._sessions:
            raise RuleViolation("上传会话标识已存在", upload_id=upload_id)
        if chunk_size <= 0 or total_size < 0:
            raise RuleViolation("分片参数非法")
        total_chunks = (total_size + chunk_size - 1) // chunk_size if total_size else 0
        if len(whole_sha256) != 64:
            raise RuleViolation("整体哈希必须是 64 位十六进制 SHA-256")
        session = UploadSession(
            upload_id=upload_id,
            referral_id=referral_id,
            item_hint=item_hint,
            filename=filename,
            total_size=total_size,
            chunk_size=chunk_size,
            total_chunks=max(total_chunks, 0),
            whole_sha256=whole_sha256,
            created_at=created_at,
        )
        self._sessions[upload_id] = session
        return session

    def get_session(self, upload_id: str) -> UploadSession:
        try:
            return self._sessions[upload_id]
        except KeyError:
            raise NotFound("上传会话不存在", upload_id=upload_id) from None

    def session_status(self, upload_id: str) -> dict:
        """续传查询：已到/缺失分片一目了然。"""
        session = self.get_session(upload_id)
        return {
            "upload_id": upload_id,
            "status": session.status.value,
            "total_chunks": session.total_chunks,
            "received": sorted(session.chunks),
            "missing": session.missing_indexes(),
            "artifact_id": session.artifact_id,
        }

    # ------------------------------------------------------------------- 分片

    def put_chunk(self, upload_id: str, index: int, data: bytes,
                  chunk_sha256: str | None = None) -> ChunkRecord:
        session = self._require_open(upload_id)
        if not 0 <= index < session.total_chunks:
            raise RuleViolation(
                "分片序号越界", index=index, total_chunks=session.total_chunks
            )
        actual = hashlib.sha256(data).hexdigest()
        if chunk_sha256 is not None and chunk_sha256.lower() != actual:
            # 不计入该分片：调用方修正后可凭同一序号重传（续传）。
            raise IntegrityError(
                "分片哈希不符，已拒绝该分片，可重传",
                upload_id=upload_id, index=index,
            )
        prior = session.chunks.get(index)
        if prior is not None:
            if prior.sha256 != actual:
                raise IntegrityError(
                    "同一序号已存在不同内容的分片", upload_id=upload_id, index=index
                )
            return prior  # 幂等重试：完全相同直接成功
        session.chunks[index] = ChunkRecord(index=index, sha256=actual, size=len(data))
        self._chunk_bytes[(upload_id, index)] = data
        return session.chunks[index]

    # ------------------------------------------------------------------- 组装

    def assemble(self, upload_id: str, assembled_at: str) -> Artifact:
        session = self._require_open(upload_id)
        missing = session.missing_indexes()
        if missing:
            raise IntegrityError(
                "尚有分片缺失，不能组装；请续传缺失分片",
                upload_id=upload_id, missing=missing,
            )
        blob = b"".join(
            self._read_chunk(session, i) for i in range(session.total_chunks)
        )
        whole = hashlib.sha256(blob).hexdigest()
        if whole != session.whole_sha256.lower():
            raise IntegrityError(
                "组装后整体哈希与申报不符",
                upload_id=upload_id, expected=session.whole_sha256, actual=whole,
            )
        artifact = self._store_artifact(blob, session.item_hint, assembled_at)
        session.status = UploadStatus.ASSEMBLED
        session.artifact_id = artifact.artifact_id
        return artifact

    def abort(self, upload_id: str) -> None:
        session = self.get_session(upload_id)
        if session.status is UploadStatus.ASSEMBLED:
            raise RuleViolation("已组装产物的会话不可中止（产物不可变）",
                                upload_id=upload_id)
        session.status = UploadStatus.ABORTED

    # ------------------------------------------------------------------- 取用

    def get_artifact(self, artifact_id: str) -> Artifact:
        try:
            return self._artifacts[artifact_id]
        except KeyError:
            raise NotFound("产物不存在", artifact_id=artifact_id) from None

    def read_blob(self, artifact_id: str) -> bytes:
        self.get_artifact(artifact_id)
        return self._blobs[artifact_id]

    def has_blob(self, artifact_id: str) -> bool:
        return artifact_id in self._blobs

    def _store_artifact(self, blob: bytes, item_hint: str, created_at: str) -> Artifact:
        artifact_id = hashlib.sha256(blob).hexdigest()
        existing = self._artifacts.get(artifact_id)
        if existing is not None:
            return existing  # 内容寻址：同一正文全库一份
        self._blobs[artifact_id] = blob
        artifact = Artifact(artifact_id=artifact_id, size=len(blob),
                            item_hint=item_hint, created_at=created_at)
        self._artifacts[artifact_id] = artifact
        return artifact

    def _require_open(self, upload_id: str) -> UploadSession:
        session = self.get_session(upload_id)
        if session.status is not UploadStatus.OPEN:
            raise RuleViolation(
                "上传会话已关闭", upload_id=upload_id, status=session.status.value
            )
        return session

    def _read_chunk(self, session: UploadSession, index: int) -> bytes:
        return self._chunk_bytes[(session.upload_id, index)]

"""分片上传：失败续传、乱序、哈希校验、内容寻址去重。"""

import unittest

from referral_packet.clock import FixedClock
from referral_packet.errors import IntegrityError, RuleViolation
from referral_packet.uploads import UploadManager, UploadStatus
from datetime import datetime

from tests._helpers import sha


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.mgr = UploadManager()
        self.now = "2026-09-20T09:00:00+08:00"
        self.blob = bytes(range(256)) * 4  # 1024 字节
        self.sid = self.mgr.open_session(
            upload_id="u1", referral_id="r1", item_hint="vitals",
            filename="v.bin", total_size=len(self.blob), chunk_size=100,
            whole_sha256=sha(self.blob), created_at=self.now,
        ).upload_id

    def test_chunk_failure_then_resume(self):
        # 第 2 片传错（申报哈希与实际内容不符）被拒收。
        with self.assertRaises(IntegrityError):
            self.mgr.put_chunk(self.sid, 2, b"wrong", chunk_sha256="0" * 64)
        status = self.mgr.session_status(self.sid)
        self.assertIn(2, status["missing"])

        # 其余分片先到齐（乱序），仅第 2 片缺失时不能组装。
        total = self.mgr.get_session(self.sid).total_chunks
        for i in range(total):
            if i == 2:
                continue
            piece = self.blob[i * 100:(i + 1) * 100]
            self.mgr.put_chunk(self.sid, i, piece, chunk_sha256=sha(piece))
        with self.assertRaises(IntegrityError):
            self.mgr.assemble(self.sid, self.now)

        # 只补传缺失分片即可续传成功。
        piece = self.blob[200:300]
        self.mgr.put_chunk(self.sid, 2, piece, chunk_sha256=sha(piece))
        artifact = self.mgr.assemble(self.sid, self.now)
        self.assertEqual(artifact.artifact_id, sha(self.blob))
        self.assertEqual(self.mgr.read_blob(artifact.artifact_id), self.blob)

    def test_duplicate_chunk_put_is_idempotent(self):
        piece = self.blob[:100]
        first = self.mgr.put_chunk(self.sid, 0, piece, chunk_sha256=sha(piece))
        second = self.mgr.put_chunk(self.sid, 0, piece, chunk_sha256=sha(piece))
        self.assertEqual(first, second)
        with self.assertRaises(IntegrityError):
            self.mgr.put_chunk(self.sid, 0, b"x", chunk_sha256=sha(b"x"))

    def test_index_out_of_range(self):
        with self.assertRaises(RuleViolation):
            self.mgr.put_chunk(self.sid, 99, b"x", chunk_sha256=sha(b"x"))

    def test_whole_hash_mismatch_rejected(self):
        bad = self.mgr.open_session(
            upload_id="u-bad", referral_id="r1", item_hint="ecg",
            filename="e.bin", total_size=4, chunk_size=4,
            whole_sha256="f" * 64, created_at=self.now,
        ).upload_id
        self.mgr.put_chunk(bad, 0, b"abcd", chunk_sha256=sha(b"abcd"))
        with self.assertRaises(IntegrityError):
            self.mgr.assemble(bad, self.now)

    def test_content_addressing_dedupes_identical_files(self):
        data = b"same-file-content"
        s1 = self.mgr.open_session(
            upload_id="s1", referral_id="r1", item_hint="lab",
            filename="a", total_size=len(data), chunk_size=64,
            whole_sha256=sha(data), created_at=self.now,
        ).upload_id
        s2 = self.mgr.open_session(
            upload_id="s2", referral_id="r1", item_hint="lab",
            filename="b", total_size=len(data), chunk_size=64,
            whole_sha256=sha(data), created_at=self.now,
        ).upload_id
        self.mgr.put_chunk(s1, 0, data, chunk_sha256=sha(data))
        self.mgr.put_chunk(s2, 0, data, chunk_sha256=sha(data))
        a1 = self.mgr.assemble(s1, self.now)
        a2 = self.mgr.assemble(s2, self.now)
        self.assertEqual(a1.artifact_id, a2.artifact_id)

    def test_abort_then_closed_session_rejects_chunks(self):
        self.mgr.abort(self.sid)
        with self.assertRaises(RuleViolation):
            self.mgr.put_chunk(self.sid, 0, b"ab", chunk_sha256=sha(b"ab"))


if __name__ == "__main__":
    unittest.main()

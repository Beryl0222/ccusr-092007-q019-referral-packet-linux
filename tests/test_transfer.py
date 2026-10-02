import hashlib
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from referral_packet import ChunkedUpload, PushGateway, TransferError

BLOB = b"fake-dicom-payload" * 4096
CHUNKS = [BLOB[i : i + 10000] for i in range(0, len(BLOB), 10000)]
DIGEST = hashlib.sha256(BLOB).hexdigest()


class ChunkedUploadTest(unittest.TestCase):
    def test_resume_after_partial_failure(self):
        up = ChunkedUpload("up-1", "ref-1", total_chunks=len(CHUNKS), expected_hash=DIGEST)
        # 模拟传输中途失败：只到了前 3 片
        for i in range(3):
            up.receive(i, CHUNKS[i])
        self.assertFalse(up.complete)
        self.assertEqual(up.missing(), tuple(range(3, len(CHUNKS))))
        # 续传：只补缺口
        for i in up.missing():
            up.receive(i, CHUNKS[i])
        self.assertTrue(up.complete)
        self.assertEqual(up.assemble(), BLOB)

    def test_duplicate_chunk_is_idempotent(self):
        up = ChunkedUpload("up-2", "ref-1", total_chunks=len(CHUNKS), expected_hash=DIGEST)
        up.receive(0, CHUNKS[0])
        up.receive(0, CHUNKS[0])
        self.assertEqual(len(up.missing()), len(CHUNKS) - 1)

    def test_hash_mismatch_rejected(self):
        up = ChunkedUpload("up-3", "ref-1", total_chunks=len(CHUNKS), expected_hash="0" * 64)
        for i, c in enumerate(CHUNKS):
            up.receive(i, c)
        with self.assertRaises(TransferError):
            up.assemble()

    def test_assemble_before_complete_rejected(self):
        up = ChunkedUpload("up-4", "ref-1", total_chunks=len(CHUNKS), expected_hash=DIGEST)
        up.receive(0, CHUNKS[0])
        with self.assertRaises(TransferError):
            up.assemble()


class PushGatewayTest(unittest.TestCase):
    def test_duplicate_push_does_not_create_second_journey(self):
        gw = PushGateway()
        j1 = gw.push("ref-1", idempotency_key="k1", packet_id="pk-1", version=1, at="2026-09-20T09:00:00+08:00")
        j2 = gw.push("ref-1", idempotency_key="k1", packet_id="pk-1", version=1, at="2026-09-20T09:00:01+08:00")
        j3 = gw.push("ref-1", idempotency_key="k2", packet_id="pk-1", version=1, at="2026-09-20T09:00:02+08:00")
        self.assertIs(j1, j2)
        self.assertIs(j1, j3)
        self.assertEqual(gw.journey_count(), 1)
        self.assertEqual(j1.duplicate_attempts, 1)
        self.assertEqual(len(j1.pushes), 2)  # 同键去重，异键记录为同一旅程的推送

    def test_distinct_referrals_have_distinct_journeys(self):
        gw = PushGateway()
        gw.push("ref-1", idempotency_key="k1", packet_id="pk-1", version=1, at="2026-09-20T09:00:00+08:00")
        gw.push("ref-2", idempotency_key="k1", packet_id="pk-2", version=1, at="2026-09-20T09:00:00+08:00")
        self.assertEqual(gw.journey_count(), 2)


if __name__ == "__main__":
    unittest.main()

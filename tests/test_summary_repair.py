"""Regression checks for explicit summary recovery and iOS icon assets."""
import struct
import unittest
import zlib

from summary_repair import _split_archive, _state_digest
from app_icon import APPLE_TOUCH_ICON


class SummaryRecoveryTests(unittest.TestCase):
    def test_split_original_fold_archive(self):
        archived = ("【旧摘要1】\n【2026-11-01】第一次记录\n\n"
                    "【旧摘要2】\n【2026-11-04】第二次记录")
        self.assertEqual(_split_archive(archived), [
            "【2026-11-01】第一次记录",
            "【2026-11-04】第二次记录",
        ])

    def test_preview_state_signature_changes_on_cursor(self):
        self.assertNotEqual(_state_digest(["same"], 2292),
                            _state_digest(["same"], 2324))

    def test_apple_icon_is_decodable_png(self):
        data = APPLE_TOUCH_ICON
        self.assertTrue(data.startswith(b"\x89PNG\\r\n\x1a\n"))
        length = struct.unpack(">I", data[8:12])[0]
        self.assertEqual(length, 13)
        self.assertEqual(struct.unpack(">II", data[16:24]), (180, 180))
        offset = 8
        compressed = b""
        while offset < len(data):
            size = struct.unpack(">I", data[offset:offset+4])[0]
            kind = data[offset+4:offset+8]
            payload = data[offset+8:offset+8+size]
            if kind == b"IDAT":
                compressed += payload
            offset += 12 + size
        raw = zlib.decompress(compressed)
        self.assertEqual(len(raw), (180*4+1)*180)


if __name__ == "__main__":
    unittest.main()

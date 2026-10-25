"""w3strings 编解码测试。

除自造样本外，第一条用例用的是公开的“英语已知字节”黄金样本：
语言密钥 0x43975139 → magic 0x79321793，初始旋转密钥 0x3217，
字符 'A'(0x41) 与 (2 * 0x3217) 的低字节 0x2E 异或得到 0x6F（'o'）。
编码结果必须逐字节还原，这样能同时校验：头部布局、bit6 计数、
ID 混淆、逐字符旋转的文本混淆、终止符、以及末尾的语言密钥低半。
"""
from __future__ import annotations

import struct
import unittest

from w3dualsub import language as lang
from w3dualsub import w3strings


def record(version=164, language_key=0, strings=(), keys=()):
    return w3strings.StringsFile(
        version=version,
        language_key=language_key,
        magic=lang.magic_for_key(language_key),
        strings=list(strings),
        keys=list(keys),
    )


class GoldenBytes(unittest.TestCase):
    def test_known_english_bytes(self):
        raw = (
            b"RTSW" + struct.pack("<IH", 164, 0x4397) + b"\x01"
            + struct.pack("<III", 0x79321792, 0, 1) + b"\x00\x02"
            + b"o\x00" + struct.pack("<H", 0x5139)
        )
        decoded = w3strings.read_bytes(raw)
        self.assertEqual(decoded.strings, [(1, "A")])
        self.assertEqual(decoded.magic, 0x79321793)
        self.assertEqual(w3strings.write_bytes(decoded), raw)

    def test_known_chinese_traditional_header(self):
        raw = w3strings.write_bytes(record(164, 0x18632176, [(7, "凯尔莫罕")]))
        self.assertEqual(raw[:4], b"RTSW")
        self.assertEqual(struct.unpack_from("<I", raw, 4)[0], 164)
        self.assertEqual(struct.unpack_from("<H", raw, 8)[0], 0x1863)
        self.assertEqual(struct.unpack_from("<H", raw, len(raw) - 2)[0], 0x2176)
        self.assertEqual(w3strings.read_bytes(raw).strings, [(7, "凯尔莫罕")])


class RoundTrip(unittest.TestCase):
    def test_all_versions_and_representative_languages(self):
        samples = {
            0x43975139: [(1, "The wolf and the cat."), (2, "Witcher"), (3, "")],
            0x18632176: [(5, "狼与猫。"), (6, "猎魔人")],
            0x00000000: [(5, "狼与猫。"), (6, "The wolf and the cat.")],
            0x54834893: [(9, "狼と猫。")],
            0x24987354: [(10, "Vlk a kočka.")],
        }
        for version in (162, 164):
            for key, strings in samples.items():
                with self.subTest(version=version, key=hex(key)):
                    item = record(version, key, strings, [(0xDEADBEEF, strings[0][0])])
                    raw = w3strings.write_bytes(item)
                    back = w3strings.read_bytes(raw)
                    self.assertEqual(back.version, version)
                    self.assertEqual(back.text_map(), dict(strings))
                    self.assertEqual(back.keys, [(0xDEADBEEF, strings[0][0])])
                    self.assertEqual(w3strings.write_bytes(back), raw)
                    self.assertTrue(w3strings.roundtrip(item))

    def test_plaintext_languages_are_stored_unobfuscated(self):
        raw = w3strings.write_bytes(record(164, 0, [(1, "简体中文")]))
        self.assertIn("简体中文".encode("utf-8"), raw)

    def test_newlines_are_preserved(self):
        text = "第一行<br>Second line\r\n第三行"
        item = record(164, 0x18632176, [(1, text)])
        self.assertEqual(w3strings.read_bytes(w3strings.write_bytes(item)).strings, [(1, text)])

    def test_large_counts_use_multibyte_framing(self):
        strings = [(index, "x" * 130) for index in range(9000)]
        item = record(164, 0, strings)
        raw = w3strings.write_bytes(item)
        self.assertGreater(len(raw), 1_000_000)
        self.assertEqual(w3strings.read_bytes(raw).text_map(), dict(strings))


class IndexOrder(unittest.TestCase):
    def test_string_index_sorted_by_obfuscated_id(self):
        item = record(164, 0x18632176, [(1, "a"), (2, "b"), (3, "c"), (0x80000000, "d")])
        raw = w3strings.write_bytes(item)
        pos = 10
        count, pos = w3strings._read_count(raw, pos)
        stored = [struct.unpack_from("<I", raw, pos + index * 12)[0] for index in range(count)]
        self.assertEqual(stored, sorted(stored))
        self.assertEqual(len(stored), 4)


class FailureModes(unittest.TestCase):
    def test_rejects_unknown_language_key(self):
        raw = bytearray(w3strings.write_bytes(record(164, 0x43975139, [(1, "A")])))
        struct.pack_into("<H", raw, 8, 0x1234)
        with self.assertRaises(lang.UnsupportedLanguage):
            w3strings.read_bytes(bytes(raw))

    def test_rejects_bad_magic(self):
        raw = b"NOPE" + b"\x00" * 20
        with self.assertRaises(w3strings.W3StringsError):
            w3strings.read_bytes(raw)

    def test_rejects_truncated(self):
        raw = w3strings.write_bytes(record(164, 0x18632176, [(1, "狼与猫")]))
        for end in range(len(raw)):
            with self.subTest(end=end):
                with self.assertRaises((w3strings.W3StringsError, lang.UnsupportedLanguage)):
                    w3strings.read_bytes(raw[:end])

    def test_rejects_duplicate_ids(self):
        with self.assertRaises(w3strings.W3StringsError):
            w3strings.write_bytes(record(164, 0, [(1, "a"), (1, "b")]))

    def test_rejects_out_of_range_text_pointer(self):
        raw = bytearray(w3strings.write_bytes(record(164, 0, [(1, "abc")])))
        struct.pack_into("<I", raw, 10 + 1 + 8, 9999)
        with self.assertRaises(w3strings.W3StringsError):
            w3strings.read_bytes(bytes(raw))

    def test_mislabeled_utf8_container_is_read_as_utf8(self):
        # 声明 162（UTF-16）但实际存的是 UTF-8：只能靠缓冲终点反推。
        body = "abc".encode("utf-8") + b"\x00"
        raw = (
            b"RTSW" + struct.pack("<IH", 162, 0x0000) + b"\x01"
            + struct.pack("<III", 1, 0, 3) + b"\x00" + bytes([len(body)])
            + body + struct.pack("<H", 0x0000)
        )
        self.assertEqual(w3strings.read_bytes(raw).strings, [(1, "abc")])


if __name__ == "__main__":
    unittest.main()

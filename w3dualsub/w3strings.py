"""w3strings 容器读写（REDengine 3/4 本地化字符串表）。

文件布局（全部小端）：

    "RTSW"            4 字节魔数
    version           u32   162/163 = UTF-16LE 老版本；>=164 = UTF-8（重制版）
    key_hi            u16   32 位语言密钥的高 16 位
    <字符串条数>       bit6 变长计数
    条数 × 12 字节     每条：(id ^ magic, 偏移, 长度)  —— 偏移/长度以“单元”计
    <键关联条数>       bit6 变长计数
    条数 × 8 字节      每条：(本地化键哈希, id ^ magic)
    <缓冲单元数>       bit6 变长计数
    字符串缓冲        每条：混淆后的文本 + 一个零单元终止符
    key_lo            u16   32 位语言密钥的低 16 位

两个要点：

* 字符串索引块必须按 **(id ^ magic)** 升序写。游戏用二分查找，写错顺序会让界面
  直接显示 "##key" 之类的原始键名（能加载，但文本全废）。
* 混淆算法：key 每字符左旋一位；第 i 个字符的异或字节由
  ``((长度 + 1) * key_i) & 0xFFFF`` 决定，因此**长度是密钥的一部分**，
  改文本就必须整条重算。

实现参考了 WolvenKit / w3strings encoder 的公开格式（经两份独立实现交叉验证），
纯标准库，无第三方依赖。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import language as lang

MAGIC_BYTES = b"RTSW"
UTF16_VERSION = 162
UTF8_VERSION = 164
# 老版本里 163 也是 UTF-16 布局，按 162 处理即可。
_SUPPORTED_OLD = (162, 163)


class W3StringsError(ValueError):
    """容器损坏或不被支持的格式。"""


@dataclass
class StringsFile:
    """一个 .w3strings 的内存形态。

    strings 与 keys 都保留“条目里实际出现的每一条”，不做按 ID 合并 ——
    真实游戏文件里同一个 ID 可能挂多个键哈希，也可能出现指向别的资源里 ID 的关联。
    """

    version: int
    language_key: int
    magic: int
    strings: list[tuple[int, str]] = field(default_factory=list)
    keys: list[tuple[int, int]] = field(default_factory=list)  # (键哈希, 字符串 ID)
    path: Path | None = None

    @property
    def unit(self) -> int:
        return 1 if self.version >= UTF8_VERSION else 2

    @property
    def encoding(self) -> str:
        return "utf-8" if self.unit == 1 else "utf-16-le"

    def text_map(self) -> dict[int, str]:
        return dict(self.strings)

    def keyed_ids(self) -> set[int]:
        return {string_id for _, string_id in self.keys}

    def __len__(self) -> int:
        return len(self.strings)


# --------------------------------------------------------------------------- #
# bit6 变长计数
# --------------------------------------------------------------------------- #

def _read_count(data: bytes, pos: int) -> tuple[int, int]:
    """读取 bit6 变长计数，返回 (值, 新位置)。"""
    if pos >= len(data):
        raise W3StringsError("计数区被截断")
    first = data[pos]
    pos += 1
    value = first & 0x3F
    if not first & 0x40:
        return value, pos
    for index in range(2, 6):
        if pos >= len(data):
            raise W3StringsError("计数区被截断")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << (6 + 7 * (index - 2))
        if not byte & 0x80:
            if value > 0xFFFFFFFF:
                raise W3StringsError("计数超出 u32")
            return value, pos
    raise W3StringsError("bit6 计数超过 5 字节")


def _write_count(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise W3StringsError("计数超出 u32")
    if value == 0:
        # 游戏自己的文件把 0 写成单字节 0x00（0x80 也能被解析成 0，但与官方文件不一致）。
        return b"\x00"
    out = bytearray()
    out.append(value & 0x3F)
    remaining = value >> 6
    while remaining:
        out.append((remaining & 0x7F) | 0x80)
        remaining >>= 7
    if len(out) > 1:
        out[0] |= 0x40
        out[-1] &= 0x7F
    return bytes(out)


# --------------------------------------------------------------------------- #
# 混淆
# --------------------------------------------------------------------------- #

def _rotate_left_16(key: int) -> int:
    return ((key << 1) | (key >> 15)) & 0xFFFF


def _keystream(first_key: int, units: int, unit: int) -> bytes:
    """生成一条文本所需的异或流。

    16 位 key 每字符左旋一位，周期恰好是 16，所以只需算出 16 个字符的图案再重复，
    避免逐字节跑 Python 循环。
    """
    multiplier = units + 1
    pattern = bytearray()
    key = first_key
    for _ in range(16):
        character_key = (multiplier * key) & 0xFFFF
        pattern.append(character_key & 0xFF)
        if unit == 2:
            pattern.append((character_key >> 8) & 0xFF)
        key = _rotate_left_16(key)
    need = units * unit
    stream = bytes(pattern) * (need // len(pattern) + 1)
    return stream[:need]


def _xor(data: bytes, stream: bytes) -> bytes:
    if not data:
        return data
    left = int.from_bytes(data, "little")
    right = int.from_bytes(stream, "little")
    return (left ^ right).to_bytes(len(data), "little")


def _obfuscate(raw: bytes, magic: int, unit: int) -> bytes:
    """编码/解码是同一个操作（异或自反）。"""
    if magic == 0:
        return raw
    units = len(raw) // unit
    first_key = (magic >> 8) & 0xFFFF
    return _xor(raw, _keystream(first_key, units, unit))


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #

def read_bytes(data: bytes, path: Path | None = None) -> StringsFile:
    if len(data) < 16:
        raise W3StringsError("文件太短，不是 w3strings")
    if data[:4] != MAGIC_BYTES:
        raise W3StringsError(f"魔数不匹配：{data[:4]!r}")

    version = struct.unpack_from("<I", data, 4)[0]
    if version not in _SUPPORTED_OLD and version != UTF8_VERSION:
        raise W3StringsError(f"不支持的容器版本 {version}（已知 162/163/164）")

    key_hi = struct.unpack_from("<H", data, 8)[0]
    key_lo = struct.unpack_from("<H", data, len(data) - 2)[0]
    language_key = (key_hi << 16) | key_lo
    magic = lang.magic_for_key(language_key)

    pos = 10
    count, pos = _read_count(data, pos)
    if pos + count * 12 > len(data) - 2:
        raise W3StringsError("字符串索引块越界")
    entries = [struct.unpack_from("<III", data, pos + index * 12) for index in range(count)]
    pos += count * 12

    key_count, pos = _read_count(data, pos)
    if pos + key_count * 8 > len(data) - 2:
        raise W3StringsError("键关联块越界")
    keys = [
        (key_hash, stored_id ^ magic)
        for key_hash, stored_id in (
            struct.unpack_from("<II", data, pos + index * 8) for index in range(key_count)
        )
    ]
    pos += key_count * 8

    units, pos = _read_count(data, pos)

    # 少数社区容器把 UTF-8 文本标成 162，按声明的单元宽度会算不准缓冲终点，
    # 这里用“缓冲必须正好结束在末尾 2 字节之前”来判定真实单元宽度。
    unit = 1 if version >= UTF8_VERSION else 2
    if unit == 2 and pos + units == len(data) - 2:
        unit = 1
    if pos + units * unit != len(data) - 2:
        raise W3StringsError("字符串缓冲长度与文件大小不一致")

    buffer = data[pos:pos + units * unit]
    encoding = "utf-8" if unit == 1 else "utf-16-le"
    strings: list[tuple[int, str]] = []
    seen: set[int] = set()
    for stored_id, offset, length in entries:
        if offset + length > units:
            raise W3StringsError("有一条文本指向缓冲之外")
        raw = buffer[offset * unit:(offset + length) * unit]
        text = _obfuscate(raw, magic, unit).decode(encoding, "strict")
        string_id = stored_id ^ magic
        if string_id in seen:
            raise W3StringsError(f"字符串 ID 重复：{string_id}")
        seen.add(string_id)
        strings.append((string_id, text))

    return StringsFile(
        version=version,
        language_key=language_key,
        magic=magic,
        strings=strings,
        keys=keys,
        path=path,
    )


def read(path: str | Path) -> StringsFile:
    path = Path(path)
    return read_bytes(path.read_bytes(), path)


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #

def write_bytes(record: StringsFile) -> bytes:
    unit = record.unit
    magic = record.magic
    encoding = record.encoding

    ids = [string_id for string_id, _ in record.strings]
    if len(set(ids)) != len(ids):
        raise W3StringsError("字符串 ID 重复，游戏索引会错乱")

    # 索引块必须按“混淆后的 ID”升序，否则游戏二分查找失败。
    ordered = sorted(record.strings, key=lambda item: item[0] ^ magic)

    buffer = bytearray()
    entries = bytearray()
    for string_id, text in ordered:
        raw = text.encode(encoding, "strict")
        if len(raw) % unit:
            raise W3StringsError(f"文本长度不是 {unit} 字节的整数倍：{string_id}")
        length = len(raw) // unit
        entries += struct.pack("<III", string_id ^ magic, len(buffer) // unit, length)
        buffer += _obfuscate(raw, magic, unit)
        buffer += bytes(unit)  # 终止符

    key_rows = sorted({(key_hash, string_id) for key_hash, string_id in record.keys})
    key_block = bytearray()
    for key_hash, string_id in key_rows:
        key_block += struct.pack("<II", key_hash, string_id ^ magic)

    out = bytearray()
    out += MAGIC_BYTES
    out += struct.pack("<I", record.version)
    out += struct.pack("<H", record.language_key >> 16)
    out += _write_count(len(ordered))
    out += entries
    out += _write_count(len(key_rows))
    out += key_block
    out += _write_count(len(buffer) // unit)
    out += buffer
    out += struct.pack("<H", record.language_key & 0xFFFF)
    return bytes(out)


def write(record: StringsFile, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(write_bytes(record))
    return path


def roundtrip(record: StringsFile) -> bool:
    """编码再解码，确认内容一字不差（用于生成前的自检）。"""
    again = read_bytes(write_bytes(record))
    return (
        again.version == record.version
        and again.language_key == record.language_key
        and again.text_map() == record.text_map()
        and sorted(again.keys) == sorted(set(record.keys))
    )

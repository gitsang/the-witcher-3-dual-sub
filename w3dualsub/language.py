"""《巫师3》官方语言表。

每个 .w3strings 文件头里带一个 32 位语言密钥（分两半存放：头部 8 字节处存高 16 位，
文件最后 2 字节存低 16 位）。这个密钥唯一决定了该语言的字符串与字符串 ID 的
混淆掩码（magic）：

    stored_id        = real_id ^ magic
    stored_text_byte = real_text_byte ^ f(magic, 长度, 位置)

游戏发售后新增的语言（阿拉伯语、巴西葡语、简体中文、墨西哥西语、韩语、土耳其语、
乌克兰语）共用语言密钥 0，也就是 magic = 0 —— 这些文件里的文本和 ID 都是明文，
不做任何混淆。语言只由文件名（en.w3strings / cn.w3strings ...）区分。

因此"任意两种官方语言"在格式层面是完全可行：只要知道 magic 就能读写。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str          # 游戏文件名用的代码，例如 "cn"
    key: int           # 32 位语言密钥
    magic: int         # 字符串/ID 的混淆掩码
    native: str        # 母语名称
    culture: str       # 文化代码，例如 zh-Hans

    @property
    def plain(self) -> bool:
        """magic 为 0 表示该语言的文件不做混淆（明文）。"""
        return self.magic == 0


# 顺序即界面展示顺序：中文玩家常用的排在前面。
LANGUAGES: tuple[Language, ...] = (
    Language("cn", 0x00000000, 0x00000000, "简体中文", "zh-Hans"),
    Language("zh", 0x18632176, 0x16875467, "繁體中文", "zh-Hant"),
    Language("en", 0x43975139, 0x79321793, "English", "en"),
    Language("jp", 0x54834893, 0x59825646, "日本語", "ja"),
    Language("ru", 0x63481486, 0x42386347, "Русский", "ru"),
    Language("pl", 0x83496237, 0x73946816, "Polski", "pl"),
    Language("de", 0x75886138, 0x42791159, "Deutsch", "de"),
    Language("fr", 0x23863176, 0x75921975, "Français", "fr"),
    Language("it", 0x45931894, 0x12375973, "Italiano", "it"),
    Language("es", 0x18796651, 0x42387566, "Español", "es"),
    Language("esMX", 0x00000000, 0x00000000, "Español (Latinoamérica)", "es-MX"),
    Language("cz", 0x24987354, 0x21793217, "Čeština", "cs"),
    Language("hu", 0x42378932, 0x67823218, "Magyar", "hu"),
    Language("ar", 0x00000000, 0x00000000, "العربية", "ar"),
    Language("br", 0x00000000, 0x00000000, "Português (Brasil)", "pt-BR"),
    Language("kr", 0x00000000, 0x00000000, "한국어", "ko"),
    Language("tr", 0x00000000, 0x00000000, "Türkçe", "tr"),
    Language("ua", 0x00000000, 0x00000000, "Українська", "uk"),
)

BY_CODE: dict[str, Language] = {language.code.casefold(): language for language in LANGUAGES}

# 同一个 magic 可能被多个语言共用（新增语言都是 0），所以只按 magic 反查展示名。
_BY_MAGIC: dict[int, list[Language]] = {}
for _language in LANGUAGES:
    _BY_MAGIC.setdefault(_language.magic, []).append(_language)


class UnsupportedLanguage(ValueError):
    """文件里的语言密钥不在已知表里。"""


def by_code(code: str) -> Language | None:
    return BY_CODE.get(code.strip().casefold())


def magic_for_key(key: int) -> int:
    """由 32 位语言密钥推出 magic；未知密钥直接报错，绝不猜测。"""
    for language in LANGUAGES:
        if language.key == key:
            return language.magic
    raise UnsupportedLanguage(
        f"未知的语言密钥 0x{key:08X}。该语言不在已知表内，无法安全地加解密。"
    )


def describe_magic(magic: int) -> str:
    names = _BY_MAGIC.get(magic)
    return "/".join(language.code for language in names) if names else "?"


def label(code: str) -> str:
    language = by_code(code)
    return f"{language.native} ({language.code})" if language else code

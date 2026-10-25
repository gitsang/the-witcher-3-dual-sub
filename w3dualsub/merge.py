"""双语合并策略。

字幕（对话台词）在 w3strings 里是**没有本地化键哈希**的条目：场景文件直接用数字 ID
引用它们。带键哈希的条目是界面/物品/任务文本（`panel_xxx`、`item_xxx` 之类）。

所以"只做字幕"这件事可以很干净地实现：

* ``dialogue`` 模式（默认）只合并「两边都没有键哈希」的同 ID 条目 —— 也就是台词；
  界面文字完全不动，仍然保持主语言。
* ``all`` 模式额外合并两边共享同一键哈希的条目，得到全文本双语。

分隔用 ``<br>``（游戏字幕文本框认这个换行标记），只有当句子以终止标点结尾时才换行；
短句/片段用一个空格，避免字幕块被撑成两行。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .w3strings import StringsFile

# 终止标点：西方语言 + 东亚/阿拉伯语系各一套。
WESTERN_PUNCTUATION = (".", "..", "...", "…", "?", "!", "，", ",", "。", "？", "！", "；", ";", "：", ":")
EASTERN_PUNCTUATION = {
    "cn": ("…", "...", "..", "，", "。", "？", "！", "；", "："),
    "zh": ("…", "...", "..", "，", "。", "？", "！", "；", "："),
    "jp": ("…", "...", "..", "、", "，", "。", "？", "！"),
    "kr": ("…", "...", "..", ".", "?", "!", ",", "，", "。", "？", "！"),
    "ar": ("…", "...", "..", ".", "?", "!", "،", "؛", "؟"),
}
CLOSING_MARKS = "\"'”’」』〉》»›）)】]"

MODE_DIALOGUE = "dialogue"
MODE_ALL = "all"
MODES = (MODE_DIALOGUE, MODE_ALL)

SEP_AUTO = "auto"
SEP_BR = "br"
SEP_SPACE = "space"
SEP_PIPE = "pipe"
SEP_NEWLINE = "newline"
SEPARATORS = (SEP_AUTO, SEP_BR, SEP_SPACE, SEP_PIPE, SEP_NEWLINE)


@dataclass
class MergeOptions:
    mode: str = MODE_DIALOGUE
    separator: str = SEP_AUTO
    only_sentences: bool = False   # 只合并以终止标点结尾的句子（更保守，界面更干净）
    max_chars: int = 300           # 主语言原文超过这个长度就不合并（书籍/任务回顾不是字幕）0=不限制

    def check(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"未知模式 {self.mode}")
        if not isinstance(self.max_chars, int) or self.max_chars < 0:
            raise ValueError("max_chars 必须是非负整数")
        if self.separator not in SEPARATORS:
            raise ValueError(f"未知分隔符 {self.separator}")


@dataclass
class MergeStats:
    entries: int = 0          # 主语言资源里的条目总数
    merged: int = 0           # 成功加上副语言的条目数
    identical: int = 0        # 两边文本相同、跳过
    already: int = 0          # 原文里已经带着副语言（旧版覆盖式双语 mod 的产物）
    untranslated: int = 0     # 主语言原文没翻译（纯拉丁/ASCII 或拟声词），无需双语
    excluded_keyed: int = 0   # 有键哈希、按模式排除
    missing: int = 0          # 副语言里没有同 ID 条目
    filtered: int = 0         # 被 only_sentences 过滤
    too_long: int = 0         # 超长（书籍、任务回顾等非字幕文本）


@dataclass
class MergeResult:
    """一个资源对（主语言文件 + 副语言文件）的合并结果。"""

    strings: list[tuple[int, str]] = field(default_factory=list)
    changed: list[tuple[int, str, str]] = field(default_factory=list)  # (id, 原文, 合并后)
    stats: MergeStats = field(default_factory=MergeStats)


def _strip_tail(text: str) -> str:
    candidate = text.rstrip()
    while candidate and candidate[-1] in CLOSING_MARKS:
        candidate = candidate[:-1].rstrip()
    return candidate


def ends_sentence(text: str, code: str | None = None) -> bool:
    punctuation = EASTERN_PUNCTUATION.get((code or "").casefold().split("-")[0], WESTERN_PUNCTUATION)
    return _strip_tail(text).endswith(punctuation)


def pick_separator(primary_text: str, secondary_text: str, options: MergeOptions,                   primary_code: str | None = None, secondary_code: str | None = None) -> str:
    if options.separator == SEP_BR:
        return "<br>"
    if options.separator == SEP_SPACE:
        return " "
    if options.separator == SEP_PIPE:
        return " | "
    if options.separator == SEP_NEWLINE:
        return "\n"
    # auto：句子换行，片段用空格。两个语言里任意一个像完整句子就换行。
    if ends_sentence(primary_text, primary_code) or ends_sentence(secondary_text, secondary_code):
        return "<br>"
    return " "


_TAG = re.compile(r"<[^<>]{1,16}>")


def _fingerprint(text: str) -> str:
    """把文本压成"只看内容"的指纹：去掉标记、全角转半角、忽略大小写、去掉标点与空白。

    两点是踩过的坑：
    * 中文版和英文版经常只差一个本地化标点（`Auuugggrrrr！` vs `Auuugggrrrr!`）；
    * `<br>` 这类标记本身带字母，不先去掉会让「是否已包含副语言」的判定失效。
    """
    cleaned = _TAG.sub("", unicodedata.normalize("NFKC", text)).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", "", cleaned)


def _ascii_only(text: str) -> bool:
    return all(ord(char) < 128 for char in unicodedata.normalize("NFKC", text))


# 这些语言使用非拉丁文字：如果主语言原文里一个本地文字都没有，说明这条根本没翻译
# （常见的 `[EN] ...` 调试串，或者 `Auuugggrrrr!` 这类拟声台词），不必再叠副语言。
NON_LATIN_PRIMARY = {"cn", "zh", "jp", "kr", "ru", "ua", "ar"}


_SEPARATOR_SPLIT = re.compile(r"<br\s*/?>|\r\n|\r|\n|\s\|\s")


def already_has(primary_text: str, secondary_text: str) -> bool:
    """判断主语言原文是否已经带着副语言文本。

    旧版覆盖式双语 mod 直接把 "中文<br>English" 写进了游戏原始文件；迁移到本工具时
    必须识别出来，否则会叠加成 "中文<br>English<br>English"。

    判定方式：按换行标记切段，最后一段的内容与副语言文本一致即认为已经是双语。
    """
    target = _fingerprint(secondary_text)
    if not target:
        return False
    segments = [part for part in _SEPARATOR_SPLIT.split(primary_text) if part.strip()]
    if len(segments) >= 2 and _fingerprint(segments[-1]) == target:
        return True
    # 兜底：整段文本以副语言结尾（副语言太短时不猜，避免误判）
    return len(target) >= 8 and _fingerprint(primary_text).endswith(target)


@dataclass
class SecondaryIndex:
    """副语言的全局索引（所有副语言资源的并集）。
    DLC 的台词 ID 常常只出现在主语言的 ``content/content0`` 里，按目录一对一配对
    会漏掉它们；按 ID 全局匹配才是稳的（本地化 ID 在整个语言库里唯一）。
    """

    texts: dict[int, str] = field(default_factory=dict)
    keyed: set[int] = field(default_factory=set)

    @classmethod
    def from_records(cls, records) -> "SecondaryIndex":
        index = cls()
        for record in records:
            index.texts.update(record.text_map())
            index.keyed |= record.keyed_ids()
        return index

    @classmethod
    def from_file(cls, record: StringsFile) -> "SecondaryIndex":
        return cls(record.text_map(), record.keyed_ids())


def merge_resource(primary: StringsFile, secondary: StringsFile | SecondaryIndex,
                   options: MergeOptions, primary_code: str | None = None,
                   secondary_code: str | None = None) -> MergeResult:
    """把一个主语言资源整体合并成双语版本（返回完整条目表）。"""
    options.check()
    if isinstance(secondary, StringsFile):
        secondary = SecondaryIndex.from_file(secondary)
    primary_keyed = primary.keyed_ids()
    secondary_keyed = secondary.keyed
    secondary_texts = secondary.texts
    result = MergeResult()

    for string_id, text in primary.strings:
        result.stats.entries += 1
        other = secondary_texts.get(string_id)

        keyed_here = string_id in primary_keyed
        keyed_there = string_id in secondary_keyed

        if options.mode == MODE_DIALOGUE:
            combinable = not keyed_here and not keyed_there
        else:
            combinable = keyed_here == keyed_there

        if not combinable:
            result.stats.excluded_keyed += 1
            result.strings.append((string_id, text))
            continue
        if other is None:
            result.stats.missing += 1
            result.strings.append((string_id, text))
            continue
        if not other.strip():
            result.stats.missing += 1
            result.strings.append((string_id, text))
            continue
        if other == text or _fingerprint(text) == _fingerprint(other):
            result.stats.identical += 1
            result.strings.append((string_id, text))
            continue
        if (primary_code or "").casefold().split("-")[0] in NON_LATIN_PRIMARY and _ascii_only(text):
            # 主语言原文里一个本地文字都没有：没翻译的调试串、拟声词，叠上去只是重复。
            result.stats.untranslated += 1
            result.strings.append((string_id, text))
            continue
        if already_has(text, other):
            # 原文已经是双语了（例如从旧版覆盖式 mod 迁移过来），不能再叠一遍。
            result.stats.already += 1
            result.strings.append((string_id, text))
            continue
        if options.max_chars and len(text) > options.max_chars:
            # 书籍、怪物图鉴、任务回顾这类整段文本不是字幕，双起来只会把面板撑爆。
            result.stats.too_long += 1
            result.strings.append((string_id, text))
            continue
        if options.only_sentences and not (
            ends_sentence(text, primary_code) or ends_sentence(other, secondary_code)
        ):
            result.stats.filtered += 1
            result.strings.append((string_id, text))
            continue

        separator = pick_separator(text, other, options, primary_code, secondary_code)
        merged = f"{text}{separator}{other}"
        result.stats.merged += 1
        result.strings.append((string_id, merged))
        result.changed.append((string_id, text, merged))

    return result


def build_report(result: MergeResult, resource: str, primary_code: str,
                 secondary_code: str, samples: int = 20) -> str:
    stats = result.stats
    ratio = (stats.merged / stats.entries * 100) if stats.entries else 0.0
    lines = [
        f"资源：{resource}",
        f"  主语言 {primary_code} 条目 {stats.entries}，已双语 {stats.merged}（{ratio:.1f}%）",
        f"  跳过：文本相同 {stats.identical} / 已是双语 {stats.already} / "
        f"未翻译 {stats.untranslated} / 超长 {stats.too_long} / "
        f"带键哈希排除 {stats.excluded_keyed} / 副语言缺条目 {stats.missing} / "
        f"标点过滤 {stats.filtered}",
    ]
    if result.changed:
        lines.append("  样例：")
        for _, original, merged in result.changed[:samples]:
            pretty = merged.replace("<br>", " ⏎ ")
            lines.append(f"    · {pretty}")
    return "\n".join(lines)

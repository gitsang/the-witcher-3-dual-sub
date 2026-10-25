"""合并策略与端到端生成测试（用一棵假的游戏目录，不碰真实安装）。

假目录刻意模仿重制版的真实结构：字符串在 ``content/content0``，
DLC 字符串在 ``DLC/<dlc>/content``，并且**只有主语言**的 DLC 文件存在
（真实安装里中文 DLC 资源就是这样），用来验证"副语言全局索引"能跨资源配对。
"""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

from w3dualsub import build as build_mod
from w3dualsub import language as lang
from w3dualsub import merge as merge_mod
from w3dualsub import w3strings
from w3dualsub.discover import GameInstall, scan_languages

EN = 0x43975139
ZH = 0x18632176
CN = 0x00000000


def rec(language_key, strings, keys=(), version=164):
    return w3strings.StringsFile(
        version=version,
        language_key=language_key,
        magic=lang.magic_for_key(language_key),
        strings=list(strings),
        keys=list(keys),
    )


class Separator(unittest.TestCase):
    def test_sentence_gets_line_break(self):
        options = merge_mod.MergeOptions()
        self.assertEqual(merge_mod.pick_separator("A full sentence.", "完整句子。", options, "en", "cn"), "<br>")

    def test_fragment_gets_space(self):
        options = merge_mod.MergeOptions()
        self.assertEqual(merge_mod.pick_separator("Yes", "是", options, "en", "cn"), " ")

    def test_closing_quote_does_not_hide_punctuation(self):
        self.assertTrue(merge_mod.ends_sentence('他说："走吧。"', "cn"))
        self.assertTrue(merge_mod.ends_sentence('He said "Go."', "en"))

    def test_eastern_punctuation_recognized(self):
        self.assertTrue(merge_mod.ends_sentence("狼与猫。", "cn"))
        self.assertTrue(merge_mod.ends_sentence("狼と猫。", "jp"))
        self.assertFalse(merge_mod.ends_sentence("Silver Sword", "en"))

    def test_explicit_overrides(self):
        self.assertEqual(merge_mod.pick_separator("a", "b", merge_mod.MergeOptions(separator="pipe")), " | ")
        self.assertEqual(merge_mod.pick_separator("a.", "b.", merge_mod.MergeOptions(separator="space")), " ")


class MergePolicy(unittest.TestCase):
    def setUp(self):
        self.primary = rec(EN, [(1, "The wolf and the cat."), (2, "Yes"),
                                (3, "Silver Sword"), (4, "Kaer Morhen"), (5, "Only here.")],
                           keys=[(0xABCD, 3)])
        self.secondary = rec(CN, [(1, "狼与猫。"), (2, "是"),
                                  (3, "银剑"), (4, "Kaer Morhen"), (6, "只在这里。")],
                             keys=[(0xABCD, 3)])

    def test_dialogue_mode_only_touches_hashless(self):
        result = merge_mod.merge_resource(self.primary, self.secondary, merge_mod.MergeOptions(), "en", "cn")
        texts = dict(result.strings)
        self.assertEqual(texts[1], "The wolf and the cat.<br>狼与猫。")
        self.assertEqual(texts[2], "Yes 是")
        self.assertEqual(texts[3], "Silver Sword")          # 带键哈希，界面文本不动
        self.assertEqual(texts[4], "Kaer Morhen")           # 两边相同，跳过
        self.assertEqual(texts[5], "Only here.")            # 副语言缺条目，保留原文
        self.assertEqual(result.stats.merged, 2)
        self.assertEqual(result.stats.excluded_keyed, 1)
        self.assertEqual(result.stats.identical, 1)
        self.assertEqual(result.stats.missing, 1)

    def test_all_mode_includes_keyed(self):
        result = merge_mod.merge_resource(
            self.primary, self.secondary, merge_mod.MergeOptions(mode=merge_mod.MODE_ALL), "en", "cn")
        texts = dict(result.strings)
        self.assertEqual(texts[3], "Silver Sword 银剑")
        self.assertEqual(result.stats.merged, 3)

    def test_only_sentences_filters_fragments(self):
        result = merge_mod.merge_resource(
            self.primary, self.secondary, merge_mod.MergeOptions(only_sentences=True), "en", "cn")
        self.assertEqual(dict(result.strings)[2], "Yes")

    def test_already_bilingual_text_is_not_doubled(self):
        """旧版覆盖式 mod 的产物（中文<br>English）不能再叠一遍。"""
        primary = rec(EN, [(1, "狼与猫。<br>The wolf and the cat."),
                           (2, "是 Yes"), (3, "Geralt")])
        secondary = rec(CN, [(1, "The wolf and the cat."), (2, "是 Yes"), (3, "Geralt")])
        result = merge_mod.merge_resource(primary, secondary, merge_mod.MergeOptions(), "cn", "en")
        texts = dict(result.strings)
        self.assertEqual(texts[1], "狼与猫。<br>The wolf and the cat.")
        self.assertEqual(texts[2], "是 Yes")
        self.assertEqual(texts[3], "Geralt")
        self.assertEqual(result.stats.merged, 0)
        self.assertEqual(result.stats.already, 1)
        self.assertEqual(result.stats.identical, 2)

    def test_punctuation_only_difference_counts_as_identical(self):
        primary = rec(CN, [(1, "Auuugggrrrr！"), (2, "H…l…a…k")])
        secondary = rec(EN, [(1, "Auuugggrrrr!"), (2, "H… l… a… k")])
        result = merge_mod.merge_resource(primary, secondary, merge_mod.MergeOptions(), "cn", "en")
        self.assertEqual(result.stats.merged, 0)
        self.assertEqual(result.stats.identical, 2)

    def test_untranslated_ascii_primary_is_left_alone(self):
        """主语言是中文时，一条纯英文的原文（[EN] 调试串、拟声词）不该再叠英文。"""
        primary = rec(CN, [(1, "[EN] Talk to Crach an Craite"), (2, "与克拉奇交谈。")])
        secondary = rec(EN, [(1, "Talk to Crach an Craite"), (2, "Talk to Crach.")])
        result = merge_mod.merge_resource(primary, secondary, merge_mod.MergeOptions(), "cn", "en")
        texts = dict(result.strings)
        self.assertEqual(texts[1], "[EN] Talk to Crach an Craite")
        self.assertEqual(texts[2], "与克拉奇交谈。<br>Talk to Crach.")
        self.assertEqual(result.stats.untranslated, 1)

    def test_latin_to_latin_pairs_are_not_mistaken_for_untranslated(self):
        primary = rec(EN, [(1, "Silver Sword")])
        secondary = rec(0x75886138, [(1, "Silberschwert")])
        result = merge_mod.merge_resource(primary, secondary, merge_mod.MergeOptions(), "en", "de")
        self.assertEqual(dict(result.strings)[1], "Silver Sword Silberschwert")

    def test_very_long_texts_are_not_merged(self):
        """书籍/任务回顾这类整段文本不是字幕，默认不合并。"""
        long_text = "长" * 400
        primary = rec(CN, [(1, long_text), (2, "短台词。")])
        secondary = rec(EN, [(1, "Long " * 100), (2, "Short line.")])
        result = merge_mod.merge_resource(primary, secondary, merge_mod.MergeOptions(), "cn", "en")
        texts = dict(result.strings)
        self.assertEqual(texts[1], long_text)
        self.assertEqual(texts[2], "短台词。<br>Short line.")
        self.assertEqual(result.stats.too_long, 1)
        unlimited = merge_mod.MergeOptions(max_chars=0)
        result = merge_mod.merge_resource(primary, secondary, unlimited, "cn", "en")
        self.assertTrue(dict(result.strings)[1].endswith("Long "))
        self.assertEqual(result.stats.too_long, 0)

    def test_invalid_options_rejected(self):
        with self.assertRaises(ValueError):
            merge_mod.MergeOptions(mode="nope").check()


class SecondaryIndexTests(unittest.TestCase):
    def test_union_across_resources(self):
        index = merge_mod.SecondaryIndex.from_records([
            rec(EN, [(1, "a"), (2, "b")], keys=[(1, 2)]),
            rec(EN, [(3, "c")], keys=[(9, 3)]),
        ])
        self.assertEqual(index.texts, {1: "a", 2: "b", 3: "c"})
        self.assertEqual(index.keyed, {2, 3})


class FakeGame:
    """临时造一棵和真实安装同构的目录树。"""

    def __init__(self, root: Path):
        (root / "bin" / "x64").mkdir(parents=True)
        (root / "bin" / "x64" / "witcher3.exe").write_bytes(b"MZ")
        base = root / "content" / "content0"
        dlc = root / "DLC" / "dlc1" / "content"
        base.mkdir(parents=True)
        dlc.mkdir(parents=True)

        keys = [(0xBEEF, 3)]
        w3strings.write(rec(CN, [(1, "狼与猫。"), (2, "是"), (3, "银剑")], keys), base / "cn.w3strings")
        # 副语言把 DLC 的台词也放在 content0 里（真实安装就是这样）
        w3strings.write(rec(EN, [(1, "The wolf and the cat."), (2, "Yes"), (3, "Silver Sword"),
                                 (11, "Come on, Roche."), (12, "Fine.")],
                            keys, version=162), base / "en.w3strings")
        # DLC 只有主语言资源 —— 靠全局索引跟 content0 的副语言文本配对
        w3strings.write(rec(CN, [(11, "来吧，罗契。"), (12, "好。"), (13, "银剑")], keys),
                        dlc / "cn.w3strings")
        self.root = root
        self.install = GameInstall(root, "测试", root / "bin" / "x64" / "witcher3.exe",
                                   scan_languages(root))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.game = FakeGame(Path(self.tmp.name) / "game")
        self.out = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_finds_nested_resources(self):
        self.assertEqual(sorted(self.game.install.languages),
                         ["cn", "en"])

    def test_overlay_layout_writes_only_merged_entries(self):
        result = build_mod.build(self.game.install, "cn", "en", out_dir=self.out)
        self.assertEqual(result.files, ["content/cn.w3strings"])
        decoded = w3strings.read(self.out / build_mod.MOD_NAME / "content" / "cn.w3strings")
        texts = decoded.text_map()
        self.assertEqual(texts[1], "狼与猫。<br>The wolf and the cat.")
        self.assertEqual(texts[2], "是 Yes")
        self.assertEqual(texts[11], "来吧，罗契。<br>Come on, Roche.")   # 跨资源配对成功
        self.assertEqual(texts[12], "好。<br>Fine.")
        self.assertNotIn(3, texts)          # 带键哈希的界面文本不写入
        self.assertNotIn(13, texts)
        self.assertEqual(decoded.language_key, CN)
        self.assertEqual(decoded.version, 164)
        self.assertEqual(decoded.keys, [])  # 只写台词，不需要键关联
        self.assertTrue((self.out / build_mod.MOD_NAME / "README.txt").is_file())
        self.assertTrue((self.out / build_mod.MOD_NAME / build_mod.MANIFEST_NAME).is_file())
        self.assertEqual(result.merged_total, 4)

    def test_mirror_layout_keeps_structure(self):
        result = build_mod.build(self.game.install, "cn", "en", out_dir=self.out, layout="mirror")
        self.assertEqual(sorted(item.casefold() for item in result.files),
                         ["content/content0/cn.w3strings", "content/dlc/dlc1/content/cn.w3strings"])
        decoded = w3strings.read(self.out / build_mod.MOD_NAME / "content" / "content0" / "cn.w3strings")
        self.assertEqual(decoded.text_map()[1], "狼与猫。<br>The wolf and the cat.")
        self.assertEqual(len(decoded), 3)   # 完整替换：条目一个不少
        dlc = w3strings.read(self.out / build_mod.MOD_NAME / "content" / "dlc" / "dlc1" / "content" / "cn.w3strings")
        self.assertEqual(dlc.text_map()[11], "来吧，罗契。<br>Come on, Roche.")

    def test_same_language_rejected(self):
        with self.assertRaises(build_mod.BuildError):
            build_mod.build(self.game.install, "cn", "cn", out_dir=self.out)

    def test_missing_language_rejected(self):
        with self.assertRaises(build_mod.BuildError):
            build_mod.build(self.game.install, "jp", "en", out_dir=self.out)

    def test_install_and_uninstall(self):
        result = build_mod.build(self.game.install, "cn", "en", out_dir=self.out)
        with mock.patch.object(build_mod, "game_running", return_value=False):
            target = build_mod.install(result, self.game.install)
        self.assertTrue((target / "content" / "cn.w3strings").is_file())
        message = build_mod.uninstall(self.game.install)
        self.assertIn("已删除", message)
        self.assertFalse(target.exists())
        self.assertTrue((self.game.root / "content" / "content0" / "cn.w3strings").is_file())

    def test_refuses_to_delete_foreign_folder(self):
        foreign = self.game.root / "Mods" / build_mod.MOD_NAME
        foreign.mkdir(parents=True)
        (foreign / "keep.me").write_text("x", encoding="utf-8")
        with self.assertRaises(build_mod.BuildError):
            build_mod.uninstall(self.game.install)

    def test_plaintext_primary_language_is_written_unobfuscated(self):
        build_mod.build(self.game.install, "cn", "en", out_dir=self.out)
        raw = (self.out / build_mod.MOD_NAME / "content" / "cn.w3strings").read_bytes()
        self.assertIn("狼与猫。".encode("utf-8"), raw)


if __name__ == "__main__":
    unittest.main()

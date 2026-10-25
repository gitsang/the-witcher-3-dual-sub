"""命令行入口。

    python -m w3dualsub languages
    python -m w3dualsub inspect --lang en --limit 8
    python -m w3dualsub build --primary cn --secondary en --install
    python -m w3dualsub uninstall
    python -m w3dualsub gui
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, build as build_mod, discover, language as lang
from . import merge as merge_mod
from . import w3strings


def _open(args) -> discover.GameInstall:
    try:
        install = discover.open_install(args.game)
    except FileNotFoundError as error:
        print(f"✗ {error}", file=sys.stderr)
        raise SystemExit(2)
    print(f"游戏目录：{install.root}")
    print(f"来源：{install.storefront}    可执行文件：{install.executable}")
    return install


def cmd_languages(args) -> int:
    install = _open(args)
    configured = discover.configured_text_language()
    if configured:
        print(f"当前游戏文本语言（user.settings）：{configured}")
    print("检测到的字符串资源：")
    for line in discover.support_report(install):
        print(line)
    unknown = [code for code in install.languages if lang.by_code(code) is None]
    if unknown:
        print(f"⚠ 未收录的代码：{', '.join(unknown)}（可能无法作为主语言写出）")
    return 0


def cmd_inspect(args) -> int:
    install = _open(args)
    code = (args.lang or discover.configured_text_language() or "").casefold()
    paths = install.languages.get(code)
    if not paths:
        print(f"✗ 没有 {code or '(未指定)'} 的资源", file=sys.stderr)
        return 2
    print(f"\n{lang.label(code)} 共 {len(paths)} 个资源：")
    for path in paths:
        record = w3strings.read(path)
        keyed = record.keyed_ids()
        hashless = [(string_id, text) for string_id, text in record.strings if string_id not in keyed]
        print(f"\n  {path.relative_to(install.root).as_posix()}")
        print(f"    版本 {record.version}（{record.encoding}）  magic 0x{record.magic:08X}"
              f"  语言密钥 0x{record.language_key:08X}")
        print(f"    条目 {len(record)}：带键哈希 {len(keyed & {i for i, _ in record.strings})}，"
              f"无键（台词）{len(hashless)}")
        for string_id, text in hashless[:max(0, args.limit)]:
            preview = text.replace("\n", "⏎").replace("\r", "⏎")[:70]
            print(f"      #{string_id}  {preview}")
    return 0


def cmd_build(args) -> int:
    install = _open(args)
    primary = (args.primary or discover.configured_text_language() or "cn").casefold()
    secondary = (args.secondary or ("en" if primary != "en" else "cn")).casefold()
    if primary == secondary:
        print("✗ 主副语言不能相同", file=sys.stderr)
        return 2

    options = merge_mod.MergeOptions(
        mode=args.mode,
        separator=args.sep,
        only_sentences=args.only_sentences,
        max_chars=args.max_chars,
    )
    print(f"\n主语言 {lang.label(primary)}   副语言 {lang.label(secondary)}   "
          f"模式 {options.mode}   分隔 {options.separator}")
    try:
        result = build_mod.build(
            install, primary, secondary, options,
            layout=args.layout, out_dir=args.out, progress=lambda msg: print(f"  {msg}"),
        )
    except build_mod.BuildError as error:
        print(f"✗ {error}", file=sys.stderr)
        return 1

    print("\n结果：")
    for report in result.reports:
        stats = report.stats
        ratio = stats.merged / stats.entries * 100 if stats.entries else 0
        print(f"  {report.relative}")
        print(f"    台词 {stats.merged}/{stats.entries}（{ratio:.1f}%），"
              f"相同跳过 {stats.identical}，缺条目 {stats.missing}，"
              f"已是双语 {stats.already}，超长跳过 {stats.too_long}")
        if args.show_samples:
            for sample in report.sample:
                print(f"      · {sample.replace('<br>', ' ⏎ ')}")
    print(f"  输出文件：{', '.join(result.files)}")
    stale = sum(report.stats.already for report in result.reports)
    if stale:
        print(f"\n⚠ 有 {stale} 条原文已经是双语（多半来自旧版覆盖式双语 mod）。")
        print("  这些条目本工具不会重复叠加。若想让它们也由本 mod 统一管理，")
        print("  建议先用 Steam/GOG 的「验证文件完整性」还原被改过的原始文件，再重新生成。")

    if args.install:
        try:
            target = build_mod.install(result, install, force=args.force)
        except build_mod.BuildError as error:
            print(f"✗ {error}", file=sys.stderr)
            return 1
        print(f"\n✓ 已安装到 {target}")
        print("  进游戏前确认：文本语言设为主语言，字幕已开启。")
    else:
        print(f"\n✓ 已生成到 {result.mod_dir}")
        print(f"  手动安装：把 {build_mod.MOD_NAME} 整个文件夹复制到 {install.root / 'Mods'}")
    print(f"  共 {len(result.files)} 个文件，双语条目 {result.merged_total}/{result.entries_total}")
    return 0


def cmd_uninstall(args) -> int:
    install = _open(args)
    try:
        print(build_mod.uninstall(install))
    except build_mod.BuildError as error:
        print(f"✗ {error}", file=sys.stderr)
        return 1
    return 0


def cmd_gui(_args) -> int:
    try:
        from .gui import main as gui_main
    except ImportError:
        print("✗ 当前 Python 没有 Tkinter，无法打开图形界面。", file=sys.stderr)
        print("  可以直接用交互模式：python -m w3dualsub", file=sys.stderr)
        print("  或装一个带 Tkinter 的官方 Python（python.org 安装包默认自带）。", file=sys.stderr)
        return 2
    return gui_main()


# --------------------------------------------------------------------------- #
# 交互模式（没有参数时进入；不依赖 Tkinter）
# --------------------------------------------------------------------------- #

def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(0) from None
    return answer or default


def wizard() -> int:
    print(f"《巫师3》双语字幕生成器 v{__version__}\n")
    try:
        install = discover.open_install()
    except FileNotFoundError as error:
        print(f"✗ {error}")
        manual = _ask("请粘贴游戏根目录（含 content 与 bin\\x64\\witcher3.exe）")
        if not manual:
            return 2
        try:
            install = discover.open_install(manual)
        except FileNotFoundError as inner:
            print(f"✗ {inner}")
            return 2

    print(f"游戏目录：{install.root}")
    codes = sorted(install.languages, key=lambda code: (0 if code in ("cn", "zh", "en") else 1, code))
    print("\n可用语言：")
    for index, code in enumerate(codes, 1):
        print(f"  {index:>2}. {lang.label(code)}")

    configured = discover.configured_text_language()
    default_primary = str(codes.index(configured) + 1) if configured in codes else "1"
    primary = codes[int(_ask("\n主字幕语言序号", default_primary)) - 1]

    fallback = next((code for code in ("en", "jp", "zh") if code in codes and code != primary), primary)
    default_secondary = str(codes.index(fallback) + 1)
    secondary = codes[int(_ask("副字幕语言序号", default_secondary)) - 1]
    if secondary == primary:
        print("✗ 主副字幕不能是同一种语言")
        return 2

    print("\n范围：1) 只双语字幕（推荐）  2) 全部文本（连菜单物品也双语）")
    mode = merge_mod.MODE_ALL if _ask("选择", "1") == "2" else merge_mod.MODE_DIALOGUE

    options = merge_mod.MergeOptions(mode=mode)
    print(f"\n主语言 {lang.label(primary)} + 副语言 {lang.label(secondary)}，开始生成……")
    try:
        result = build_mod.build(install, primary, secondary, options,
                                 out_dir=Path.cwd() / "build", progress=lambda msg: print(f"  {msg}"))
    except build_mod.BuildError as error:
        print(f"✗ {error}")
        return 1

    for report in result.reports:
        stats = report.stats
        ratio = stats.merged / stats.entries * 100 if stats.entries else 0
        print(f"  {report.relative}：台词 {stats.merged}/{stats.entries}（{ratio:.1f}%）")
        for sample in report.sample[:3]:
            print(f"      · {sample.replace('<br>', ' ⏎ ')[:100]}")
    print(f"\n✓ 已生成 {result.mod_dir}")

    if _ask("\n现在安装到游戏 Mods/ 吗？(Y/n)", "y").casefold() in ("y", "yes", ""):
        try:
            target = build_mod.install(result, install)
            print(f"✓ 已安装到 {target}")
        except build_mod.BuildError as error:
            print(f"✗ {error}")
            print(f"  也可以手动把 {result.mod_dir} 复制到 {install.root / 'Mods'}")
            return 1
        print("  进游戏后：文本语言设为主语言，确认字幕已开启。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="w3dualsub",
        description="《巫师3》双语字幕 mod 生成器（只做字幕）。不带参数运行进入交互模式。",
    )
    parser.add_argument("--version", action="version", version=f"w3dualsub {__version__}")
    sub = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--game", help="游戏根目录（含 content 与 bin\\x64\\witcher3.exe）")

    p = sub.add_parser("languages", parents=[common], help="列出检测到的语言资源")
    p.set_defaults(func=cmd_languages)

    p = sub.add_parser("inspect", parents=[common], help="查看某个语言的资源与台词样例")
    p.add_argument("--lang", help="语言代码，默认取游戏当前文本语言")
    p.add_argument("--limit", type=int, default=5, help="每个资源展示的台词条数")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("build", parents=[common], help="生成（并可安装）双语字幕 mod")
    p.add_argument("--primary", help="主字幕语言，默认取游戏当前文本语言")
    p.add_argument("--secondary", help="副字幕语言，默认 en（主语言为 en 时用 cn）")
    p.add_argument("--mode", choices=merge_mod.MODES, default=merge_mod.MODE_DIALOGUE,
                   help="dialogue=只合并台词（默认）；all=连界面文本一起合并")
    p.add_argument("--sep", choices=merge_mod.SEPARATORS, default=merge_mod.SEP_AUTO,
                   help="主副文本之间的分隔：auto/br/space/pipe/newline")
    p.add_argument("--only-sentences", action="store_true",
                   help="只合并以标点结尾的完整句子（界面更干净，可能漏掉短台词）")
    p.add_argument("--max-chars", type=int, default=300,
                   help="主语言原文超过这个长度就不合并（书籍/任务回顾），0 表示不限制，默认 300")
    p.add_argument("--layout", choices=build_mod.LAYOUTS, default=build_mod.LAYOUT_OVERLAY,
                   help="overlay=只写台词汇总覆盖（默认）；mirror=逐资源完整替换")
    p.add_argument("--out", help="生成目录，默认 ./build")
    p.add_argument("--install", action="store_true", help="生成后直接装进 Mods/")
    p.add_argument("--force", action="store_true",
                   help="游戏运行中也允许安装（装好后需要重启游戏才生效）")
    p.add_argument("--show-samples", action="store_true", help="打印合并样例")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("uninstall", parents=[common], help="删除已安装的 mod")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("gui", help="打开图形界面")
    p.set_defaults(func=cmd_gui)

    args = parser.parse_args(argv)
    if args.command is None:
        return wizard()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

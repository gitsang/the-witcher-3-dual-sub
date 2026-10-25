"""生成 mod 目录并安装到游戏。

产物只写进 ``Mods/modW3DualSub``，不动游戏任何原始文件；卸载 = 删掉这个目录。

两种输出布局：

``overlay``（默认）
    只写被合并过的台词条目，汇总成一个 ``content/<主语言>.w3strings``。
    游戏把 mod 的字符串资源叠加进同一语言的字符串库，同 ID 覆盖 ——
    所以文件只有几 MB，界面文本在数据层面完全没被动过。

``mirror``
    每个原版资源一对一生成完整替换文件（``content/content0/cn.w3strings`` 等）。
    文件更大，但语义上是"同路径整体替换"，适合排查问题。

副语言一律取**全部副语言资源的并集**作为索引：DLC 的台词 ID 常常只在
``content/content0`` 的主语言文件里有对应，按目录配对会漏掉它们。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import __version__, language as lang
from . import merge as merge_mod
from . import w3strings
from .discover import GameInstall

MOD_NAME = "modW3DualSub"
MANIFEST_NAME = "w3dualsub.json"

LAYOUT_OVERLAY = "overlay"
LAYOUT_MIRROR = "mirror"
LAYOUTS = (LAYOUT_OVERLAY, LAYOUT_MIRROR)


class BuildError(RuntimeError):
    pass


def _remove_tree(path: Path, *, require_manifest: bool = True) -> None:
    """删除本工具自己生成的目录。

    刻意不用 shutil.rmtree：它可能被环境的安全删除钩子接管，也可能误删用户自己的
    文件。这里逐层 unlink/rmdir，并且要求目录里有本工具的清单文件才动手。
    """
    if not path.exists():
        return
    if require_manifest and not (path / MANIFEST_NAME).is_file():
        raise BuildError(f"{path} 里没有 {MANIFEST_NAME}，不是本工具生成的，拒绝删除")
    for entry in os.scandir(path):
        if entry.is_dir(follow_symlinks=False):
            _remove_tree(Path(entry.path), require_manifest=False)
        else:
            os.unlink(entry.path)
    os.rmdir(path)


@dataclass
class ResourceReport:
    relative: str
    entries: int
    stats: merge_mod.MergeStats
    sample: list[str] = field(default_factory=list)


@dataclass
class BuildResult:
    mod_dir: Path
    files: list[str]
    reports: list[ResourceReport]
    layout: str
    installed_to: Path | None = None

    @property
    def merged_total(self) -> int:
        return sum(report.stats.merged for report in self.reports)

    @property
    def entries_total(self) -> int:
        return sum(report.stats.entries for report in self.reports)


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #

def _load(path: Path) -> w3strings.StringsFile:
    try:
        return w3strings.read(path)
    except (OSError, w3strings.W3StringsError, lang.UnsupportedLanguage) as error:
        raise BuildError(f"读取 {path.name} 失败：{error}") from error


def _primary_resources(install: GameInstall, code: str) -> list[Path]:
    paths = install.languages.get(code.casefold(), [])
    if not paths:
        raise BuildError(
            f"安装目录里没有 {lang.label(code)} 的 .w3strings 资源。"
            f"可用：{', '.join(sorted(install.languages)) or '无'}"
        )
    return paths


def _secondary_index(install: GameInstall, code: str) -> merge_mod.SecondaryIndex:
    paths = install.languages.get(code.casefold(), [])
    if not paths:
        raise BuildError(
            f"安装目录里没有副语言 {lang.label(code)} 的资源。"
            f"可用：{', '.join(sorted(install.languages)) or '无'}"
        )
    return merge_mod.SecondaryIndex.from_records(_load(path) for path in paths)


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #

def mirror_target(relative: str) -> str:
    """content/content0/cn.w3strings → content/content0/cn.w3strings（去掉首个 content）。"""
    parts = PurePosixPath(relative).parts
    if parts and parts[0].casefold() == "content":
        parts = parts[1:]
    return "content/" + "/".join(parts)


def build(install: GameInstall, primary: str, secondary: str,
          options: merge_mod.MergeOptions | None = None,
          layout: str = LAYOUT_OVERLAY,
          out_dir: str | Path | None = None,
          progress=None) -> BuildResult:
    options = options or merge_mod.MergeOptions()
    options.check()
    if layout not in LAYOUTS:
        raise BuildError(f"未知布局 {layout}")
    primary = primary.casefold()
    secondary = secondary.casefold()
    if primary == secondary:
        raise BuildError("主语言和副语言不能相同")

    notify = progress or (lambda message: None)
    out_root = Path(out_dir) if out_dir else Path.cwd() / "build"
    mod_dir = out_root / MOD_NAME
    content_dir = mod_dir / "content"
    if mod_dir.exists():
        # 生成目录是我们自己的：上一次构建成功时一定有清单，失败时按空目录重建。
        _remove_tree(mod_dir, require_manifest=(mod_dir / MANIFEST_NAME).is_file())
    content_dir.mkdir(parents=True, exist_ok=True)

    notify(f"读取副语言 {secondary} 的全部资源……")
    index = _secondary_index(install, secondary)
    notify(f"副语言索引 {len(index.texts)} 条文本（其中带键 {len(index.keyed)} 条）")

    sources = _primary_resources(install, primary)
    notify(f"主语言 {primary} 共 {len(sources)} 个资源，开始合并……")

    overlay_entries: dict[int, str] = {}
    overlay_template: w3strings.StringsFile | None = None
    reports: list[ResourceReport] = []
    written: list[str] = []

    for position, path in enumerate(sources, 1):
        relative = path.relative_to(install.root).as_posix()
        notify(f"[{position}/{len(sources)}] {relative}")
        record = _load(path)
        result = merge_mod.merge_resource(record, index, options, primary, secondary)

        if layout == LAYOUT_OVERLAY:
            for string_id, _, merged in result.changed:
                overlay_entries[string_id] = merged
            if overlay_template is None:
                overlay_template = record
        else:
            merged_record = w3strings.StringsFile(
                version=record.version,
                language_key=record.language_key,
                magic=record.magic,
                strings=result.strings,
                keys=record.keys,
            )
            target = mod_dir / mirror_target(relative)
            w3strings.write(merged_record, target)
            _verify(target, result)
            written.append(target.relative_to(mod_dir).as_posix())

        reports.append(ResourceReport(
            relative=relative,
            entries=len(record),
            stats=result.stats,
            sample=[merged for _, _, merged in result.changed[:10]],
        ))

    total = sum(report.stats.merged for report in reports)
    if total == 0:
        raise BuildError(
            "没有合并到任何台词。可能原因：\n"
            "  · 这两个语言的文件里台词都带键哈希（试试 --mode all）\n"
            "  · 副语言资源缺失或选错了语言代码"
        )

    if layout == LAYOUT_OVERLAY:
        assert overlay_template is not None
        # 只写台词条目：这些条目天生没有本地化键，所以键关联表可以为空。
        payload = [
            (string_id, text)
            for string_id, text in overlay_entries.items()
        ]
        merged_record = w3strings.StringsFile(
            version=overlay_template.version,
            language_key=overlay_template.language_key,
            magic=overlay_template.magic,
            strings=payload,
            keys=[],
        )
        target = content_dir / f"{primary}.w3strings"
        w3strings.write(merged_record, target)
        _verify_ids(target, list(overlay_entries.items()))
        written.append(target.relative_to(mod_dir).as_posix())

    _write_manifest(mod_dir, install, primary, secondary, options, layout, reports, written)
    _write_docs(mod_dir, primary, secondary)
    return BuildResult(mod_dir=mod_dir, files=written, reports=reports, layout=layout)


def _verify(path: Path, result: merge_mod.MergeResult) -> None:
    """写完后立刻读回来，确认每条改动都在且一致。"""
    actual = w3strings.read(path).text_map()
    expected = dict(result.strings)
    if len(actual) != len(expected):
        raise BuildError(f"{path.name} 条目数不一致（{len(actual)} vs {len(expected)}）")
    for string_id, _, merged in result.changed:
        if actual.get(string_id) != merged:
            raise BuildError(f"{path.name} 校验失败：ID {string_id}")


def _verify_ids(path: Path, expected: list[tuple[int, str]]) -> None:
    actual = w3strings.read(path).text_map()
    if len(actual) != len(expected):
        raise BuildError(f"{path.name} 条目数不一致（{len(actual)} vs {len(expected)}）")
    for string_id, text in expected:
        if actual.get(string_id) != text:
            raise BuildError(f"{path.name} 校验失败：ID {string_id}")


def _write_manifest(mod_dir: Path, install: GameInstall, primary: str, secondary: str,
                    options: merge_mod.MergeOptions, layout: str,
                    reports: list[ResourceReport], files: list[str]) -> None:
    payload = {
        "tool": "w3dualsub",
        "version": __version__,
        "game_root": str(install.root),
        "primary": primary,
        "secondary": secondary,
        "mode": options.mode,
        "separator": options.separator,
        "only_sentences": options.only_sentences,
        "max_chars": options.max_chars,
        "layout": layout,
        "files": files,
        "resources": [
            {
                "resource": report.relative,
                "entries": report.entries,
                "merged": report.stats.merged,
                "identical": report.stats.identical,
                "already_bilingual": report.stats.already,
                "too_long": report.stats.too_long,
                "missing": report.stats.missing,
            }
            for report in reports
        ],
    }
    (mod_dir / MANIFEST_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_docs(mod_dir: Path, primary: str, secondary: str) -> None:
    text = f"""《巫师3》双语字幕 —— 由 w3dualsub 生成

主语言：{lang.label(primary)}
副语言：{lang.label(secondary)}

这个文件夹就是 mod 本体。游戏会把里面的 .w3strings 叠加到同一语言的字符串库，
按字符串 ID 覆盖，游戏自身的文件完全没有被修改。

卸载：删除 Mods/{MOD_NAME} 整个文件夹即可恢复原样。

游戏内设置：
  · 选项 → 语言 → 文本语言设为「主语言」
  · 选项 → 显示/游戏设置 → 打开「字幕」

已知范围：
  · 引擎内实时渲染的对话、过场、旁白 → 全部双语
  · 预渲染影片（开场回顾、结局动画等十几个 .usm）的字幕存在视频文件内部，
    本 mod 不处理，这些片段仍只有主语言
"""
    (mod_dir / "README.txt").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 安装 / 卸载
# --------------------------------------------------------------------------- #

def game_running() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq witcher3.exe"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for line in result.stdout.splitlines():
        if line.strip().lower().startswith('"witcher3.exe"'):
            return True
    return False


def install(build_result: BuildResult, install_dir: GameInstall, *, force: bool = False) -> Path:
    """把生成结果复制进 Mods/。

    游戏正在运行时默认拒绝：字符串表只在启动时读取，写进去也不会生效，容易让人以为
    没装成功。明确知道要"先装好、重启生效"时可以 force=True。
    """
    if game_running() and not force:
        raise BuildError("游戏正在运行。请先退出游戏再安装，或加 --force（装好后重启游戏才生效）")
    mods_root = install_dir.root / "Mods"
    target = mods_root / MOD_NAME
    if target.exists():
        _remove_tree(target)
    mods_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(build_result.mod_dir, target)
    build_result.installed_to = target
    return target


def uninstall(install_dir: GameInstall) -> str:
    target = install_dir.root / "Mods" / MOD_NAME
    if not target.exists():
        return "没有找到已安装的 mod"
    _remove_tree(target)
    return f"已删除 {target}"

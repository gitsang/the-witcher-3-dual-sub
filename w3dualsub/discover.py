"""定位游戏安装、清点可用的语言资源、读取玩家当前文本语言。

只做只读探测，绝不修改游戏目录。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import language as lang

# 巫师3 各平台的 AppID / 标识
STEAM_APP_IDS = ("292030", "3786480")  # 本体 / 可能的重制版独立条目
LANGUAGE_STEM = re.compile(r"^[a-z]{2,4}$", re.IGNORECASE)


@dataclass
class GameInstall:
    root: Path
    storefront: str
    executable: Path | None = None
    languages: dict[str, list[Path]] = field(default_factory=dict)

    @property
    def remastered(self) -> bool:
        return any(
            path.suffix.casefold() == ".w3strings" and _is_utf8(path)
            for paths in self.languages.values()
            for path in paths[:1]
        )

    def describe(self) -> str:
        codes = ", ".join(sorted(self.languages))
        return f"{self.root}（{self.storefront}）可用语言：{codes or '无'}"


def _is_utf8(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return False
    return len(head) >= 8 and head[:4] == b"RTSW" and int.from_bytes(head[4:8], "little") >= 164


def find_executable(root: Path) -> Path | None:
    for directory in ("x64_dx12", "x64", "x64_dx11"):
        candidate = root / "bin" / directory / "witcher3.exe"
        if candidate.is_file():
            return candidate
    return None


def looks_like_game(root: Path) -> bool:
    try:
        return (root / "content").is_dir() and find_executable(root) is not None
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 平台安装记录
# --------------------------------------------------------------------------- #

def _registry_values(root_key, subkey: str) -> dict[str, str]:
    try:
        import winreg
    except ImportError:
        return {}
    try:
        with winreg.OpenKey(root_key, subkey) as key:
            count = winreg.QueryInfoKey(key)[1]
            values = {}
            for index in range(count):
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    continue
                if isinstance(value, str):
                    values[name] = value
            return values
    except OSError:
        return {}


def _steam_libraries() -> list[Path]:
    libraries: list[Path] = []
    steam_roots: list[Path] = []

    try:
        import winreg

        for host, sub in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        ):
            values = _registry_values(host, sub)
            for name in ("SteamPath", "InstallPath"):
                if values.get(name):
                    steam_roots.append(Path(values[name]))
    except ImportError:
        pass

    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(variable)
        if base:
            steam_roots.append(Path(base) / "Steam")

    for steam in steam_roots:
        if not steam.is_dir():
            continue
        libraries.append(steam)
        vdf = steam / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s*"((?:[^"\\]|\\.)*)"', text):
            path = Path(match.group(1).replace("\\\\", "\\"))
            if path.is_dir():
                libraries.append(path)

    unique: dict[str, Path] = {}
    for library in libraries:
        unique.setdefault(os.path.normcase(str(library)), library)
    return list(unique.values())


def _steam_candidates() -> list[Path]:
    found: list[Path] = []
    for library in _steam_libraries():
        apps = library / "steamapps"
        installdirs: list[str] = []
        for app_id in STEAM_APP_IDS:
            manifest = apps / f"appmanifest_{app_id}.acf"
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = re.search(r'"installdir"\s*"([^"]+)"', text)
            if match:
                installdirs.append(match.group(1))
        for instaldir in installdirs:
            found.append(apps / "common" / instaldir)
        common = apps / "common"
        try:
            for child in common.iterdir():
                if child.is_dir() and "witcher 3" in child.name.casefold():
                    found.append(child)
        except OSError:
            pass
    return found


def _gog_candidates() -> list[Path]:
    found: list[Path] = []
    try:
        import winreg
    except ImportError:
        return found
    for host in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for wow in (r"SOFTWARE\WOW6432Node\GOG.com\Games", r"SOFTWARE\GOG.com\Games"):
            try:
                with winreg.OpenKey(host, wow) as key:
                    for index in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            name = winreg.EnumKey(key, index)
                        except OSError:
                            continue
                        values = _registry_values(host, f"{wow}\\{name}")
                        if "witcher 3" in values.get("gameName", "").casefold():
                            if values.get("path"):
                                found.append(Path(values["path"]))
            except OSError:
                continue
    return found


def _epic_candidates() -> list[Path]:
    found: list[Path] = []
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    manifests = Path(program_data) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    try:
        items = list(manifests.glob("*.item"))
    except OSError:
        return found
    for item in items:
        try:
            data = json.loads(item.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if "witcher 3" in str(data.get("DisplayName", "")).casefold():
            location = data.get("InstallLocation")
            if location:
                found.append(Path(location))
    return found


# --------------------------------------------------------------------------- #
# 语言清点
# --------------------------------------------------------------------------- #

def scan_languages(root: Path) -> dict[str, list[Path]]:
    """在 content 与 dlc 下递归查找 <语言>.w3strings。

    目录名大小写用实际扫描结果（``DLC`` / ``dlc`` 在不同版本、不同文件系统下不一致）。
    """
    inventory: dict[str, list[Path]] = {}
    bases: list[Path] = []
    try:
        for entry in os.scandir(root):
            try:
                if entry.is_dir() and entry.name.casefold() in ("content", "dlc"):
                    bases.append(Path(entry.path))
            except OSError:
                continue
    except OSError:
        return inventory
    for base in bases:
        for parent, _, names in os.walk(base):
            for name in names:
                path = Path(parent) / name
                if path.suffix.casefold() != ".w3strings":
                    continue
                if not LANGUAGE_STEM.match(path.stem):
                    continue
                inventory.setdefault(path.stem.casefold(), []).append(path)
    return {code: sorted(paths) for code, paths in sorted(inventory.items())}


def _documents_dirs() -> list[Path]:
    candidates: list[Path] = []
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            personal = winreg.QueryValueEx(key, "Personal")[0]
        candidates.append(Path(os.path.expandvars(personal)))
    except (ImportError, OSError, TypeError, ValueError):
        pass
    for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Documents")
    candidates.append(Path.home() / "Documents")
    unique: dict[str, Path] = {}
    for candidate in candidates:
        unique.setdefault(os.path.normcase(str(candidate)), candidate)
    return list(unique.values())


def configured_text_language() -> str | None:
    """读 user.settings 里当前生效的文本语言，用来预选主语言。"""
    for documents in _documents_dirs():
        path = documents / "The Witcher 3" / "user.settings"
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        text = None
        for encoding in ("utf-8-sig", "utf-16"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeError:
                continue
        if text is None:
            continue
        section = ""
        values: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip().casefold()
                continue
            if section != "localization" or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().casefold() in ("textlanguage", "requestedtextlanguage"):
                values[key.strip().casefold()] = value.strip().strip('"').strip("'")
        for key in ("textlanguage", "requestedtextlanguage"):
            code = values.get(key, "").strip().casefold()
            if LANGUAGE_STEM.match(code):
                return code
    return None


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #

def open_install(root: str | Path | None = None) -> GameInstall:
    if root is not None:
        path = Path(root).expanduser().resolve()
        if not looks_like_game(path):
            raise FileNotFoundError(
                f"{path} 不像巫师3 安装目录（需要 content 目录和 bin\\x64\\witcher3.exe）"
            )
        return GameInstall(path, "手动指定", find_executable(path), scan_languages(path))

    seen: dict[str, GameInstall] = {}
    for storefront, provider in (
        ("Steam", _steam_candidates),
        ("GOG", _gog_candidates),
        ("Epic", _epic_candidates),
    ):
        for candidate in provider():
            try:
                path = candidate.expanduser().resolve()
            except OSError:
                continue
            key = os.path.normcase(str(path))
            if key in seen or not looks_like_game(path):
                continue
            seen[key] = GameInstall(path, storefront, find_executable(path), scan_languages(path))

    if not seen:
        raise FileNotFoundError(
            "没有自动找到巫师3。请用 --game 指定游戏根目录"
            "（里面有 content 目录和 bin\\x64\\witcher3.exe 的那层）。"
        )
    return next(iter(seen.values()))


def support_report(install: GameInstall) -> list[str]:
    """列出检测到的语言，并标注是否支持（未知语言密钥会明确说清楚）。"""
    lines = []
    for code, paths in install.languages.items():
        known = lang.by_code(code)
        if known is None:
            note = "未收录的语言代码"
        else:
            note = "明文（不混淆）" if known.plain else f"magic 0x{known.magic:08X}"
        lines.append(f"  {code:<5} {lang.label(code):<28} {len(paths)} 个资源  {note}")
    return lines

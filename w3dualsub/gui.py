"""Tkinter 图形界面：选游戏目录、选两种语言、生成并安装。

界面故意做得简单：所有选项都有安全默认值，点「生成并安装」就能用。
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, build as build_mod, discover, language as lang
from . import merge as merge_mod

MODE_LABELS = {
    merge_mod.MODE_DIALOGUE: "只双语字幕（推荐）——界面/物品/任务名保持主语言",
    merge_mod.MODE_ALL: "全部文本双语——连菜单和物品说明也双语",
}
SEP_LABELS = {
    merge_mod.SEP_AUTO: "自动：整句换行，短语用空格",
    merge_mod.SEP_BR: "总是换行",
    merge_mod.SEP_SPACE: "总是同一行（空格分隔）",
    merge_mod.SEP_PIPE: "同一行，用 | 分隔",
}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.install: discover.GameInstall | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None

        root.title(f"巫师3 双语字幕生成器 v{__version__}")
        root.minsize(780, 560)

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)

        # 游戏目录
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Label(row, text="游戏目录").pack(side="left")
        self.game_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.game_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="浏览…", command=self.browse).pack(side="left")
        ttk.Button(row, text="检测", command=self.detect).pack(side="left", padx=(6, 0))

        # 语言选择
        langs = ttk.LabelFrame(frame, text="字幕语言", padding=10)
        langs.pack(fill="x", pady=(10, 0))
        ttk.Label(langs, text="主字幕").grid(row=0, column=0, sticky="w")
        self.primary = ttk.Combobox(langs, state="readonly", width=28)
        self.primary.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(langs, text="副字幕").grid(row=0, column=2, sticky="w", padx=(20, 0))
        self.secondary = ttk.Combobox(langs, state="readonly", width=28)
        self.secondary.grid(row=0, column=3, sticky="w", padx=6)
        self.swap = ttk.Button(langs, text="⇄ 互换", command=self.swap_languages)
        self.swap.grid(row=0, column=4, padx=(10, 0))
        self.available = ttk.Label(langs, text="尚未检测到游戏", foreground="#666")
        self.available.grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))

        # 选项
        options = ttk.LabelFrame(frame, text="选项", padding=10)
        options.pack(fill="x", pady=(10, 0))
        ttk.Label(options, text="范围").grid(row=0, column=0, sticky="w")
        self.mode = ttk.Combobox(options, state="readonly", width=52,
                                 values=list(MODE_LABELS.values()))
        self.mode.current(0)
        self.mode.grid(row=0, column=1, columnspan=3, sticky="w", padx=6)

        ttk.Label(options, text="分隔").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.sep = ttk.Combobox(options, state="readonly", width=52,
                                values=list(SEP_LABELS.values()))
        self.sep.current(0)
        self.sep.grid(row=1, column=1, columnspan=3, sticky="w", padx=6, pady=(8, 0))

        self.only_sentences = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="只合并以标点结尾的完整句子（界面更干净，可能漏几句短台词）",
                        variable=self.only_sentences).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))

        self.do_install = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="生成后直接安装到游戏 Mods/ 目录（游戏需先退出）",
                        variable=self.do_install).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # 按钮
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(12, 0))
        self.build_button = ttk.Button(actions, text="生成并安装", command=self.build)
        self.build_button.pack(side="left")
        ttk.Button(actions, text="打开输出目录", command=self.open_output).pack(side="left", padx=6)
        ttk.Button(actions, text="卸载 mod", command=self.uninstall).pack(side="left")

        # 日志
        log_frame = ttk.LabelFrame(frame, text="日志", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.log = tk.Text(log_frame, height=14, wrap="word", state="disabled",
                           font=("Consolas", 9))
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.out_dir = Path.cwd() / "build"
        self.root.after(120, self.log_queue_pump)
        self.root.after(200, self.detect)

    # ------------------------------------------------------------------ 日志

    def write(self, message: str) -> None:
        self.log_queue.put(message)

    def log_queue_pump(self) -> None:
        while not self.log_queue.empty():
            message = self.log_queue.get_nowait()
            self.log.configure(state="normal")
            self.log.insert("end", message + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(120, self.log_queue_pump)

    # ------------------------------------------------------------------ 探测

    def browse(self) -> None:
        chosen = filedialog.askdirectory(title="选择《巫师3》游戏根目录")
        if chosen:
            self.game_var.set(chosen)
            self.detect()

    def detect(self) -> None:
        manual = self.game_var.get().strip() or None
        try:
            install = discover.open_install(manual)
        except FileNotFoundError as error:
            self.write(f"✗ {error}")
            self.available.configure(text="没有找到游戏，请手动选择游戏根目录")
            return
        self.install = install
        self.game_var.set(str(install.root))
        codes = sorted(install.languages, key=lambda code: (
            0 if code in ("cn", "zh", "en") else 1, code))
        values = [lang.label(code) for code in codes]
        self.primary["values"] = values
        self.secondary["values"] = values

        configured = discover.configured_text_language()
        primary = configured if configured in codes else ("cn" if "cn" in codes else codes[0])
        secondary = "en" if primary != "en" and "en" in codes else next(
            (code for code in codes if code != primary), primary)
        self.primary.set(lang.label(primary))
        self.secondary.set(lang.label(secondary))

        self.write(f"✓ 游戏：{install.root}")
        self.write(f"  当前文本语言：{configured or '未知'}    可用语言 {len(codes)} 种")
        self.available.configure(
            text="可用语言：" + "、".join(lang.label(code) for code in codes))

    # ------------------------------------------------------------------ 动作

    def swap_languages(self) -> None:
        left, right = self.primary.get(), self.secondary.get()
        self.primary.set(right)
        self.secondary.set(left)

    def _code(self, combobox: ttk.Combobox) -> str:
        text = combobox.get()
        return text.rsplit("(", 1)[-1].rstrip(")") if "(" in text else text

    def build(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self.install is None:
            messagebox.showwarning("还没选游戏", "请先检测或选择游戏根目录")
            return
        primary, secondary = self._code(self.primary), self._code(self.secondary)
        if not primary or primary == secondary:
            messagebox.showwarning("语言不对", "主副字幕必须是两种不同的语言")
            return
        mode = list(MODE_LABELS)[self.mode.current()]
        separator = list(SEP_LABELS)[self.sep.current()]
        options = merge_mod.MergeOptions(
            mode=mode, separator=separator, only_sentences=self.only_sentences.get())

        install = self.install
        do_install = self.do_install.get()
        out_dir = self.out_dir
        self.build_button.configure(state="disabled")
        self.write(f"\n=== 开始生成 {lang.label(primary)} + {lang.label(secondary)} ===")

        def work():
            try:
                result = build_mod.build(install, primary, secondary, options,
                                         out_dir=out_dir, progress=self.write)
                self.write(f"✓ 生成完成：{result.mod_dir}")
                self.write(f"  输出 {', '.join(result.files)}")
                self.write(f"  双语台词 {result.merged_total}/{result.entries_total}")
                if do_install:
                    target = build_mod.install(result, install)
                    self.write(f"✓ 已安装到 {target}")
                    self.write("  启动游戏后确认：文本语言 = 主字幕语言，字幕已开启。")
                else:
                    self.write("  未安装。要安装，把 build/modW3DualSub 复制到 游戏目录/Mods")
            except Exception as error:  # noqa: BLE001 - 界面上要显示任何失败原因
                self.write(f"✗ 失败：{error}")
            finally:
                self.root.after(0, lambda: self.build_button.configure(state="normal"))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def uninstall(self) -> None:
        if self.install is None:
            return
        if not messagebox.askyesno("卸载", "删除 Mods/modW3DualSub？"):
            return
        try:
            self.write(build_mod.uninstall(self.install))
        except Exception as error:  # noqa: BLE001
            self.write(f"✗ {error}")

    def open_output(self) -> None:
        path = self.out_dir
        path.mkdir(parents=True, exist_ok=True)
        try:
            import os
            os.startfile(path)  # noqa: S606 - Windows 专用
        except OSError:
            self.write(str(path))


def main() -> int:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

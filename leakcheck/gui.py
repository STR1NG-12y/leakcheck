"""图形界面（Tkinter，浅色主题）。

设计目标
--------
- 把命令行那套能力变成"点按钮就能用"：建索引 / 路径判定 / 内容比对 / 三阶扫描 / 关键词
- 浅色配色，不用黑底终端
- 所有耗时任务放到后台线程，界面不卡；结果回到表格里

启动：
    python -m leakcheck.gui
    leakcheck.exe gui

左栏导航切换页面，底部表格显示最近一次任务的结果，状态栏显示进度。
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import Config
from .loader import collect, read_text
from .matcher import Matcher
from .parse import parse
from .pathpolicy import PathPolicy, parse_keyword_file, save_keyword_file

# ---------------------------------------------------------------- 配色
BG = "#F5F7FA"
CARD = "#FFFFFF"
BORDER = "#E5E7EB"
TEXT = "#1F2937"
MUTED = "#6B7280"
ACCENT = "#2563EB"
ACCENT_DARK = "#1D4ED8"
ACCENT_LIGHT = "#EFF4FF"
GREEN = "#16A34A"
RED = "#DC2626"
ORANGE = "#D97706"
FIELD = "#F9FAFB"

FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 18, "bold")
FONT_H2 = ("Microsoft YaHei UI", 12, "bold")


def with_idx_suffix(name: str) -> str:
    """只填名字时自动补 .idx；已经带后缀就原样返回。"""
    text = name.strip()
    if not text:
        return text
    path = Path(text)
    if path.suffix:
        return text
    return text + ".idx"


def _root_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


DEFAULT_KEYWORDS = """# 一行一组：[类别] 词 词 词
# 命中任意一个词就认为该文件敏感（优先级最高）
[contract] 合同 协议 法务 补充协议
[finance] 薪酬 工资 财务报表 预算 报销
[secret] 密钥 密码 secret token api_key
[hr] 简历 员工名单 绩效考核
"""

ROOT = _root_dir()
#: 用户放资料的地方（发布版会预建这三个子目录）
DATA = ROOT / "我的资料"
DATA_SENSITIVE = DATA / "敏感文件"
DATA_PAYLOAD = DATA / "待检测"
DATA_INDEX = DATA / "索引"
INDEX = DATA_INDEX / "leakcheck.idx"
KEYWORDS = ROOT / "关键词.txt"


class Tooltip:
    """鼠标悬停显示完整路径（输入框里长路径会被截断）。"""

    def __init__(self, widget: tk.Widget, text_fn, delay: int = 450) -> None:
        self.widget = widget
        self.text_fn = text_fn
        self.delay = delay
        self._after_id: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self) -> None:
        text = self.text_fn()
        if not text:
            return
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{self.widget.winfo_rootx() + 12}"
                              f"+{self.widget.winfo_rooty() + self.widget.winfo_height() + 4}")
        tk.Label(self._tip, text=text, bg="#1F2937", fg="white",
                 font=("Microsoft YaHei UI", 9), padx=8, pady=4,
                 wraplength=640, justify="left").pack()

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("leakcheck · 敏感内容外发检测")
        # 按屏幕大小自适应：高 DPI 下 1120 可能超出屏幕，右侧按钮就会被切掉
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(1120, max(880, sw - 80))
        height = min(720, max(600, sh - 120))
        self.geometry(f"{width}x{height}+{max(0, (sw - width) // 2)}+"
                      f"{max(0, (sh - height) // 3)}")
        self.minsize(880, 600)
        self.configure(bg=BG)

        self.cfg = Config()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self._job_failed = False
        self._source_map: dict = {}
        self.current_page = "build"

        self._ensure_data_files()
        self._init_style()
        self._build_header()
        self._build_body()
        self._build_status()
        self._apply_data_defaults()
        self.show_page("build")
        self.after(80, self._drain)

    # ------------------------------------------------------------ 路径选择
    @staticmethod
    def _split_paths(text: str) -> list[str]:
        """输入框里可以放一个路径，也可以用 ; 分隔多个（可混着目录和文件）。"""
        parts = [p.strip().strip('"') for p in text.replace("\n", ";").split(";")]
        return [p for p in parts if p]

    def _choose_dir(self, var, title: str, initial: Path | None = None) -> None:
        path = filedialog.askdirectory(title=title, initialdir=str(initial or ROOT))
        if path:
            var.set(path)

    def _choose_files(self, var, title: str, initial: Path | None = None) -> None:
        paths = filedialog.askopenfilenames(
            title=title, initialdir=str(initial or ROOT),
            filetypes=[("常见文档", "*.txt *.md *.csv *.docx *.pdf *.xlsx *.pptx *.json *.py *.js *.ts"),
                       ("所有文件", "*.*")])
        if paths:
            var.set(" ; ".join(paths))

    def _path_picker(self, var, title: str, initial: Path | None = None):
        """返回给 _form_row 用的 browse 回调：点一下弹菜单，可选目录或文件。"""
        def pick(btn: tk.Button) -> None:
            menu = tk.Menu(self, tearoff=0)
            menu.add_command(label="选择文件夹…",
                             command=lambda: self._choose_dir(var, title, initial))
            menu.add_command(label="选择文件（可多选）…",
                             command=lambda: self._choose_files(var, title, initial))
            try:
                menu.tk_popup(btn.winfo_rootx(),
                              btn.winfo_rooty() + btn.winfo_height())
            finally:
                menu.grab_release()
        return pick

    def _resolve_index_path(self, text: str) -> Path:
        """只填名字时落到 我的资料\\索引\\；填了路径就按路径来。"""
        raw = with_idx_suffix(text.strip()) or "leakcheck.idx"
        path = Path(raw)
        if path.is_absolute() or path.parent != Path("."):
            return path
        return DATA_INDEX / path.name

    def _apply_data_defaults(self) -> None:
        """如果程序旁边有「我的资料」目录，就把各页的默认路径指过去。"""
        if DATA_SENSITIVE.exists():
            for name in ("build_dir", "scan_dir", "screen_dir"):
                var = getattr(self, name, None)
                if var is not None and not var.get().strip():
                    var.set(str(DATA_SENSITIVE))
        if DATA_PAYLOAD.exists():
            var = getattr(self, "scan_file", None)
            if var is not None and not var.get().strip():
                var.set(str(DATA_PAYLOAD))


    def _ensure_data_files(self) -> None:
        """首次运行自动生成关键词表，避免界面上显示"不存在"。"""
        if not KEYWORDS.exists():
            try:
                KEYWORDS.write_text(DEFAULT_KEYWORDS, encoding="utf-8")
            except Exception:                      # noqa: BLE001
                pass

    def report_callback_exception(self, exc, val, tb) -> None:   # noqa: N802
        """Tk 回调里的异常：弹窗提示，不再只打印到控制台。"""
        import traceback
        detail = "".join(traceback.format_exception(exc, val, tb))
        try:
            self._set_status(f"出错：{val}", RED)
        except Exception:                          # noqa: BLE001
            pass
        try:
            print(detail, file=sys.stderr)
        except Exception:                          # noqa: BLE001
            pass
        messagebox.showerror("操作失败", str(val))

    # ------------------------------------------------------------ 样式
    def _init_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", background=CARD, fieldbackground=CARD,
                        foreground=TEXT, rowheight=26, borderwidth=0, font=FONT_SMALL)
        style.configure("Treeview.Heading", background="#F3F4F6", foreground=MUTED,
                        relief="flat", font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Treeview", background=[("selected", ACCENT_LIGHT)],
                  foreground=[("selected", TEXT)])
        style.configure("Horizontal.TProgressbar", troughcolor="#EFF1F5",
                        background=ACCENT, borderwidth=0, thickness=8)

    # ------------------------------------------------------------ 顶栏
    def _build_header(self) -> None:
        bar = tk.Frame(self, bg=CARD, height=56)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)

        logo = tk.Canvas(bar, width=28, height=28, bg=CARD, highlightthickness=0)
        logo.create_rectangle(0, 0, 28, 28, fill=ACCENT, outline="")
        logo.create_text(14, 14, text="L", fill="white",
                         font=("Microsoft YaHei UI", 12, "bold"))
        logo.pack(side="left", padx=(20, 10), pady=14)

        tk.Label(bar, text="leakcheck", bg=CARD, fg=TEXT,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        tk.Label(bar, text="  敏感内容外发检测", bg=CARD, fg=MUTED,
                 font=FONT_SMALL).pack(side="left")

        self.header_status = tk.Label(bar, text="", bg=CARD, fg=MUTED, font=FONT_SMALL)
        self.header_status.pack(side="right", padx=20)
        self._refresh_header()

        tk.Frame(self, bg=BORDER, height=1).pack(side="top", fill="x")

    def _refresh_header(self) -> None:
        if INDEX.exists():
            size = INDEX.stat().st_size / 1024
            self.header_status.config(text=f"● 索引已建立  {size:.0f} KB", fg=GREEN)
        else:
            self.header_status.config(text="○ 索引未建立", fg=MUTED)

    # ------------------------------------------------------------ 主体
    def _build_body(self) -> None:
        body = tk.Frame(self, bg=BG)
        body.pack(side="top", fill="both", expand=True)

        self.sidebar = tk.Frame(body, bg=CARD, width=196)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")

        self.main = tk.Frame(body, bg=BG)
        self.main.pack(side="left", fill="both", expand=True)

        self._build_sidebar()
        self._build_pages()
        self._build_results()

    def _build_sidebar(self) -> None:
        self.nav_buttons: dict[str, tk.Button] = {}
        items = [
            ("build", "①", "建立索引"),
            ("paths", "②", "路径判定"),
            ("scan", "③", "两阶扫描"),
            ("screen", "④", "语义筛选"),
            ("keywords", "⑤", "关键词"),
            ("about", "⑥", "设置"),
        ]
        for key, num, label in items:
            btn = tk.Button(self.sidebar, text=f"  {num}  {label}", anchor="w",
                            bg=CARD, fg=TEXT, activebackground=ACCENT_LIGHT,
                            activeforeground=ACCENT, relief="flat", bd=0,
                            font=FONT, cursor="hand2", padx=10, pady=10,
                            command=lambda k=key: self.show_page(k))
            btn.pack(fill="x", padx=12, pady=2)
            self.nav_buttons[key] = btn

        tk.Frame(self.sidebar, bg=BORDER, height=1).pack(side="bottom", pady=(0, 60),
                                                         padx=16, fill="x")
        info = tk.Frame(self.sidebar, bg=CARD)
        info.pack(side="bottom", fill="x", padx=20, pady=(0, 16))
        tk.Label(info, text=f"Python {sys.version.split()[0]}", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 8), anchor="w").pack(fill="x")
        tk.Label(info, text="索引 leakcheck.idx", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 8), anchor="w").pack(fill="x")

    # ------------------------------------------------------------ 页面容器
    def _build_pages(self) -> None:
        self.pages: dict[str, tk.Frame] = {}
        self.page_container = tk.Frame(self.main, bg=BG)
        self.page_container.pack(side="top", fill="both", expand=True)

        self.pages["build"] = self._page_build()
        self.pages["paths"] = self._page_paths()
        self.pages["scan"] = self._page_scan()
        self.pages["screen"] = self._page_screen()
        self.pages["keywords"] = self._page_keywords()
        self.pages["about"] = self._page_about()

    def show_page(self, key: str) -> None:
        self.current_page = key
        for k, frame in self.pages.items():
            frame.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        for k, btn in self.nav_buttons.items():
            if k == key:
                btn.config(bg=ACCENT_LIGHT, fg=ACCENT, font=(FONT[0], FONT[1], "bold"))
            else:
                btn.config(bg=CARD, fg=TEXT, font=FONT)
        self._set_status("就绪")

    # ------------------------------------------------------------ 通用控件
    def _title(self, parent: tk.Frame, text: str, sub: str) -> None:
        tk.Label(parent, text=text, bg=BG, fg=TEXT, font=FONT_TITLE,
                 anchor="w").pack(fill="x", padx=28, pady=(18, 0))
        tk.Label(parent, text=sub, bg=BG, fg=MUTED, font=FONT_SMALL,
                 anchor="w").pack(fill="x", padx=28, pady=(2, 10))

    def _card(self, parent: tk.Frame) -> tk.Frame:
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="x", padx=28)
        card = tk.Frame(wrap, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x")
        return card

    def _source_row(self, card: tk.Frame, var: tk.StringVar) -> None:
        """数据来源二选一：用索引 还是 直接扫敏感资料。"""
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=20, pady=(14, 2))
        tk.Label(row, text="数据来源", bg=CARD, fg=TEXT, font=FONT,
                 width=13, anchor="w").pack(side="left")
        for value, label in (("index", "用索引"),
                             ("dir", "直接扫敏感资料")):
            tk.Radiobutton(row, text=label, value=value, variable=var,
                           bg=CARD, fg=TEXT, activebackground=CARD,
                           selectcolor=CARD, font=FONT_SMALL,
                           highlightthickness=0, bd=0,
                           command=lambda v=var: self._apply_source(v)).pack(
                side="left", padx=(0, 16))
        hint = tk.Label(card, text="", bg=CARD, fg=MUTED,
                        font=("Microsoft YaHei UI", 8), anchor="w")
        hint.pack(fill="x", padx=20, pady=(0, 2))
        return hint

    def _apply_source(self, var) -> None:
        """只把不参与的那一项文字调灰，不禁用——免得看不清也点不动。"""
        entries, hint = self._source_map.get(id(var), ([], None))
        if len(entries) < 2:
            return
        index_mode = var.get() == "index"

        def set_state(entry: tk.Entry, active: bool) -> None:
            if active:
                entry.configure(state="normal", fg=TEXT, bg=CARD)
            else:
                entry.configure(state="disabled",
                                disabledbackground="#ECEEF1",
                                disabledforeground="#9AA0A6")
            btn = getattr(entry, "_browse_btn", None)
            if btn is not None:
                btn.configure(state="normal" if active else "disabled",
                              fg=TEXT if active else "#B0B4BA")

        set_state(entries[0], index_mode)
        set_state(entries[1], not index_mode)
        if hint is not None:
            hint.config(
                text="当前只用索引，下面「敏感资料」不参与比对"
                     if index_mode else
                     "当前直接扫「敏感资料」，上面的索引不参与比对")

    def _form_row(self, card: tk.Frame, label: str, var: tk.StringVar,
                  browse, width: int = 60) -> None:
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=20, pady=8)
        tk.Label(row, text=label, bg=CARD, fg=TEXT, font=FONT,
                 width=13, anchor="w").pack(side="left")
        # 顺序很关键：先把「选择…」靠右占位，输入框再吃剩下的空间。
        # 反过来的话窗口一窄，最后 pack 的按钮会被挤到窗口外看不见。
        browse_btn = tk.Button(row, text="选择…", bg=CARD, fg=TEXT,
                               relief="flat", highlightbackground=BORDER,
                               highlightthickness=1, bd=0, font=FONT_SMALL,
                               padx=14, pady=4, cursor="hand2")
        browse_btn.config(command=lambda: browse(browse_btn))
        browse_btn.pack(side="right", padx=(8, 0))
        entry = tk.Entry(row, textvariable=var, font=FONT, bg=CARD, fg=TEXT,
                         width=8, relief="flat", highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=ACCENT,
                         insertbackground=ACCENT)
        entry.pack(side="left", fill="x", expand=True, ipady=6)
        entry._browse_btn = browse_btn          # 供数据来源切换时一起禁用

        def _scroll_to_end() -> None:
            try:
                entry.xview_moveto(1.0)
            except tk.TclError:
                pass

        def _sync(*_args) -> None:
            # 长路径默认看开头，这里滚到末尾，优先露出文件名 / 最后一级目录。
            # 必须等控件重排完再滚，否则会被重置回开头。
            entry.after_idle(_scroll_to_end)
            # 有内容 → 边框变蓝、字加粗，一眼能看出"这一项填了"
            if var.get().strip():
                entry.configure(font=("Microsoft YaHei UI", 10, "bold"),
                                highlightbackground="#93B4F5",
                                highlightcolor=ACCENT)
            else:
                entry.configure(font=FONT, highlightbackground=BORDER)

        var.trace_add("write", _sync)
        _sync()
        Tooltip(entry, lambda: var.get().strip())
        return entry


    def _primary(self, card: tk.Frame, text: str, command) -> tk.Button:
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=20, pady=(12, 16))
        btn = tk.Button(row, text=text, command=command, bg=ACCENT, fg="white",
                        activebackground=ACCENT_DARK, activeforeground="white",
                        relief="flat", bd=0, font=("Microsoft YaHei UI", 10, "bold"),
                        padx=22, pady=8, cursor="hand2")
        btn.pack(side="left")
        return btn

    # ------------------------------------------------------------ 页面：建索引
    def _page_build(self) -> tk.Frame:
        page = tk.Frame(self.main, bg=BG)
        self._title(page, "建立索引",
                    "按路径规则扫描一个目录，把敏感文件的指纹存进索引。"
                    "关键词表可选：词出现在路径里就判为敏感。")
        card = self._card(page)

        self.build_dir = tk.StringVar()
        self.build_out = tk.StringVar(value="leakcheck")
        self.build_kw = tk.StringVar(value="关键词.txt" if KEYWORDS.exists() else "")

        self._form_row(card, "敏感资料目录", self.build_dir,
                      self._path_picker(self.build_dir, "选择敏感资料（文件夹或文件）"))
        self._form_row(card, "输出索引", self.build_out,
                      lambda _btn: self._pick_out())
        self._form_row(card, "关键词表", self.build_kw,
                      lambda _btn: self._pick_kw())

        tip = tk.Frame(card, bg=CARD)
        tip.pack(fill="x", padx=20, pady=(0, 2))
        tk.Label(tip, text="只填名字即可：自动存到 我的资料\\索引\\ 并补 .idx（例如 mylib → 我的资料\\索引\\mylib.idx）",
                 bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side="left")

        self.btn_build = self._primary(card, "开始建索引", self.run_build)
        return page

    def _pick_build_dir(self) -> None:
        path = filedialog.askdirectory(title="选择敏感资料目录")
        if path:
            self.build_dir.set(path)

    def _pick_out(self) -> None:
        path = filedialog.asksaveasfilename(title="索引保存为（会自动补 .idx）", defaultextension=".idx",
                                           filetypes=[("索引", "*.idx")])
        if path:
            self.build_out.set(path)

    def _pick_kw(self) -> None:
        path = filedialog.askopenfilename(title="选择关键词表",
                                          filetypes=[("文本", "*.txt"), ("所有文件", "*.*")])
        if path:
            self.build_kw.set(path)

    # ------------------------------------------------------------ 页面：路径判定
    def _page_paths(self) -> tk.Frame:
        page = tk.Frame(self.main, bg=BG)
        self._title(page, "路径判定", "只看路径该不该保护，不读文件内容，不需要索引。")
        card = self._card(page)

        self.paths_target = tk.StringVar()
        self._form_row(card, "文件或目录", self.paths_target,
                      lambda _btn: self._pick_paths_target())
        self.btn_paths = self._primary(card, "开始判定", self.run_paths)
        return page

    def _pick_paths_target(self) -> None:
        if messagebox.askyesno("选择", "要选一个目录吗？\n（选“否”则选单个文件）"):
            path = filedialog.askdirectory(title="选择目录")
        else:
            path = filedialog.askopenfilename(title="选择文件")
        if path:
            self.paths_target.set(path)

    # ------------------------------------------------------------ 页面：内容比对
    # ------------------------------------------------------------ 页面：两阶扫描
    def _page_scan(self) -> tk.Frame:
        page = tk.Frame(self.main, bg=BG)
        self._title(page, "两阶扫描",
                    "逐字 → 骨架：抓同义改写和语序调整。"
                    "填了索引文件就直接用 ① 建好的库，不用再扫目录。")
        card = self._card(page)

        self.scan_index = tk.StringVar(value=str(INDEX) if INDEX.exists() else "")
        self.scan_dir = tk.StringVar()
        self.scan_file = tk.StringVar()
        self.scan_source = tk.StringVar(value="index" if INDEX.exists() else "dir")
        hint = self._source_row(card, self.scan_source)
        e_index = self._form_row(card, "索引", self.scan_index,
                                 self._path_picker(self.scan_index,
                                                   "选择索引（.idx）", DATA_INDEX))
        e_dir = self._form_row(card, "敏感资料", self.scan_dir,
                               self._path_picker(self.scan_dir,
                                                 "选择敏感资料（文件夹或文件）",
                                                 DATA_SENSITIVE))
        self._source_map[id(self.scan_source)] = ([e_index, e_dir], hint)
        self._apply_source(self.scan_source)
        self._form_row(card, "待检测文件", self.scan_file,
                      lambda _btn: self._pick_file_for(
                          self.scan_file, "选择待检测文件"))
        self.btn_scan = self._primary(card, "开始扫描", self.run_scan)
        return page

    def _pick_scan_dir(self) -> None:
        path = filedialog.askdirectory(title="选择敏感资料目录")
        if path:
            self.scan_dir.set(path)

    def _pick_scan_index(self) -> None:
        path = filedialog.askopenfilename(title="选择索引（① 建好的 .idx）",
                                          filetypes=[("索引", "*.idx"), ("所有文件", "*.*")])
        if path:
            self.scan_index.set(path)

    @staticmethod
    def _score_band(score: float) -> tuple[str, str, str]:
        """(文字档位, 颜色, 行标签)"""
        if score >= 0.85:
            return "高度相似", RED, "bad"
        if score >= 0.70:
            return "像改写", ORANGE, "warn"
        if score >= 0.60:
            return "可能相关", ORANGE, "warn"
        return "只是同话题", MUTED, "muted"

    def _pick_file_for(self, var, title: str) -> None:
        path = filedialog.askopenfilename(
            title=title, initialdir=str(DATA_PAYLOAD),
            filetypes=[("常见文档", "*.txt;*.md;*.csv;*.docx;*.pdf;*.xlsx;*.pptx;*.json"),
                       ("所有文件", "*.*")])
        if path:
            var.set(path)

    def _pick_scan_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择待检测文件",
            filetypes=[("支持的格式", "*.txt *.md *.docx *.pdf *.xlsx *.pptx"),
                       ("所有文件", "*.*")])
        if path:
            self.scan_file.set(path)

    # ------------------------------------------------------------ 页面：语义筛选
    def _page_screen(self) -> tk.Frame:
        page = tk.Frame(self.main, bg=BG)
        self._title(page, "语义筛选",
                    "用向量模型找「意思相近」的块。产出是线索，不是证据，不会自动阻断。")
        card = self._card(page)

        self.screen_dir = tk.StringVar()
        self.screen_file = tk.StringVar()
        self.screen_index = tk.StringVar(value=str(INDEX) if INDEX.exists() else "")
        self.screen_source = tk.StringVar(value="index" if INDEX.exists() else "dir")
        hint = self._source_row(card, self.screen_source)
        e_index = self._form_row(card, "索引", self.screen_index,
                                 self._path_picker(self.screen_index,
                                                   "选择索引（.idx）", DATA_INDEX))
        e_dir = self._form_row(card, "敏感资料", self.screen_dir,
                               self._path_picker(self.screen_dir,
                                                 "选择敏感资料（文件夹或文件）",
                                                 DATA_SENSITIVE))
        self._source_map[id(self.screen_source)] = ([e_index, e_dir], hint)
        self._apply_source(self.screen_source)
        self._form_row(card, "待检测文件", self.screen_file,
                      lambda _btn: self._pick_file_for(
                          self.screen_file, "选择待检测文件"))

        from .vector_screen import available_backends, find_local_models
        state = available_backends()
        local = find_local_models()
        if local["onnx"] and state["onnxruntime"]:
            text, color = f"● 模型可用：{local['onnx'][0].stem}（内置）", GREEN
        elif state["sentence_transformers"]:
            text, color = "● 模型可用：sentence-transformers", GREEN
        elif state["onnxruntime"]:
            text, color = "● 模型可用：onnxruntime（需指定 .onnx 模型）", GREEN
        else:
            text, color = ("● 未安装向量模型 → 使用内置降级模式"
                           "（只有字面重合，没有真语义能力）"), ORANGE
        tip = tk.Frame(card, bg=CARD)
        tip.pack(fill="x", padx=20, pady=(0, 2))
        tk.Label(tip, text=text, bg=CARD, fg=color, font=FONT_SMALL).pack(side="left")
        tk.Label(tip, text="  安装：pip install leakcheck[vector]", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(side="left")

        self.btn_screen = self._primary(card, "开始筛选", self.run_screen)
        return page

    # ------------------------------------------------------------ 页面：关键词
    def _page_keywords(self) -> tk.Frame:
        page = tk.Frame(self.main, bg=BG)
        self._title(page, "关键词",
                    "关键词只看路径（目录名/文件名），命中即判为敏感；改完记得重建索引。")
        card = self._card(page)

        self.kw_file = tk.StringVar(value=str(KEYWORDS))
        row0 = tk.Frame(card, bg=CARD)
        row0.pack(fill="x", padx=20, pady=(16, 6))
        tk.Label(row0, text="当前文件", bg=CARD, fg=TEXT, font=FONT,
                 width=13, anchor="w").pack(side="left")
        tk.Entry(row0, textvariable=self.kw_file, font=FONT_SMALL, bg=FIELD, fg=TEXT,
                 relief="flat", highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=ACCENT).pack(side="left", fill="x", expand=True, ipady=5)
        tk.Button(row0, text="更换…", command=self._pick_keyword_file, bg=CARD, fg=TEXT,
                  relief="flat", highlightbackground=BORDER, highlightthickness=1,
                  bd=0, font=FONT_SMALL, padx=14, pady=4,
                  cursor="hand2").pack(side="left", padx=(8, 0))

        self.kw_text = tk.Text(card, height=7, font=("Consolas", 10), bg=FIELD, fg=TEXT,
                               relief="flat", highlightthickness=1,
                               highlightbackground=BORDER, wrap="none")
        self.kw_text.pack(fill="x", padx=20, pady=(6, 8))
        self.kw_text.insert("1.0", self._read_keywords())

        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=20, pady=(0, 16))
        tk.Button(row, text="保存", command=self.save_keywords, bg=ACCENT, fg="white",
                  relief="flat", bd=0, font=("Microsoft YaHei UI", 10, "bold"),
                  padx=20, pady=6, cursor="hand2").pack(side="left")
        tk.Button(row, text="重新加载", command=self.reload_keywords, bg=CARD, fg=TEXT,
                  relief="flat", highlightbackground=BORDER, highlightthickness=1,
                  bd=0, font=FONT_SMALL, padx=14, pady=5,
                  cursor="hand2").pack(side="left", padx=8)
        tk.Button(row, text="新建一份", command=self.new_keyword_file, bg=CARD, fg=TEXT,
                  relief="flat", highlightbackground=BORDER, highlightthickness=1,
                  bd=0, font=FONT_SMALL, padx=14, pady=5,
                  cursor="hand2").pack(side="left")
        tk.Label(row, text="保存到上面那个文件；改完要重建索引才生效",
                 bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side="left", padx=10)
        return page

    def _keyword_path(self) -> Path:
        raw = self.kw_file.get().strip()
        return Path(raw) if raw else KEYWORDS

    def _pick_keyword_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择关键词表（.txt）",
            initialdir=str(ROOT), filetypes=[("文本", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        self.kw_file.set(path)
        self.reload_keywords()

    def new_keyword_file(self) -> None:
        path = filedialog.asksaveasfilename(
            title="新建关键词表", initialdir=str(ROOT), defaultextension=".txt",
            filetypes=[("文本", "*.txt")])
        if not path:
            return
        Path(path).write_text(DEFAULT_KEYWORDS, encoding="utf-8")
        self.kw_file.set(path)
        self.reload_keywords()
        self._set_status(f"已新建：{path}")

    def _read_keywords(self) -> str:
        path = self._keyword_path()
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return DEFAULT_KEYWORDS

    def reload_keywords(self) -> None:
        self.kw_text.delete("1.0", "end")
        self.kw_text.insert("1.0", self._read_keywords())
        self._set_status(f"已加载 {self._keyword_path().name}")

    def save_keywords(self) -> None:
        path = self._keyword_path()
        text = self.kw_text.get("1.0", "end")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("保存失败", f"{exc}\n\n{path}")
            return
        self._set_status(f"已保存 {path.name}（{len(text.splitlines())} 行）")

    # ------------------------------------------------------------ 页面：设置
    def _page_about(self) -> tk.Frame:
        page = tk.Frame(self.main, bg=BG)
        self._title(page, "设置与关于", "所有文件都放在程序旁边，可以直接编辑。")
        card = self._card(page)

        info = [
            ("程序目录", str(ROOT)),
            ("索引文件", str(INDEX) + ("（已建立）" if INDEX.exists() else "（未建立）")),
            ("关键词表", str(KEYWORDS) + ("（存在）" if KEYWORDS.exists() else "（不存在）")),
            ("参数", f"k={self.cfg.k}  w={self.cfg.w}  保证检出 {self.cfg.guarantee_len} 字"),
        ]
        for key, value in info:
            row = tk.Frame(card, bg=CARD)
            row.pack(fill="x", padx=20, pady=6)
            tk.Label(row, text=key, bg=CARD, fg=MUTED, font=FONT_SMALL,
                     width=10, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=CARD, fg=TEXT, font=FONT_SMALL,
                     anchor="w").pack(side="left")

        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=20, pady=(10, 16))
        tk.Button(row, text="复制程序目录", command=self._copy_folder, bg=ACCENT,
                  fg="white", relief="flat", bd=0,
                  font=("Microsoft YaHei UI", 10, "bold"), padx=18, pady=6,
                  cursor="hand2").pack(side="left")
        tk.Button(row, text="尝试打开目录", command=self._open_folder, bg=CARD, fg=TEXT,
                  relief="flat", highlightbackground=BORDER, highlightthickness=1,
                  bd=0, font=FONT_SMALL, padx=14, pady=5,
                  cursor="hand2").pack(side="left", padx=8)
        tk.Button(row, text="删除索引", command=self._delete_index, bg=CARD, fg=RED,
                  relief="flat", highlightbackground=BORDER, highlightthickness=1,
                  bd=0, font=FONT_SMALL, padx=14, pady=5,
                  cursor="hand2").pack(side="left")

        hint = tk.Frame(card, bg=CARD)
        hint.pack(fill="x", padx=20, pady=(0, 14))
        tk.Label(hint, text="为什么要有这两个按钮：程序目录里放着 关键词.txt、leakcheck.idx、"
                            "使用说明.txt，需要直接编辑或备份时用得上。",
                 bg=CARD, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(side="left")
        return page

    def _open_folder(self) -> None:
        """打开程序目录。多种方式兜底，失败时给出可复制的路径，不抛异常。"""
        import os
        import subprocess

        path = str(ROOT)
        for opener in (
            lambda: os.startfile(path),                       # noqa: S606
            lambda: subprocess.Popen(["explorer", path]),     # noqa: S603,S607
            lambda: self._shell_open(path),
        ):
            try:
                opener()
                # 有些受限环境会"静默失败"：不报错但也不弹资源管理器，
                # 所以给个明确反馈，并提示用「复制程序目录」
                self._set_status("已请求打开资源管理器；若无反应，请用「复制程序目录」", MUTED)
                return
            except Exception:                                 # noqa: BLE001
                continue
        self._copy_path(path)
        messagebox.showinfo(
            "打不开资源管理器",
            f"系统拒绝了打开目录的请求（常见于受限环境）。\n\n"
            f"路径已复制到剪贴板，可以粘贴到资源管理器地址栏：\n{path}")

    @staticmethod
    def _shell_open(path: str) -> None:
        import ctypes
        result = ctypes.windll.shell32.ShellExecuteW(None, "open", path, None, None, 1)
        if result <= 32:                                      # ≤32 表示失败
            raise OSError(f"ShellExecute 返回 {result}")

    def _copy_path(self, path: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(path)
            self._set_status("路径已复制到剪贴板")
        except Exception:                                     # noqa: BLE001
            pass

    def _copy_folder(self) -> None:
        self._copy_path(str(ROOT))
        messagebox.showinfo("已复制", f"程序目录已复制：\n{ROOT}")

    def _copy_index_path(self) -> None:
        self._copy_path(str(INDEX))
        messagebox.showinfo("已复制", f"索引路径已复制：\n{INDEX}")

    def _delete_index(self) -> None:
        if INDEX.exists() and messagebox.askyesno("确认", "删除索引文件？"):
            INDEX.unlink()
            self._refresh_header()
            self._set_status("索引已删除")

    # ------------------------------------------------------------ 结果区
    def _build_results(self) -> None:
        frame = tk.Frame(self.main, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=1, height=228)
        frame.pack(side="bottom", fill="x", padx=28, pady=(6, 12))
        frame.pack_propagate(False)

        head = tk.Frame(frame, bg=CARD)
        head.pack(fill="x", padx=14, pady=(8, 4))
        tk.Label(head, text="结果", bg=CARD, fg=TEXT, font=FONT_H2).pack(side="left")
        self.result_hint = tk.Label(head, text="还没有运行任务", bg=CARD, fg=MUTED,
                                    font=FONT_SMALL)
        self.result_hint.pack(side="left", padx=12)

        cols = ("item", "result", "detail")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=6)
        self.tree.heading("item", text="项目")
        self.tree.heading("result", text="结果")
        self.tree.heading("detail", text="说明")
        self.tree.column("item", width=250, anchor="w")
        self.tree.column("result", width=90, anchor="w")
        self.tree.column("detail", width=440, anchor="w")
        self.tree.tag_configure("ok", foreground=GREEN)
        self.tree.tag_configure("bad", foreground=RED)
        self.tree.tag_configure("warn", foreground=ORANGE)
        self.tree.tag_configure("muted", foreground=MUTED)

        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(14, 0), pady=(0, 10))
        scroll.pack(side="right", fill="y", padx=(0, 10), pady=(0, 10))

    # ------------------------------------------------------------ 状态栏
    def _build_status(self) -> None:
        tk.Frame(self, bg=BORDER, height=1).pack(side="bottom", fill="x")
        bar = tk.Frame(self, bg=CARD, height=30)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)

        self.dot = tk.Label(bar, text="●", bg=CARD, fg=GREEN, font=("Microsoft YaHei UI", 8))
        self.dot.pack(side="left", padx=(16, 4), pady=6)
        self.status = tk.Label(bar, text="就绪", bg=CARD, fg=MUTED, font=FONT_SMALL)
        self.status.pack(side="left")

        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=160,
                                        style="Horizontal.TProgressbar")
        self.progress.pack(side="right", padx=16, pady=9)
        self.progress.pack_forget()

        tk.Label(bar, text="Ctrl+O 选目录 · F5 运行 · Esc 退出", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(side="right", padx=10)

        self.bind("<F5>", lambda _e: self._run_current())
        self.bind("<Control-o>", lambda _e: self._pick_build_dir())
        self.bind("<Escape>", lambda _e: self.destroy())

    def _run_current(self) -> None:
        mapping = {"build": self.run_build, "paths": self.run_paths,
                   "scan": self.run_scan, "screen": self.run_screen}
        action = mapping.get(self.current_page)
        if action:
            action()

    def _read_any(self, path: Path) -> str | None:
        """任意格式取文本：Office/PDF 走 T0 解析，其它按纯文本读。"""
        suffix = path.suffix.lower()
        if suffix in {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".pdf",
                      ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}:
            try:
                parsed = parse(path)
            except RuntimeError as exc:
                self.events.put(("warn", str(exc)))
                return None
            except Exception:                      # noqa: BLE001
                return None
            text = "\n\n".join(b.text for b in parsed.blocks)
            return text or None
        return read_text(path, 16 * 1024 * 1024)

    def _collect_any(self, roots: list[str], policy) -> list[tuple[str, str, str]]:
        """手动选的文件一律收录；目录按路径规则（敏感资料目录下则全收）。"""
        docs: list[tuple[str, str, str]] = []

        def accept(path: Path, forced: bool) -> None:
            decision = policy.check(str(path))
            if not decision.sensitive and not forced:
                return
            if path.suffix.lower() in {".exe", ".dll", ".zip", ".7z", ".rar",
                                       ".png", ".jpg", ".jpeg", ".gif", ".mp4",
                                       ".mp3", ".iso", ".bin"}:
                return
            text = self._read_any(path)
            if not text:
                return
            tag = decision.tag if decision.sensitive else "listed"
            docs.append((path.name, tag, text))

        for raw in roots:
            path = Path(raw)
            if path.is_file():
                accept(path, True)
            elif path.is_dir():
                forced = self._under_sensitive(raw)
                for child in sorted(path.rglob("*")):
                    if child.is_file():
                        accept(child, forced)
        return docs

    @staticmethod
    def _under_sensitive(raw: str) -> bool:
        try:
            Path(raw).resolve().relative_to(DATA_SENSITIVE.resolve())
            return True
        except (ValueError, OSError):
            return False

    @staticmethod
    def _short(path: Path) -> str:
        try:
            return str(path.relative_to(ROOT))
        except ValueError:
            return str(path)

    def _set_status(self, text: str, color: str = MUTED) -> None:
        self.status.config(text=text, fg=color)

    # ------------------------------------------------------------ 后台任务
    def _start_job(self, name: str, fn) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("正在运行", "上一个任务还没结束，请稍候。")
            return
        self._clear_results()
        self._job_failed = False
        self._source_map: dict = {}
        self.result_hint.config(text=name)
        self.progress.pack(side="right", padx=16, pady=9)
        self.progress.start(12)
        self._set_status("正在运行…", ACCENT)

        def run() -> None:
            try:
                fn()
                self.events.put(("done", name))
            except Exception as exc:                    # noqa: BLE001
                self.events.put(("error", f"{type(exc).__name__}: {exc}"))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "row":
                    item, result, detail, tag = payload       # type: ignore[misc]
                    self.tree.insert("", "end", values=(item, result, detail),
                                     tags=(tag,))
                elif kind == "done":
                    self.progress.stop()
                    self.progress.pack_forget()
                    if not getattr(self, "_job_failed", False):
                        self._set_status(f"完成：{payload}", GREEN)
                    self.dot.config(fg=GREEN)
                    self._refresh_header()
                elif kind == "warn":
                    self._job_failed = True
                    self.progress.stop()
                    self.progress.pack_forget()
                    self._set_status("没有可用的内容", RED)
                    messagebox.showwarning("提示", str(payload))
                elif kind == "error":
                    self._job_failed = True
                    self.progress.stop()
                    self.progress.pack_forget()
                    self._set_status(f"出错：{payload}", RED)
                    self.dot.config(fg=RED)
                    messagebox.showerror("执行失败", str(payload))
                elif kind == "index":
                    path = Path(str(payload))
                    if not self.scan_index.get().strip():
                        self.scan_index.set(str(path))
                    self.events.put(("status", f"索引已保存 → {self._short(path)}"))
                elif kind == "status":
                    self._set_status(str(payload))
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _clear_results(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

    def _row(self, item: str, result: str, detail: str = "", tag: str = "muted") -> None:
        self.events.put(("row", (item, result, detail, tag)))

    # ------------------------------------------------------------ 任务：建索引
    def run_build(self) -> None:
        roots = self._split_paths(self.build_dir.get())
        missing = [p for p in roots if not Path(p).exists()]
        if not roots or missing:
            messagebox.showwarning("提示", "请先选择存在的文件夹或文件" +
                                   (f"\n找不到：{missing[0]}" if missing else ""))
            self._clear_results()
            self._set_status("未开始：路径无效", RED)
            return
        out = with_idx_suffix(self.build_out.get().strip()) or "leakcheck.idx"
        self.build_out.set(out)
        kw = self.build_kw.get().strip()

        def job() -> None:
            policy = PathPolicy()
            if kw and Path(kw).exists():
                from .pathpolicy import rules_from_keywords
                policy.prepend_includes(rules_from_keywords(kw))
            # 用户把文件放进「我的资料\敏感文件」就是"我认为它敏感"，
            # 不再让路径规则二次筛选，否则 .txt/.md 这类会被漏掉。
            docs = self._collect_any(roots, policy)
            if not docs:
                self.events.put(("warn", "没有读到任何内容：\n"
                                 "- docx/pdf 需要解析库（打包版已内置）\n"
                                 "- 文件可能是空的或加密的\n"
                                 "- 也可能是纯图片扫描件（需要 OCR）"))
                return
            from .cascade import Cascade
            casc = Cascade(self.cfg)
            for doc_id, tag, text in docs:
                casc.add(doc_id, text, tag, doc_id)
            target = self._resolve_index_path(out)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            casc.save(target)
            self.events.put(("index", str(target)))
            for doc_id, tag, text in docs[:200]:
                label = Path(doc_id).name if "\\" in doc_id or "/" in doc_id else doc_id
                self._row(label, tag or "已收录", f"{len(text)} 字", "ok")
            self.events.put(("status", f"共 {len(docs)} 份，{casc.matcher.stats()['fingerprints']} 个指纹（含骨架）"))

        self._start_job("建立索引", job)

    # ------------------------------------------------------------ 任务：路径判定
    def run_paths(self) -> None:
        target = self.paths_target.get().strip()
        if not target or not Path(target).exists():
            messagebox.showwarning("提示", "请先选择一个文件或目录")
            return

        def job() -> None:
            policy = PathPolicy()
            root = Path(target)
            paths = ([p for p in root.rglob("*") if p.is_file()]
                     if root.is_dir() else [root])
            sensitive = 0
            for path in paths[:500]:
                decision = policy.check(str(path))
                if decision.sensitive:
                    sensitive += 1
                self._row(str(path), "敏感" if decision.sensitive else "普通",
                          f"{decision.rule}：{decision.reason}",
                          "ok" if decision.sensitive else "muted")
            self.events.put(("status", f"{len(paths)} 条路径，敏感 {sensitive} 条"))

        self._start_job("路径判定", job)

    # ------------------------------------------------------------ 任务：语义筛选
    def run_screen(self) -> None:
        directory = self.screen_dir.get().strip()
        target = self.screen_file.get().strip()
        if not directory or not Path(directory.split(";")[0].strip()).exists():
            messagebox.showwarning("提示", "请先选择敏感资料（文件夹或文件）")
            self._clear_results()
            self._set_status("未开始：没有敏感资料", RED)
            return
        if not target or not Path(target).exists():
            messagebox.showwarning("提示", "请选择待检测文件")
            self._clear_results()
            self._set_status("未开始：没有待检测文件", RED)
            return

        def job() -> None:
            from .vector_screen import VectorScreen, make_embedder
            policy = PathPolicy()
            if KEYWORDS.exists():
                from .pathpolicy import rules_from_keywords
                policy.prepend_includes(rules_from_keywords(KEYWORDS))
            docs = self._collect_any(self._split_paths(directory), policy)

            screen = VectorScreen(self.cfg, make_embedder("auto"), min_score=0.55)
            for doc_id, tag, text in docs:
                screen.add(doc_id, text, tag, doc_id)

            parsed = parse(target)
            payload = "\n\n".join(b.text for b in parsed.blocks)
            hits = screen.search(payload, top_k=5)
            mode = "语义" if screen.semantic else "降级（无模型）"

            if not hits:
                self._row(Path(target).name, "无线索",
                          f"没有块的相似度达到 0.55（后端：{screen.backend_name}）", "ok")
                self.events.put(("status", "语义筛选完成：没有线索"))
                return

            for hit in hits:
                band, _color, tag = self._score_band(hit.score)
                snippet = hit.chunk.replace("\n", " ")[:52]
                self._row(hit.doc_id, f"{hit.score:.0%}",
                          f"{band} · 第{hit.chunk_index + 1}块 · {snippet}…",
                          tag)
            top = hits[0].score
            self.events.put(("status",
                             f"线索 {len(hits)} 条，最高 {top:.0%}（仅供参考，不是证据）"))

        self._start_job("语义筛选", job)

    # ------------------------------------------------------------ 任务：三阶扫描
    def run_scan(self) -> None:
        target = self.scan_file.get().strip()
        if not target or not Path(target).exists():
            messagebox.showwarning("提示", "请选择待检测文件")
            self._clear_results()
            self._set_status("未开始：没有待检测文件", RED)
            return

        use_index = self.scan_source.get() == "index"
        index = self.scan_index.get().strip() if use_index else ""
        directory = self.scan_dir.get().strip()

        if use_index:
            if not index:
                messagebox.showwarning("提示", "选了「用索引」，但索引路径是空的")
                self._clear_results()
                self._set_status("未开始：没有索引", RED)
                return
            if Path(index).suffix.lower() != ".idx":
                messagebox.showwarning(
                    "这不是索引文件",
                    f"{Path(index).name} 看起来不是索引。\n\n"
                    f"索引是 ① 建立索引 生成的文件（后缀 .idx）。")
                self._clear_results()
                self._set_status("未开始：索引填错了", RED)
                return
            if not Path(index).exists():
                messagebox.showwarning("提示", "索引文件不存在，请重新选择")
                self._clear_results()
                self._set_status("未开始：索引路径无效", RED)
                return
        else:
            if not directory or not Path(directory.split(";")[0].strip()).exists():
                messagebox.showwarning("提示", "选了「直接扫敏感资料」，但没选资料")
                self._clear_results()
                self._set_status("未开始：没有敏感资料", RED)
                return

        def job() -> None:
            from .cascade import LEVEL_DESC, Cascade
            if index:
                casc = Cascade.load(index)
                self.events.put(("status", f"已加载索引：{len(casc)} 份"))
                doc_count = len(casc)
            else:
                policy = PathPolicy()
                if KEYWORDS.exists():
                    from .pathpolicy import rules_from_keywords
                    policy.prepend_includes(rules_from_keywords(KEYWORDS))
                docs = self._collect_any(self._split_paths(directory), policy)
                casc = Cascade(self.cfg)
                for doc_id, tag, text in docs:
                    casc.add(doc_id, text, tag, doc_id)
                doc_count = len(docs)

            total_hits = 0
            for name, payload in self._iter_payloads(Path(target)):
                result = casc.scan(payload, name)
                if not result.findings:
                    self._row(name, "放行", "没有发现重合内容", "ok")
                    continue
                total_hits += 1
                for item in result.findings:
                    self._row(name, f"{item.level} 级",
                              f"{item.doc_id} · {LEVEL_DESC.get(item.level, '')} · "
                              f"{item.length} 字", "bad")
                for warn in result.warnings:
                    self._row(name, "注意", warn, "warn")

            self.events.put(("status", f"{doc_count} 份敏感资料，命中 {total_hits} 条"))

        self._start_job("两阶扫描", job)

    def _iter_payloads(self, target: Path):
        """把待检测目标展开成 (名称, 文本)：支持单文件 / 目录 / .jsonl。"""
        if target.is_dir():
            for file in sorted(target.rglob("*")):
                if not file.is_file():
                    continue
                text = read_text(file, 16 * 1024 * 1024)
                if text is not None:
                    yield file.name, text
            return

        if target.suffix.lower() == ".jsonl":
            for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield obj.get("id", "?"), obj.get("text", "")
            return

        parsed = parse(target)
        for warn in parsed.warnings:
            self._row("提示", "注意", warn, "warn")
        yield target.name, "\n\n".join(b.text for b in parsed.blocks)


def _setup_temp() -> None:
    """把 Python 的临时目录挪到用户缓存目录，别落在程序文件夹里。

    某些依赖（onnxruntime / OCR）启动时会 tempfile.mkdtemp()，
    如果 TEMP 没配好，就会在程序目录下留一堆空的 tmpXXXX 文件夹。
    """
    import os
    import tempfile

    base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ROOT)
    target = base / "leakcheck" / "tmp"
    try:
        target.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(target)
        os.environ["TMP"] = str(target)
        os.environ["TEMP"] = str(target)
    except OSError:
        pass

    # 顺手清掉以前遗留的空 tmp* 文件夹
    try:
        for path in ROOT.glob("tmp*"):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    except OSError:
        pass


def _hide_console() -> None:
    """打包成 exe 后双击会带一个黑窗口，GUI 模式把它藏起来。"""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:                              # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    _setup_temp()
    _hide_console()
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

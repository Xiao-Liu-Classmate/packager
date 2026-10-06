# -*- coding: utf-8 -*-
"""PySide6 界面层：液态玻璃风格（Windows 11 原生 acrylic + Qt 原生控件）。

本模块只负责界面。所有打包、校验、配置、脚本生成等核心逻辑都在
packager.py 内，本模块只调用、不复制、不改动。

毛玻璃策略（全部使用引擎/系统原生能力，无自写像素渲染）：
1. 窗口级：Win11 build 22621+ 通过 DwmSetWindowAttribute 开启
   DWMWA_SYSTEMBACKDROP_TYPE = DWMSBT_TRANSIENTWINDOW（acrylic）。
   模糊由系统合成器实时完成，是真正的"背景折射"。
2. 窗口底色设为透明（WA_TranslucentBackground），让合成器模糊后的
   背景从卡片缝隙透进来。
3. 卡片用半透明 QSS 叠加，形成多层玻璃层次。
4. 不支持的环境（Win10 / Linux / macOS）自动降级为半透明纯色卡片，
   观感接近但无背景折射；文字始终保持可读。

注意：不能用 QGraphicsBlurEffect 实现卡片毛玻璃 —— 它会连同卡片内的
文字一起模糊。真正"透出背后内容"的毛玻璃在 Qt Widgets 里只有系统
backdrop 这一条路。
"""

import os
import subprocess
import sys
import threading
import time
import traceback

from PySide6 import QtCore, QtGui, QtWidgets

# 与消息重复时只提示一次，避免拖拽窗口时刷屏
_warned = set()


def _ui_warn(msg, once_key=None):
    """把界面层异常写到 stderr。

    打包后的 exe 是 --windowed（无控制台），stderr 会丢；但这里至少
    保证了 CLI / 调试环境可追溯。同 key 的重复消息只提示一次。
    """
    if once_key is not None:
        if once_key in _warned:
            return
        _warned.add(once_key)
    try:
        sys.stderr.write(msg.rstrip() + "\n")
        sys.stderr.flush()
    except Exception:
        pass

# ============================================================
# A. 主题（Qt 样式表数据，与旧版色板语义对齐）
# ============================================================
THEMES = {
    "light": {
        "window": "rgba(238,242,251,236)",
        "card": "rgba(255,255,255,152)",
        "tab_pane": "rgba(255,255,255,86)",
        "tab_bar": "rgba(255,255,255,120)",
        "card_border": "rgba(255,255,255,200)",
        "field": "rgba(255,255,255,225)",
        "field_border": "rgba(120,140,190,90)",
        "text": "#151a2c",
        "text_dim": "#5a6483",
        "accent": "#4c6fe7",
        "accent_hover": "#3d5ed0",
        "on_accent": "#ffffff",
        "log_bg": "rgba(255,255,255,205)",
        "hover": "rgba(0,0,0,14)",
        "press": "rgba(0,0,0,26)",
        "sel": "#4c6fe7",
        "sel_text": "#ffffff",
        "tree_alt": "rgba(20,32,68,14)",
        "ok": "#1f8b4c",
        "danger": "#c62828",
    },
    "dark": {
        "window": "rgba(18,26,51,240)",
        "card": "rgba(255,255,255,30)",
        "tab_pane": "rgba(255,255,255,16)",
        "tab_bar": "rgba(255,255,255,34)",
        "card_border": "rgba(255,255,255,40)",
        "field": "rgba(255,255,255,20)",
        "field_border": "rgba(255,255,255,48)",
        "text": "#e9edf9",
        "text_dim": "#98a3c2",
        "accent": "#5b8cff",
        "accent_hover": "#7ba3ff",
        "on_accent": "#ffffff",
        "log_bg": "rgba(6,10,22,225)",
        "hover": "rgba(255,255,255,32)",
        "press": "rgba(255,255,255,18)",
        "sel": "#4c6fe7",
        "sel_text": "#ffffff",
        "tree_alt": "rgba(255,255,255,12)",
        "ok": "#5ad19a",
        "danger": "#ff7b7b",
    },
}

APP_FONT = "Microsoft YaHei UI"
MONO_FONT = "Cascadia Mono"


def font(size=9, bold=False):
    f = QtGui.QFont(APP_FONT, size)
    f.setBold(bold)
    return f


def mono(size=9):
    f = QtGui.QFont(MONO_FONT, size)
    f.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    return f


def build_stylesheet(theme_name):
    """生成整窗 QSS。所有控件外观均由 Qt 样式系统负责。

    注意：模板里是 {key} 这种**具名**占位符，必须用 .format(**t) 展开。
    若写成 .format(t) 会把 t 当作位置参数，具名占位符取不到值而抛
    KeyError: 'app'（dict 本身没问题，是传参方式错了）。
    """
    t = dict(THEMES[theme_name])
    t["app"] = APP_FONT
    return """
QWidget {{
    font-family: "{app}";
    font-size: 13px;
    color: {text};
}}
/* 窗口底色用「近不透明」的主题色：acrylic 只在卡片缝隙里透出模糊层，
   若完全透明，标题区文字会直接压在桌面上而不可读 */
QMainWindow, QDialog {{ background: {window}; }}
QDialog QWidget {{ background: transparent; }}

/* --- 玻璃卡片 --- */
QFrame#GlassCard {{
    background: {card};
    border: 1px solid {card_border};
    border-radius: 18px;
}}
QWidget#RootPane {{
    background: {window};
    border-radius: 12px;
}}
QFrame#Plain {{ background: transparent; }}

/* --- 文本 --- */
QLabel#Title {{ font-size: 22px; font-weight: 600; color: {text}; }}
QLabel#Subtitle {{ font-size: 12px; color: {text_dim}; }}
QLabel#Section {{ font-size: 14px; font-weight: 600; color: {text}; }}
QLabel#Hint, QLabel#Status {{ font-size: 12px; color: {text_dim}; }}
QLabel#FieldName {{ font-size: 13px; color: {text}; }}
QLabel#OK {{ color: {ok}; font-weight: 600; }}
QLabel#Danger {{ color: {danger}; font-weight: 600; }}
QLabel#Link {{ color: {accent}; }}
QLabel#Link:hover {{ text-decoration: underline; }}

/* --- 输入控件 --- */
QLineEdit, QPlainTextEdit, QTextEdit {{
    background: {field};
    border: 1px solid {field_border};
    border-radius: 8px;
    padding: 6px 10px;
    selection-background-color: {sel};
    selection-color: {sel_text};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {accent};
}}
QLineEdit:disabled, QTextEdit:disabled {{ color: {text_dim}; }}

/* --- 按钮 --- */
QPushButton {{
    background: {field};
    border: 1px solid {card_border};
    border-radius: 8px;
    padding: 7px 16px;
}}
QPushButton:hover {{ background: {hover}; }}
QPushButton:pressed {{ background: {press}; }}
QPushButton:disabled {{ color: {text_dim}; }}
QPushButton#Primary {{
    background: {accent};
    color: {on_accent};
    border: 1px solid {accent};
    font-weight: 600;
    padding: 8px 26px;
}}
QPushButton#Primary:hover {{ background: {accent_hover}; }}
QPushButton#Primary:disabled {{ background: {press}; color: {text_dim}; }}
QPushButton#Ghost {{ background: transparent; }}
QPushButton#Ghost:hover {{ background: {hover}; }}

/* --- 选项卡 --- */
QTabWidget::pane {{
    background: {tab_pane};
    border: 1px solid {card_border};
    border-radius: 14px;
    top: -1px;
}}
/* tab 栏底色必须跟随主题：深色主题下若留默认浅色，
   会切到"白底黑字"与整窗深色调割裂 */
QTabBar {{
    background: {tab_bar};
    border: none;
    border-top-left-radius: 12px;
    border-top-right-radius: 12px;
}}
QTabBar::tab {{
    background: transparent;
    color: {text_dim};
    padding: 8px 18px;
    margin-right: 4px;
    border: 1px solid transparent;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
}}
QTabBar::tab:hover {{ background: {hover}; color: {text}; }}
QTabBar::tab:selected {{
    background: {accent};
    color: {on_accent};
    font-weight: 600;
}}

/* --- 表格 / 树 / 列表 --- */
QTreeWidget {{
    background: {field};
    alternate-background-color: {tree_alt};
    border: 1px solid {card_border};
    border-radius: 10px;
    gridline-color: transparent;
    selection-background-color: {sel};
    selection-color: {sel_text};
    outline: none;
}}
QTreeWidget::item {{ padding: 4px 6px; border: none; }}
QTreeWidget::item:selected {{ background: {sel}; color: {sel_text}; }}
QHeaderView::section {{
    background: transparent;
    color: {text_dim};
    border: none;
    border-bottom: 1px solid {card_border};
    padding: 7px 8px;
    font-weight: 600;
}}

/* --- 其他 --- */
QCheckBox, QRadioButton {{ spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px; height: 15px; background: {field};
    border: 1px solid {field_border};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QRadioButton::indicator:checked {{ background: {accent}; border-color: {accent}; }}
QComboBox {{
    background: {field}; border: 1px solid {field_border};
    border-radius: 8px; padding: 5px 10px; min-width: 90px;
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {field}; border: 1px solid {card_border};
    selection-background-color: {sel}; selection-color: {sel_text};
    border-radius: 8px;
}}
QMenuBar {{ background: transparent; }}
QMenuBar::item {{ background: transparent; padding: 5px 11px; border-radius: 6px; }}
QMenuBar::item:selected {{ background: {hover}; }}
QMenu {{
    background: {field}; border: 1px solid {card_border};
    border-radius: 9px; padding: 4px;
}}
QMenu::item {{ padding: 6px 22px 6px 20px; border-radius: 6px; }}
QMenu::item:selected {{ background: {sel}; color: {sel_text}; }}
QMenu::separator {{ height: 1px; background: {card_border}; margin: 4px 8px; }}
QProgressBar {{
    background: {field}; border: none; border-radius: 4px;
    height: 8px; text-align: center;
}}
QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}
QToolTip {{
    background: {field}; color: {text};
    border: 1px solid {card_border}; border-radius: 6px; padding: 5px;
}}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{
    background: {field_border}; border-radius: 5px; min-height: 28px;
}}
QScrollBar::handle:hover {{ background: {accent}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; width: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
""" .format(**t)


# ============================================================
# B. Windows 11 原生毛玻璃（系统合成器，非自写渲染）
# ============================================================
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMWA_MICA_EFFECT = 1029      # Win11 22H2 之前的 Mica
_DWMSBT_TRANSIENTWINDOW = 3   # Acrylic
_NATIVE_BACKDROP_SUPPORTED = None


def native_backdrop_supported():
    """Win11 build 22621 起才有 DWMWA_SYSTEMBACKDROP_TYPE（系统级 acrylic）。"""
    global _NATIVE_BACKDROP_SUPPORTED
    if _NATIVE_BACKDROP_SUPPORTED is None:
        ok = False
        if os.name == "nt":
            try:
                ok = sys.getwindowsversion().build >= 22621
            except Exception:
                ok = False
        _NATIVE_BACKDROP_SUPPORTED = ok
    return _NATIVE_BACKDROP_SUPPORTED


def enable_native_backdrop(win, dark=False):
    """开启 Windows 11 窗口级 acrylic 毛玻璃，返回是否成功。

    失败不影响使用：卡片仍是半透明，只是没有背景折射。
    """
    if not native_backdrop_supported():
        return False
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = wintypes.HWND(int(win.winId()))
        dwm = ctypes.windll.dwmapi

        dark_val = ctypes.c_int(1 if dark else 0)
        if dwm.DwmSetWindowAttribute(
                hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(dark_val), ctypes.sizeof(dark_val)) != 0:
            return False

        bt = ctypes.c_int(_DWMSBT_TRANSIENTWINDOW)
        if dwm.DwmSetWindowAttribute(
                hwnd, _DWMWA_SYSTEMBACKDROP_TYPE,
                ctypes.byref(bt), ctypes.sizeof(bt)) == 0:
            return True

        # 回退到 22H2 之前的 Mica 属性
        mica = ctypes.c_int(1)
        return dwm.DwmSetWindowAttribute(
            hwnd, _DWMWA_MICA_EFFECT,
            ctypes.byref(mica), ctypes.sizeof(mica)) == 0
    except Exception:
        return False


class LinkLabel(QtWidgets.QLabel):
    """可点击链接标签。"""

    def __init__(self, url, parent=None):
        super().__init__(url, parent)
        self.setObjectName("Link")
        self._url = url
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(self._url))
        super().mouseReleaseEvent(event)


class GlassFrame(QtWidgets.QFrame):
    """玻璃卡片容器：半透明 QSS 叠加。"""

    def __init__(self, parent=None, title=None, hint=None):
        super().__init__(parent)
        self.setObjectName("GlassCard")
        # QWidget/QFrame 的自定义子类默认不绘制样式表背景，
        # 必须显式开启 WA_StyledBackground，否则卡片会完全透明不可见
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)
        if title:
            head = QtWidgets.QHBoxLayout()
            head.setSpacing(8)
            lbl = QtWidgets.QLabel(title, self)
            lbl.setObjectName("Section")
            head.addWidget(lbl)
            if hint:
                hint_lbl = QtWidgets.QLabel(hint, self)
                hint_lbl.setObjectName("Hint")
                head.addWidget(hint_lbl)
            head.addStretch(1)
            outer.addLayout(head)
        self.body = QtWidgets.QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)
        outer.addLayout(self.body, 1)


class UiInvoker(QtCore.QObject):
    """把后台线程的回调安全地调度到 UI 线程执行。

    Qt 规定 QObject 只能在其所属线程访问，直接从 worker 线程调用
    控件是未定义行为。经由 QueuedConnection 投递到主线程执行，
    与旧版 Tk 用 after 队列轮询的约束完全一致。
    """

    _invoked = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._invoked.connect(self._run, QtCore.Qt.ConnectionType.QueuedConnection)

    def _run(self, fn):
        if fn is None:
            return
        try:
            fn()
        except RuntimeError as exc:
            # 只有"对象已析构"才可静默放行；其余 RuntimeError 是真实 bug
            # （例如 list 在遍历中被改写），必须报出来
            if "destroyed" in str(exc).lower() or "deleted" in str(exc).lower():
                return
            _ui_warn("界面回调执行失败: %s" % traceback.format_exc())
        except Exception:
            # PySide6 中槽函数抛出未捕获异常会打印 traceback 后终止进程，
            # 界面里的一个普通 bug 不该让整个应用崩掉
            _ui_warn("界面回调执行失败: %s" % traceback.format_exc())

    def call(self, fn):
        """任意线程可调用。"""
        self._invoked.emit(fn)


class LogPump:
    """日志汇聚器：攒够一批或到一个防抖周期就写入。

    必须有防抖定时器兜底：只靠 threshold 阈值时，打包收尾的汇总日志
    （成功/失败计数、各目录输出路径、耗时）几乎必然落在不足一批的残留里，
    永远不会被写入日志区。
    """

    def __init__(self, window, threshold=40, flush_ms=250):
        self._win = window
        self._pending = []
        self._threshold = threshold
        self._timer = None
        self._flush_ms = flush_ms

    def _ensure_timer(self):
        if self._timer is None:
            self._timer = QtCore.QTimer(self._win)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self.flush)
        return self._timer

    def post(self, msg):
        self._pending.append(msg)
        if len(self._pending) >= self._threshold:
            self.flush()
        else:
            timer = self._ensure_timer()
            timer.start(self._flush_ms)

    def flush(self):
        if self._timer is not None:
            self._timer.stop()
        if not self._pending:
            return
        items, self._pending = self._pending, []
        self._win._append_log_lines(items)


# ============================================================
# C. 主窗口
# ============================================================
class PackagerWindow(QtWidgets.QMainWindow):
    """所有界面逻辑；核心打包逻辑通过 core 模块调用。"""

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.setWindowTitle("%s v%s" % (core.APP_NAME, core.APP_VERSION))
        self.cfg = core.load_config()
        w = self.cfg.get("window_width", 1180)
        h = self.cfg.get("window_height", 900)
        try:
            # 旧配置存的是改造前的小尺寸，沿用会让批量页与日志区显得拥挤
            w, h = max(1060, int(w)), max(700, int(h))
        except (TypeError, ValueError):
            w, h = 1180, 900
        self.resize(w, h)
        self.setMinimumSize(900, 640)

        self.current_theme = self.cfg.get("theme", "light")
        if self.current_theme not in THEMES:
            self.current_theme = "light"
        self.current_project = self.cfg.get("last_project_file", "")
        self.scanned_files = []
        self.scanned_size = 0
        self._building = False
        self._closing = False
        self._scan_seq = 0
        self._log_expanded = False
        self._search_timer = None
        self._source_timer = None
        self._backdrop_ok = False
        self._tools_detail = ""

        self.invoker = UiInvoker(self)

        self._apply_theme(self.current_theme)
        self._build_menu()
        self._build_ui()
        self._bind_shortcuts()
        self._restore_batch()
        self._refresh_recent_menu()
        self._check_tools()
        self.pump = LogPump(self)

        self._autosave_timer = QtCore.QTimer(self)
        self._autosave_timer.timeout.connect(self._auto_save_project)
        self._autosave_timer.start(60000)

    # --------------------------------------------------------
    # 线程桥接
    # --------------------------------------------------------
    def call_in_ui(self, fn):
        """线程安全地把回调调度到 UI 线程执行。

        已在 UI 线程时直接执行以保持即时性（构建完成的回调链
        有十余层，每层都排一轮事件循环会让界面明显延迟）。
        """
        if threading.current_thread() is threading.main_thread():
            try:
                fn()
            except RuntimeError as exc:
                if "destroyed" in str(exc).lower() or "deleted" in str(exc).lower():
                    return
                _ui_warn("界面回调执行失败: %s" % traceback.format_exc())
            except Exception:
                _ui_warn("界面回调执行失败: %s" % traceback.format_exc())
            return
        self.invoker.call(fn)

    # --------------------------------------------------------
    # 菜单与快捷键
    # --------------------------------------------------------
    def _build_menu(self):
        bar = self.menuBar()
        m_file = bar.addMenu("文件")
        m_file.addAction("保存项目", self._save_project,
                         QtGui.QKeySequence.StandardKey.Save)
        m_file.addAction("打开项目", self._open_project,
                         QtGui.QKeySequence.StandardKey.Open)
        self.recent_menu = m_file.addMenu("最近项目")
        m_file.addSeparator()
        m_file.addAction("导出配置", self._export_config)
        m_file.addAction("导入配置", self._import_config)
        m_file.addSeparator()
        m_file.addAction("打开输出目录", self._open_output_dir)
        m_file.addSeparator()
        m_file.addAction("退出", self._on_close,
                         QtGui.QKeySequence.StandardKey.Quit)

        m_view = bar.addMenu("外观")
        m_view.addAction("切换主题 (Ctrl+T)", self._toggle_theme)
        m_view.addSeparator()
        m_view.addAction("浅色主题", lambda: self._apply_theme("light"))
        m_view.addAction("深色主题", lambda: self._apply_theme("dark"))

        m_help = bar.addMenu("帮助")
        m_help.addAction("关于 (F1)", self._show_about,
                         QtGui.QKeySequence.StandardKey.HelpContents)

    def _bind_shortcuts(self):
        def sc(seq, fn):
            act = QtGui.QAction(self)
            act.setShortcut(QtGui.QKeySequence(seq))
            act.triggered.connect(lambda: fn())
            self.addAction(act)

        # 菜单项已声明的快捷键由 QAction 处理，这里只补菜单没有的
        sc("Ctrl+B", self._start_build)
        sc("Ctrl+T", self._toggle_theme)
        sc("F5", self._refresh_file_tree)

    # --------------------------------------------------------
    # 界面骨架
    # --------------------------------------------------------
    def _build_ui(self):
        root = QtWidgets.QWidget(self)
        self.setCentralWidget(root)
        # 底色由 QSS 的 {window} 提供（近不透明的主题色）；
        # acrylic 模式下同样需要该底色，否则标题区文字会直接压在桌面上
        root.setObjectName("RootPane")

        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(18, 14, 18, 16)
        outer.setSpacing(12)
        outer.addLayout(self._build_header())

        content_card = GlassFrame(root)
        tabs = QtWidgets.QTabWidget(content_card)
        tabs.setDocumentMode(True)
        self.tabs = tabs
        content_card.body.addWidget(tabs, 1)
        outer.addWidget(content_card, 1)

        self._build_tab_basic(tabs)
        self._build_tab_batch(tabs)
        self._build_tab_files(tabs)
        self._build_tab_stats(tabs)
        self._build_tab_advanced(tabs)
        self._build_tab_history(tabs)

        outer.addLayout(self._build_bottom())
        self._init_log_colors()

    def _build_header(self):
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(2)
        title = QtWidgets.QLabel(self.core.APP_NAME)
        title.setObjectName("Title")
        sub = QtWidgets.QLabel("v%s · 一键生成 Windows 安装包" % self.core.APP_VERSION)
        sub.setObjectName("Subtitle")
        left.addWidget(title)
        left.addWidget(sub)
        row.addLayout(left)
        row.addStretch(1)

        self.tools_summary = QtWidgets.QLabel("")
        self.tools_summary.setObjectName("Status")
        row.addWidget(self.tools_summary)

        theme_btn = QtWidgets.QPushButton("切换主题")
        theme_btn.setObjectName("Ghost")
        theme_btn.clicked.connect(self._toggle_theme)
        row.addWidget(theme_btn)
        return row

    def _plain(self, parent):
        f = QtWidgets.QFrame(parent)
        f.setObjectName("Plain")
        return f

    def _label(self, parent, text, obj="Status"):
        lbl = QtWidgets.QLabel(text, parent)
        lbl.setObjectName(obj)
        return lbl

    def _field(self, parent, placeholder=""):
        e = QtWidgets.QLineEdit(parent)
        if placeholder:
            e.setPlaceholderText(placeholder)
        return e

    def _row_widget(self, parent, name, widget, browse=None, stretch=True):
        """标签 + 控件 + 可选浏览按钮，返回该行容器。"""
        w = self._plain(parent)
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(10)
        lay.addWidget(self._label(w, name, "FieldName"))
        lay.addWidget(widget, 1 if stretch else 0)
        if browse:
            btn = QtWidgets.QPushButton("浏览", w)
            btn.clicked.connect(browse)
            lay.addWidget(btn)
        return w

    # --------------------------------------------------------
    # Tab 1: 基本设置
    # --------------------------------------------------------
    def _build_tab_basic(self, tabs):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(18, 16, 18, 16)

        card = GlassFrame(page, title="基本信息")
        self.src_edit = self._field(card, "源文件夹")
        card.body.addWidget(
            self._row_widget(card, "源文件夹", self.src_edit, self._browse_source))
        self.out_edit = self._field(card, "输出目录")
        card.body.addWidget(
            self._row_widget(card, "输出目录", self.out_edit, self._browse_output))
        self.appname_edit = self._field(card)
        # str() 兜底：setText 只接受 str，脏配置里的非字符串值会 TypeError
        self.appname_edit.setText(str(self.cfg.get("app_name", "MyApp")))
        card.body.addWidget(self._row_widget(card, "应用名称", self.appname_edit))
        self.appver_edit = self._field(card)
        self.appver_edit.setText(
            str(self.cfg.get("app_version", self.core.APP_VERSION)))
        self.appver_edit.setMaximumWidth(180)
        card.body.addWidget(self._row_widget(card, "版本号", self.appver_edit, stretch=False))
        self.pub_edit = self._field(card)
        self.pub_edit.setText(str(self.cfg.get("publisher", "")))
        card.body.addWidget(self._row_widget(card, "发布者", self.pub_edit))
        card.body.addStretch(1)
        lay.addWidget(card, 1)

        self.batch_tip = self._label(page, "", "Hint")
        lay.addWidget(self.batch_tip)
        tabs.addTab(page, "基本设置")
        self.src_edit.textChanged.connect(self._on_source_changed)

    # --------------------------------------------------------
    # Tab 2: 批量打包
    # --------------------------------------------------------
    def _build_tab_batch(self, tabs):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(18, 16, 18, 16)

        card = GlassFrame(page, title="待打包文件夹",
                          hint="一次打包多个文件夹，各自输出到带序号的子目录")
        self.batch_list = QtWidgets.QListWidget(card)
        self.batch_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.batch_list.setFont(mono(9))
        self.batch_list.itemSelectionChanged.connect(self._refresh_batch_hint)
        card.body.addWidget(self.batch_list, 1)

        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(8)
        for text, fn in (("添加文件夹", self._add_batch_folder),
                         ("移除选中", self._remove_batch_folder),
                         ("清空列表", self._clear_batch_folders),
                         ("从剪贴板粘贴", self._paste_path_to_source)):
            b = QtWidgets.QPushButton(text, card)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addWidget(self._label(card, "Ctrl+V 可直接粘贴路径", "Hint"))
        btns.addStretch(1)
        card.body.addLayout(btns)
        lay.addWidget(card, 1)
        tabs.addTab(page, "批量打包")

    def _restore_batch(self):
        self.batch_list.clear()
        for src in self.cfg.get("batch_sources", []):
            if src:
                self.batch_list.addItem(src)
        self._refresh_batch_hint()

    def _batch_items(self):
        return [self.batch_list.item(i).text()
                for i in range(self.batch_list.count())]

    def _refresh_batch_hint(self):
        n = self.batch_list.count()
        if n <= 0:
            text = ("批量列表为空。切换到「批量打包」页添加多个源目录，"
                    "之后直接点「开始打包」即可逐个生成产物。")
        else:
            text = "批量列表共 %d 个文件夹，开始打包后将逐个输出到带序号的子目录。" % n
        self.batch_tip.setText(text)

    # --------------------------------------------------------
    # Tab 3: 文件预览
    # --------------------------------------------------------
    def _build_tab_files(self, tabs):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(18, 16, 18, 16)

        card = GlassFrame(page)
        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setSpacing(9)
        toolbar.addWidget(self._label(card, "搜索", "FieldName"))
        self.search_edit = self._field(card)
        self.search_edit.setFixedWidth(300)
        self.search_edit.textChanged.connect(self._on_search_key)
        toolbar.addWidget(self.search_edit)
        self.case_check = QtWidgets.QCheckBox("区分大小写", card)
        self.case_check.toggled.connect(self._filter_file_tree)
        toolbar.addWidget(self.case_check)
        toolbar.addStretch(1)
        b_csv = QtWidgets.QPushButton("导出 CSV", card)
        b_csv.clicked.connect(self._export_file_list_csv)
        toolbar.addWidget(b_csv)
        b_ref = QtWidgets.QPushButton("刷新", card)
        b_ref.clicked.connect(self._refresh_file_tree)
        toolbar.addWidget(b_ref)
        card.body.addLayout(toolbar)

        self.file_tree = QtWidgets.QTreeWidget(card)
        self.file_tree.setColumnCount(2)
        self.file_tree.setHeaderLabels(["文件路径", "大小"])
        self.file_tree.setRootIsDecorated(False)
        self.file_tree.setAlternatingRowColors(True)
        self.file_tree.setFont(mono(9))
        self.file_tree.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.file_tree.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_tree.customContextMenuRequested.connect(self._on_file_tree_right_click)
        self.file_tree.itemDoubleClicked.connect(self._on_file_tree_double_click)
        hdr = self.file_tree.header()
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        card.body.addWidget(self.file_tree, 1)

        stats = QtWidgets.QHBoxLayout()
        self.file_count_label = self._label(card, "文件数  0", "Status")
        self.file_size_label = self._label(card, "总大小  0 B", "Status")
        stats.addWidget(self.file_count_label)
        stats.addStretch(1)
        stats.addWidget(self.file_size_label)
        card.body.addLayout(stats)
        lay.addWidget(card, 1)
        tabs.addTab(page, "文件预览")

    # --------------------------------------------------------
    # Tab 4: 文件统计
    # --------------------------------------------------------
    def _build_tab_stats(self, tabs):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(18, 16, 18, 16)

        card = GlassFrame(page, title="文件类型分布",
                          hint="按扩展名统计当前源文件夹的文件构成")
        self.stats_tree = QtWidgets.QTreeWidget(card)
        self.stats_tree.setColumnCount(4)
        self.stats_tree.setHeaderLabels(["文件扩展名", "文件数量", "总大小", "占比"])
        self.stats_tree.setRootIsDecorated(False)
        self.stats_tree.setAlternatingRowColors(True)
        hdr = self.stats_tree.header()
        for i in range(4):
            hdr.setSectionResizeMode(i, QtWidgets.QHeaderView.ResizeMode.Stretch)
        card.body.addWidget(self.stats_tree, 1)

        bottom = QtWidgets.QHBoxLayout()
        bottom.setSpacing(24)
        self.stats_total_files = self._label(card, "总文件数  0", "Status")
        self.stats_total_size = self._label(card, "总大小  0 B", "Status")
        self.stats_type_count = self._label(card, "文件类型数  0", "Status")
        for lbl in (self.stats_total_files, self.stats_total_size, self.stats_type_count):
            bottom.addWidget(lbl)
        bottom.addStretch(1)
        card.body.addLayout(bottom)
        lay.addWidget(card, 1)
        tabs.addTab(page, "文件统计")

    # --------------------------------------------------------
    # Tab 5: 高级选项
    # --------------------------------------------------------
    def _build_tab_advanced(self, tabs):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(18, 16, 18, 16)

        opt = GlassFrame(page, title="打包选项")
        modes = QtWidgets.QHBoxLayout()
        modes.setSpacing(16)
        modes.addWidget(self._label(opt, "打包模式", "FieldName"))
        self.mode_buttons = {}
        for mode in ("inno", "nsis", "7zip", "zip"):
            rb = QtWidgets.QRadioButton(self.core.MODE_NAMES[mode], opt)
            modes.addWidget(rb)
            self.mode_buttons[mode] = rb
        modes.addStretch(1)
        opt.body.addLayout(modes)

        comp_row = QtWidgets.QHBoxLayout()
        comp_row.setSpacing(10)
        comp_row.addWidget(self._label(opt, "压缩级别", "FieldName"))
        self.comp_combo = QtWidgets.QComboBox(opt)
        self.comp_labels = {"0": "存储 · 最快", "1": "最快", "3": "快速",
                            "5": "标准", "7": "最大", "9": "极限 · 最小体积"}
        self.comp_combo.addItems(list(self.comp_labels.keys()))
        self.comp_combo.setFixedWidth(110)
        self.comp_hint = self._label(opt, "", "Hint")
        self.comp_combo.currentTextChanged.connect(
            lambda v: self.comp_hint.setText(self.comp_labels.get(v, "")))
        comp_row.addWidget(self.comp_combo)
        comp_row.addWidget(self.comp_hint)
        comp_row.addStretch(1)
        opt.body.addLayout(comp_row)

        self.icon_edit = self._field(opt)
        opt.body.addWidget(
            self._row_widget(opt, "应用图标", self.icon_edit, self._browse_icon))
        self.exclude_edit = self._field(opt)
        opt.body.addWidget(self._row_widget(opt, "排除规则", self.exclude_edit))
        opt.body.addWidget(self._label(
            opt, "多个规则用逗号分隔，支持通配符，如 *.log、__pycache__、.git", "Hint"))
        lay.addWidget(opt, 1)

        inno = GlassFrame(page, title="安装程序设置", hint="仅 Inno Setup 模式生效")
        self.license_edit = self._field(inno)
        inno.body.addWidget(
            self._row_widget(inno, "许可证文件", self.license_edit, self._browse_license))
        self.pre_edit = self._field(inno)
        inno.body.addWidget(self._row_widget(inno, "安装前命令", self.pre_edit))
        post_row = QtWidgets.QHBoxLayout()
        post_row.setSpacing(10)
        post_row.addWidget(self._label(inno, "安装后命令", "FieldName"))
        self.post_edit = self._field(inno)
        post_row.addWidget(self.post_edit, 1)
        b_prev = QtWidgets.QPushButton("预览脚本", inno)
        b_prev.clicked.connect(self._preview_iss_script)
        post_row.addWidget(b_prev)
        inno.body.addLayout(post_row)
        lay.addWidget(inno)

        # 从配置回填
        # 配置文件里的 default_mode 可能非法（用户手改或旧版本残留），
        # 直取字典会 KeyError 让窗口创建失败，程序直接起不来
        mode = self.cfg.get("default_mode", "inno")
        self.mode_buttons[mode if mode in self.mode_buttons else "inno"].setChecked(True)
        idx = self.comp_combo.findText(str(self.cfg.get("compression_level", "9")))
        if idx >= 0:
            self.comp_combo.setCurrentIndex(idx)
        self.comp_hint.setText(self.comp_labels.get(self.comp_combo.currentText(), ""))
        self.icon_edit.setText(str(self.cfg.get("icon_path", "")))
        self.exclude_edit.setText(str(self.cfg.get(
            "exclude_patterns", self.core.DEFAULT_CONFIG["exclude_patterns"])))
        self.license_edit.setText(str(self.cfg.get("license_file", "")))
        self.pre_edit.setText(str(self.cfg.get("pre_install_cmd", "")))
        self.post_edit.setText(str(self.cfg.get("post_install_cmd", "")))
        tabs.addTab(page, "高级选项")

    def _mode(self):
        for mode, btn in self.mode_buttons.items():
            if btn.isChecked():
                return mode
        return "inno"

    # --------------------------------------------------------
    # Tab 6: 构建历史
    # --------------------------------------------------------
    def _build_tab_history(self, tabs):
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(18, 16, 18, 16)

        card = GlassFrame(page)
        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(self._label(card, "最近构建", "Section"))
        toolbar.addStretch(1)
        b_clear = QtWidgets.QPushButton("清除历史", card)
        b_clear.clicked.connect(self._clear_build_history)
        toolbar.addWidget(b_clear)
        b_ref = QtWidgets.QPushButton("刷新", card)
        b_ref.clicked.connect(self._refresh_build_history)
        toolbar.addWidget(b_ref)
        card.body.addLayout(toolbar)

        self.history_tree = QtWidgets.QTreeWidget(card)
        self.history_tree.setColumnCount(5)
        self.history_tree.setHeaderLabels(["时间", "应用名", "模式", "输出文件", "状态"])
        self.history_tree.setRootIsDecorated(False)
        self.history_tree.setAlternatingRowColors(True)
        hdr = self.history_tree.header()
        for i in range(5):
            hdr.setSectionResizeMode(i, QtWidgets.QHeaderView.ResizeMode.Stretch)
        card.body.addWidget(self.history_tree, 1)
        lay.addWidget(card, 1)
        tabs.addTab(page, "构建历史")
        self._refresh_build_history()

    # --------------------------------------------------------
    # 底部操作区 + 日志
    # --------------------------------------------------------
    def _build_bottom(self):
        box = QtWidgets.QVBoxLayout()
        box.setSpacing(10)

        action = GlassFrame()
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        self.build_btn = QtWidgets.QPushButton("开始打包", action)
        self.build_btn.setObjectName("Primary")
        self.build_btn.clicked.connect(self._start_build)
        row.addWidget(self.build_btn)
        for text, fn in (("打开输出目录", self._open_output_dir),
                         ("预览脚本", self._preview_iss_script),
                         ("退出", self._on_close)):
            b = QtWidgets.QPushButton(text, action)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        self.pct_label = self._label(action, "0%", "Status")
        row.addWidget(self.pct_label)
        self.progress = QtWidgets.QProgressBar(action)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumWidth(260)
        row.addWidget(self.progress)
        action.body.addLayout(row)
        self.status_label = self._label(action, "就绪", "Status")
        action.body.addWidget(self.status_label)
        box.addWidget(action)

        log_card = GlassFrame()
        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(self._label(log_card, "运行日志", "Section"))
        toolbar.addStretch(1)
        b_clear = QtWidgets.QPushButton("清空", log_card)
        b_clear.clicked.connect(self._clear_log)
        toolbar.addWidget(b_clear)
        self.log_toggle_btn = QtWidgets.QPushButton("展开", log_card)
        self.log_toggle_btn.setObjectName("Ghost")
        self.log_toggle_btn.clicked.connect(self._toggle_log_panel)
        toolbar.addWidget(self.log_toggle_btn)
        log_card.body.addLayout(toolbar)

        self.log_holder = QtWidgets.QWidget(log_card)
        holder_lay = QtWidgets.QVBoxLayout(self.log_holder)
        holder_lay.setContentsMargins(0, 6, 0, 0)
        holder_lay.setSpacing(6)
        b_save = QtWidgets.QPushButton("保存日志", self.log_holder)
        b_save.setObjectName("Ghost")
        b_save.clicked.connect(self._save_log)
        holder_lay.addWidget(b_save, 0, QtCore.Qt.AlignmentFlag.AlignRight)
        self.log_text = QtWidgets.QPlainTextEdit(self.log_holder)
        self.log_text.setReadOnly(True)
        self.log_text.setFont(mono(9))
        self.log_text.setMaximumHeight(150)
        holder_lay.addWidget(self.log_text)
        log_card.body.addWidget(self.log_holder)
        self.log_holder.setVisible(False)
        box.addWidget(log_card)
        return box

    def _toggle_log_panel(self):
        self._log_expanded = not self._log_expanded
        self.log_holder.setVisible(self._log_expanded)
        self.log_toggle_btn.setText("收起" if self._log_expanded else "展开")

    def _init_log_colors(self):
        t = THEMES[self.current_theme]
        self.log_text.setStyleSheet("QPlainTextEdit { background: %s; }" % t["log_bg"])

        def mk(color):
            f = QtGui.QTextCharFormat()
            f.setForeground(QtGui.QColor(color))
            return f

        self._fmt_success = mk(t["ok"])
        self._fmt_error = mk(t["danger"])
        self._fmt_warning = mk("#f57f17")

    # --------------------------------------------------------
    # 主题
    # --------------------------------------------------------
    def _apply_theme(self, theme_name):
        """应用主题。__init__ 会在控件创建前先调一次，故所有对控件的
        访问都必须判空，不能假定 log_text 等已经存在。"""
        if theme_name not in THEMES:
            theme_name = "light"
        self.current_theme = theme_name
        self.cfg["theme"] = theme_name
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(theme_name))
        if getattr(self, "log_text", None) is not None:
            self._init_log_colors()
        self._refresh_tree_colors()
        self._backdrop_ok = enable_native_backdrop(self, dark=(theme_name == "dark"))

    def _refresh_tree_colors(self):
        """树控件的截断提示行需要用当前主题的文字色重绘。

        __init__ 会在控件创建前先调一次 _apply_theme，故必须用 getattr 判空。
        """
        tree = getattr(self, "file_tree", None)
        if tree is None:
            return
        dim = QtGui.QColor(THEMES[self.current_theme]["text_dim"])
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            if item.data(0, QtCore.Qt.ItemDataRole.UserRole):
                item.setForeground(0, dim)

    def _toggle_theme(self):
        new_theme = "dark" if self.current_theme == "light" else "light"
        self._apply_theme(new_theme)
        self._set_status("已切换到%s主题" % ("深色" if new_theme == "dark" else "浅色"))
        self.core.save_config(self.cfg)

    # --------------------------------------------------------
    # 工具探测
    # --------------------------------------------------------
    def _check_tools(self):
        results = []
        for name, path, show_dir in (
                ("Inno Setup", self.core.find_inno_setup(), False),
                ("7-Zip", self.core.find_7zip(), True),
                ("NSIS", self.core.find_nsis(), True)):
            if path:
                where = (os.path.dirname(path) if show_dir
                         else os.path.basename(os.path.dirname(path)))
                results.append((name, True, where))
            else:
                results.append((name, False, ""))
        self.tools_summary.setText("   ".join(
            "%s %s" % (n, "OK" if ok else "缺") for n, ok, _ in results))
        self._tools_detail = "\n".join(
            "%s: %s" % (n, ("已就绪  " + w) if ok else "未安装")
            for n, ok, w in results)
        self.tools_summary.setToolTip(self._tools_detail)

    # --------------------------------------------------------
    # 日志 / 进度 / 状态
    # --------------------------------------------------------
    def _append_log_lines(self, lines):
        # 逐行 appendPlainText 后再对整行套格式：
        # insertText(text, fmt) 只对**光标之前的字符**生效，
        # 而行尾的 "\n" 会被下一行继承格式，导致着色错位一行。
        for line in lines:
            self.log_text.appendPlainText(line)
            if "[成功]" in line:
                fmt = self._fmt_success
            elif "[错误]" in line:
                fmt = self._fmt_error
            elif "[警告]" in line:
                fmt = self._fmt_warning
            else:
                continue
            # 只套用行内文字，不含换行符
            cursor = self.log_text.textCursor()
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            cursor.movePosition(
                QtGui.QTextCursor.MoveOperation.StartOfBlock,
                QtGui.QTextCursor.MoveMode.KeepAnchor)
            if not cursor.hasSelection():
                continue
            cursor.mergeCharFormat(fmt)
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _log(self, msg):
        text = str(msg)

        def do():
            # 判断必须在 UI 线程内做：_log_expanded 是 UI 状态，
            # 且若在投递时求值，worker 连续投递两条 "==========" 会
            # toggle 两次把面板又折叠回去
            if not self._log_expanded and "==========" in text:
                self._toggle_log_panel()
            self.pump.post(text)

        self.call_in_ui(do)

    def _clear_log(self):
        self.log_text.clear()

    def _save_log(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "保存日志", "", "文本文件 (*.txt);;所有文件 (*.*)")
        if not path:
            return
        if not path.lower().endswith(".txt"):
            path += ".txt"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_text.toPlainText())
            self._set_status("日志已保存: %s" % path)
        except Exception as e:
            self._warn_box("错误", "保存日志失败:\n%s" % e, error=True)

    def _set_progress(self, value):
        try:
            value = max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return

        def do():
            self.progress.setValue(value)
            self.pct_label.setText("%d%%" % value)

        self.call_in_ui(do)

    def _set_status(self, msg):
        self.call_in_ui(lambda: self.status_label.setText(str(msg)))

    # --------------------------------------------------------
    # 选择对话框
    # --------------------------------------------------------
    def _pick_dir(self, title, start=""):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, title, start if os.path.isdir(start) else "")
        return path or None

    def _pick_file(self, title, filt, start=""):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, title, start if os.path.isdir(start) else "", filt)
        return path or None

    def _warn_box(self, title, msg, error=False):
        if self._closing:
            return
        if error:
            QtWidgets.QMessageBox.critical(self, title, msg)
        else:
            QtWidgets.QMessageBox.warning(self, title, msg)

    def _info_box(self, title, msg):
        if self._closing:
            return
        QtWidgets.QMessageBox.information(self, title, msg)

    # --------------------------------------------------------
    # 文件浏览
    # --------------------------------------------------------
    def _browse_source(self):
        d = self._pick_dir("选择源文件夹", self.src_edit.text())
        if d:
            self.src_edit.setText(d)
            if not self.out_edit.text():
                self.out_edit.setText(os.path.join(os.path.dirname(d), "output"))
            self._refresh_file_tree()

    def _browse_output(self):
        d = self._pick_dir("选择输出目录", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def _browse_icon(self):
        f = self._pick_file("选择图标文件",
                            "图标文件 (*.ico);;所有文件 (*.*)",
                            os.path.dirname(self.icon_edit.text())
                            if self.icon_edit.text() else self.cfg.get("last_icon_dir", ""))
        if f:
            self.icon_edit.setText(f)
            self.cfg["last_icon_dir"] = os.path.dirname(f)

    def _browse_license(self):
        f = self._pick_file("选择许可证文件",
                            "文本文件 (*.txt);;所有文件 (*.*)",
                            os.path.dirname(self.license_edit.text())
                            if self.license_edit.text() else "")
        if f:
            self.license_edit.setText(f)

    def _paste_path_to_source(self):
        clip = QtWidgets.QApplication.clipboard().text()
        if not clip:
            return
        path = clip.strip()
        if os.path.isdir(path):
            self.src_edit.setText(path)
            if not self.out_edit.text():
                self.out_edit.setText(os.path.join(os.path.dirname(path), "output"))
            self._refresh_file_tree()

    # --------------------------------------------------------
    # 批量列表
    # --------------------------------------------------------
    def _add_batch_folder(self):
        d = self._pick_dir("选择要添加的文件夹")
        if d:
            if d not in self._batch_items():
                self.batch_list.addItem(d)
            else:
                self._info_box("提示", "该文件夹已在列表中")
            self._refresh_batch_hint()

    def _remove_batch_folder(self):
        for item in self.batch_list.selectedItems():
            self.batch_list.takeItem(self.batch_list.row(item))
        self._refresh_batch_hint()

    def _clear_batch_folders(self):
        self.batch_list.clear()
        self._refresh_batch_hint()

    # --------------------------------------------------------
    # 文件树
    # --------------------------------------------------------
    def _on_source_changed(self, *_a):
        if self._source_timer is None:
            self._source_timer = QtCore.QTimer(self)
            self._source_timer.setSingleShot(True)
            self._source_timer.timeout.connect(self._refresh_file_tree)
        self._source_timer.start(500)

    def _on_search_key(self, *_a):
        if self._search_timer is None:
            self._search_timer = QtCore.QTimer(self)
            self._search_timer.setSingleShot(True)
            self._search_timer.timeout.connect(self._filter_file_tree)
        self._search_timer.start(300)

    def _add_tree_row(self, tree, name, *values, truncated=False):
        item = QtWidgets.QTreeWidgetItem(tree, [name] + [str(v) for v in values])
        if truncated:
            # 用 UserRole 标记截断行，而不是靠 "…" 前缀判断：
            # U+2026 在 Windows 文件名里合法，真实文件可能以 … 开头
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, True)
            item.setForeground(0, QtGui.QColor(THEMES[self.current_theme]["text_dim"]))
        return item

    def _refresh_file_tree(self):
        self.file_tree.clear()
        source = self.src_edit.text().strip()
        if not source or not os.path.isdir(source):
            self.file_count_label.setText("文件数  0")
            self.file_size_label.setText("总大小  0 B")
            self._update_stats()
            return
        self._set_status("正在扫描文件...")
        exclude = self.exclude_edit.text()
        self._scan_seq += 1
        seq = self._scan_seq

        def worker():
            files, total = self.core.scan_folder(source, exclude)

            def update():
                # 代次不一致说明已有更新的扫描在进行，丢弃本次结果
                if seq != self._scan_seq:
                    return
                self.scanned_files = files
                self.scanned_size = total
                limit = self.core.FILE_TREE_MAX_DISPLAY
                for rel_path, sz in files[:limit]:
                    self._add_tree_row(self.file_tree, rel_path,
                                       self.core.format_size(sz))
                if len(files) > limit:
                    self._add_tree_row(
                        self.file_tree,
                        "… 还有 %d 个文件未显示 (已截断，搜索可匹配全部)"
                        % (len(files) - limit), "", truncated=True)
                self.file_count_label.setText("文件数  %d" % len(files))
                self.file_size_label.setText("总大小  %s" % self.core.format_size(total))
                self._set_status("扫描完成: %d 个文件, %s"
                                 % (len(files), self.core.format_size(total)))
                self._update_stats()

            self.call_in_ui(update)

        threading.Thread(target=worker, daemon=True).start()

    def _filter_file_tree(self):
        keyword = self.search_edit.text()
        case_sensitive = self.case_check.isChecked()
        self.file_tree.clear()
        matched = []
        for rel_path, sz in self.scanned_files:
            if keyword:
                cp = rel_path if case_sensitive else rel_path.lower()
                ck = keyword if case_sensitive else keyword.lower()
                if ck not in cp:
                    continue
            matched.append((rel_path, sz))
        limit = self.core.FILE_TREE_MAX_DISPLAY
        for rel_path, sz in matched[:limit]:
            self._add_tree_row(self.file_tree, rel_path, self.core.format_size(sz))
        if len(matched) > limit:
            self._add_tree_row(self.file_tree,
                               "… 匹配 %d 个，仅显示前 %d 个" % (len(matched), limit),
                               "", truncated=True)
        self.file_count_label.setText("文件数: %d / %d"
                                      % (len(matched), len(self.scanned_files)))

    def _current_tree_path(self):
        item = self.file_tree.currentItem()
        if item is None:
            return None
        if item.data(0, QtCore.Qt.ItemDataRole.UserRole):
            return None          # 截断提示行不是真实文件
        rel = item.text(0)
        source = self.src_edit.text().strip()
        return os.path.join(source, rel) if source else rel

    def _build_file_tree_menu(self, path):
        """构造文件树右键菜单。各 action 的 data 里带上作用的路径，
        便于测试断言（直接 monkeypatch QMenu.exec 会动到 PySide6 的
        C++ 方法，不可靠且可能挂起）。"""
        menu = QtWidgets.QMenu(self)
        name_act = menu.addAction("复制文件名")
        name_act.setData(path)
        name_act.triggered.connect(lambda: self._clip_copy(os.path.basename(path)))
        full_act = menu.addAction("复制完整路径")
        full_act.setData(path)
        full_act.triggered.connect(lambda: self._clip_copy(path))
        menu.addSeparator()
        open_act = menu.addAction("在资源管理器中打开")
        open_act.setData(path)
        open_act.triggered.connect(lambda: self._open_in_explorer(path))
        term_act = menu.addAction("在此文件夹中打开终端")
        term_act.setData(path)
        term_act.triggered.connect(lambda: self._open_terminal(os.path.dirname(path)))
        return menu

    def _on_file_tree_right_click(self, pos):
        # Qt 的 customContextMenuRequested 只在右键时触发，不会更新
        # currentItem；必须用 itemAt(pos) 反查鼠标所在行，否则菜单会作用到
        # 上一次选中的行（复制出错误路径、打开错误文件）
        item = self.file_tree.itemAt(pos)
        if item is None:
            return None
        self.file_tree.setCurrentItem(item)
        path = self._current_tree_path()
        if not path:
            return None
        menu = self._build_file_tree_menu(path)
        menu.exec(self.file_tree.viewport().mapToGlobal(pos))
        return menu

    def _on_file_tree_double_click(self, _item, _col):
        path = self._current_tree_path()
        if not path:
            return
        if os.path.isfile(path):
            try:
                os.startfile(path)
            except Exception as e:
                self._warn_box("错误", "无法打开文件:\n%s\n%s" % (path, e), error=True)
        elif os.path.isdir(path):
            self._open_in_explorer(path)

    def _clip_copy(self, text):
        QtWidgets.QApplication.clipboard().setText(text)
        self._set_status("已复制: %s" % text)

    def _open_in_explorer(self, path):
        if os.path.isfile(path):
            subprocess.run(["explorer", "/select,", path], check=False)
        elif os.path.isdir(path):
            os.startfile(path)

    def _open_terminal(self, directory):
        if os.path.isdir(directory):
            # 用 cwd 指定工作目录，避免把路径拼进命令行导致 cmd /k 重新解析注入
            subprocess.Popen(["cmd", "/k"], cwd=directory,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))

    def _export_file_list_csv(self):
        if not self.scanned_files:
            self._info_box("提示", "没有可导出的文件列表，请先扫描。")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出文件列表", "", "CSV 文件 (*.csv);;所有文件 (*.*)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            import csv
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["文件路径", "大小(字节)"])
                for rel_path, sz in self.scanned_files:
                    writer.writerow([rel_path, sz])
            self._set_status("文件列表已导出: %s" % path)
            self._info_box("成功", "已导出 %d 个文件到:\n%s"
                           % (len(self.scanned_files), path))
        except Exception as e:
            self._warn_box("错误", "导出失败:\n%s" % e, error=True)

    def _update_stats(self):
        self.stats_tree.clear()
        if not self.scanned_files:
            self.stats_total_files.setText("总文件数  0")
            self.stats_total_size.setText("总大小  0 B")
            self.stats_type_count.setText("文件类型数  0")
            return
        ext_map = {}
        for rel_path, sz in self.scanned_files:
            _, ext = os.path.splitext(rel_path)
            ext = ext.lower() if ext else "(无扩展名)"
            info = ext_map.setdefault(ext, {"count": 0, "size": 0})
            info["count"] += 1
            info["size"] += sz
        total = sum(v["size"] for v in ext_map.values())
        for ext, info in sorted(ext_map.items(), key=lambda x: x[1]["size"], reverse=True):
            ratio = "%.1f%%" % (info["size"] / total * 100) if total > 0 else "0.0%"
            self._add_tree_row(self.stats_tree, ext, info["count"],
                               self.core.format_size(info["size"]), ratio)
        self.stats_total_files.setText("总文件数  %d" % len(self.scanned_files))
        self.stats_total_size.setText("总大小  %s" % self.core.format_size(total))
        self.stats_type_count.setText("文件类型数  %d" % len(ext_map))

    # --------------------------------------------------------
    # 项目 / 配置
    # --------------------------------------------------------
    def _project_data(self):
        return {
            "last_source": self.src_edit.text(),
            "last_output": self.out_edit.text(),
            "app_name": self.appname_edit.text(),
            "app_version": self.appver_edit.text(),
            "publisher": self.pub_edit.text(),
            "default_mode": self._mode(),
            "icon_path": self.icon_edit.text(),
            "exclude_patterns": self.exclude_edit.text(),
            "compression_level": self.comp_combo.currentText(),
            "license_file": self.license_edit.text(),
            "pre_install_cmd": self.pre_edit.text(),
            "post_install_cmd": self.post_edit.text(),
            "batch_sources": self._batch_items(),
        }

    def _apply_project_data(self, data):
        pairs = (
            ("last_source", self.src_edit), ("last_output", self.out_edit),
            ("app_name", self.appname_edit), ("app_version", self.appver_edit),
            ("publisher", self.pub_edit), ("icon_path", self.icon_edit),
            ("exclude_patterns", self.exclude_edit),
            ("license_file", self.license_edit),
            ("pre_install_cmd", self.pre_edit),
            ("post_install_cmd", self.post_edit),
        )
        for key, widget in pairs:
            if key in data:
                widget.setText(str(data[key] or ""))
        if "default_mode" in data and data["default_mode"] in self.mode_buttons:
            self.mode_buttons[data["default_mode"]].setChecked(True)
        if "compression_level" in data:
            idx = self.comp_combo.findText(str(data["compression_level"]))
            if idx >= 0:
                self.comp_combo.setCurrentIndex(idx)
        if "batch_sources" in data:
            self.batch_list.clear()
            for src in data["batch_sources"]:
                if src:
                    self.batch_list.addItem(src)
            self._refresh_batch_hint()

    def _save_project(self):
        if self.current_project:
            if self.core.save_project(self.current_project, self._project_data()):
                self._set_status("项目已保存: %s" % self.current_project)
            else:
                self._warn_box("错误", "保存项目失败", error=True)
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "保存项目", "", "Packager 项目文件 (*.packager);;所有文件 (*.*)")
        if not path:
            return
        if not path.lower().endswith(".packager"):
            path += ".packager"
        if self.core.save_project(path, self._project_data()):
            self.current_project = path
            self.cfg["last_project_file"] = path
            self._add_recent_project(path)
            self.core.save_config(self.cfg)
            self._set_status("项目已保存: %s" % path)
        else:
            self._warn_box("错误", "保存项目失败", error=True)

    def _open_project(self):
        path = self._pick_file("打开项目",
                               "Packager 项目文件 (*.packager);;所有文件 (*.*)",
                               os.path.dirname(self.current_project)
                               if self.current_project else "")
        if path:
            self._do_open_project(path)

    def _do_open_project(self, path):
        data = self.core.load_project(path)
        if data is None:
            self._warn_box("错误", "无法加载项目文件:\n%s" % path, error=True)
            return
        self._apply_project_data(data)
        self.current_project = path
        self.cfg["last_project_file"] = path
        self._add_recent_project(path)
        self.core.save_config(self.cfg)
        self._set_status("项目已加载: %s" % path)
        self._refresh_file_tree()

    def _add_recent_project(self, path):
        # 必须先复制再改：load_config() 是浅拷贝，
        # 原地修改会连带改掉模块级 DEFAULT_CONFIG 里的同一 list
        recent = list(self.cfg.get("recent_projects", []))
        if path in recent:
            recent.remove(path)
        recent.insert(0, path)
        self.cfg["recent_projects"] = recent[:self.core.RECENT_PROJECTS_MAX]
        self._refresh_recent_menu()

    def _refresh_recent_menu(self):
        self.recent_menu.clear()
        recent = self.cfg.get("recent_projects", [])
        if not recent:
            act = self.recent_menu.addAction("(无最近项目)")
            act.setEnabled(False)
            return
        for p in recent:
            # 默认参数绑定，避免闭包捕获最后一个循环变量
            act = self.recent_menu.addAction(
                "%s  (%s)" % (os.path.basename(p) if p else "", p))
            act.triggered.connect(lambda _c=False, fp=p: self._load_recent_project(fp))
        self.recent_menu.addSeparator()
        act = self.recent_menu.addAction("清除最近项目列表")
        act.triggered.connect(self._clear_recent_projects)

    def _load_recent_project(self, path):
        if not os.path.isfile(path):
            self._warn_box("提示", "项目文件不存在:\n%s" % path)
            return
        self._do_open_project(path)

    def _clear_recent_projects(self):
        self.cfg["recent_projects"] = []
        self._refresh_recent_menu()

    def _export_config(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出配置", "", "JSON 配置文件 (*.json);;所有文件 (*.*)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            import json
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._project_data(), f, ensure_ascii=False, indent=2)
            self._set_status("配置已导出: %s" % path)
        except Exception as e:
            self._warn_box("错误", "导出配置失败:\n%s" % e, error=True)

    def _import_config(self):
        path = self._pick_file("导入配置",
                               "JSON 配置文件 (*.json);;所有文件 (*.*)",
                               os.path.dirname(self.current_project)
                               if self.current_project else "")
        if not path:
            return
        try:
            import json
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._apply_project_data(data)
            self._set_status("配置已导入: %s" % path)
            self._refresh_file_tree()
        except json.JSONDecodeError:
            self._warn_box("错误", "配置文件格式错误:\n%s" % path, error=True)
        except Exception as e:
            self._warn_box("错误", "导入配置失败:\n%s" % e, error=True)

    def _auto_save_project(self):
        try:
            import json
            import tempfile
            path = os.path.join(tempfile.gettempdir(), "packager_autosave.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._project_data(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # --------------------------------------------------------
    # 构建历史
    # --------------------------------------------------------
    def _add_build_history(self, mode, app_name, output_file, success):
        """线程安全入口：批量打包会在 worker 线程调用。

        cfg["build_history"] 的写入与 UI 刷新必须同处 UI 线程，
        否则主线程遍历该 list 时被 worker insert 会抛
        "list changed size during iteration"。
        """
        self.call_in_ui(lambda: self._add_build_history_sync(
            mode, app_name, output_file, success))

    def _add_build_history_sync(self, mode, app_name, output_file, success):
        from datetime import datetime
        history = list(self.cfg.get("build_history", []))
        history.insert(0, {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "app_name": app_name,
            "mode": mode,
            "output": output_file or "",
            "status": "成功" if success else "失败",
        })
        self.cfg["build_history"] = history[:self.core.BUILD_HISTORY_MAX]
        self.core.save_config(self.cfg)
        self._refresh_build_history()

    def _refresh_build_history(self):
        self.history_tree.clear()
        for entry in list(self.cfg.get("build_history", [])):
            QtWidgets.QTreeWidgetItem(self.history_tree, [
                entry.get("time", ""), entry.get("app_name", ""),
                entry.get("mode", ""), entry.get("output", ""),
                entry.get("status", "")])

    def _clear_build_history(self):
        self.cfg["build_history"] = []
        self.core.save_config(self.cfg)
        self._refresh_build_history()

    # --------------------------------------------------------
    # 打包流程
    # --------------------------------------------------------
    def _sync_cfg_from_ui(self):
        self.cfg["last_source"] = self.src_edit.text().strip()
        self.cfg["last_output"] = self.out_edit.text().strip()
        self.cfg["app_name"] = self.appname_edit.text().strip()
        self.cfg["app_version"] = self.appver_edit.text().strip()
        self.cfg["publisher"] = self.pub_edit.text().strip()
        self.cfg["default_mode"] = self._mode()
        self.cfg["icon_path"] = self.icon_edit.text().strip()
        self.cfg["exclude_patterns"] = self.exclude_edit.text().strip()
        self.cfg["compression_level"] = self.comp_combo.currentText()
        self.cfg["license_file"] = self.license_edit.text().strip()
        self.cfg["pre_install_cmd"] = self.pre_edit.text().strip()
        self.cfg["post_install_cmd"] = self.post_edit.text().strip()

    def _set_build_buttons_state(self, enabled):
        self.build_btn.setEnabled(enabled)

    def _start_build(self):
        # 重入保护：打包进行中（含 Ctrl+B 快捷键触发）直接忽略
        if self._building:
            return
        self._building = True
        source = self.src_edit.text().strip()
        output = self.out_edit.text().strip()
        app_name = self.appname_edit.text().strip()
        batch_items = self._batch_items()
        core = self.core

        def abort(msg, is_error=False):
            """校验失败：先保持重入锁，提示后再复位标志。

            复位放在弹窗之后，使模态期间 _building 仍为 True，
            此时按 Ctrl+B 会被开头的重入保护拦截，不会叠加对话框。
            """
            try:
                self._warn_box("错误" if is_error else "提示", msg, error=is_error)
            finally:
                self._building = False

        if not batch_items and (not source or not os.path.isdir(source)):
            abort("请选择有效的源文件夹（或在批量列表中添加文件夹）")
            return
        if not output:
            abort("请选择输出目录")
            return
        if not app_name:
            abort("请输入应用名称")
            return

        valid, msg = core.validate_app_name(app_name)
        if not valid:
            abort(msg, is_error=True)
            return
        version = self.appver_edit.text().strip()
        if not version:
            abort("请输入版本号")
            return
        # 这些字段会拼进 Inno/NSIS 脚本字符串，必须先做脚本安全校验
        fields = (("应用名称", app_name), ("版本号", version),
                  ("发布者", self.pub_edit.text().strip()),
                  ("排除规则", self.exclude_edit.text().strip()),
                  ("图标路径", self.icon_edit.text().strip()),
                  ("许可文件", self.license_edit.text().strip()),
                  ("安装前命令", self.pre_edit.text().strip()),
                  ("安装后命令", self.post_edit.text().strip()))
        for fname, fval in fields:
            ok_field, fmsg = core.validate_script_field(fval, fname)
            if not ok_field:
                abort(fmsg, is_error=True)
                return

        # 路径安全校验（拒绝双引号与控制字符，防止脚本字符串闭合注入）
        if source:
            try:
                source = core.validate_path(source)
            except ValueError as e:
                abort("源文件夹路径非法: %s" % e, is_error=True)
                return
        try:
            output = core.validate_path(output)
        except ValueError as e:
            abort("输出目录路径非法: %s" % e, is_error=True)
            return
        try:
            os.makedirs(output, exist_ok=True)
        except Exception as e:
            abort("无法创建输出目录:\n%s" % e, is_error=True)
            return

        self._sync_cfg_from_ui()
        self.cfg["last_source"] = source
        self.cfg["last_output"] = output
        self.cfg["window_width"] = self.width()
        self.cfg["window_height"] = self.height()
        core.save_config(self.cfg)

        mode = self._mode()
        build_func = {"inno": core.build_with_inno, "7zip": core.build_with_7zip,
                      "zip": core.build_with_zip, "nsis": core.build_with_nsis}.get(mode)
        if not build_func:
            abort("未知的打包模式: %s" % mode, is_error=True)
            return

        if batch_items:
            self._start_batch_build(batch_items, mode, build_func, app_name)
            return

        start_time = time.time()
        try:
            self._set_build_buttons_state(False)
            self._set_progress(0)
            self._set_status("打包中...")
            self.log_text.clear()
        except Exception as e:
            self._set_build_buttons_state(True)
            self._building = False
            self._warn_box("错误", "界面初始化失败，无法开始打包:\n%s" % e, error=True)
            return

        def finish():
            self._building = False
            self._set_build_buttons_state(True)

        def worker():
            output_file = None
            try:
                ok, out_path = build_func(self.cfg, self._log, self._set_progress)
                elapsed = time.time() - start_time
                if out_path:
                    output_file = out_path

                def done():
                    finish()
                    if ok:
                        self._set_status("打包完成!")
                        log_msg = "\n========== 打包结果 =========="
                        if output_file and os.path.isfile(output_file):
                            log_msg += "\n输出文件: %s" % output_file
                            log_msg += "\n文件大小: %s" % core.format_size(
                                os.path.getsize(output_file))
                        log_msg += "\n耗时: %.1f 秒" % elapsed
                        log_msg += "\n================================"
                        self._log(log_msg)
                        self._add_build_history(core.MODE_NAMES.get(mode, mode),
                                                app_name, output_file, True)
                        self._play_finish_sound(success=True)
                        self._info_box("完成", "打包成功完成!")
                    else:
                        self._set_status("打包失败")
                        self._add_build_history(core.MODE_NAMES.get(mode, mode),
                                                app_name, output_file, False)
                        self._play_finish_sound(success=False)

                self.call_in_ui(done)
            except Exception as e:
                def fail():
                    finish()
                    self._set_status("打包异常: %s" % e)
                    self._play_finish_sound(success=False)
                self.call_in_ui(fail)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as e:
            finish()
            self._log("[错误] 无法启动打包线程: %s" % e)

    def _start_batch_build(self, batch_items, mode, build_func, app_name=None):
        core = self.core
        self._building = True
        if app_name is None:
            app_name = self.appname_edit.text()
        total = len(batch_items)
        # 与单构建一致：UI 准备段整体保护，异常时复位标志与按钮，
        # 否则 _building 卡 True、按钮永久禁用（此后 Ctrl+B 被重入保护拦截）
        try:
            self._set_build_buttons_state(False)
            self._set_progress(0)
            self._set_status("批量打包中 (共 %d 个)..." % total)
            self.log_text.clear()
            self._log("========== 批量打包开始 (共 %d 个文件夹) ==========" % total)
        except Exception as e:
            self._set_build_buttons_state(True)
            self._building = False
            self._warn_box("错误", "界面初始化失败，无法开始批量打包:\n%s" % e, error=True)
            return

        def worker():
            results = []
            try:
                for idx, src in enumerate(batch_items):
                    if not os.path.isdir(src):
                        self._log("[警告] 跳过无效路径: %s" % src)
                        results.append((src, False, None))
                        continue
                    # 每个文件夹独立配置
                    batch_cfg = dict(self.cfg)
                    batch_cfg["last_source"] = src
                    # 输出子目录用源文件夹名 + 序号，避免不同盘同名目录冲突
                    sub_name = "%02d_%s" % (
                        idx + 1, os.path.basename(src.rstrip("\\/")) or "output")
                    sub_output = os.path.join(self.cfg["last_output"], sub_name)
                    # makedirs 纳入 try，失败只跳过该项而不是线程崩溃
                    try:
                        os.makedirs(sub_output, exist_ok=True)
                    except Exception as e:
                        self._log("[错误] 无法创建输出目录 %s: %s" % (sub_output, e))
                        results.append((src, False, None))
                        continue
                    batch_cfg["last_output"] = sub_output

                    def batch_progress(val, _idx=idx):
                        self._set_progress(int((_idx / total) * 100 + val / total))

                    self._log("\n--- [%d/%d] %s ---" % (idx + 1, total, src))
                    try:
                        ok, out_path = build_func(batch_cfg, self._log, batch_progress)
                        results.append((src, ok, out_path))
                        self._add_build_history(
                            core.MODE_NAMES.get(mode, mode), app_name,
                            out_path if ok else None, ok)
                    except Exception as e:
                        self._log("[错误] 异常: %s" % e)
                        results.append((src, False, None))

                ok_count = sum(1 for _, ok, _ in results if ok)
                summary = "\n========== 批量打包结果 ==========\n"
                summary += "成功: %d / %d\n" % (ok_count, total)
                for src, ok, out in results:
                    summary += "  %s %s" % ("[OK]" if ok else "[FAIL]", src)
                    if out:
                        summary += " -> %s" % out
                    summary += "\n"
                summary += "================================"
                self._log(summary)
                self._set_status("批量打包完成: %d/%d 成功" % (ok_count, total))
                self._set_progress(100)
                self._play_finish_sound(success=(ok_count == total))
                self.call_in_ui(lambda: self._info_box(
                    "批量打包完成", "成功: %d / %d\n详见日志。" % (ok_count, total)))
            except Exception as e:
                # 意外异常也要写日志，不能让线程静默死亡
                try:
                    self._log("[错误] 批量打包异常终止: %s" % e)
                except Exception:
                    pass
                self._play_finish_sound(success=False)
            finally:
                # 无论成败都恢复按钮，避免永久禁用
                def reset():
                    self._building = False
                    self._set_build_buttons_state(True)
                self.call_in_ui(reset)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as e:
            self._building = False
            self._set_build_buttons_state(True)
            self._log("[错误] 无法启动批量打包线程: %s" % e)

    def _play_finish_sound(self, success=True):
        """完成提示音：放到后台线程，避免阻塞 UI。"""
        def play():
            try:
                import winsound
                winsound.MessageBeep(
                    winsound.MB_ICONASTERISK if success else winsound.MB_ICONHAND)
            except Exception:
                pass
        threading.Thread(target=play, daemon=True).start()

    # --------------------------------------------------------
    # 预览 / 关于 / 退出
    # --------------------------------------------------------
    def _preview_iss_script(self):
        mode = self._mode()
        core = self.core
        if mode not in ("inno", "nsis"):
            self._info_box("提示", "脚本预览仅在 Inno Setup 或 NSIS 模式下可用。")
            return
        cfg = self._project_data()
        if mode == "inno":
            content = core.generate_inno_script(cfg)
            title = "Inno Setup 脚本预览"
        else:
            content = core.generate_nsis_script(cfg)
            title = "NSIS 脚本预览"

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(720, 580)
        lay = QtWidgets.QVBoxLayout(dlg)
        edit = QtWidgets.QPlainTextEdit(dlg)
        edit.setReadOnly(True)
        edit.setFont(mono(10))
        edit.setPlainText(content)
        lay.addWidget(edit, 1)
        row = QtWidgets.QHBoxLayout()

        def copy_all():
            QtWidgets.QApplication.clipboard().setText(content)
            self._set_status("脚本已复制到剪贴板")

        b_copy = QtWidgets.QPushButton("复制到剪贴板", dlg)
        b_copy.clicked.connect(copy_all)
        row.addWidget(b_copy)
        b_close = QtWidgets.QPushButton("关闭", dlg)
        b_close.clicked.connect(dlg.close)
        row.addWidget(b_close)
        row.addStretch(1)
        lay.addLayout(row)
        dlg.exec()

    def _open_output_dir(self):
        output = self.out_edit.text().strip()
        if output and os.path.isdir(output):
            try:
                os.startfile(output)
            except Exception:
                self._info_box("提示", "输出目录: %s" % output)
        else:
            self._warn_box("提示", "输出目录不存在: %s" % output)

    def _show_about(self):
        core = self.core
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("关于 %s" % core.APP_NAME)
        dlg.setFixedWidth(470)
        lay = QtWidgets.QVBoxLayout(dlg)
        lay.setContentsMargins(22, 22, 22, 22)
        title = QtWidgets.QLabel(core.APP_NAME, dlg)
        title.setObjectName("Title")
        lay.addWidget(title)
        ver = QtWidgets.QLabel("版本 %s" % core.APP_VERSION, dlg)
        ver.setObjectName("Subtitle")
        lay.addWidget(ver)
        sec = QtWidgets.QLabel("功能列表", dlg)
        sec.setObjectName("Section")
        lay.addWidget(sec)
        for feat in (
                "PySide6 / Qt 6 界面，支持深色 / 浅色主题",
                "Windows 11 原生 acrylic 毛玻璃",
                "四种打包模式: Inno Setup / NSIS / 7-Zip SFX / ZIP",
                "排除规则在打包阶段生效，四模式语义一致",
                "批量打包多个文件夹，输出带序号的子目录",
                "文件预览搜索过滤 + 文件类型统计",
                "项目文件保存与加载 / 最近项目",
                "Inno Setup / NSIS 安装脚本预览",
                "构建历史记录",
                "文件树右键菜单（复制路径 / 打开）",
                "自动保存防崩溃",
                "键盘快捷键支持 (Ctrl+B / Ctrl+T / F5)",
                "命令行模式打包 (支持 --dry-run --list-modes)",
        ):
            lay.addWidget(QtWidgets.QLabel("· " + feat, dlg))
        line = QtWidgets.QLabel("项目主页", dlg)
        line.setObjectName("Section")
        lay.addWidget(line)
        for _name, url in core.REPO_URLS:
            lay.addWidget(LinkLabel(url, dlg))
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        b = QtWidgets.QPushButton("关闭", dlg)
        b.clicked.connect(dlg.close)
        row.addWidget(b)
        lay.addLayout(row)
        dlg.exec()

    def showEvent(self, event):
        super().showEvent(event)
        # winId 必须等窗口 handle 创建后才有效，故在 showEvent 里补一次
        if not self._backdrop_ok:
            self._backdrop_ok = enable_native_backdrop(
                self, dark=(self.current_theme == "dark"))

    def _persist_and_stop(self):
        """落盘配置并停掉所有定时器（所有关闭路径的统一收口）。"""
        try:
            self.cfg.update(self._project_data())
            self.cfg["window_width"] = self.width()
            self.cfg["window_height"] = self.height()
            self.cfg["last_project_file"] = self.current_project
            self.core.save_config(self.cfg)
        except Exception:
            pass
        for timer in (self._search_timer, self._source_timer):
            if timer is not None:
                timer.stop()
        if self._autosave_timer is not None:
            self._autosave_timer.stop()

    def closeEvent(self, event):
        """标题栏 X 是最常见的关闭方式，必须与菜单「退出」等价。

        旧版用 root.protocol("WM_DELETE_WINDOW", _on_close) 把所有关闭路径
        都导向 _on_close；Qt 里点 X 直接进 closeEvent，若不在这里保存配置，
        窗口尺寸 / 源路径 / 批量列表 / last_project_file 全部丢失。
        """
        if self._building:
            ok = QtWidgets.QMessageBox.question(
                self, "确认退出", "正在打包中，关闭会中断当前任务。\n确定要退出吗?")
            if ok != QtWidgets.QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._closing = True
        self._persist_and_stop()
        super().closeEvent(event)

    def _on_close(self):
        """菜单「退出」与底部按钮入口；实际收口在 closeEvent。"""
        self.close()
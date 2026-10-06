# -*- coding: utf-8 -*-
"""packager_ui.py 的界面层单元测试（PySide6 / Qt 6 offscreen）。

运行: python -m pytest test_ui.py -v

界面层已从 tkinter 迁移到 PySide6，故 GUI 用例全部在此文件；
test_packager.py 只保留不依赖界面的核心逻辑用例。

测试统一使用 QT_QPA_PLATFORM=offscreen，可在无显示环境（含 CI）
运行；需要真实窗口行为的用例（如原生毛玻璃）会被显式跳过。
"""
import os
import sys
import threading
import time

# 必须在导入 QtWidgets 之前设置：offscreen 平台插件允许无显示环境建窗口
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import packager as pk

pytest.importorskip("PySide6", reason="未安装 PySide6")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

import packager_ui as ui  # noqa: E402


# ------------------------------------------------------------
# fixtures
# ------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance()
    created = False
    if app is None:
        app = QtWidgets.QApplication([])
        created = True
    yield app
    if created:
        app.quit()


@pytest.fixture
def win(qapp, tmp_path_factory, monkeypatch):
    """实例化主窗口。

    CONFIG_FILE 指向临时目录：GUI 用例不得读写开发者真实的
    ~/.packager_config.json（既避免污染，也避免受本机既有设置影响）。
    """
    home = tmp_path_factory.mktemp("ui_home")
    original = pk.CONFIG_FILE
    pk.CONFIG_FILE = str(home / ".packager_config.json")
    window = ui.PackagerWindow(pk)
    window.show()   # offscreen 下也有效；不 show 则 isVisible() 恒为 False
    qapp.processEvents()
    try:
        yield window
    finally:
        # 拆卸时若 _building 仍为 True，closeEvent 会弹模态确认框把
        # 整个测试进程挂住，必须先清标志
        window._building = False
        window._closing = True
        pk.CONFIG_FILE = original
        window.close()
        window.deleteLater()
        qapp.processEvents()


def pump(app, rounds=6):
    """跑事件循环，让 QueuedConnection 的回调真正执行。"""
    for _ in range(rounds):
        app.processEvents()
        time.sleep(0.005)


# ------------------------------------------------------------
# 主题与样式
# ------------------------------------------------------------
def test_both_themes_define_same_keys():
    """两套主题键必须一致，否则 QSS 会静默缺字段。"""
    assert sorted(ui.THEMES["light"]) == sorted(ui.THEMES["dark"])


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_stylesheet_has_no_unresolved_placeholder(theme):
    """QSS 里不能残留 {key} 占位符。

    之前踩过的坑：模板用 .format(t) 而非 .format(**t)，
    具名占位符取不到值直接抛 KeyError，连带整个界面无样式。
    """
    css = ui.build_stylesheet(theme)
    assert "{{" not in css and "}}" not in css, "存在未展开的双花括号"
    for key in ("{card}", "{text}", "{window}"):
        assert key not in css, "占位符 %s 未被展开" % key
    assert "rgba(" in css, "玻璃卡片应使用半透明颜色"


def test_stylesheet_brace_balance():
    css = ui.build_stylesheet("dark")
    assert css.count("{") == css.count("}"), "QSS 花括号不配平会导致整段失效"


def test_apply_theme_switches_and_persists(win, qapp):
    win._apply_theme("dark")
    pump(qapp)
    assert win.current_theme == "dark"
    assert win.cfg["theme"] == "dark"
    win._apply_theme("light")
    pump(qapp)
    assert win.current_theme == "light"


def test_apply_theme_ignores_unknown_theme(win, qapp):
    win._apply_theme("不存在的主题")
    pump(qapp)
    assert win.current_theme == "light", "未知主题应回退到 light"


def test_toggle_theme_flips(win, qapp):
    win._apply_theme("light")
    win._toggle_theme()
    pump(qapp)
    assert win.current_theme == "dark"
    win._toggle_theme()
    pump(qapp)
    assert win.current_theme == "light"


# ------------------------------------------------------------
# 界面骨架
# ------------------------------------------------------------
def test_all_tabs_present(win):
    titles = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    assert titles == ["基本设置", "批量打包", "文件预览",
                      "文件统计", "高级选项", "构建历史"]


def test_building_flag_starts_false(win):
    assert win._building is False


def test_build_button_enabled_when_idle(win, qapp):
    win._set_build_buttons_state(False)
    pump(qapp)
    assert win.build_btn.isEnabled() is False
    win._set_build_buttons_state(True)
    pump(qapp)
    assert win.build_btn.isEnabled() is True


# ------------------------------------------------------------
# 线程桥：后台线程的回调必须在 UI 线程执行
# ------------------------------------------------------------
def test_call_in_ui_runs_inline_on_main_thread(win):
    seen = []
    win.call_in_ui(lambda: seen.append(threading.current_thread()))
    assert seen == [threading.main_thread()], "主线程调用应立即执行以保持即时性"


def test_call_in_ui_defers_from_worker_thread(win, qapp):
    """worker 线程投递的回调必须排到 UI 线程，不能就地执行。

    Qt 规定 QObject 只能在其所属线程访问；跨线程直接操作控件是
    未定义行为，轻则警告重则崩溃。
    """
    seen = []
    started = threading.Event()
    done = threading.Event()

    def worker():
        def probe():
            seen.append(threading.current_thread())
            done.set()

        win.call_in_ui(probe)
        started.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    started.wait(2)
    assert not done.is_set(), "worker 线程投递的回调被就地执行了"
    for _ in range(80):
        qapp.processEvents()
        time.sleep(0.005)
        if done.is_set():
            break
    t.join(2)
    assert done.is_set(), "回调没有在事件循环中被执行"
    assert seen == [threading.main_thread()]


def test_call_in_ui_survives_broken_callback(win, qapp):
    """回调抛异常不得把事件循环带崩。"""
    calls = []

    def boom():
        calls.append("boom")
        raise ValueError("故意抛出")

    win.call_in_ui(boom)
    pump(qapp)
    win.call_in_ui(lambda: calls.append("after"))
    pump(qapp)
    assert calls == ["boom", "after"], "异常回调之后的正常回调应继续执行"


def test_worker_callback_exception_does_not_kill_process(win, qapp, capsys):
    """回归测试：worker 线程回调里的普通异常不得让进程崩溃。

    PySide6 中槽函数抛未捕获异常会终止应用；旧版 _poll_ui_queue
    是全捕获。这里必须真正走 UiInvoker 这条 worker 路径。
    """
    seen = []
    done = threading.Event()

    def boom():
        seen.append("boom")
        raise ValueError("故意抛出")

    def worker():
        win.invoker.call(boom)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    for _ in range(100):
        qapp.processEvents()
        time.sleep(0.005)
        if seen:
            break
    t.join(2)
    pump(qapp)
    assert seen == ["boom"], "回调未执行"
    # 之后仍能继续投递并执行，说明事件循环没有被异常打断
    win.invoker.call(lambda: seen.append("after"))
    pump(qapp)
    assert seen == ["boom", "after"]


def test_call_in_ui_after_window_closed_is_ignored(win, qapp):
    """窗口关闭后迟到的回调必须被吞掉，而不是抛到 stderr。"""
    win.close()
    pump(qapp)
    win.call_in_ui(lambda: (_ for _ in ()).throw(RuntimeError("destroyed")))
    pump(qapp)   # 不抛异常即通过


# ------------------------------------------------------------
# 日志 / 进度 / 状态
# ------------------------------------------------------------
def test_log_pump_batches_lines(win, qapp):
    win._log("[信息] 第一行")
    win._log("[信息] 第二行")
    win.pump.flush()
    pump(qapp)
    text = win.log_text.toPlainText()
    assert "第一行" in text and "第二行" in text


def _block_text_color(win, length=3):
    """读取行内文字的颜色。

    注意：不能用 QTextBlock.charFormat()——它返回的是**块默认**格式，
    而 mergeCharFormat 是按区间施加的，只用 QTextCursor 才能读到。
    """
    cur = win.log_text.textCursor()
    cur.movePosition(QtGui.QTextCursor.MoveOperation.End)
    cur.movePosition(QtGui.QTextCursor.MoveOperation.Left,
                     QtGui.QTextCursor.MoveMode.KeepAnchor, length)
    return cur.charFormat().foreground().color()


@pytest.mark.parametrize("marker,theme_key", [
    ("[成功] 好了", "ok"),
    ("[错误] 坏了", "danger"),
    ("[警告] 注意", None),        # 警告色固定，不随主题
])
def test_log_uses_marker_colors(win, qapp, marker, theme_key):
    win._clear_log()
    win._log(marker)
    win.pump.flush()
    pump(qapp)
    assert win.log_text.toPlainText().strip() == marker
    expected = ("#f57f17" if theme_key is None
                else ui.THEMES[win.current_theme][theme_key])
    assert _block_text_color(win) == QtGui.QColor(expected)


def test_log_without_marker_stays_uncolored(win, qapp):
    """无标记行不应被着色成错误色。"""
    win._clear_log()
    win._log("普通日志行")
    win.pump.flush()
    pump(qapp)
    danger = QtGui.QColor(ui.THEMES[win.current_theme]["danger"])
    ok = QtGui.QColor(ui.THEMES[win.current_theme]["ok"])
    got = _block_text_color(win)
    assert got not in (danger, ok), "无标记行被错误着色"


def test_log_auto_expands_on_summary(win, qapp):
    """打包汇总日志（==========）到达时自动展开日志区。"""
    if win._log_expanded:
        win._toggle_log_panel()
    pump(qapp)
    assert win._log_expanded is False
    win._log("========== 批量打包开始 ==========")
    pump(qapp)
    assert win._log_expanded is True
    assert win.log_holder.isVisible() is True


def test_log_auto_expand_runs_on_ui_thread(win, qapp):
    """回归测试：自动展开必须在 UI 线程执行。

    _log 会被后台打包线程调用；若在调用者线程直接操作控件，
    状态检查类断言会假通过，所以这里用 spy 记录调用线程。
    """
    if win._log_expanded:
        win._toggle_log_panel()
    pump(qapp)
    caller_threads = []
    orig = win._toggle_log_panel

    def spy(*a, **k):
        caller_threads.append(threading.current_thread())
        return orig(*a, **k)

    win._toggle_log_panel = spy
    try:
        done = threading.Event()

        def worker():
            win._log("========== 批量打包结果 ==========")
            done.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        for _ in range(80):
            qapp.processEvents()
            time.sleep(0.005)
            if done.is_set() and caller_threads:
                break
        t.join(2)
        pump(qapp)
    finally:
        win._toggle_log_panel = orig
    assert caller_threads, "未观察到 _toggle_log_panel 调用"
    assert all(t is threading.main_thread() for t in caller_threads), \
        "日志自动展开在非 UI 线程执行了"


def test_clear_log(win, qapp):
    win._log("待清空")
    win.pump.flush()
    pump(qapp)
    win._clear_log()
    pump(qapp)
    assert win.log_text.toPlainText() == ""


def test_log_pump_flushes_short_tail_via_timer(win, qapp):
    """回归测试：不足一批的残留日志也必须最终写入。

    打包收尾的汇总（成功/失败计数、各目录输出路径、耗时）几乎必然
    落在残留里；若只靠 threshold 触发 flush，这些关键信息会永远丢失。
    """
    win._clear_log()
    win.pump.flush()
    win.log_text.clear()
    win._log("仅一行，不足阈值")
    assert win.log_text.toPlainText() == "", "未到阈值前不应立即写入"
    # 等待防抖定时器到点
    for _ in range(120):
        qapp.processEvents()
        time.sleep(0.01)
        if "不足阈值" in win.log_text.toPlainText():
            break
    assert "不足阈值" in win.log_text.toPlainText(), "残留日志未被防抖定时器刷出"


def test_toggle_log_panel_flips_visibility(win, qapp):
    win.log_holder.setVisible(False)
    win._log_expanded = False
    win._toggle_log_panel()
    pump(qapp)
    assert win._log_expanded is True
    assert win.log_toggle_btn.text() == "收起"
    win._toggle_log_panel()
    pump(qapp)
    assert win._log_expanded is False
    assert win.log_toggle_btn.text() == "展开"


def test_log_text_is_read_only(win):
    assert win.log_text.isReadOnly() is True


def test_save_log_writes_file(win, qapp, tmp_path, monkeypatch):
    win._log("保存我")
    win.pump.flush()
    pump(qapp)
    target = tmp_path / "log.txt"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))
    win._save_log()
    pump(qapp)
    assert target.exists()
    assert "保存我" in target.read_text(encoding="utf-8")


def test_save_log_appends_txt_suffix(win, qapp, tmp_path, monkeypatch):
    target = tmp_path / "log"          # 无扩展名
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))
    win._save_log()
    pump(qapp)
    assert (tmp_path / "log.txt").exists(), "未自动补 .txt 扩展名"


@pytest.mark.parametrize("value,expected", [
    (0, 0), (50, 50), (100, 100),
    (-20, 0), (180, 100),          # 越界需钳制
])
def test_set_progress_clamps(win, qapp, value, expected):
    win._set_progress(value)
    pump(qapp)
    assert win.progress.value() == expected
    assert win.pct_label.text() == "%d%%" % expected


def test_set_progress_ignores_non_numeric(win, qapp):
    """构建器回调可能传来脏值，不能让 UI 崩掉。"""
    before = win.progress.value()
    win._set_progress("不是数字")
    win._set_progress(None)
    pump(qapp)
    assert win.progress.value() == before


def test_set_status_updates_label(win, qapp):
    win._set_status("就绪完成")
    pump(qapp)
    assert win.status_label.text() == "就绪完成"


# ------------------------------------------------------------
# 工具探测
# ------------------------------------------------------------
def test_check_tools_fills_summary(win, qapp):
    win._check_tools()
    text = win.tools_summary.text()
    for name in ("Inno Setup", "7-Zip", "NSIS"):
        assert name in text
    assert "OK" in text or "缺" in text
    assert win._tools_detail, "tooltip 详情不应为空"


# ------------------------------------------------------------
# 批量列表
# ------------------------------------------------------------
def test_batch_items_empty_by_default(win):
    assert win._batch_items() == []


def test_add_and_clear_batch_items(win, qapp, monkeypatch):
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: r"C:\some\folder"))
    win._add_batch_folder()
    pump(qapp)
    assert win._batch_items() == [r"C:\some\folder"]
    win._clear_batch_folders()
    pump(qapp)
    assert win._batch_items() == []


def test_add_duplicate_batch_folder_warns(win, qapp, monkeypatch):
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: r"C:\dup"))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Ok))
    warned = []
    monkeypatch.setattr(win, "_info_box", lambda *a, **k: warned.append(a))
    win._add_batch_folder()
    win._add_batch_folder()
    pump(qapp)
    assert win._batch_items() == [r"C:\dup"], "重复项不应被再次插入"
    assert warned, "重复添加应给出提示"


def test_remove_selected_batch_item(win, qapp):
    win.batch_list.addItem(r"C:\a")
    win.batch_list.addItem(r"C:\b")
    win.batch_list.item(0).setSelected(True)
    win._remove_batch_folder()
    pump(qapp)
    assert win._batch_items() == [r"C:\b"]


def test_batch_hint_reflects_count(win, qapp):
    win._clear_batch_folders()
    pump(qapp)
    assert "批量列表为空" in win.batch_tip.text()
    win.batch_list.addItem(r"C:\x")
    win._refresh_batch_hint()
    assert "共 1 个" in win.batch_tip.text()


# ------------------------------------------------------------
# 模式与压缩级别
# ------------------------------------------------------------
def test_mode_reads_checked_radio(win):
    win.mode_buttons["nsis"].setChecked(True)
    assert win._mode() == "nsis"
    win.mode_buttons["inno"].setChecked(True)
    assert win._mode() == "inno"


def test_mode_falls_back_when_none_checked(win):
    for btn in win.mode_buttons.values():
        btn.setChecked(False)
    assert win._mode() == "inno", "无选中项时应回退到默认模式"


def test_compression_hint_updates(win, qapp):
    win.comp_combo.setCurrentText("9")
    pump(qapp)
    assert "极限" in win.comp_hint.text()
    win.comp_combo.setCurrentText("0")
    pump(qapp)
    assert "最快" in win.comp_hint.text()


# ------------------------------------------------------------
# 项目数据读写
# ------------------------------------------------------------
def test_project_data_roundtrip(win, qapp):
    win.src_edit.setText(r"C:\src")
    win.out_edit.setText(r"C:\out")
    win.appname_edit.setText("RoundTrip")
    win.appver_edit.setText("2.5.1")
    win.pub_edit.setText("Someone")
    win.mode_buttons["zip"].setChecked(True)
    win.icon_edit.setText(r"C:\a.ico")
    win.exclude_edit.setText("*.tmp")
    win.comp_combo.setCurrentText("5")
    win.license_edit.setText(r"C:\l.txt")
    win.pre_edit.setText("pre")
    win.post_edit.setText("post")
    win.batch_list.clear()
    win.batch_list.addItem(r"C:\b1")
    pump(qapp)

    data = win._project_data()
    assert data["app_name"] == "RoundTrip"
    assert data["default_mode"] == "zip"
    assert data["compression_level"] == "5"
    assert data["batch_sources"] == [r"C:\b1"]

    # 改乱后再应用回去
    win.appname_edit.setText("Clobbered")
    win.batch_list.clear()
    win._apply_project_data(data)
    pump(qapp)
    assert win.appname_edit.text() == "RoundTrip"
    assert win._mode() == "zip"
    assert win._batch_items() == [r"C:\b1"]


def test_apply_project_data_ignores_unknown_keys(win, qapp):
    """导入旧项目文件时可能带未知键，不能 KeyError。"""
    win.appname_edit.setText("Keep")
    win._apply_project_data({"完全不存在的键": 1})
    pump(qapp)
    assert win.appname_edit.text() == "Keep"


def test_apply_project_data_coerces_none(win, qapp):
    win.appname_edit.setText("X")
    win._apply_project_data({"app_name": None})
    pump(qapp)
    assert win.appname_edit.text() == ""


# ------------------------------------------------------------
# 文件树与统计
# ------------------------------------------------------------
def test_refresh_file_tree_invalid_path_clears(win, qapp):
    win.src_edit.setText("")
    win._refresh_file_tree()
    pump(qapp)
    assert win.file_tree.topLevelItemCount() == 0
    assert "0" in win.file_count_label.text()


def test_refresh_file_tree_populates(win, qapp, tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("a", encoding="utf-8")
    (src / "b.bin").write_bytes(b"bb" * 10)
    monkeypatch.setattr(pk, "scan_folder",
                        lambda s, e: ([("a.txt", 1), ("b.bin", 20)], 21))
    win.src_edit.setText(str(src))
    win._refresh_file_tree()
    for _ in range(60):
        qapp.processEvents()
        time.sleep(0.005)
        if win.file_tree.topLevelItemCount() >= 2:
            break
    assert win.file_tree.topLevelItemCount() == 2
    assert win.scanned_files == [("a.txt", 1), ("b.bin", 20)]
    assert "2" in win.file_count_label.text()


def test_file_tree_truncation_marker(win, qapp, monkeypatch):
    monkeypatch.setattr(pk, "FILE_TREE_MAX_DISPLAY", 2)
    monkeypatch.setattr(pk, "scan_folder",
                        lambda s, e: ([("f%d" % i, i) for i in range(5)], 5))
    monkeypatch.setattr(pk, "validate_path", lambda p: p)
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    win.src_edit.setText(r"C:\fake")
    win._refresh_file_tree()
    for _ in range(60):
        qapp.processEvents()
        time.sleep(0.005)
        if win.file_tree.topLevelItemCount() >= 3:
            break
    names = [win.file_tree.topLevelItem(i).text(0)
             for i in range(win.file_tree.topLevelItemCount())]
    assert len(names) == 3, "应显示 2 条 + 1 条截断提示"
    assert names[-1].startswith("…"), "末行应为截断提示"
    win.src_edit.setText("")


def test_filter_file_tree_case_sensitivity(win, qapp):
    win.scanned_files = [("Alpha.TXT", 1), ("beta.txt", 2), ("gamma.log", 3)]
    win.search_edit.setText("TXT")
    win.case_check.setChecked(True)
    _settle(qapp)
    names = [win.file_tree.topLevelItem(i).text(0)
             for i in range(win.file_tree.topLevelItemCount())]
    assert names == ["Alpha.TXT"]

    win.case_check.setChecked(False)
    _settle(qapp)
    names = [win.file_tree.topLevelItem(i).text(0)
             for i in range(win.file_tree.topLevelItemCount())]
    assert sorted(names) == ["Alpha.TXT", "beta.txt"]


def _settle(app, ms=420):
    """等待防抖定时器（搜索 300ms / 源路径 500ms）真正触发。"""
    deadline = time.time() + ms / 1000.0 + 0.3
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)


def test_filter_file_tree_empty_keyword_shows_all(win, qapp):
    win.scanned_files = [("a", 1), ("b", 2)]
    win.search_edit.setText("z")
    _settle(qapp)
    assert win.file_tree.topLevelItemCount() == 0, "无匹配时应清空"
    win.search_edit.setText("")
    _settle(qapp)
    assert win.file_tree.topLevelItemCount() == 2, "清空关键词应显示全部"


def test_update_stats_groups_by_extension(win, qapp):
    win.scanned_files = [("a.txt", 10), ("b.txt", 30), ("c.log", 60)]
    win._update_stats()
    pump(qapp)
    assert win.stats_tree.topLevelItemCount() == 2
    rows = {win.stats_tree.topLevelItem(i).text(0): (
        win.stats_tree.topLevelItem(i).text(1),
        win.stats_tree.topLevelItem(i).text(3))
        for i in range(2)}
    assert rows[".log"][0] == "1"
    assert rows[".log"][1] == "60.0%"
    assert rows[".txt"][0] == "2"
    assert "3" in win.stats_total_files.text()


def test_update_stats_empty_resets(win, qapp):
    win.scanned_files = [("a", 1)]
    win._update_stats()
    win.scanned_files = []
    win._update_stats()
    pump(qapp)
    assert win.stats_tree.topLevelItemCount() == 0
    assert "0" in win.stats_total_files.text()


def test_current_tree_path_rejects_truncation_row(win, qapp):
    win.src_edit.setText(r"C:\src")
    item = win._add_tree_row(win.file_tree, "… 还有 3 个文件未显示", "",
                             truncated=True)
    win.file_tree.setCurrentItem(item)
    assert win._current_tree_path() is None, "截断提示行不是真实文件"
    win.src_edit.setText("")


def test_real_file_starting_with_ellipsis_is_not_truncation(win, qapp):
    """U+2026 在 Windows 文件名里合法，不能靠前缀判截断。"""
    win.src_edit.setText(r"C:\src")
    item = win._add_tree_row(win.file_tree, "…真正的文件.txt", "1 B")
    win.file_tree.setCurrentItem(item)
    assert win._current_tree_path() == r"C:\src\…真正的文件.txt"
    win.src_edit.setText("")


class _FakeMenu:
    """记录 exec 调用但不真正弹出模态菜单（exec 会阻塞事件循环）。"""

    def __init__(self):
        self.executed = False

    def exec(self, *_a, **_k):
        self.executed = True
        return None


def test_file_tree_right_click_targets_clicked_row(win, qapp, monkeypatch):
    """回归测试：右键菜单必须作用于鼠标所在行。

    Qt 的 customContextMenuRequested 不会更新 currentItem，
    若用 currentItem 取值，右键第 N 行会作用到上一次选中的行。
    """
    win.src_edit.setText(r"C:\src")
    win.file_tree.clear()
    rows = [win._add_tree_row(win.file_tree, "f%d.txt" % i, "1 B")
            for i in range(5)]
    win.file_tree.resizeColumnToContents(0)
    win.file_tree.expandAll()
    win.resize(900, 700)
    qapp.processEvents()
    # 制造"当前选中项"与"右键目标"不一致的场景
    win.file_tree.setCurrentItem(rows[1])

    target_row = 3
    pos = win.file_tree.visualItemRect(rows[target_row]).center()
    seen = {}
    menu = _FakeMenu()

    def fake_build(path):
        seen["path"] = path
        return menu

    monkeypatch.setattr(win, "_build_file_tree_menu", fake_build)
    win._on_file_tree_right_click(pos)

    assert seen.get("path") == r"C:\src\f3.txt", \
        "右键菜单作用到了错误的行（实际: %r）" % seen.get("path")
    assert win.file_tree.currentItem() is rows[target_row]
    assert menu.executed is True
    win.src_edit.setText("")


def test_file_tree_right_click_ignores_blank_area(win, qapp, monkeypatch):
    win.file_tree.clear()
    win._add_tree_row(win.file_tree, "a.txt", "1 B")
    win.resize(900, 700)
    qapp.processEvents()
    monkeypatch.setattr(win, "_build_file_tree_menu",
                        lambda path: pytest.fail("空白区域不应构造菜单"))
    blank = QtCore.QPoint(2, win.file_tree.viewport().height() - 2)
    assert win._on_file_tree_right_click(blank) is None


def test_file_tree_menu_actions_carry_target_path(win, qapp):
    path = r"C:\src\sub\file.txt"
    menu = win._build_file_tree_menu(path)
    labels = [a.text() for a in menu.actions() if a.text()]
    assert labels == ["复制文件名", "复制完整路径",
                      "在资源管理器中打开", "在此文件夹中打开终端"]
    assert all(a.data() == path for a in menu.actions() if a.text()), \
        "菜单项必须携带其作用的路径"
    copied = []
    win._clip_copy = lambda t: copied.append(t)
    menu.actions()[1].trigger()      # 复制完整路径
    assert copied == [path]
    menu.actions()[0].trigger()      # 复制文件名
    assert copied[-1] == "file.txt"


def test_export_file_list_csv(win, qapp, tmp_path, monkeypatch):
    win.scanned_files = [("a.txt", 1), ("b/c.log", 2)]
    target = tmp_path / "out.csv"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))
    monkeypatch.setattr(win, "_info_box", lambda *a, **k: None)
    win._export_file_list_csv()
    pump(qapp)
    assert target.exists()
    body = target.read_text(encoding="utf-8-sig")
    assert "a.txt" in body and "b/c.log" in body


def test_export_file_list_csv_without_scan_warns(win, qapp):
    win.scanned_files = []
    warned = []
    win._info_box = lambda *a, **k: warned.append(a)
    win._export_file_list_csv()
    pump(qapp)
    assert warned, "未扫描时导出应给出提示"


# ------------------------------------------------------------
# 构建历史
# ------------------------------------------------------------
def test_add_build_history_defers_to_ui_thread(win, qapp):
    """批量打包会在 worker 线程写历史，必须排队回 UI 线程。

    cfg["build_history"] 若在 worker 线程 insert，主线程同时遍历会抛
    "list changed size during iteration"。
    """
    done = threading.Event()

    def worker():
        win._add_build_history("ZIP", "AppX", None, True)
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    for _ in range(80):
        qapp.processEvents()
        time.sleep(0.005)
        if win.cfg.get("build_history"):
            break
    t.join(2)
    pump(qapp)
    assert win.cfg.get("build_history"), "历史未写入"
    assert win.cfg["build_history"][0]["app_name"] == "AppX"


def test_add_build_history_copies_list(win, qapp):
    """不得原地改 DEFAULT_CONFIG 里的同一 list。"""
    original = list(pk.DEFAULT_CONFIG.get("build_history", []))
    win._add_build_history("Inno Setup", "AppY", r"C:\o.exe", True)
    pump(qapp)
    assert pk.DEFAULT_CONFIG.get("build_history", []) == original, \
        "模块级 DEFAULT_CONFIG 被就地修改了"
    assert len(win.cfg["build_history"]) == 1


def test_add_build_history_caps_length(win, qapp):
    for i in range(pk.BUILD_HISTORY_MAX + 5):
        win._add_build_history("ZIP", "App%d" % i, None, True)
    pump(qapp)
    assert len(win.cfg["build_history"]) == pk.BUILD_HISTORY_MAX


def test_clear_build_history(win, qapp):
    win._add_build_history("ZIP", "App", None, True)
    pump(qapp)
    win._clear_build_history()
    pump(qapp)
    assert win.cfg["build_history"] == []
    assert win.history_tree.topLevelItemCount() == 0


# ------------------------------------------------------------
# 最近项目
# ------------------------------------------------------------
def test_add_recent_project_copies_list(win, qapp):
    original = list(pk.DEFAULT_CONFIG.get("recent_projects", []))
    win._add_recent_project(r"C:\p1.packager")
    pump(qapp)
    assert pk.DEFAULT_CONFIG.get("recent_projects", []) == original
    assert win.cfg["recent_projects"][0] == r"C:\p1.packager"


def test_add_recent_project_dedups_and_caps(win, qapp):
    for i in range(pk.RECENT_PROJECTS_MAX + 3):
        win._add_recent_project(r"C:\p%d.packager" % i)
    pump(qapp)
    recent = win.cfg["recent_projects"]
    assert len(recent) == pk.RECENT_PROJECTS_MAX
    assert len(set(recent)) == len(recent), "不应出现重复项"


def test_recent_menu_shows_placeholder_when_empty(win, qapp):
    win.cfg["recent_projects"] = []
    win._refresh_recent_menu()
    pump(qapp)
    assert win.recent_menu.actions()[0].text() == "(无最近项目)"
    assert win.recent_menu.actions()[0].isEnabled() is False


# ------------------------------------------------------------
# 打包校验与重入保护
# ------------------------------------------------------------
def test_rejected_build_resets_building_flag(win, qapp, monkeypatch, tmp_path):
    """校验失败必须复位 _building，否则按钮永久禁用。"""
    win.src_edit.setText("")
    win.out_edit.setText(str(tmp_path))
    win.appname_edit.setText("")
    win._building = False
    monkeypatch.setattr(win, "_warn_box", lambda *a, **k: None)
    win._start_build()
    pump(qapp)
    assert win._building is False, "校验失败后 _building 未复位"


def test_reentrant_build_is_blocked(win, qapp, monkeypatch, tmp_path):
    """打包进行中重复触发（含 Ctrl+B）须被重入保护拦截。"""
    win._building = True
    called = []
    monkeypatch.setattr(win, "_warn_box", lambda *a, **k: called.append(a))
    win._start_build()
    pump(qapp)
    assert called == [], "重入时不应弹出校验提示"
    assert win._building is True
    win._building = False


def test_build_rejects_script_injection(win, qapp, monkeypatch, tmp_path):
    """拼进 Inno/NSIS 脚本的字段必须被校验拦下。"""
    win.src_edit.setText(str(tmp_path))
    win.out_edit.setText(str(tmp_path / "out"))
    win.appname_edit.setText('Evil";\n[Setup]\nX=1')
    monkeypatch.setattr(win, "_warn_box", lambda *a, **k: None)
    win._start_build()
    pump(qapp)
    assert win._building is False


def test_build_rejects_quote_in_path(win, qapp, monkeypatch, tmp_path):
    """双引号路径会闭合脚本字符串，必须拒绝。"""
    win.src_edit.setText(str(tmp_path) + '"')
    win.out_edit.setText(str(tmp_path))
    win.appname_edit.setText("Ok")
    errors = []
    monkeypatch.setattr(win, "_warn_box", lambda *a, **k: errors.append(a))
    win._start_build()
    pump(qapp)
    assert errors, "含引号的源路径应被拒绝"
    assert win._building is False


def test_set_build_buttons_state_tolerates(win, qapp):
    win._set_build_buttons_state(False)
    pump(qapp)
    win._set_build_buttons_state(True)
    pump(qapp)
    assert win.build_btn.isEnabled() is True


# ------------------------------------------------------------
# 对话框与辅助
# ------------------------------------------------------------
def test_warn_box_suppressed_when_closing(win, qapp, monkeypatch):
    """关窗后 worker 线程的回调不得再弹窗（会打到已销毁窗口）。"""
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.append(a)))
    win._closing = True
    win._warn_box("提示", "不该出现")
    win._info_box("提示", "不该出现")
    pump(qapp)
    assert shown == [], "关窗后仍弹出了对话框"
    win._closing = False


def test_preview_script_rejects_zip_mode(win, qapp, monkeypatch):
    """脚本预览只在 inno / nsis 下可用。"""
    win.mode_buttons["zip"].setChecked(True)
    warned = []
    monkeypatch.setattr(win, "_info_box", lambda *a, **k: warned.append(a))
    win._preview_iss_script()
    pump(qapp)
    assert warned, "zip 模式应提示不支持脚本预览"


def test_link_label_opens_url(qapp):
    label = ui.LinkLabel("https://example.com/x")
    assert label.text() == "https://example.com/x"
    assert label.cursor().shape() == QtCore.Qt.CursorShape.PointingHandCursor


def test_glass_frame_sets_styled_background(win):
    """自定义 QFrame 必须开 WA_StyledBackground，否则卡片背景不绘制。"""
    card = ui.GlassFrame(win)
    assert card.testAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground)


def test_auto_save_project_writes_file(win, qapp, tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    win.appname_edit.setText("AutoSaved")
    win._auto_save_project()
    target = tmp_path / "packager_autosave.json"
    assert target.exists(), "自动保存文件未生成"
    import json
    assert json.loads(target.read_text(encoding="utf-8"))["app_name"] == "AutoSaved"


def test_close_event_persists_config(win, qapp, monkeypatch):
    """回归测试：点标题栏 X 关闭必须落盘配置。

    这是最常见的关闭方式。旧版用 protocol("WM_DELETE_WINDOW") 把所有
    关闭路径都导向 _on_close；Qt 里点 X 直接进 closeEvent，
    若不在那里保存，窗口尺寸 / 源路径 / 批量列表全部丢失。
    """
    win.src_edit.setText(r"C:\persist-me")
    win.appname_edit.setText("PersistMe")
    win.batch_list.addItem(r"C:\b1")
    saved = {}

    def fake_save(cfg):
        saved.update(cfg)

    monkeypatch.setattr(pk, "save_config", fake_save)
    win.close()
    pump(qapp)
    assert saved, "关闭时未调用 save_config"
    assert saved["last_source"] == r"C:\persist-me"
    assert saved["app_name"] == "PersistMe"
    assert saved["batch_sources"] == [r"C:\b1"]


def test_close_event_can_be_vetoed_while_building(win, qapp, monkeypatch):
    """打包中点 X 必须先确认；取消时窗口不能关。"""
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No))
    win._building = True
    win.close()
    pump(qapp)
    assert win.isVisible() is True, "用户取消确认后窗口不应关闭"
    win._building = False


def test_close_event_stops_timers(win, qapp):
    # 防抖定时器是懒创建的，先触发一次使其存在
    win.src_edit.setText("x")
    pump(qapp)
    assert win._source_timer is not None
    win.close()
    pump(qapp)
    assert win._source_timer.isActive() is False
    assert win._autosave_timer.isActive() is False


@pytest.mark.parametrize("bad_cfg", [
    {"default_mode": "rar"},          # 未知模式
    {"app_name": 12345},              # 非字符串
    {"app_version": None},
    {"publisher": 3.5},
    {"exclude_patterns": ["a", "b"]},  # list 而非 str
    {"icon_path": 7},
])
def test_corrupt_config_still_builds_window(qapp, monkeypatch, bad_cfg):
    """脏配置必须被逐项收敛，不能抛异常让窗口创建失败。"""
    import json
    import tempfile
    home = tempfile.mkdtemp()
    cfg_path = os.path.join(home, "c.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(bad_cfg, fh)
    monkeypatch.setattr(pk, "CONFIG_FILE", cfg_path)
    window = ui.PackagerWindow(pk)
    try:
        window.show()
        qapp.processEvents()
        assert window.isVisible() is True
        assert window._mode() in ("inno", "nsis", "7zip", "zip")
        # 文本框必须是 str，不能是 int/list
        for wdg in (window.appname_edit, window.appver_edit,
                    window.pub_edit, window.exclude_edit, window.icon_edit):
            assert isinstance(wdg.text(), str)
    finally:
        window._building = False
        window._closing = True
        window.close()
        window.deleteLater()
        qapp.processEvents()


def test_debounce_timers_created_lazily(win, qapp):
    """源路径/搜索防抖定时器只创建一次，避免每次输入都新建。"""
    win.src_edit.setText("a")
    win.src_edit.setText("ab")
    pump(qapp)
    first = win._source_timer
    win.src_edit.setText("abc")
    pump(qapp)
    assert win._source_timer is first, "防抖定时器被重复创建"
    assert first.isSingleShot() is True

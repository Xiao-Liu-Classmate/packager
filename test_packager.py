# -*- coding: utf-8 -*-
"""packager.py 纯函数单元测试（无需 GUI）

运行: python -m pytest test_packager.py -v
"""
import os
import sys
import subprocess
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import packager as pk


# ------------------------------------------------------------
# _normalize_version
# ------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    ("1.2.3", "1.2.3.0"),
    ("v1.2", "1.2.0.0"),
    ("2.0", "2.0.0.0"),
    ("1.2.3.4.5", "1.2.3.4"),
    ("", "0.0.0.0"),
    (None, "0.0.0.0"),
    ("abc", "0.0.0.0"),
    ("1.2.3.99999", "1.2.3.65535"),          # 超上限需钳制
    ("20260925.1.1.1", "65535.1.1.1"),        # 段内超上限需钳制
    ("１２３.1", "0.1.0.0"),                  # 全角数字必须被拒绝（不可混入脚本）
    ("1..2", "1.0.2.0"),
])
def test_normalize_version(value, expected):
    assert pk._normalize_version(value) == expected


def test_normalize_version_always_four_parts():
    result = pk._normalize_version("9")
    assert len(result.split(".")) == 4


def test_normalize_version_is_all_ascii_digits():
    result = pk._normalize_version("一.二.三")
    for seg in result.split("."):
        assert seg.isdigit()
        assert all(ch in "0123456789" for ch in seg)


# ------------------------------------------------------------
# validate_path / _DANGEROUS_CHARS
# ------------------------------------------------------------
def test_validate_path_allows_ampersand():
    """& 在 Windows 合法目录名中，不应被误拒"""
    assert pk.validate_path(r"C:\A & B\app") == r"C:\A & B\app"


def test_validate_path_allows_semicolon_and_backtick():
    assert pk.validate_path(r"C:\dir;name") == r"C:\dir;name"
    assert pk.validate_path(r"C:\dir`name") == r"C:\dir`name"


def test_validate_path_rejects_quote():
    with pytest.raises(ValueError):
        pk.validate_path('C:\\dir"evil')


def test_validate_path_rejects_control_char():
    with pytest.raises(ValueError):
        pk.validate_path("C:\\dir\nname")


def test_validate_path_empty_passthrough():
    assert pk.validate_path("") == ""


# ------------------------------------------------------------
# validate_script_field
# ------------------------------------------------------------
@pytest.mark.parametrize("value", [
    'x" \nDelete "C:\\Windows',
    "has $PROGRAMFILES",
    "line1\nline2",
    "ctrl\x01char",
    "{app}",          # Inno 常量语法会被解析
    "}",
])
def test_validate_script_field_rejects(value):
    ok, msg = pk.validate_script_field(value, "test")
    assert ok is False
    assert msg


@pytest.mark.parametrize("value", ["MyApp", "应用 1.0", "Foo-Bar_2", "", None,
                                   "A & B", "O'Brien App", "path\\with\\slash"])
def test_validate_script_field_accepts(value):
    ok, msg = pk.validate_script_field(value, "test")
    assert ok is True
    assert msg == ""


# ------------------------------------------------------------
# generate_inno_script - Excludes
# ------------------------------------------------------------
def _base_cfg(**kw):
    cfg = {
        "last_source": r"D:\src",
        "last_output": r"D:\out",
        "app_name": "Demo",
        "app_version": "1.2.3",
        "publisher": "Pub",
        "icon_path": "",
        "exclude_patterns": "",
        "compression_level": "9",
        "license_file": "",
        "pre_install_cmd": "",
        "post_install_cmd": "",
    }
    cfg.update(kw)
    return cfg


def test_inno_script_uses_excludes_param():
    """Inno [Files] 没有 Ignore: 参数，排除必须走 Excludes:"""
    script = pk.generate_inno_script(_base_cfg(
        exclude_patterns="*.log,__pycache__,.git"))
    assert "Ignore:" not in script
    assert 'Excludes: "*.log,__pycache__,.git"' in script


def test_inno_script_source_line_has_required_fields():
    script = pk.generate_inno_script(_base_cfg(exclude_patterns=""))
    files_lines = [l for l in script.splitlines() if l.startswith("Source:")]
    assert files_lines, "缺少 Source 行"
    line = files_lines[0]
    assert "DestDir:" in line
    assert "Flags:" in line


def test_inno_script_no_excludes_when_empty():
    script = pk.generate_inno_script(_base_cfg(exclude_patterns=""))
    assert "Excludes:" not in script


# ------------------------------------------------------------
# generate_nsis_script
# ------------------------------------------------------------
def test_nsis_script_exclude_via_file_x():
    """排除必须在打包阶段生效，被排除内容不能进包"""
    script = pk.generate_nsis_script(_base_cfg(
        exclude_patterns="*.log,__pycache__,.git"))
    file_lines = [l for l in script.splitlines() if l.strip().startswith("File /r")]
    assert file_lines
    line = file_lines[0]
    assert '/x "*.log"' in line
    assert '/x "__pycache__"' in line
    assert '/x ".git"' in line
    assert line.rstrip().endswith(r'"D:\src\*.*"')
    # 不应再生成无效的安装后删除指令
    assert 'Delete "$INSTDIR\\*.log"' not in script
    assert 'RMDir /r "$INSTDIR\\__pycache__"' not in script


def test_nsis_script_requires_admin():
    script = pk.generate_nsis_script(_base_cfg())
    assert "RequestExecutionLevel admin" in script


def test_nsis_script_uninstaller_name_consistent():
    """WriteUninstaller 与卸载段/注册表 UninstallString 必须一致"""
    script = pk.generate_nsis_script(_base_cfg())
    assert "uninstaller.exe" not in script, "残留了错误的 uninstaller.exe"
    assert script.count(r'WriteUninstaller "$INSTDIR\uninstall.exe"') == 1
    assert r'UninstallString" "$INSTDIR\uninstall.exe"' in script


def test_nsis_script_uninstall_switches_cwd():
    """卸载器 CWD 即 $INSTDIR，须先 SetOutPath 切走"""
    script = pk.generate_nsis_script(_base_cfg())
    unsec = script.split('Section "Uninstall"')[1]
    assert 'SetOutPath "$TEMP"' in unsec
    assert 'RMDir /r "$INSTDIR"' in unsec
    assert unsec.index('SetOutPath "$TEMP"') < unsec.index('RMDir /r "$INSTDIR"')


def test_nsis_script_viproductversion_format():
    script = pk.generate_nsis_script(_base_cfg(app_version="2.5"))
    assert 'VIProductVersion "2.5.0.0"' in script


def test_nsis_script_viproductversion_clamped():
    script = pk.generate_nsis_script(_base_cfg(app_version="1.2.3.99999999"))
    assert 'VIProductVersion "1.2.3.65535"' in script


def test_nsis_script_unicode_enabled():
    script = pk.generate_nsis_script(_base_cfg())
    assert "Unicode True" in script


def test_nsis_script_escapes_dollar_in_path():
    """路径中的 $ 会被 NSIS 当变量展开，必须双写"""
    script = pk.generate_nsis_script(_base_cfg(
        last_output=r"D:\out$dir", last_source=r"D:\sr$c"))
    assert r'D:\out$$dir\Demo_setup.exe' in script
    assert r'File /r "D:\sr$$c\*.*"' in script


def test_nsis_script_name_ampersand_escaped():
    script = pk.generate_nsis_script(_base_cfg(app_name="Foo & Bar"))
    assert 'Name "Foo & Bar" "Foo && Bar"' in script


def test_nsis_script_name_without_ampersand_single_param():
    script = pk.generate_nsis_script(_base_cfg(app_name="Plain"))
    assert 'Name "Plain"\n' in script
    assert 'Name "Plain" "' not in script


def test_nsis_script_displayicon_present():
    script = pk.generate_nsis_script(_base_cfg())
    assert '"DisplayIcon" "$INSTDIR\\Demo.exe"' in script


# ------------------------------------------------------------
# CLI 参数覆盖优先级
# ------------------------------------------------------------
def _run_cli(args, cwd=None, tmp_home=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 隔离真实用户配置，避免本机 ~/.packager_config.json 影响断言
    if tmp_home is not None:
        env["USERPROFILE"] = str(tmp_home)
        env["HOME"] = str(tmp_home)
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.argv=['packager.py'] + %r; "
         "import packager; packager.cli_main()" % (list(args),)],
        capture_output=True, encoding="utf-8", errors="replace",
        env=env, cwd=cwd or os.path.dirname(os.path.abspath(__file__)),
    )


def test_cli_explicit_name_wins_over_project(tmp_path):
    """显式传 --name 应覆盖项目里的值（即使值恰为默认 MyApp）"""
    proj = tmp_path / "p.packager"
    proj.write_text('{"app_name": "FromProject", "app_version": "9.9.9", '
                    '"default_mode": "zip", "compression_level": "5"}',
                    encoding="utf-8")
    r = _run_cli(["--project", str(proj), "--name", "MyApp", "--dry-run", "-m", "inno"],
                 tmp_home=tmp_path / "home")
    # --name MyApp 显式指定，不应被项目值覆盖（脚本里体现 AppName）
    assert r.returncode == 0, r.stderr
    assert "AppName=MyApp" in r.stdout


def test_cli_project_values_used_when_flag_absent(tmp_path):
    proj = tmp_path / "p.packager"
    proj.write_text('{"app_name": "FromProject", "app_version": "9.9.9", '
                    '"last_source": "D:\\\\src", "last_output": "D:\\\\out"}',
                    encoding="utf-8")
    r = _run_cli(["--project", str(proj), "--dry-run", "-m", "inno"],
                 tmp_home=tmp_path / "home")
    assert r.returncode == 0, r.stderr
    assert "AppName=FromProject" in r.stdout
    assert "AppVersion=9.9.9" in r.stdout


def test_cli_missing_project_fails(tmp_path):
    r = _run_cli(["--project", str(tmp_path / "nope.packager"), "--dry-run"])
    assert r.returncode == 1


def test_cli_list_modes():
    r = _run_cli(["--list-modes"])
    assert r.returncode == 0
    for m in ("inno", "7zip", "zip", "nsis"):
        assert m in r.stdout


def test_cli_script_field_validation_rejects_injection():
    r = _run_cli(["--source", os.path.dirname(os.path.abspath(__file__)),
                  "--name", 'bad"name', "--dry-run", "-m", "inno"])
    assert r.returncode == 1
    assert "非法字符" in (r.stdout + r.stderr)


# ------------------------------------------------------------
# 打包模式的排除规则一致性
# ------------------------------------------------------------
def _mk_src(tmp_path):
    src = tmp_path / "src"
    (src / "__pycache__").mkdir(parents=True)
    (src / "sub").mkdir()
    (src / "hello.txt").write_text("hi", encoding="utf-8")
    (src / "app.log").write_text("log", encoding="utf-8")
    (src / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")
    (src / "sub" / "deep.log").write_text("deep", encoding="utf-8")
    return src


def test_zip_build_honors_exclude(tmp_path):
    """ZIP 模式必须与 Inno/NSIS 一样应用排除规则"""
    import zipfile
    src = _mk_src(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    cfg = _base_cfg(last_source=str(src), last_output=str(out),
                    exclude_patterns="__pycache__,*.log")
    logs = []
    ok, path = pk.build_with_zip(cfg, logs.append, lambda v: None)
    assert ok, logs
    names = set(zipfile.ZipFile(path).namelist())
    assert "src/hello.txt" in names
    assert "src/app.log" not in names
    assert "src/sub/deep.log" not in names
    assert not any("__pycache__" in n for n in names)


def test_scan_folder_honors_exclude(tmp_path):
    src = _mk_src(tmp_path)
    files, _size = pk.scan_folder(str(src), "__pycache__,*.log")
    rels = {f for f, _ in files}
    assert "hello.txt" in rels
    assert "app.log" not in rels
    assert not any("__pycache__" in r for r in rels)
    assert "sub" + os.sep + "deep.log" not in rels


def test_inno_nsis_exclude_semantics_match(tmp_path):
    """Inno Excludes 与 NSIS /x 应使用同一份排除规则"""
    excl = "__pycache__,*.log,.git"
    inno = pk.generate_inno_script(_base_cfg(exclude_patterns=excl))
    nsis = pk.generate_nsis_script(_base_cfg(exclude_patterns=excl))
    assert 'Excludes: "%s"' % excl in inno
    for frag in excl.split(","):
        assert '/x "%s"' % frag in nsis


# ------------------------------------------------------------
# save_config 原子写
# ------------------------------------------------------------
def test_save_config_atomic_and_valid(tmp_path, monkeypatch):
    target = tmp_path / "cfg.json"
    monkeypatch.setattr(pk, "CONFIG_FILE", str(target))
    pk.save_config({"a": 1, "b": "中文"})
    assert target.exists()
    assert not (tmp_path / "cfg.json.tmp").exists(), "临时文件未清理"
    data = pk.load_config()
    assert data["a"] == 1
    assert data["b"] == "中文"


def test_load_config_corrupt_backs_up(tmp_path, monkeypatch, capsys):
    target = tmp_path / "cfg.json"
    target.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(pk, "CONFIG_FILE", str(target))
    data = pk.load_config()
    assert data["app_name"] == pk.DEFAULT_CONFIG["app_name"]
    bak = tmp_path / "cfg.json.bak"
    assert bak.exists(), "损坏配置未备份"
    assert bak.read_text(encoding="utf-8") == "{ this is not json"


def test_validate_path_allows_apostrophe():
    """单引号在 Windows 合法路径中存在，不应被误拒"""
    assert pk.validate_path(r"C:\Users\O'Brien\proj") == r"C:\Users\O'Brien\proj"
    assert pk.validate_path(r"C:\O'Brien A & B") == r"C:\O'Brien A & B"


def test_validate_path_still_rejects_quote_and_control():
    with pytest.raises(ValueError):
        pk.validate_path('C:\\bad"name')
    with pytest.raises(ValueError):
        pk.validate_path("C:\\bad\x01name")


# ------------------------------------------------------------
# build_with_zip 失败路径与产物完整性
# ------------------------------------------------------------
def test_zip_build_source_missing(tmp_path):
    cfg = _base_cfg(last_source=str(tmp_path / "nope"),
                    last_output=str(tmp_path))
    logs = []
    ok, path = pk.build_with_zip(cfg, logs.append, lambda v: None)
    assert ok is False and path is None
    assert any("不存在" in l for l in logs)


def test_zip_build_all_excluded_is_failure(tmp_path):
    """全部文件被排除 = 失败，且不能留下 .part 残留"""
    src = _mk_src(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    cfg = _base_cfg(last_source=str(src), last_output=str(out),
                    exclude_patterns="*.txt,*.log,__pycache__")
    logs = []
    ok, path = pk.build_with_zip(cfg, logs.append, lambda v: None)
    assert ok is False and path is None
    assert not list(out.glob("*.part")), ".part 残留未清理"
    assert not list(out.glob("*.zip"))


def test_zip_build_atomic_no_part_left(tmp_path):
    """成功后不得残留 .part"""
    src = _mk_src(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    cfg = _base_cfg(last_source=str(src), last_output=str(out))
    ok, path = pk.build_with_zip(cfg, lambda m: None, lambda v: None)
    assert ok
    assert not list(out.glob("*.part")), "成功后 .part 未清理"
    assert os.path.isfile(path)


def test_zip_build_failure_removes_part(tmp_path, monkeypatch):
    """写入中途异常时须清理 .part，不得截断旧产物"""
    import zipfile as zf_mod
    src = _mk_src(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    good = out / "Demo_portable.zip"
    good.write_bytes(b"old-good-product")

    cfg = _base_cfg(last_source=str(src), last_output=str(out))

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(zf_mod, "ZipFile", boom)
    logs = []
    ok, path = pk.build_with_zip(cfg, logs.append, lambda v: None)
    assert ok is False and path is None
    assert not list(out.glob("*.part")), ".part 未清理"
    # 旧产物未被截断
    assert good.read_bytes() == b"old-good-product"


# ------------------------------------------------------------
# 7zip 排除参数（任意深度）
# ------------------------------------------------------------
def test_7zip_exclude_args_cover_nested_dirs():
    """7z 裸名只匹配顶层，须额外传 *<pattern> 覆盖嵌套层"""
    args = pk.build_7zip_exclude_args("__pycache__,*.log,.git")
    assert "-xr!__pycache__" in args
    assert "-xr!*__pycache__" in args, "裸名缺 * 前缀无法排除嵌套目录"
    assert "-xr!*.log" in args
    assert "-xr!**.log" not in args, "已含通配符的规则不应重复加 *"
    assert "-xr!.git" in args and "-xr!*.git" in args
    # 所有参数都须在归档名之前，且带 -xr! 前缀
    assert args and all(a.startswith("-xr!") for a in args)


def test_7zip_exclude_args_empty_and_blank():
    assert pk.build_7zip_exclude_args("") == []
    assert pk.build_7zip_exclude_args("  , ,") == []
    assert pk.build_7zip_exclude_args(" a/b ") == ["-xr!a/b", "-xr!*a/b"]


# ------------------------------------------------------------
# GUI 重入与按钮状态回归（上一轮 HIGH-1）
# ------------------------------------------------------------
@pytest.fixture(scope="module")
def app(request):
    """实例化 GUI；无显示环境时跳过"""
    try:
        root = pk.Tk()
    except Exception as exc:  # pragma: no cover
        pytest.skip("无法创建 Tk 根窗口: %s" % exc)
    root.withdraw()
    instance = pk.PackagerApp(root)
    yield instance, root
    try:
        root.destroy()
    except Exception:
        pass


def test_building_flag_starts_false(app):
    instance, _root = app
    assert instance._building is False


def test_rejected_build_resets_flag(app, monkeypatch):
    """校验失败必须复位 _building，否则按钮永久禁用"""
    instance, root = app
    # 避免真实弹窗阻塞
    monkeypatch.setattr(pk.messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(pk.messagebox, "showwarning", lambda *a, **k: None)
    instance.source_var.set("")
    instance.output_var.set(str(__import__("tempfile").gettempdir()))
    instance.appname_var.set("")
    # 批量列表清空，强制走单构建校验失败路径
    instance.batch_listbox.delete(0, pk.END)
    instance._building = False
    instance._start_build()
    root.update()
    assert instance._building is False, "校验失败后 _building 未复位"


def test_reentrant_build_is_blocked(app, monkeypatch):
    """打包进行中重复触发（Ctrl+B / 按钮）须被重入保护拦截"""
    instance, root = app
    messagebox_calls = []
    monkeypatch.setattr(pk.messagebox, "showerror",
                        lambda *a, **k: messagebox_calls.append(a))
    monkeypatch.setattr(pk.messagebox, "showwarning",
                        lambda *a, **k: messagebox_calls.append(a))
    instance._building = True
    instance._start_build()   # 应在开头直接 return，不校验也不弹窗
    root.update()
    assert instance._building is True, "重入保护未生效"
    assert messagebox_calls == [], "打包中不应弹出任何对话框"
    instance._building = False


def test_build_buttons_restored(app):
    instance, _root = app
    instance._set_build_buttons_state(pk.DISABLED)
    instance._set_build_buttons_state(pk.NORMAL)
    # tkinter cget 返回 Tcl String，须转 str 再比较
    assert str(instance.build_button.cget("state")) == pk.NORMAL
    instance._set_build_buttons_state(pk.DISABLED)
    assert str(instance.build_button.cget("state")) == pk.DISABLED
    instance._set_build_buttons_state(pk.NORMAL)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

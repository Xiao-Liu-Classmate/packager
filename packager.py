import os
import re
import sys
import json
import shutil
import subprocess
import threading
import argparse
import tempfile
import zipfile
import time
import queue
import traceback
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import (Tk, Toplevel, Frame, Label, Button, Entry, StringVar,
                     IntVar, Text, filedialog, messagebox, N, S, E, W, LEFT,
                     RIGHT, TOP, BOTTOM, X, Y, BOTH, HORIZONTAL, DISABLED,
                     NORMAL, END, WORD, Checkbutton, OptionMenu, Radiobutton,
                     Listbox)
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from fnmatch import fnmatch

APP_VERSION = "4.3.0"
APP_NAME = "文件夹打包工具"
RECENT_PROJECTS_MAX = 5
BUILD_HISTORY_MAX = 20
FILE_TREE_MAX_DISPLAY = 5000
# 打包模式显示名（单处定义，避免各处重复导致漏改）
MODE_NAMES = {"inno": "Inno Setup", "7zip": "7-Zip SFX",
              "zip": "ZIP", "nsis": "NSIS"}
CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".packager_config.json")
PROJECT_EXTENSIONS = [("Packager 项目文件", "*.packager"), ("所有文件", "*.*")]
# 项目镜像地址（"关于"对话框与文档共用，避免出现占位链接）
REPO_URLS = (
    ("GitHub", "https://github.com/Xiao-Liu-Classmate/packager"),
    ("Gitee", "https://gitee.com/xiao-xiao-liuA/packager"),
)

DEFAULT_CONFIG = {
    "last_source": "",
    "last_output": "",
    "app_name": "MyApp",
    "app_version": APP_VERSION,
    "publisher": "",
    "default_mode": "inno",
    "icon_path": "",
    "exclude_patterns": "*.log,*.tmp,*.bak,Thumbs.db,.DS_Store,__pycache__,*.pyc,.git,.svn",
    "window_width": 1180,
    "window_height": 900,
    "last_icon_dir": "",
    "theme": "light",
    "recent_projects": [],
    "build_history": [],
    "last_project_file": "",
    "compression_level": "9",
    "license_file": "",
    "pre_install_cmd": "",
    "post_install_cmd": "",
    "batch_sources": [],
}

# ============================================================
# A0. Path & Input Validation
# ============================================================
# 只拒绝双引号与 NTFS 禁止的控制字符（0x00-0x1F）。
# & ; ` | ' 等在 Windows 合法目录名中会出现（如 O'Brien），不应误拒；
# 且 subprocess 全部使用列表形式调用，不存在 shell 注入面；
# 单引号对 Inno/NSIS 的双引号字符串亦无特殊含义。
_DANGEROUS_CHARS = set('"') | {chr(c) for c in range(0x20)}


def validate_path(path):
    """验证路径安全性，防止路径遍历和注入攻击"""
    if not path:
        return path
    normalized = os.path.normpath(path)
    for char in _DANGEROUS_CHARS:
        if char in normalized:
            raise ValueError("路径包含非法字符: %s" % repr(char))
    return normalized


def validate_app_name(name):
    """验证应用名称是否合法"""
    if not name or not name.strip():
        return False, "应用名称不能为空"
    illegal = ['\\', '/', ':', '*', '?', '"', '<', '>', '|', '\n', '\r']
    for ch in illegal:
        if ch in name:
            return False, "应用名称不能包含字符: %s" % ch
    if len(name) > 255:
        return False, "应用名称过长（最多255个字符）"
    return True, ""


def validate_script_field(value, field_name):
    """校验会拼进安装脚本字符串的字段，防止截断/注入/展开

    这些值会进入 Inno/NSIS 脚本的引号字符串，含引号、$、{}、换行
    会截断字符串或触发变量展开/常量解析。
    返回 (是否合法, 错误信息)。
    """
    if value is None:
        return True, ""
    s = str(value)
    # " 会截断引号字符串；$ 是 NSIS 变量前缀；{} 是 Inno 常量语法
    for ch in ('"', "$", "{", "}", "\n", "\r"):
        if ch in s:
            return False, "%s 包含非法字符: %s" % (field_name, repr(ch))
    for ch in s:
        if ord(ch) < 0x20:
            return False, "%s 包含控制字符: %s" % (field_name, repr(ch))
    return True, ""


def copy_file_chunked(src, dst, chunk_size=65536):
    """分块复制文件，避免大文件内存占用过高"""
    with open(src, "rb") as f_in:
        with open(dst, "wb") as f_out:
            while True:
                chunk = f_in.read(chunk_size)
                if not chunk:
                    break
                f_out.write(chunk)


# ============================================================
# A. Configuration Management
# ============================================================
# 配置写入锁：主线程与后台构建线程可能同时写 CONFIG_FILE
_config_lock = threading.Lock()


def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                cfg = DEFAULT_CONFIG.copy()
                cfg.update(data)
                return cfg
    except json.JSONDecodeError:
        # 配置损坏：保留现场，避免用户"设置莫名丢失"却无从排查。
        # .bak 已存在时用微秒时间戳命名，既不覆盖旧备份，坏文件也不会滞留。
        try:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
            bak = CONFIG_FILE + ".bak"
            if os.path.exists(bak):
                bak = "%s.%s.bak" % (CONFIG_FILE, stamp)
            shutil.move(CONFIG_FILE, bak)
            print("[警告] 配置文件已损坏，已备份为: %s" % bak, file=sys.stderr)
        except Exception as e:
            print("[警告] 配置损坏且备份失败: %s" % e, file=sys.stderr)
    except PermissionError:
        pass
    except Exception:
        pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    # 加锁 + 临时文件原子替换，防止并发写导致 JSON 截断损坏
    with _config_lock:
        tmp = CONFIG_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)
        except PermissionError:
            print("[警告] 配置写入失败（权限不足），本次设置未保存: %s"
                  % CONFIG_FILE, file=sys.stderr)
            _cleanup_tmp(tmp)
        except OSError as e:
            print("[警告] 配置写入失败（磁盘错误）: %s" % e, file=sys.stderr)
            _cleanup_tmp(tmp)
        except Exception as e:
            print("[警告] 配置写入失败: %s: %s"
                  % (type(e).__name__, e), file=sys.stderr)
            _cleanup_tmp(tmp)


def _cleanup_tmp(tmp):
    """清理失败残留的临时文件"""
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass


def load_project(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None
    except PermissionError:
        return None
    except Exception:
        return None


def save_project(filepath, data):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except PermissionError:
        return False
    except OSError:
        return False
    except Exception:
        return False


# ============================================================
# B. Tool Detection
# ============================================================
def find_inno_setup():
    candidates = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
        r"C:\Program Files\Inno Setup 5\ISCC.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    path_env = os.environ.get("PATH", "")
    for p in path_env.split(os.pathsep):
        exe = os.path.join(p, "ISCC.exe")
        if os.path.isfile(exe):
            return exe
    return None


def find_7zip():
    candidates = [
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    path_env = os.environ.get("PATH", "")
    for p in path_env.split(os.pathsep):
        exe = os.path.join(p, "7z.exe")
        if os.path.isfile(exe):
            return exe
    return None


def find_nsis():
    """检测 NSIS 安装路径"""
    candidates = [
        r"C:\Program Files (x86)\NSIS\makensis.exe",
        r"C:\Program Files\NSIS\makensis.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    path_env = os.environ.get("PATH", "")
    for p in path_env.split(os.pathsep):
        exe = os.path.join(p, "makensis.exe")
        if os.path.isfile(exe):
            return exe
    return None


# ============================================================
# C. File Scanning
# ============================================================
def format_size(size_bytes):
    if size_bytes < 1024:
        return "%d B" % size_bytes
    elif size_bytes < 1024 * 1024:
        return "%.1f KB" % (size_bytes / 1024)
    elif size_bytes < 1024 * 1024 * 1024:
        return "%.1f MB" % (size_bytes / (1024 * 1024))
    else:
        return "%.2f GB" % (size_bytes / (1024 * 1024 * 1024))


def scan_folder(source_dir, exclude_patterns):
    """扫描文件夹，返回 (文件列表, 总大小)。支持通配符排除规则。"""
    patterns = [p.strip() for p in exclude_patterns.split(",") if p.strip()]
    files = []
    total_size = 0
    if not os.path.isdir(source_dir):
        return files, total_size
    for root, dirs, filenames in os.walk(source_dir):
        excluded_dirs = []
        for d in dirs:
            skip = False
            for pat in patterns:
                if fnmatch(d, pat):
                    skip = True
                    break
            if skip:
                excluded_dirs.append(d)
        for d in excluded_dirs:
            dirs.remove(d)
        for fname in filenames:
            skip = False
            for pat in patterns:
                if fnmatch(fname, pat):
                    skip = True
                    break
            if skip:
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, source_dir)
            # 路径遍历保护：跳过包含..的相对路径
            if ".." in rel.split(os.sep):
                continue
            try:
                sz = os.path.getsize(full)
            except OSError:
                sz = 0
            files.append((rel, sz))
            total_size += sz
    files.sort(key=lambda x: x[0])
    return files, total_size


# ============================================================
# D. Build Functions
# ============================================================
def generate_inno_script(cfg):
    source_esc = cfg["last_source"].replace("\\", "\\\\")
    output_esc = cfg["last_output"].replace("\\", "\\\\")
    app_name = cfg["app_name"]
    app_version = cfg["app_version"]
    publisher = cfg["publisher"]
    icon_path = cfg.get("icon_path", "")
    exclude_str = cfg["exclude_patterns"]
    compression_level = cfg.get("compression_level", "9")
    license_file = cfg.get("license_file", "")
    pre_install_cmd = cfg.get("pre_install_cmd", "")
    post_install_cmd = cfg.get("post_install_cmd", "")

    excludes = [p.strip() for p in exclude_str.split(",") if p.strip()]

    icon_line = ""
    if icon_path and os.path.isfile(icon_path):
        icon_line = "SetupIconFile=%s" % icon_path.replace("\\", "\\\\")

    license_line = ""
    if license_file and os.path.isfile(license_file):
        license_line = "LicenseFile=%s" % license_file.replace("\\", "\\\\")

    L = []
    L.append("[Setup]")
    L.append("AppName=%s" % app_name)
    L.append("AppVersion=%s" % app_version)
    L.append("AppPublisher=%s" % publisher)
    L.append("DefaultDirName={autopf}\\%s" % app_name)
    L.append("DefaultGroupName=%s" % app_name)
    L.append("OutputDir=%s" % output_esc)
    L.append("OutputBaseFilename=%s_setup" % app_name)
    L.append("Compression=lzma2")
    L.append("SolidCompression=yes")
    L.append("CompressionLevel=%s" % compression_level)
    L.append("PrivilegesRequired=lowest")
    L.append("PrivilegesRequiredOverrideAllowed=tellUser")
    L.append("UninstallDisplayIcon={app}\\%s.exe" % app_name)
    if icon_line:
        L.append(icon_line)
    if license_line:
        L.append(license_line)
    L.append("ArchitecturesAllowed=x64compatible")
    L.append("ArchitecturesInstallIn64BitMode=x64compatible")
    L.append("")
    L.append("[Languages]")
    L.append('Name: "chinesesimplified"; MessagesFile: "compiler:Languages\\ChineseSimplified.isl"')
    L.append('Name: "english"; MessagesFile: "compiler:Default.isl"')
    L.append("")
    L.append("[Files]")
    files_line = 'Source: "%s\\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs' % source_esc
    if excludes:
        # Inno [Files] 用 Excludes: 参数递归排除（不存在 Ignore: 参数）
        files_line += '; Excludes: "%s"' % ",".join(excludes)
    L.append(files_line)
    L.append("")
    L.append("[Icons]")
    L.append('Name: "{group}\\%s"; Filename: "{app}\\%s.exe"' % (app_name, app_name))
    L.append('Name: "{autodesktop}\\%s"; Filename: "{app}\\%s.exe"; Tasks: desktopicon' % (app_name, app_name))
    L.append("")
    L.append("[Tasks]")
    L.append('Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"')
    L.append("")
    L.append("[Run]")
    if pre_install_cmd:
        L.append('Filename: "%s"; Flags: runhidden' % pre_install_cmd.replace("\\", "\\\\"))
    L.append('Filename: "{app}\\%s.exe"; Description: "{cm:LaunchProgram,%s}"; Flags: nowait postinstall skipifsilent' % (app_name, app_name))
    if post_install_cmd:
        L.append('Filename: "%s"; Flags: runhidden postinstall' % post_install_cmd.replace("\\", "\\\\"))
    return "\n".join(L)


def build_with_inno(cfg, log_callback, progress_callback):
    """使用 Inno Setup 编译安装程序。返回 (成功标志, 输出文件路径)"""
    log_callback("========== Inno Setup 打包开始 ==========")
    iscc = find_inno_setup()
    if not iscc:
        log_callback("[错误] 未找到 Inno Setup (ISCC.exe)")
        log_callback("请安装 Inno Setup 6: https://jrsoftware.org/isinfo.php")
        return False, None
    log_callback("ISCC.exe: %s" % iscc)
    progress_callback(10)
    script_content = generate_inno_script(cfg)
    tmp_iss = None
    try:
        tmp_fd, tmp_iss = tempfile.mkstemp(suffix=".iss", prefix="packager_build_")
        os.close(tmp_fd)
        # UTF-8 BOM：ISCC 见 BOM 即按 UTF-8 解析，非中文系统也能正确编译
        with open(tmp_iss, "w", encoding="utf-8-sig") as f:
            f.write(script_content)
        log_callback("已生成脚本: %s" % tmp_iss)
        progress_callback(20)
        log_callback("开始编译...")
        progress_callback(30)
        result = subprocess.run(
            [iscc, tmp_iss],
            capture_output=True, text=True, timeout=600,
            encoding="gbk", errors="replace"
        )
        progress_callback(80)
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    log_callback("  %s" % line.strip())
        if result.returncode == 0:
            progress_callback(100)
            out_file = os.path.join(cfg["last_output"], "%s_setup.exe" % cfg["app_name"])
            log_callback("[成功] Inno Setup 安装包编译完成!")
            return True, out_file if os.path.isfile(out_file) else None
        else:
            log_callback("[错误] ISCC 返回码: %s" % result.returncode)
            if result.stderr:
                log_callback("错误详情: %s" % result.stderr.strip())
            return False, None
    except subprocess.TimeoutExpired:
        log_callback("[错误] 编译超时(600秒)")
        log_callback("提示: 请检查源文件夹大小或尝试减小编译选项")
        return False, None
    except PermissionError:
        log_callback("[错误] 权限不足，无法创建临时文件或编译")
        return False, None
    except Exception as e:
        log_callback("[错误] 编译失败: %s" % type(e).__name__)
        log_callback("详细信息: %s" % str(e))
        return False, None
    finally:
        if tmp_iss and os.path.exists(tmp_iss):
            try:
                os.unlink(tmp_iss)
            except OSError:
                pass


def build_7zip_exclude_args(exclude_str):
    """把逗号分隔的排除规则转换成 7z 的 -xr! 命令行参数列表。

    7z 的 -x 模式按完整相对路径匹配，裸名（如 __pycache__）只会命中顶层；
    因此每个规则同时返回裸名与 *<pattern> 两种形式，使任意嵌套深度的
    同名项也被排除，语义与 Inno Excludes / NSIS /x / ZIP scan_folder 对齐。
    """
    args = []
    for p in [x.strip().strip("\\/") for x in (exclude_str or "").split(",") if x.strip()]:
        if not p:
            continue
        args.append("-xr!%s" % p)
        if not p.startswith("*"):
            args.append("-xr!*%s" % p)
    return args


def build_with_7zip(cfg, log_callback, progress_callback):
    """使用 7-Zip SFX 创建自解压安装包。返回 (成功标志, 输出文件路径)"""
    log_callback("========== 7-Zip SFX 打包开始 ==========")
    sz = find_7zip()
    if not sz:
        log_callback("[错误] 未找到 7-Zip (7z.exe)")
        log_callback("请安装 7-Zip: https://www.7-zip.org/")
        return False, None
    log_callback("7z.exe: %s" % sz)
    progress_callback(10)
    source = cfg["last_source"]
    output = cfg["last_output"]
    app_name = cfg["app_name"]
    app_version = cfg["app_version"]
    compression_level = cfg.get("compression_level", "9")

    # 路径安全验证
    try:
        source = validate_path(source)
        output = validate_path(output)
    except ValueError as e:
        log_callback("[错误] 路径验证失败: %s" % e)
        return False, None

    # 排除规则（与 Inno Excludes / NSIS /x / ZIP scan_folder 语义对齐）
    exclude_str = cfg.get("exclude_patterns", "")
    exclude_args = build_7zip_exclude_args(exclude_str)
    if exclude_args:
        log_callback("排除规则: %s" % exclude_str)

    archive_name = "%s_sfx" % app_name
    archive_path = os.path.join(output, archive_name)
    final_exe = os.path.join(output, "%s_setup.exe" % app_name)
    tmp_config = None
    log_callback("创建7z压缩包...")
    progress_callback(20)
    try:
        # -r 显式递归：避免依赖 `dir\*` 通配符的隐式递归行为，
        # 确保所有层级的子目录都进包（与 scan_folder 全深度扫描一致）
        cmd_create = [sz, "a", "-t7z", "-m0=lzma2", "-mx=%s" % compression_level,
                      "-mmt=on", "-r"] + exclude_args + [
                      "%s.7z" % archive_path,
                      "%s\\*" % source]
        result = subprocess.run(
            cmd_create, capture_output=True, text=True, timeout=600,
            encoding="gbk", errors="replace"
        )
        progress_callback(60)
        if result.returncode != 0:
            log_callback("[错误] 7z压缩失败")
            if result.stderr:
                log_callback("错误详情: %s" % result.stderr.strip())
            return False, None
        log_callback("7z压缩完成")
        sfx_module = os.path.join(os.path.dirname(sz), "7z.sfx")
        if not os.path.isfile(sfx_module):
            prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
            sfx_module = os.path.join(prog_files, "7-Zip", "7z.sfx")
        if not os.path.isfile(sfx_module):
            log_callback("[错误] 未找到7z.sfx模块")
            log_callback("提示: 请确认 7-Zip 安装完整，包含 7z.sfx 文件")
            return False, None
        config_lines = []
        config_lines.append(";!@Install@!UTF-8!")
        config_lines.append('Title="%s %s"' % (app_name, app_version))
        config_lines.append('BeginPrompt="是否安装 %s?"' % app_name)
        config_lines.append('RunProgram="%%T\\%s.exe"' % app_name)
        config_lines.append(";!@InstallEnd@!")
        config_content = "\n".join(config_lines).encode("gbk")
        tmp_fd, tmp_config = tempfile.mkstemp(suffix=".txt", prefix="sfx_config_")
        os.close(tmp_fd)
        with open(tmp_config, "wb") as f:
            f.write(config_content)
        log_callback("合并SFX模块...")
        progress_callback(70)
        # 分块复制，避免大文件内存溢出
        with open(final_exe, "wb") as outf:
            copy_file_chunked(sfx_module, outf)
            copy_file_chunked(tmp_config, outf)
            copy_file_chunked("%s.7z" % archive_path, outf)
        progress_callback(90)
        try:
            os.remove("%s.7z" % archive_path)
        except OSError:
            pass
        progress_callback(100)
        log_callback("[成功] 7-Zip SFX 安装包: %s" % final_exe)
        return True, final_exe if os.path.isfile(final_exe) else None
    except subprocess.TimeoutExpired:
        log_callback("[错误] 7z操作超时(600秒)")
        log_callback("提示: 请检查源文件夹大小或网络连接")
        return False, None
    except PermissionError:
        log_callback("[错误] 权限不足，无法访问文件或目录")
        return False, None
    except Exception as e:
        log_callback("[错误] 打包失败: %s" % type(e).__name__)
        log_callback("详细信息: %s" % str(e))
        return False, None
    finally:
        if tmp_config and os.path.exists(tmp_config):
            try:
                os.unlink(tmp_config)
            except OSError:
                pass


def build_with_zip(cfg, log_callback, progress_callback):
    """使用 Python 内置 ZIP 打包。返回 (成功标志, 输出文件路径)"""
    log_callback("========== ZIP 打包开始 ==========")
    source = cfg["last_source"]
    output = cfg["last_output"]
    app_name = cfg["app_name"]
    exclude = cfg.get("exclude_patterns", "")
    zip_name = "%s_portable" % app_name
    zip_path = os.path.join(output, zip_name)
    zip_file = "%s.zip" % zip_path
    # 先写 .part，成功后再原子替换，避免中途失败留下半截/截断的 zip
    part_file = zip_file + ".part"
    log_callback("源文件夹: %s" % source)
    log_callback("输出路径: %s" % zip_file)
    if exclude.strip():
        log_callback("排除规则: %s" % exclude)
    progress_callback(10)
    try:
        if not os.path.isdir(source):
            log_callback("[错误] 源文件夹不存在: %s" % source)
            return False, None
        # 按排除规则逐个写入，保证 ZIP 与 Inno/NSIS 的排除语义一致
        progress_callback(20)
        files, total_size = scan_folder(source, exclude)
        if not files:
            if exclude.strip():
                log_callback("[错误] 排除规则生效后没有剩余文件: %s" % exclude)
            else:
                log_callback("[错误] 源文件夹为空: %s" % source)
            return False, None
        progress_callback(30)
        base = os.path.basename(source.rstrip("\\/")) or "app"
        count = len(files)
        done = 0
        skipped = 0
        last_tick = 0.0
        with zipfile.ZipFile(part_file, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel, _sz in files:
                full = os.path.join(source, rel)
                # 扫描与写入之间文件可能被删除/占用，记录而非静默跳过
                if not os.path.isfile(full):
                    skipped += 1
                    log_callback("[警告] 文件不可读，已跳过: %s" % rel)
                    continue
                zf.write(full, os.path.join(base, rel).replace("\\", "/"))
                done += 1
                now = time.time()
                if count and (done % 20 == 0 or now - last_tick >= 0.2):
                    last_tick = now
                    progress_callback(30 + int(done / count * 60))
        if done == 0:
            log_callback("[错误] 没有任何文件被写入，打包中止")
            return False, None
        # 原子替换：仅在写入完整成功后覆盖旧产物
        os.replace(part_file, zip_file)
        progress_callback(90)
        fsize = os.path.getsize(zip_file)
        log_callback("ZIP文件: %s" % zip_file)
        log_callback("已打包: %d/%d 个文件, 实际大小: %s"
                     % (done, count, format_size(fsize)))
        if skipped:
            log_callback("[警告] %d 个文件未能写入，产物可能不完整" % skipped)
        if exclude.strip():
            log_callback("(已按排除规则过滤: %s)" % exclude)
        progress_callback(100)
        log_callback("[成功] ZIP 打包完成!")
        return True, zip_file
    except PermissionError:
        log_callback("[错误] 权限不足，无法创建压缩包")
        return False, None
    except OSError as e:
        log_callback("[错误] 磁盘操作失败: %s" % str(e))
        return False, None
    except Exception as e:
        log_callback("[错误] 打包失败: %s" % type(e).__name__)
        log_callback("详细信息: %s" % str(e))
        return False, None
    finally:
        # 失败路径清理残留的 .part，避免污染输出目录
        try:
            if os.path.isfile(part_file):
                os.remove(part_file)
        except OSError:
            pass


def generate_nsis_script(cfg):
    """生成 NSIS 安装脚本内容"""
    source = cfg["last_source"]
    app_name = cfg["app_name"]
    app_version = cfg["app_version"]
    publisher = cfg.get("publisher", "")
    icon_path = cfg.get("icon_path", "")
    exclude_str = cfg.get("exclude_patterns", "")
    license_file = cfg.get("license_file", "")

    # 排除规则（逗号分隔），在 File /r 阶段用 /x 过滤，不进包
    excludes = [p.strip().strip("\\/") for p in exclude_str.split(",") if p.strip()]

    def _nsis_escape_path(p):
        # NSIS 字符串中 $ 是变量前缀（如 $PROGRAMFILES），须双写转义；
        # 反斜杠在双引号内是字面量，无需转义。
        return p.replace("$", "$$")

    L = []
    L.append("; NSIS 安装脚本 - 由 %s v%s 自动生成" % (APP_NAME, APP_VERSION))
    L.append("")
    L.append('!include "MUI2.nsh"')
    L.append("")
    # 需要管理员权限写 Program Files 和注册表
    L.append("RequestExecutionLevel admin")
    # 启用 Unicode 与版本信息
    L.append("Unicode True")
    L.append("VIProductVersion \"%s\"" % _normalize_version(app_version))
    L.append('VIAddVersionKey "ProductName" "%s"' % app_name)
    L.append('VIAddVersionKey "FileVersion" "%s"' % app_version)
    if publisher:
        L.append('VIAddVersionKey "CompanyName" "%s"' % publisher)
    L.append("")
    L.append("Name \"%s\"%s" % (
        app_name,
        # NSIS 中 & 是加速键标记，需双写避免标题/开始菜单显示异常
        " \"%s\"" % app_name.replace("&", "&&") if "&" in app_name else ""))
    # 脚本面向 Windows，路径分隔符固定用反斜杠。
    # 不能用 os.path.join：在 Linux 上生成会变成 /，产物无法安装。
    out_dir = cfg["last_output"].rstrip("\\/")
    if not out_dir:
        out_dir = "."
    L.append("OutFile \"%s_setup.exe\"" % _nsis_escape_path(out_dir + "\\" + app_name))
    L.append("InstallDir \"$PROGRAMFILES\\%s\"" % app_name)
    L.append("InstallDirRegKey HKLM \"Software\\%s\" \"InstallDir\"" % app_name)
    if icon_path and os.path.isfile(icon_path):
        L.append("Icon \"%s\"" % _nsis_escape_path(icon_path))
    L.append("")
    # 许可证
    if license_file and os.path.isfile(license_file):
        L.append("!insertmacro MUI_PAGE_LICENSE \"%s\"" % _nsis_escape_path(license_file))
    L.append("!insertmacro MUI_PAGE_DIRECTORY")
    L.append("!insertmacro MUI_PAGE_INSTFILES")
    L.append("!insertmacro MUI_PAGE_FINISH")
    L.append("")
    L.append("!insertmacro MUI_UNPAGE_CONFIRM")
    L.append("!insertmacro MUI_UNPAGE_INSTFILES")
    L.append("")
    L.append('!insertmacro MUI_LANGUAGE "SimpChinese"')
    L.append('!insertmacro MUI_LANGUAGE "English"')
    L.append("")
    L.append("Section")
    L.append("  SetOutPath \"$INSTDIR\"")
    # 主文件：用 /x 在打包阶段就排除，避免被排除内容（.git、__pycache__ 等）进包
    file_line = "  File /r"
    for ex in excludes:
        if ex:
            # NSIS /x 期望文件名/通配符片段，不带路径分隔符
            file_line += ' /x "%s"' % ex.replace("\\", "/").split("/")[-1]
    file_line += " \"%s\\*.*\"" % _nsis_escape_path(source)
    L.append(file_line)
    L.append("")
    # 卸载器
    L.append("  WriteUninstaller \"$INSTDIR\\uninstall.exe\"")
    L.append("")
    # 注册表
    L.append("  WriteRegStr HKLM \"Software\\%s\" \"InstallDir\" \"$INSTDIR\"" % app_name)
    L.append("  WriteRegStr HKLM \"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\%s\" \"DisplayName\" \"%s\"" % (app_name, app_name))
    L.append("  WriteRegStr HKLM \"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\%s\" \"UninstallString\" \"$INSTDIR\\uninstall.exe\"" % app_name)
    L.append("  WriteRegStr HKLM \"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\%s\" \"DisplayVersion\" \"%s\"" % (app_name, app_version))
    if publisher:
        L.append("  WriteRegStr HKLM \"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\%s\" \"Publisher\" \"%s\"" % (app_name, publisher))
    L.append("  WriteRegStr HKLM \"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\%s\" \"DisplayIcon\" \"$INSTDIR\\%s.exe\"" % (app_name, app_name))
    L.append("  WriteRegStr HKLM \"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\%s\" \"EstimatedSize\" \"%d\"" % (app_name, _estimate_dir_size(source) // 1024))
    L.append("")
    # 开始菜单快捷方式
    L.append("  CreateDirectory \"$SMPROGRAMS\\%s\"" % app_name)
    L.append("  CreateShortCut \"$SMPROGRAMS\\%s\\%s.lnk\" \"$INSTDIR\\%s.exe\"" % (app_name, app_name, app_name))
    L.append("  CreateShortCut \"$SMPROGRAMS\\%s\\卸载 %s.lnk\" \"$INSTDIR\\uninstall.exe\"" % (app_name, app_name))
    L.append("  CreateShortCut \"$DESKTOP\\%s.lnk\" \"$INSTDIR\\%s.exe\"" % (app_name, app_name))
    L.append("SectionEnd")
    L.append("")
    L.append("Section \"Uninstall\"")
    # 卸载器 CWD 默认即 $INSTDIR，须先切走否则 RMDir /r "$INSTDIR" 会失败
    L.append("  SetOutPath \"$TEMP\"")
    L.append("  RMDir /r \"$INSTDIR\"")
    L.append("  Delete \"$SMPROGRAMS\\%s\\*.*\"" % app_name)
    L.append("  RMDir \"$SMPROGRAMS\\%s\"" % app_name)
    L.append("  Delete \"$DESKTOP\\%s.lnk\"" % app_name)
    L.append("  DeleteRegKey HKLM \"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\%s\"" % app_name)
    L.append("  DeleteRegKey HKLM \"Software\\%s\"" % app_name)
    L.append("SectionEnd")
    return "\n".join(L)


def _estimate_dir_size(directory):
    """粗略统计目录总字节数（用于注册表 EstimatedSize，KB）"""
    total = 0
    try:
        for root, _dirs, files in os.walk(directory):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _normalize_version(version):
    """将版本号规范化为 X.X.X.X 格式（VIProductVersion 要求，每段 0-65535）"""
    parts = []
    for seg in str(version).split("."):
        # 只接受 ASCII 数字，避免全角数字混入
        digits = "".join(ch for ch in seg if ch in "0123456789")
        try:
            n = int(digits) if digits else 0
        except ValueError:
            n = 0
        parts.append(str(min(n, 65535)))
    while len(parts) < 4:
        parts.append("0")
    return ".".join(parts[:4])


def build_with_nsis(cfg, log_callback, progress_callback):
    """使用 NSIS 编译安装包。返回 (成功标志, 输出文件路径)"""
    log_callback("========== NSIS 打包开始 ==========")
    makensis = find_nsis()
    if not makensis:
        log_callback("[错误] 未找到 NSIS (makensis.exe)")
        log_callback("请安装 NSIS: https://nsis.sourceforge.io/")
        return False, None
    log_callback("makensis.exe: %s" % makensis)
    progress_callback(10)
    script_content = generate_nsis_script(cfg)
    tmp_nsi = None
    try:
        tmp_fd, tmp_nsi = tempfile.mkstemp(suffix=".nsi", prefix="packager_build_")
        os.close(tmp_fd)
        # UTF-8 BOM + Unicode True：makensis 见 BOM 即按 UTF-8 解析
        with open(tmp_nsi, "w", encoding="utf-8-sig") as f:
            f.write(script_content)
        log_callback("已生成脚本: %s" % tmp_nsi)
        progress_callback(20)
        log_callback("开始编译...")
        progress_callback(30)
        result = subprocess.run(
            [makensis, "/V2", tmp_nsi],
            capture_output=True, text=True, timeout=600,
            encoding="gbk", errors="replace"
        )
        progress_callback(80)
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    log_callback("  %s" % line.strip())
        if result.returncode == 0:
            progress_callback(100)
            out_file = os.path.join(cfg["last_output"], "%s_setup.exe" % cfg["app_name"])
            log_callback("[成功] NSIS 安装包编译完成!")
            return True, out_file if os.path.isfile(out_file) else None
        else:
            log_callback("[错误] makensis 返回码: %s" % result.returncode)
            if result.stderr:
                log_callback("错误详情: %s" % result.stderr.strip())
            return False, None
    except subprocess.TimeoutExpired:
        log_callback("[错误] 编译超时(600秒)")
        return False, None
    except PermissionError:
        log_callback("[错误] 权限不足，无法创建临时文件或编译")
        return False, None
    except Exception as e:
        log_callback("[错误] 编译失败: %s" % type(e).__name__)
        log_callback("详细信息: %s" % str(e))
        return False, None
    finally:
        if tmp_nsi and os.path.exists(tmp_nsi):
            try:
                os.unlink(tmp_nsi)
            except OSError:
                pass


# ============================================================
# UI. Liquid Glass Design System
# ============================================================
# Tkinter 原生控件没有圆角与半透明，这里用 Pillow 预渲染出
# 「渐变环境光背景 + 半透明玻璃卡片 + 柔和阴影 + 高光描边」，
# 再以 PhotoImage 铺在容器底层，从而在纯 Tk 下得到接近
# Liquid Glass 的观感。
#
# 设计要点：
#   1. 背景先在 1/8 尺寸上绘制渐变与光斑，再上采样放大并轻微模糊，
#      避免逐像素计算导致启动变慢。
#   2. ttk 控件只接受不透明颜色，因此卡片内的控件背景用
#      mix_rgb() 把半透明填充"折算"成与背景混合后的等效实色，
#      使控件与玻璃卡片在视觉上融为一体。
#   3. Pillow 缺失时 GLASS_AVAILABLE 为 False，整体回退到
#      扁平单色主题，界面依然完全可用。
# ------------------------------------------------------------
try:
    from PIL import Image, ImageDraw, ImageFilter, ImageChops, ImageTk
    GLASS_AVAILABLE = True
    _PIL_IMPORT_ERROR = None
except Exception as _exc:  # pragma: no cover - 环境缺 Pillow
    Image = ImageDraw = ImageFilter = ImageChops = ImageTk = None
    GLASS_AVAILABLE = False
    _PIL_IMPORT_ERROR = str(_exc)

UI_FONT = "Microsoft YaHei UI"
UI_FONT_MONO = "Consolas"

# 单次渲染的像素上限（见 _clamp_render_size）
MAX_RENDER_W = 2560
MAX_RENDER_H = 1600

# UI 渲染类问题的去重告警，避免缩放窗口时刷屏
_ui_warned = set()


def _ui_warn(msg, once_key=None):
    """向 stderr 报告 UI 层问题，并做去重，避免刷屏。"""
    key = once_key or msg
    if key in _ui_warned:
        return
    _ui_warned.add(key)
    try:
        print("[UI] %s" % msg, file=sys.stderr)
    except Exception:
        pass

# 背景光斑：(相对位置x, 相对位置y, 半径倍数, 颜色, 峰值alpha)
# 位置用 0~1 的比例，保证窗口缩放时光斑位置随之自适应。
GLASS_PALETTES = {
    "dark": {
        "name": "深色",
        "bg_top": (10, 15, 31),
        "bg_bottom": (22, 28, 54),
        # 光斑铺在中央区域：整窗大卡片会盖住四角，靠中央才透得出来
        "blobs": (
            (0.50, 0.06, 0.62, (56, 189, 248), 74),
            (0.12, 0.42, 0.46, (139, 92, 246), 66),
            (0.88, 0.52, 0.46, (37, 99, 235), 62),
            (0.42, 0.96, 0.52, (16, 185, 129), 52),
            (0.72, 0.24, 0.34, (236, 72, 153), 40),
        ),
        # 卡片填充刻意很淡：半透明越高越像"实色面板"，越低才透出环境光
        "surface": (24, 31, 58),
        "card_fill": (255, 255, 255, 26),
        # 暗背景下 1px 亮描边会被放大成刺眼白线，压到几乎不可见，
        # 靠阴影与顶部反光表达层次就够了
        "card_border": (255, 255, 255, 12),
        "card_sheen": (255, 255, 255, 20),
        "shadow": (0, 0, 0, 120),
        "text": "#E9EDF9",
        "text_dim": "#98A3C2",
        "accent": "#5B8CFF",
        "accent2": "#8B5CF6",
        "on_accent": "#FFFFFF",
        "field": (255, 255, 255, 14),
        "field_border": (255, 255, 255, 34),
        "tree": (255, 255, 255, 10),
        "tree_head": (255, 255, 255, 24),
        "log": (7, 11, 24),
        "hover": (255, 255, 255, 28),
        "press": (255, 255, 255, 14),
        "sel": "#4C6FE7",
        "border_strong": (255, 255, 255, 30),
    },
    "light": {
        "name": "浅色",
        "bg_top": (238, 243, 254),
        "bg_bottom": (215, 224, 246),
        "blobs": (
            (0.50, 0.04, 0.60, (125, 211, 252), 132),
            (0.10, 0.40, 0.44, (167, 139, 250), 112),
            (0.90, 0.50, 0.44, (147, 197, 253), 128),
            (0.40, 0.98, 0.50, (134, 239, 172), 104),
            (0.74, 0.22, 0.32, (251, 207, 232), 96),
        ),
        "surface": (232, 238, 250),
        "card_fill": (255, 255, 255, 150),
        "card_border": (255, 255, 255, 150),
        "card_sheen": (255, 255, 255, 60),
        "shadow": (30, 41, 82, 62),
        "text": "#151A2C",
        "text_dim": "#5A6483",
        "accent": "#4C6FE7",
        "accent2": "#7C5CFF",
        "on_accent": "#FFFFFF",
        "field": (223, 231, 248, 205),
        "field_border": (255, 255, 255, 190),
        "tree": (255, 255, 255, 126),
        "tree_head": (255, 255, 255, 178),
        "log": (250, 252, 255),
        "hover": (236, 242, 253, 235),
        "press": (206, 216, 238, 215),
        "sel": "#4C6FE7",
        "border_strong": (255, 255, 255, 190),
    },
}


def mix_rgb(bg, fg, alpha):
    """把半透明前景按 alpha 叠到不透明背景上，返回等效实色。

    ttk 控件不接受 RGBA，玻璃卡片里的控件必须用这个函数把
    卡片填充"折算"成实色，才能与半透明卡片视觉一致。
    """
    a = max(0.0, min(1.0, float(alpha) / 255.0))
    return tuple(int(round(b + (f - b) * a)) for b, f in zip(bg, fg[:3]))


def rgb_to_hex(rgb):
    """(r,g,b) -> '#rrggbb'"""
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb[:3])


def _make_blob(size, color, max_alpha):
    """生成一个径向衰减的圆形光斑（RGBA）。"""
    blob = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(blob)
    steps = 28
    half = size / 2.0
    for i in range(steps):
        t = i / float(steps)
        r = half * (1.0 - t)
        if r <= 0.5:
            break
        # 平方衰减使中心更集中、边缘更柔和
        alpha = int(round(max_alpha * (t ** 2.0)))
        if alpha <= 0:
            continue
        draw.ellipse([half - r, half - r, half + r, half + r],
                     fill=(color[0], color[1], color[2], alpha))
    return blob


def _clamp_render_size(width, height):
    """把渲染尺寸限制在合理上限。

    多屏铺开（7680x4320）时单个面板要同时持有多张全尺寸 RGBA 图，
    峰值内存可达数百 MB 且主线程会被阻塞数秒。超限时按比例降采样，
    显示时由 Tk 拉伸，视觉上几乎无差别。
    """
    w = max(4, int(width))
    h = max(4, int(height))
    if w <= MAX_RENDER_W and h <= MAX_RENDER_H:
        return w, h
    scale = min(MAX_RENDER_W / float(w), MAX_RENDER_H / float(h))
    return max(4, int(w * scale)), max(4, int(h * scale))


def render_aurora_background(width, height, palette):
    """渲染窗口背景：多光斑环境光渐变。

    先在 1/8 尺寸绘制再上采样，避免全尺寸逐像素运算。
    """
    width, height = _clamp_render_size(width, height)
    sw = max(8, width // 8)
    sh = max(8, height // 8)
    top = palette["bg_top"]
    bottom = palette["bg_bottom"]

    base = Image.new("RGB", (sw, sh), bottom)
    draw = ImageDraw.Draw(base)
    for y in range(sh):
        t = y / float(max(1, sh - 1))
        draw.line([(0, y), (sw, y)],
                  fill=tuple(int(round(top[i] + (bottom[i] - top[i]) * t))
                             for i in range(3)))

    # 叠加光斑
    for fx, fy, rratio, color, alpha in palette["blobs"]:
        size = max(8, int(min(sw, sh) * rratio * 2))
        if size < 8:
            continue
        blob = _make_blob(size, color, alpha)
        cx = int(fx * sw)
        cy = int(fy * sh)
        base.paste(blob, (cx - size // 2, cy - size // 2), blob)

    img = base.resize((width, height), Image.Resampling.LANCZOS)
    # 轻微模糊，消除上采样带来的轻微色带（半径封顶，避免大图上耗时过长）
    return img.filter(ImageFilter.GaussianBlur(
        radius=max(1.0, min(4.0, width / 400.0))))


def render_glass_card(width, height, palette, radius=18, surface=None,
                       shadow=True):
    """渲染一张玻璃卡片。

    重要：Tk 的 PhotoImage 会**丢弃 alpha 通道只显示 RGB**，因此不能把
    "纯色 + 低 alpha" 的像素直接交给它——描边会变成刺眼的纯白线。
    这里改为在 PIL 内部把半透明图层与底色 surface 合成完毕，
    最终输出一张完全不透明的 RGB 图：视觉上仍是半透明玻璃，
    但不再依赖 Tk 的 alpha 支持。
    """
    width, height = _clamp_render_size(width, height)
    radius = max(2, min(int(radius), min(width, height) // 2))
    if surface is None:
        surface = tuple(palette["bg_bottom"][:3])

    base = Image.new("RGB", (width, height), surface)

    pad = 14 if shadow else 0
    cw = max(4, width - pad * 2)
    ch = max(4, height - pad * 2)

    # 外阴影：圆角矩形做高斯模糊，再与底色合成
    if shadow:
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.rounded_rectangle([pad, pad + 3, pad + cw, pad + ch],
                             radius=radius, fill=palette["shadow"])
        layer = layer.filter(ImageFilter.GaussianBlur(radius=7))
        base = Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")

    # 卡片本体
    card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle(
        [pad, pad, pad + cw, pad + ch], radius=radius,
        fill=palette["card_fill"])
    base = Image.alpha_composite(base.convert("RGBA"), card).convert("RGB")

    # 顶部反光：玻璃特有的斜向高光带，裁剪在卡片轮廓内
    sheen_h = max(1, int(ch * 0.45))
    sheen = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sheen)
    a = palette["card_sheen"][3]
    for y in range(sheen_h):
        t = y / float(max(1, sheen_h - 1))
        alpha = int(round(a * (1.0 - t) ** 1.6))
        if alpha <= 0:
            continue
        sd.line([(pad, pad + y), (pad + cw, pad + y)],
                fill=(255, 255, 255, alpha))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, pad + cw, pad + ch],
                                           radius=radius, fill=255)
    sheen.putalpha(ImageChops.multiply(sheen.split()[3], mask))
    base = Image.alpha_composite(base.convert("RGBA"), sheen).convert("RGB")

    # 1px 高光描边（在已合成的底色上绘制，RGB 已是混合结果）
    edge = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(
        [pad, pad, pad + cw - 1, pad + ch - 1], radius=radius,
        outline=palette["card_border"], width=1)
    return Image.alpha_composite(base.convert("RGBA"), edge).convert("RGB")


class GlassPanel(tk.Frame):
    """液态玻璃面板：底层铺一张半透明圆角卡片，上层承载真实控件。

    PhotoImage 必须保留在实例上，否则会被 GC 回收导致图片消失；
    重绘走 Configure + 节流，避免拖拽窗口时连续渲染卡顿。
    """

    REDRAW_DELAY_MS = 130
    MIN_SIZE = 12
    def __init__(self, master, app, radius=20, padding=16, title=None,
                 hint=None, surface="card", **kw):
        tk.Frame.__init__(self, master, **kw)
        self.app = app
        self.radius = radius
        self.pad_in = padding
        # "window" = 直接压在环境光背景上；"card" = 压在另一张玻璃卡片上
        self.surface_kind = surface
        self._photo = None
        self._pending = None
        self._last_size = (0, 0)
        # 先创建背景层，保证位于子控件之下。
        # 必须给底色：尺寸不足或渲染失败时 redraw() 会早退，
        # 此时若留 Tk 系统默认色，深色主题下会出现灰块。
        self._bg = tk.Label(self, bd=0, highlightthickness=0,
                            bg=app.card_bg())
        self._bg.place(x=0, y=0, relwidth=1, relheight=1)
        self.inner = tk.Frame(self)
        self.inner.pack(fill=BOTH, expand=True, padx=padding, pady=padding)
        # 标题与正文分区：标题固定 pack，正文容器独立，
        # 避免同一个父容器里混用 pack 与 grid（Tk 不允许）
        if title:
            head = tk.Frame(self.inner)
            head.pack(fill=X, pady=(0, 10))
            ttk.Label(head, text=title, style="Section.TLabel").pack(anchor=W)
            if hint:
                ttk.Label(head, text=hint, style="Hint.TLabel").pack(anchor=W, pady=(3, 0))
        self.body = tk.Frame(self.inner)
        self.body.pack(fill=BOTH, expand=True)
        self._sync_inner_bg()
        self.bind("<Configure>", self._on_configure, add="+")
        self.after_idle(self.redraw)

    def _sync_inner_bg(self):
        """内容容器用与卡片等效的实色，控件才不会"透出"卡片底图。"""
        try:
            bg = self.app.card_bg()
            for w in (self.inner, self.body):
                w.configure(bg=bg)
            for child in self.inner.winfo_children():
                if isinstance(child, tk.Frame) and child is not self.body:
                    child.configure(bg=bg)
        except Exception:
            pass

    def _on_configure(self, event=None):
        size = (self.winfo_width(), self.winfo_height())
        # 忽略小幅抖动，显著减少重绘次数
        if (abs(size[0] - self._last_size[0]) < 8
                and abs(size[1] - self._last_size[1]) < 8):
            return
        if self._pending is not None:
            try:
                self.after_cancel(self._pending)
            except Exception:
                pass
        self._pending = self.after(self.REDRAW_DELAY_MS, self.redraw)

    def redraw(self, force=False):
        """立即重绘（切换主题时用 force 忽略尺寸去抖）"""
        if self._pending is not None:
            try:
                self.after_cancel(self._pending)
            except Exception:
                pass
            self._pending = None
        if not self.app.glass_enabled():
            try:
                self._bg.configure(image="", bg=self.app.card_bg())
            except Exception:
                pass
            return
        w = self.winfo_width()
        h = self.winfo_height()
        if w < self.MIN_SIZE or h < self.MIN_SIZE:
            return
        self._last_size = (w, h)
        try:
            pal = self.app.palette()
            base = (pal["surface"] if self.surface_kind == "window"
                    else self.app.card_bg_rgb())
            img = render_glass_card(w, h, pal, radius=self.radius,
                                    surface=base)
            self._photo = ImageTk.PhotoImage(img)
            # 有 image 时 bg 不参与显示；传空串是非法颜色会抛 TclError
            self._bg.configure(image=self._photo)
        except Exception as exc:  # pragma: no cover - 渲染异常不应拖垮界面
            _ui_warn("玻璃面板渲染失败: %s" % exc)

    def refresh_theme(self):
        self._sync_inner_bg()
        self.redraw(force=True)


# ============================================================
# E. GUI Class - Initialization & Theme
# ============================================================
class PackagerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("%s v%s" % (APP_NAME, APP_VERSION))
        self.cfg = load_config()
        w = self.cfg.get("window_width", 1180)
        h = self.cfg.get("window_height", 900)
        # 旧配置里存的是改造前的小尺寸（960x780），直接沿用会让
        # 独立出来的"批量打包"页与折叠日志区显得拥挤，这里做下限钳制
        try:
            w = max(1060, int(w))
            h = max(700, int(h))
        except (TypeError, ValueError):
            w, h = 1180, 900
        self.root.geometry("%dx%d" % (w, h))
        self.root.minsize(780, 580)
        self.source_var = StringVar(value=self.cfg.get("last_source", ""))
        self.output_var = StringVar(value=self.cfg.get("last_output", ""))
        self.appname_var = StringVar(value=self.cfg.get("app_name", "MyApp"))
        self.appver_var = StringVar(value=self.cfg.get("app_version", APP_VERSION))
        self.publisher_var = StringVar(value=self.cfg.get("publisher", ""))
        self.mode_var = StringVar(value=self.cfg.get("default_mode", "inno"))
        self.icon_var = StringVar(value=self.cfg.get("icon_path", ""))
        self.exclude_var = StringVar(value=self.cfg.get("exclude_patterns", DEFAULT_CONFIG["exclude_patterns"]))
        self.status_var = StringVar(value="就绪")
        self.progress_var = IntVar(value=0)
        self.search_var = StringVar(value="")
        self.case_sensitive_var = IntVar(value=0)
        self.scanned_files = []
        self.scanned_size = 0
        self.current_project = self.cfg.get("last_project_file", "")
        self.current_theme = self.cfg.get("theme", "light")
        self._building = False
        # 打包中关窗并被确认后置位：收尾回调据此跳过弹窗，
        # 避免对已销毁的控件操作
        self._closing = False
        # UI 更新队列：后台构建线程只能通过该队列投递回调，
        # 由主线程 after 轮询消费。Tkinter 非线程安全，
        # 直接跨线程调用 root.after 属未定义行为，长期运行有崩溃风险。
        self._ui_queue = queue.Queue()
        # 液态玻璃：环境光背景铺满窗口，所有 GlassPanel 登记在此，
        # 切换主题时统一重绘
        self._glass_panels = []
        self._backdrop_img = None
        self._backdrop_size = None
        self._backdrop = tk.Label(self.root, bd=0, highlightthickness=0,
                                  bg=self.solid_bg())
        self._backdrop.place(x=0, y=0, relwidth=1, relheight=1)
        self._backdrop_after_id = None
        self.root.bind("<Configure>", self._on_root_configure, add="+")
        self.root.bind("<Map>", self._on_root_map, add="+")
        if not GLASS_AVAILABLE and _PIL_IMPORT_ERROR:
            _ui_warn("未启用液态玻璃效果（缺少 Pillow: %s），"
                     "已回退为扁平主题" % _PIL_IMPORT_ERROR, "pil-missing")
        self._setup_styles()
        self._build_menu()
        self._build_ui()
        self._apply_theme(self.current_theme)
        self._check_tools()
        self._bind_shortcuts()
        self._auto_save_project()
        self.source_var.trace_add("write", self._on_source_changed)
        self._source_after_id = None
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._load_recent_projects_menu()
        # 启动 UI 队列轮询（必须在所有控件创建完毕后）
        self.root.after(30, self._poll_ui_queue)

    # --- Thread-safe UI dispatch ---
    # 单次轮询最多处理的回调条数：批量打包会产生突发日志，
    # 不设上限会长时间占住 UI 线程导致界面假死；超出部分留给下一 tick。
    _UI_QUEUE_BUDGET = 50

    def _call_in_ui(self, fn, *args, **kwargs):
        """线程安全地把回调调度到主线程执行。

        主线程直接调用（保持即时性）；后台线程投递到队列，
        由 _poll_ui_queue 在主线程统一消费。
        """
        if threading.current_thread() is threading.main_thread():
            try:
                fn(*args, **kwargs)
            except Exception:
                # 保留可诊断性：与 Tk 默认的 report_callback_exception 一致输出
                traceback.print_exc()
            return
        try:
            self._ui_queue.put((fn, args, kwargs))
        except Exception:
            pass

    def _poll_ui_queue(self):
        """主线程轮询：消费 UI 队列并重新调度自身。

        每 tick 最多处理 _UI_QUEUE_BUDGET 条，未消费完则下一 tick 继续，
        避免突发日志把主线程占满。
        """
        try:
            for _ in range(self._UI_QUEUE_BUDGET):
                try:
                    fn, args, kwargs = self._ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn(*args, **kwargs)
                except Exception:
                    # 控件可能已在窗口关闭后销毁，忽略单条失败
                    traceback.print_exc()
        except Exception:
            pass
        try:
            self.root.after(30, self._poll_ui_queue)
        except Exception:
            pass  # 窗口已销毁，停止轮询

    def _setup_styles(self):
        style = ttk.Style()
        available = style.theme_names()
        for theme in ("clam", "vista", "winnative", "default"):
            if theme in available:
                style.theme_use(theme)
                break
        self.style = style
        self._font_ok = self._pick_fonts()
        self._configure_modern_styles()

    def _pick_fonts(self):
        """挑一个系统里真实存在的字体族，缺失时回退默认。

        直接假定 "Microsoft YaHei UI" 存在会在精简版 Windows 上
        静默回退成难看的默认字体。
        """
        try:
            import tkinter.font as tkfont
            families = set(tkfont.families(self.root))
        except Exception:
            families = set()
        for name in (UI_FONT, "Microsoft YaHei", "Segoe UI", "Tahoma", "Arial"):
            if name in families:
                self.ui_font = name
                break
        else:
            self.ui_font = UI_FONT
        for name in (UI_FONT_MONO, "Consolas", "Courier New"):
            if name in families:
                self.mono_font = name
                break
        else:
            self.mono_font = UI_FONT_MONO
        return self.ui_font

    def _font(self, size=9, weight="normal"):
        return (self.ui_font, size, weight)

    def _mono(self, size=9):
        return (self.mono_font, size)

    def _configure_modern_styles(self):
        """统一的现代扁平风格：去边框、大内边距、圆角观感。"""
        p = self.palette()
        f = self.ui_font
        accent = p["accent"]
        on_accent = p["on_accent"]
        text = p["text"]
        dim = p["text_dim"]
        # 玻璃卡片内的控件底色 = 半透明填充与背景混合后的等效实色
        card_bg = rgb_to_hex(self.card_bg_rgb())
        accent2 = p["accent2"]
        surface = p["surface"]
        field_bg = rgb_to_hex(mix_rgb(surface, p["field"][:3], p["field"][3]))
        hover = rgb_to_hex(mix_rgb(surface, p["hover"][:3], p["hover"][3]))
        press = rgb_to_hex(mix_rgb(surface, p["press"][:3], p["press"][3]))
        sel = p["sel"]

        s = self.style
        s.configure(".", font=(f, 9), background=card_bg, foreground=text,
                    borderwidth=0, focuscolor=card_bg)
        s.configure("TFrame", background=card_bg)
        # Shell 层与玻璃卡片等效实色同色：嵌套 Frame 不会露出系统默认灰
        s.configure("Shell.TFrame", background=card_bg)
        s.configure("TLabel", background=card_bg, foreground=text,
                    font=(f, 9))
        s.configure("Title.TLabel", background=card_bg, foreground=text,
                    font=(f, 17, "bold"))
        s.configure("Subtitle.TLabel", background=card_bg, foreground=dim,
                    font=(f, 9))
        s.configure("Section.TLabel", background=card_bg, foreground=text,
                    font=(f, 10, "bold"))
        s.configure("Status.TLabel", background=card_bg, foreground=dim,
                    font=(f, 9))
        s.configure("Hint.TLabel", background=card_bg, foreground=dim,
                    font=(f, 8))
        s.configure("Danger.TLabel", background=card_bg, foreground="#ff6b6b",
                    font=(f, 9))
        s.configure("OK.TLabel", background=card_bg, foreground="#2ecc71",
                    font=(f, 9))
        # 链接色用强调色：硬编码 "blue" 在深色主题下对比度不足
        s.configure("Link.TLabel", background=card_bg, foreground=accent,
                    font=(f, 9))

        # 主按钮：强调色实底 + 大内边距，形成明确的主 CTA
        s.configure("TButton", background=field_bg, foreground=text,
                    borderwidth=0, focusthickness=0, padding=(12, 7),
                    font=(f, 9), relief="flat", anchor="center")
        s.map("TButton",
              background=[("disabled", press), ("pressed", press), ("active", hover)],
              foreground=[("disabled", dim)])
        s.configure("Build.TButton", background=accent, foreground=on_accent,
                    font=(f, 11, "bold"), padding=(20, 11), relief="flat",
                    borderwidth=0, focusthickness=0)
        s.map("Build.TButton",
              background=[("disabled", press), ("pressed", accent2), ("active", accent2)],
              foreground=[("disabled", dim)])

        s.configure("Ghost.TButton", background=card_bg, foreground=accent,
                    font=(f, 9, "bold"), padding=(10, 5), relief="flat",
                    borderwidth=0, focusthickness=0)
        s.map("Ghost.TButton",
              background=[("pressed", press), ("active", hover)])

        s.configure("TEntry", fieldbackground=field_bg, foreground=text,
                    borderwidth=0, relief="flat", padding=(10, 6),
                    insertcolor=text, lightcolor=field_bg, darkcolor=field_bg)
        s.map("TEntry", lightcolor=[("focus", accent)],
              darkcolor=[("focus", accent)],
              bordercolor=[("focus", accent)])
        s.configure("TCombobox", fieldbackground=field_bg, background=field_bg,
                    foreground=text, borderwidth=0, relief="flat",
                    padding=(9, 7), arrowcolor=dim,
                    lightcolor=field_bg, darkcolor=field_bg)
        s.map("TCombobox", fieldbackground=[("readonly", field_bg)],
              foreground=[("readonly", text)])
        s.configure("TScrollbar", background=hover, troughcolor=card_bg,
                    bordercolor=card_bg, arrowcolor=dim, borderwidth=0)
        s.configure("TProgressbar", background=accent, troughcolor=field_bg,
                    bordercolor=field_bg, lightcolor=accent, darkcolor=accent,
                    borderwidth=0, thickness=8)
        s.configure("TCheckbutton", background=card_bg, foreground=text,
                    font=(f, 9), focuscolor=card_bg)
        s.map("TCheckbutton", background=[("active", card_bg)])
        s.configure("TRadiobutton", background=card_bg, foreground=text,
                    font=(f, 9), focuscolor=card_bg)
        s.map("TRadiobutton", background=[("active", card_bg)])

        # Notebook：页签做成胶囊状，选中态用强调色
        s.configure("TNotebook", background=card_bg, borderwidth=0, tabmargins=(2, 6, 2, 0))
        s.configure("TNotebook.Tab", background=field_bg, foreground=dim,
                    padding=(16, 9), borderwidth=0, font=(f, 9))
        s.map("TNotebook.Tab",
              background=[("selected", accent), ("active", hover)],
              foreground=[("selected", on_accent), ("active", text)])

        # 表格：透明行、柔和表头。light/dark/border 三色都要压到卡片底色，
        # 否则 clam 主题的默认边框会在深色下形成刺眼白框。
        s.configure("Treeview", background=card_bg, fieldbackground=card_bg,
                    foreground=text, borderwidth=0, relief="flat",
                    rowheight=26, lightcolor=card_bg, darkcolor=card_bg,
                    bordercolor=card_bg, font=(self.mono_font, 9))
        s.map("Treeview", background=[("selected", sel)],
              foreground=[("selected", "#ffffff")],
              lightcolor=[("selected", sel)], darkcolor=[("selected", sel)],
              bordercolor=[("selected", sel)])
        s.configure("Treeview.Heading", background=field_bg, foreground=dim,
                    font=(f, 9, "bold"), relief="flat", borderwidth=0,
                    padding=(8, 7))
        s.map("Treeview.Heading", background=[("active", hover)])
        s.map("Treeview", background=[("selected", sel)],
              foreground=[("selected", "#ffffff")])
        s.configure("History.Treeview", background=card_bg,
                    fieldbackground=card_bg, foreground=text, rowheight=26)
        s.configure("History.Treeview.Heading", background=field_bg,
                    foreground=dim, font=(f, 9, "bold"), relief="flat")

    # --- Liquid Glass helpers ---
    def _on_root_configure(self, event=None):
        """窗口尺寸变化时重建环境光背景（节流，避免拖拽时连续渲染）。"""
        if event is not None and getattr(event, "widget", None) is not self.root:
            return
        if self._backdrop_after_id is not None:
            try:
                self.root.after_cancel(self._backdrop_after_id)
            except Exception:
                pass
        self._backdrop_after_id = self.root.after(180, self._redraw_backdrop)

    def _on_root_map(self, event=None):
        """窗口首次显示 / 从最小化恢复：此时才有真实尺寸。

        复用 after 节流而非同步渲染，否则任务栏反复点击会连续触发
        全窗口 PIL 渲染造成明显卡顿。
        """
        if event is not None and getattr(event, "widget", None) is not self.root:
            return
        size = (self.root.winfo_width(), self.root.winfo_height())
        if size == getattr(self, "_backdrop_size", None) and self._backdrop_img is not None:
            return
        if self._backdrop_after_id is not None:
            try:
                self.root.after_cancel(self._backdrop_after_id)
            except Exception:
                pass
        self._backdrop_after_id = self.root.after(120, self._redraw_backdrop)

    def _redraw_backdrop(self):
        self._backdrop_after_id = None
        self._backdrop_size = (self.root.winfo_width(), self.root.winfo_height())
        self._refresh_window_backdrop()

    def register_glass(self, panel):
        self._glass_panels.append(panel)
        return panel

    def palette(self):
        """当前主题调色板（GLASS_PALETTES 的一项）"""
        return GLASS_PALETTES.get(getattr(self, "current_theme", "light"),
                                  GLASS_PALETTES["light"])

    def glass_enabled(self):
        return GLASS_AVAILABLE and not getattr(self, "_glass_off", False)

    def solid_bg(self):
        """无玻璃效果时使用的实色背景（降级路径）"""
        return rgb_to_hex(self.palette()["bg_bottom"])

    def card_bg_rgb(self):
        """卡片填充与 surface 混合后的实际颜色（RGB 元组）。"""
        p = self.palette()
        return mix_rgb(p["surface"], p["card_fill"][:3], p["card_fill"][3])

    def card_bg(self):
        """玻璃卡片内控件应使用的等效实色背景。"""
        return rgb_to_hex(self.card_bg_rgb())


    # --- Theme Management ---
    def _apply_theme(self, theme_name):
        """应用主题：重设 ttk 样式、窗口背景、日志区，并刷新所有玻璃面板。"""
        if theme_name not in GLASS_PALETTES:
            theme_name = "light"
        self.current_theme = theme_name
        p = self.palette()

        # 窗口底色：玻璃模式下会被背景图覆盖，这里只保证降级路径有正确底色
        self.root.configure(bg=self.solid_bg())
        self._refresh_window_backdrop()
        self._configure_modern_styles()

        # 工具状态标签用状态色
        for attr, ok_style, bad_style in (
                ("inno_label", "OK.TLabel", "Danger.TLabel"),
                ("zip7_label", "OK.TLabel", "Danger.TLabel"),
                ("nsis_label", "OK.TLabel", "Danger.TLabel")):
            widget = getattr(self, attr, None)
            if widget is None:
                continue
            try:
                text = str(widget.cget("text"))
                widget.configure(style=ok_style if "未安装" not in text
                                 else bad_style)
            except Exception:
                pass

        # Tk 的颜色必须写成 #rrggbb，元组会被当成颜色名而报
        # "unknown color name '7 11 24'"
        try:
            self.log_text.configure(bg=rgb_to_hex(p["log"]), fg=p["text"],
                                    insertbackground=p["text"],
                                    highlightthickness=0, borderwidth=0)
        except Exception as exc:
            _ui_warn("日志区配色失败: %s" % exc)

        # 原生 Listbox 不吃 ttk 样式，须单独配色
        try:
            field = rgb_to_hex(mix_rgb(p["surface"], p["field"][:3], p["field"][3]))
            self.batch_listbox.configure(bg=field, fg=p["text"],
                                         selectbackground=p["sel"],
                                         selectforeground="#ffffff",
                                         highlightthickness=0, borderwidth=0)
        except Exception as exc:
            _ui_warn("批量列表配色失败: %s" % exc)

        for panel in getattr(self, "_glass_panels", []):
            try:
                panel.refresh_theme()
            except Exception:
                pass
        self.cfg["theme"] = theme_name

    def _refresh_window_backdrop(self):
        """重建铺满窗口的环境光背景图。"""
        if not self.glass_enabled():
            try:
                if self._backdrop is not None:
                    self._backdrop.configure(image="")
            except Exception:
                pass
            return
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        if w < 40 or h < 40:
            return
        try:
            img = render_aurora_background(w, h, self.palette())
            self._backdrop_img = ImageTk.PhotoImage(img)
            self._backdrop.configure(image=self._backdrop_img)
        except Exception as exc:  # pragma: no cover
            _ui_warn("窗口背景渲染失败: %s" % exc)


    # --- Menu Bar ---
    def _build_menu(self):
        import tkinter as _tk_menu
        self.menubar = _tk_menu.Menu(self.root, tearoff=0)
        self._build_file_menu(_tk_menu)
        self._build_appearance_menu(_tk_menu)
        self._build_help_menu(_tk_menu)
        self.root.config(menu=self.menubar)

    def _build_file_menu(self, _tk_menu):
        self.file_menu = _tk_menu.Menu(self.menubar, tearoff=0)
        self.file_menu.add_command(label="保存项目 (Ctrl+S)", command=self._save_project)
        self.file_menu.add_command(label="打开项目 (Ctrl+O)", command=self._open_project)
        self.recent_menu = _tk_menu.Menu(self.file_menu, tearoff=0)
        self.file_menu.add_cascade(label="最近项目", menu=self.recent_menu)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="导出配置", command=self._export_config)
        self.file_menu.add_command(label="导入配置", command=self._import_config)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="打开输出目录", command=self._open_output_dir)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="退出 (Ctrl+Q)", command=self._on_close)
        self.menubar.add_cascade(label="文件", menu=self.file_menu)

    def _build_appearance_menu(self, _tk_menu):
        self.appearance_menu = _tk_menu.Menu(self.menubar, tearoff=0)
        self.appearance_menu.add_command(label="切换主题 (Ctrl+T)", command=self._toggle_theme)
        self.appearance_menu.add_separator()
        self.appearance_menu.add_command(label="浅色主题", command=lambda: self._apply_theme("light"))
        self.appearance_menu.add_command(label="深色主题", command=lambda: self._apply_theme("dark"))
        self.menubar.add_cascade(label="外观", menu=self.appearance_menu)

    def _toggle_theme(self):
        """在浅色/深色主题之间切换"""
        new_theme = "dark" if getattr(self, "current_theme", "light") == "light" else "light"
        self._apply_theme(new_theme)
        self._set_status("已切换到%s主题" % ("深色" if new_theme == "dark" else "浅色"))
        save_config(self.cfg)

    def _build_help_menu(self, _tk_menu):
        help_m = _tk_menu.Menu(self.menubar, tearoff=0)
        help_m.add_command(label="关于 (F1)", command=self._show_about)
        self.menubar.add_cascade(label="帮助", menu=help_m)

    def _load_recent_projects_menu(self):
        self.recent_menu.delete(0, "end")
        recent = self.cfg.get("recent_projects", [])
        if not recent:
            self.recent_menu.add_command(label="(无最近项目)", state="disabled")
            return
        for p in recent:
            display = os.path.basename(p) if p else ""
            self.recent_menu.add_command(
                label="%s  (%s)" % (display, p),
                command=lambda fp=p: self._load_recent_project(fp)
            )
        self.recent_menu.add_separator()
        self.recent_menu.add_command(label="清除最近项目列表", command=self._clear_recent_projects)

    def _add_recent_project(self, filepath):
        # 必须先复制再改：load_config() 用 DEFAULT_CONFIG.copy()（浅拷贝），
        # 原地 remove/insert 会连带改掉模块级 DEFAULT_CONFIG 里的同一 list
        recent = list(self.cfg.get("recent_projects", []))
        if filepath in recent:
            recent.remove(filepath)
        recent.insert(0, filepath)
        self.cfg["recent_projects"] = recent[:RECENT_PROJECTS_MAX]
        self._load_recent_projects_menu()

    def _load_recent_project(self, filepath):
        if not os.path.isfile(filepath):
            messagebox.showwarning("提示", "项目文件不存在:\n%s" % filepath)
            return
        self._do_open_project(filepath)

    def _clear_recent_projects(self):
        self.cfg["recent_projects"] = []
        self._load_recent_projects_menu()

    # --- Keyboard Shortcuts ---
    def _bind_shortcuts(self):
        self.root.bind("<Control-o>", lambda e: self._open_project())
        self.root.bind("<Control-O>", lambda e: self._open_project())
        self.root.bind("<Control-s>", lambda e: self._save_project())
        self.root.bind("<Control-S>", lambda e: self._save_project())
        self.root.bind("<Control-b>", lambda e: self._start_build())
        self.root.bind("<Control-B>", lambda e: self._start_build())
        self.root.bind("<Control-q>", lambda e: self._on_close())
        self.root.bind("<Control-Q>", lambda e: self._on_close())
        self.root.bind("<F5>", lambda e: self._refresh_file_tree())
        self.root.bind("<F1>", lambda e: self._show_about())
        self.root.bind("<Control-t>", lambda e: self._toggle_theme())
        self.root.bind("<Control-T>", lambda e: self._toggle_theme())


    # --- Build UI ---
    def _build_ui(self):
        # 整窗作为一张大玻璃卡片：留出较宽的边距让环境光背景透进来，
        # 这是"玻璃"观感的来源；内部再嵌套卡片，形成多层次半透明质感。
        shell = self.register_glass(
            GlassPanel(self.root, self, radius=30, padding=13,
                       surface="window"))
        shell.pack(fill=BOTH, expand=True, padx=16, pady=(14, 16))
        body = shell.inner

        self._build_header(body)

        # 主内容卡片
        content_panel = self.register_glass(
            GlassPanel(body, self, radius=20, padding=8))
        content_panel.pack(fill=BOTH, expand=True, pady=(0, 12))
        content = content_panel.inner

        notebook = ttk.Notebook(content)
        notebook.pack(fill=BOTH, expand=True)
        self.notebook = notebook

        self._build_tab_basic(notebook)
        self._build_tab_batch(notebook)
        self._build_tab_files(notebook)
        self._build_tab_stats(notebook)
        self._build_tab_advanced(notebook)
        self._build_tab_history(notebook)

        self._build_bottom(body)
        self._init_log_tags()

    def _build_header(self, parent):
        """标题区：大标题 + 版本副标题，右侧放主题切换等入口。"""
        header = ttk.Frame(parent)
        header.pack(fill=X, pady=(2, 14))
        header.configure(style="Shell.TFrame")

        left = ttk.Frame(header)
        left.pack(side=LEFT, fill=X, expand=True)
        left.configure(style="Shell.TFrame")

        ttk.Label(left, text=APP_NAME, style="Title.TLabel").pack(anchor=W)
        ttk.Label(left, text="v%s · 一键生成 Windows 安装包" % APP_VERSION,
                  style="Subtitle.TLabel").pack(anchor=W, pady=(3, 0))

        right = ttk.Frame(header)
        right.pack(side=RIGHT)
        right.configure(style="Shell.TFrame")
        ttk.Button(right, text="切换主题", style="Ghost.TButton",
                   command=self._toggle_theme).pack(side=RIGHT)
        # 外部工具状态常驻标题栏：原先放在"高级选项"页里，那个 tab 因此
        # 请求高度达 735px，会把 notebook 撑大并把底部操作区挤出窗口
        self._tools_summary_var = StringVar(value="")
        tools_lbl = ttk.Label(right, textvariable=self._tools_summary_var,
                              style="Hint.TLabel")
        tools_lbl.pack(side=RIGHT, padx=(0, 16))
        self._tools_detail = StringVar(value="")
        try:
            # tkinter.Tooltip 在 3.13 才有，且位于子模块，
            # `from tkinter import Tooltip` 会直接 ImportError
            from tkinter.tooltip import Tooltip
            Tooltip(tools_lbl, self._tools_detail)
        except Exception:
            pass  # 低版本无内置 Tooltip，静默降级
        # 隐藏的标签对象：状态文本 + 颜色由 _check_tools 统一维护
        # （放在 header 里但不可见，供 tooltip 与测试读取）
        self._tool_labels = {}
        for key, name in (("inno_label", "Inno Setup"),
                          ("zip7_label", "7-Zip"),
                          ("nsis_label", "NSIS")):
            lbl = ttk.Label(right, text=name)
            lbl.place(x=0, y=0, width=1, height=1)
            self._tool_labels[key] = lbl
            setattr(self, key, lbl)
        self.header_actions = right

    def _build_tab_basic(self, notebook):
        tab_basic = ttk.Frame(notebook, padding=(18, 16))
        notebook.add(tab_basic, text=" 基本设置 ")
        tab_basic.columnconfigure(0, weight=1)
        # 让卡片撑满内容区：否则下方会留一大片"空容器"显得未完成。
        # rowconfigure 只影响多余空间的分配，不会增加请求高度。
        tab_basic.rowconfigure(0, weight=1)

        # 分组卡片：基本信息
        info = self.register_glass(GlassPanel(tab_basic, self, radius=16,
                                               padding=14, title="基本信息"))
        info.grid(row=0, column=0, columnspan=3, sticky="nsew", pady=(0, 12))
        f = info.body

        row = 0
        ttk.Label(f, text="源文件夹").grid(row=row, column=0, sticky=W, pady=5)
        src_entry = ttk.Entry(f, textvariable=self.source_var)
        src_entry.grid(row=row, column=1, sticky="ew", padx=(14, 10), pady=6)
        self.src_entry = src_entry
        ttk.Button(f, text="浏览", command=self._browse_source).grid(row=row, column=2, pady=6)
        f.columnconfigure(1, weight=1)

        row += 1
        ttk.Label(f, text="输出目录").grid(row=row, column=0, sticky=W, pady=5)
        ttk.Entry(f, textvariable=self.output_var).grid(row=row, column=1, sticky="ew", padx=(14, 10), pady=6)
        ttk.Button(f, text="浏览", command=self._browse_output).grid(row=row, column=2, pady=6)

        row += 1
        ttk.Label(f, text="应用名称").grid(row=row, column=0, sticky=W, pady=5)
        ttk.Entry(f, textvariable=self.appname_var).grid(row=row, column=1, sticky="ew", padx=(14, 10), pady=6)

        row += 1
        ttk.Label(f, text="版本号").grid(row=row, column=0, sticky=W, pady=5)
        ttk.Entry(f, textvariable=self.appver_var, width=16).grid(row=row, column=1, sticky=W, padx=(14, 10), pady=6)

        row += 1
        ttk.Label(f, text="发布者").grid(row=row, column=0, sticky=W, pady=5)
        ttk.Entry(f, textvariable=self.publisher_var).grid(row=row, column=1, sticky="ew", padx=(14, 10), pady=6)

        # 仅在 Ctrl+V 且用户明确触发时粘贴（避免 FocusIn 自动覆盖）
        src_entry.bind("<Control-v>", lambda e: self._paste_path_to_source())

        # 批量打包独立成 tab：与基本信息同页时 tab 请求高度过大，
        # 会把底部的操作区与日志区整个挤出窗口。
        self._batch_hint_var = StringVar(value="")
        tip = ttk.Label(tab_basic, textvariable=self._batch_hint_var,
                        style="Hint.TLabel")
        tip.grid(row=1, column=0, columnspan=3, sticky=W, pady=(0, 0))
        self._batch_tab_tip = tip

    def _batch_count_text(self):
        try:
            n = int(self.batch_listbox.size())
        except Exception:
            return ""
        if n <= 0:
            return ("批量列表为空。切换到「批量打包」页添加多个源目录，"
                    "之后直接点「开始打包」即可逐个生成产物。")
        return "批量列表共 %d 个文件夹，开始打包后将逐个输出到带序号的子目录。" % n

    def _build_tab_batch(self, notebook):
        tab_batch = ttk.Frame(notebook, padding=(18, 16))
        notebook.add(tab_batch, text=" 批量打包 ")
        tab_batch.columnconfigure(0, weight=1)
        tab_batch.rowconfigure(0, weight=1)

        card = self.register_glass(
            GlassPanel(tab_batch, self, radius=16, padding=16, title="待打包文件夹",
                       hint="一次打包多个文件夹，各自输出到带序号的子目录"))
        card.grid(row=0, column=0, sticky="nsew")
        batch_frame = card.body

        batch_list_frame = ttk.Frame(batch_frame)
        batch_list_frame.pack(fill=BOTH, expand=True)
        batch_list_frame.configure(style="Shell.TFrame")

        self.batch_listbox = Listbox(batch_list_frame, height=8, font=self._mono(9),
                                     selectmode="extended", bd=0,
                                     highlightthickness=0, activestyle="none")
        batch_scroll = ttk.Scrollbar(batch_list_frame, orient="vertical", command=self.batch_listbox.yview)
        self.batch_listbox.configure(yscrollcommand=batch_scroll.set)
        self.batch_listbox.pack(side=LEFT, fill=BOTH, expand=True)
        batch_scroll.pack(side=RIGHT, fill=Y)
        # 列表变化时同步「基本设置」页的提示文案
        self.batch_listbox.bind("<<ListboxSelect>>", self._on_batch_list_change)
        self.batch_listbox.bind("<ButtonRelease-1>", self._on_batch_list_change)

        batch_btn_frame = ttk.Frame(batch_frame)
        batch_btn_frame.pack(fill=X, pady=(12, 0))
        batch_btn_frame.configure(style="Shell.TFrame")
        ttk.Button(batch_btn_frame, text="添加文件夹", command=self._add_batch_folder).pack(side=LEFT, padx=(0, 8))
        ttk.Button(batch_btn_frame, text="移除选中", command=self._remove_batch_folder).pack(side=LEFT, padx=(0, 8))
        ttk.Button(batch_btn_frame, text="清空列表", command=self._clear_batch_folders).pack(side=LEFT, padx=(0, 8))
        ttk.Button(batch_btn_frame, text="从剪贴板粘贴", command=self._paste_path_to_source).pack(side=LEFT, padx=(0, 8))
        ttk.Label(batch_btn_frame, text="Ctrl+V 可直接粘贴路径", style="Hint.TLabel").pack(side=LEFT, padx=(8, 0))

        # 启动时恢复上次的批量列表
        for _src in self.cfg.get("batch_sources", []):
            if _src:
                self.batch_listbox.insert(END, _src)
        self._refresh_batch_hint()

    def _on_batch_list_change(self, event=None):
        self._refresh_batch_hint()

    def _refresh_batch_hint(self):
        var = getattr(self, "_batch_hint_var", None)
        if var is None:
            return
        try:
            var.set(self._batch_count_text())
        except Exception:
            pass


    def _build_tab_files(self, notebook):
        tab_files = ttk.Frame(notebook, padding=(18, 16))
        notebook.add(tab_files, text=" 文件预览 ")
        tab_files.columnconfigure(0, weight=1)
        tab_files.rowconfigure(0, weight=1)

        card = self.register_glass(GlassPanel(tab_files, self, radius=16, padding=16))
        card.pack(fill=BOTH, expand=True)
        body = card.inner

        search_frame = ttk.Frame(body)
        search_frame.pack(fill=X, pady=(0, 12))
        search_frame.configure(style="Shell.TFrame")
        ttk.Label(search_frame, text="搜索").pack(side=LEFT, padx=(0, 10))
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=32)
        search_entry.pack(side=LEFT, padx=(0, 12))
        self._search_after_id = None
        search_entry.bind("<KeyRelease>", self._on_search_key)
        ttk.Checkbutton(search_frame, text="区分大小写", variable=self.case_sensitive_var,
                        command=self._filter_file_tree).pack(side=LEFT, padx=(0, 12))
        ttk.Button(search_frame, text="刷新", command=self._refresh_file_tree).pack(side=RIGHT, padx=(8, 0))
        ttk.Button(search_frame, text="导出 CSV", command=self._export_file_list_csv).pack(side=RIGHT)

        tree_frame = ttk.Frame(body)
        tree_frame.pack(fill=BOTH, expand=True)
        tree_frame.configure(style="Shell.TFrame")

        columns = ("name", "size")
        self.file_tree = ttk.Treeview(tree_frame, columns=columns, show="headings",
                                      selectmode="browse")
        self.file_tree.heading("name", text="文件路径")
        self.file_tree.heading("size", text="大小")
        self.file_tree.column("name", width=500, minwidth=200)
        self.file_tree.column("size", width=110, minwidth=70, anchor=E)

        tree_scroll_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.file_tree.yview)
        tree_scroll_x = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.file_tree.xview)
        self.file_tree.configure(yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set)

        self.file_tree.bind("<Button-3>", self._on_file_tree_right_click)
        self.file_tree.bind("<Double-1>", self._on_file_tree_double_click)

        self.file_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll_y.grid(row=0, column=1, sticky="ns")
        tree_scroll_x.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        stats_frame = ttk.Frame(body)
        stats_frame.pack(fill=X, pady=(12, 0))
        stats_frame.configure(style="Shell.TFrame")
        self.file_count_label = ttk.Label(stats_frame, text="文件数  0", style="Status.TLabel")
        self.file_count_label.pack(side=LEFT)
        self.file_size_label = ttk.Label(stats_frame, text="总大小  0 B", style="Status.TLabel")
        self.file_size_label.pack(side=RIGHT)


    def _build_tab_advanced(self, notebook):
        tab_adv = ttk.Frame(notebook, padding=(18, 16))
        notebook.add(tab_adv, text=" 高级选项 ")
        tab_adv.columnconfigure(0, weight=1)
        tab_adv.rowconfigure(0, weight=1)

        # 分组卡片：打包选项
        opt_card = self.register_glass(GlassPanel(tab_adv, self, radius=16,
                                                  padding=13, title="打包选项"))
        opt_card.grid(row=0, column=0, columnspan=3, sticky="nsew", pady=(0, 12))
        f = opt_card.body

        row = 0
        ttk.Label(f, text="打包模式").grid(row=row, column=0, sticky=W, pady=7)
        mode_frame = ttk.Frame(f)
        mode_frame.grid(row=row, column=1, sticky=W, padx=(14, 0), pady=7)
        mode_frame.configure(style="Shell.TFrame")
        ttk.Radiobutton(mode_frame, text="Inno Setup", variable=self.mode_var, value="inno").pack(side=LEFT, padx=(0, 16))
        ttk.Radiobutton(mode_frame, text="NSIS", variable=self.mode_var, value="nsis").pack(side=LEFT, padx=(0, 16))
        ttk.Radiobutton(mode_frame, text="7-Zip SFX", variable=self.mode_var, value="7zip").pack(side=LEFT, padx=(0, 16))
        ttk.Radiobutton(mode_frame, text="ZIP", variable=self.mode_var, value="zip").pack(side=LEFT)

        row += 1
        ttk.Label(f, text="压缩级别").grid(row=row, column=0, sticky=W, pady=7)
        self.compression_var = StringVar(value=self.cfg.get("compression_level", "9"))
        comp_combo = ttk.Combobox(f, textvariable=self.compression_var, width=14, state="readonly",
                                   values=["0", "1", "3", "5", "7", "9"])
        comp_combo.grid(row=row, column=1, sticky=W, padx=(14, 10), pady=7)
        comp_labels = {"0": "存储 · 最快", "1": "最快", "3": "快速",
                       "5": "标准", "7": "最大", "9": "极限 · 最小体积"}
        self.comp_hint_label = ttk.Label(f, text=comp_labels.get(self.compression_var.get(), ""),
                                         style="Hint.TLabel")
        self.comp_hint_label.grid(row=row, column=2, sticky=W, padx=(0, 0), pady=7)

        def _update_comp_label(event=None):
            val = self.compression_var.get()
            self.comp_hint_label.config(text=comp_labels.get(val, ""))
        comp_combo.bind("<<ComboboxSelected>>", _update_comp_label)

        row += 1
        ttk.Label(f, text="应用图标").grid(row=row, column=0, sticky=W, pady=7)
        icon_entry = ttk.Entry(f, textvariable=self.icon_var)
        icon_entry.grid(row=row, column=1, sticky="ew", padx=(14, 10), pady=7)
        ttk.Button(f, text="浏览", command=self._browse_icon).grid(row=row, column=2, pady=7)
        f.columnconfigure(1, weight=1)

        row += 1
        ttk.Label(f, text="排除规则").grid(row=row, column=0, sticky=W, pady=7)
        ttk.Entry(f, textvariable=self.exclude_var).grid(row=row, column=1, columnspan=2,
                                                       sticky="ew", padx=(14, 0), pady=7)

        row += 1
        ttk.Label(f, text="多个规则用逗号分隔，支持通配符，如 *.log、__pycache__、.git",
                  style="Hint.TLabel").grid(row=row, column=0, columnspan=3, sticky=W, pady=(2, 0))

        # Inno Setup 专属选项
        inno_card = self.register_glass(
            GlassPanel(tab_adv, self, radius=16, padding=13, title="安装程序设置",
                       hint="仅 Inno Setup 模式生效"))
        inno_card.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        inno_frame = inno_card.body

        inno_row = 0
        ttk.Label(inno_frame, text="许可证文件").grid(row=inno_row, column=0, sticky=W, pady=5)
        self.license_var = StringVar(value=self.cfg.get("license_file", ""))
        ttk.Entry(inno_frame, textvariable=self.license_var).grid(row=inno_row, column=1, sticky="ew", padx=(14, 10), pady=6)
        ttk.Button(inno_frame, text="浏览", command=self._browse_license).grid(row=inno_row, column=2, pady=6)
        inno_frame.columnconfigure(1, weight=1)

        inno_row += 1
        ttk.Label(inno_frame, text="安装前命令").grid(row=inno_row, column=0, sticky=W, pady=5)
        self.pre_install_var = StringVar(value=self.cfg.get("pre_install_cmd", ""))
        ttk.Entry(inno_frame, textvariable=self.pre_install_var).grid(row=inno_row, column=1, columnspan=2,
                                                                     sticky="ew", padx=(14, 0), pady=6)

        inno_row += 1
        ttk.Label(inno_frame, text="安装后命令").grid(row=inno_row, column=0, sticky=W, pady=5)
        self.post_install_var = StringVar(value=self.cfg.get("post_install_cmd", ""))
        ttk.Entry(inno_frame, textvariable=self.post_install_var).grid(row=inno_row, column=1,
                                                                      sticky="ew", padx=(14, 10), pady=5)
        ttk.Button(inno_frame, text="预览脚本", command=self._preview_iss_script).grid(row=inno_row, column=2, pady=5)

    def _build_tab_history(self, notebook):
        tab_hist = ttk.Frame(notebook, padding=(18, 16))
        notebook.add(tab_hist, text=" 构建历史 ")
        tab_hist.columnconfigure(0, weight=1)
        tab_hist.rowconfigure(0, weight=1)

        card = self.register_glass(GlassPanel(tab_hist, self, radius=16, padding=16))
        card.pack(fill=BOTH, expand=True)
        body = card.inner

        toolbar = ttk.Frame(body)
        toolbar.pack(fill=X, pady=(0, 12))
        toolbar.configure(style="Shell.TFrame")
        ttk.Label(toolbar, text="最近构建", style="Section.TLabel").pack(side=LEFT)
        ttk.Button(toolbar, text="刷新", command=self._refresh_build_history).pack(side=RIGHT)
        ttk.Button(toolbar, text="清除历史", command=self._clear_build_history).pack(side=RIGHT, padx=(0, 8))

        tree_wrap = ttk.Frame(body)
        tree_wrap.pack(fill=BOTH, expand=True)
        tree_wrap.configure(style="Shell.TFrame")

        columns = ("time", "app_name", "mode", "output", "status")
        self.history_tree = ttk.Treeview(tree_wrap, columns=columns, show="headings",
                                      selectmode="browse")
        self.history_tree.heading("time", text="时间")
        self.history_tree.heading("app_name", text="应用名")
        self.history_tree.heading("mode", text="模式")
        self.history_tree.heading("output", text="输出文件")
        self.history_tree.heading("status", text="状态")
        self.history_tree.column("time", width=150, minwidth=110)
        self.history_tree.column("app_name", width=130, minwidth=80)
        self.history_tree.column("mode", width=90, minwidth=60)
        self.history_tree.column("output", width=320, minwidth=150)
        self.history_tree.column("status", width=80, minwidth=60, anchor="center")

        hist_scroll_y = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.history_tree.yview)
        hist_scroll_x = ttk.Scrollbar(tree_wrap, orient="horizontal", command=self.history_tree.xview)
        self.history_tree.configure(yscrollcommand=hist_scroll_y.set, xscrollcommand=hist_scroll_x.set)

        self.history_tree.grid(row=0, column=0, sticky="nsew")
        hist_scroll_y.grid(row=0, column=1, sticky="ns")
        hist_scroll_x.grid(row=1, column=0, sticky="ew")
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        self._refresh_build_history()

    def _build_bottom(self, parent):
        # 操作卡片：按钮 + 进度 + 状态压到两行，给内容区让出垂直空间
        action_panel = self.register_glass(
            GlassPanel(parent, self, radius=20, padding=12))
        action_panel.pack(fill=X, pady=(0, 10))
        row1 = ttk.Frame(action_panel.body)
        row1.pack(fill=X)
        row1.configure(style="Shell.TFrame")

        self.build_button = ttk.Button(row1, text="开始打包", style="Build.TButton",
                                       command=self._start_build)
        self.build_button.pack(side=LEFT)

        btn_group = ttk.Frame(row1)
        btn_group.pack(side=LEFT, padx=(10, 0))
        btn_group.configure(style="Shell.TFrame")
        ttk.Button(btn_group, text="打开输出目录", command=self._open_output_dir).pack(side=LEFT, padx=(0, 8))
        ttk.Button(btn_group, text="预览脚本", command=self._preview_iss_script).pack(side=LEFT, padx=(0, 8))
        ttk.Button(btn_group, text="退出", command=self._on_close).pack(side=LEFT)

        # 进度条与百分比并入同一行，替代原先独占一行
        pct = ttk.Label(row1, textvariable=self.progress_var, style="Status.TLabel",
                        width=5, anchor=E)
        pct.pack(side=RIGHT)
        self.progress_bar = ttk.Progressbar(row1, variable=self.progress_var,
                                            maximum=100, mode="determinate")
        self.progress_bar.pack(side=RIGHT, fill=X, expand=True, padx=(16, 10))

        ttk.Label(action_panel.body, textvariable=self.status_var,
                  style="Status.TLabel").pack(fill=X, pady=(9, 0))

        # 日志卡片：可折叠。默认收起以把垂直空间让给内容区；
        # 打包日志开始输出时会自动展开，用户也可手动切换。
        log_panel = self.register_glass(
            GlassPanel(parent, self, radius=20, padding=12))
        log_panel.pack(fill=X, pady=(0, 2))
        log_body = log_panel.body

        log_toolbar = ttk.Frame(log_body)
        log_toolbar.pack(fill=X)
        log_toolbar.configure(style="Shell.TFrame")
        ttk.Label(log_toolbar, text="运行日志", style="Section.TLabel").pack(side=LEFT)
        # 默认收起：空闲时把垂直空间让给内容区；打包日志一到会自动展开
        self._log_toggle_btn = ttk.Button(log_toolbar, text="展开",
                                          style="Ghost.TButton",
                                          command=self._toggle_log_panel)
        self._log_toggle_btn.pack(side=RIGHT)
        ttk.Button(log_toolbar, text="清空", command=self._clear_log).pack(side=RIGHT, padx=(0, 8))

        self._log_holder = ttk.Frame(log_body)
        self._log_holder.configure(style="Shell.TFrame")
        ttk.Button(self._log_holder, text="保存日志", style="Ghost.TButton",
                   command=self._save_log).pack(side=RIGHT, pady=(6, 0))

        self.log_text = ScrolledText(self._log_holder, height=5, font=self._mono(9),
                                     state=DISABLED, wrap=WORD, bd=0,
                                     highlightthickness=0,
                                     padx=12, pady=10)
        self.log_text.pack(fill=BOTH, expand=True)
        # 收起状态：holder 不 pack
        self._log_expanded = False

    def _toggle_log_panel(self):
        """折叠/展开日志区（默认展开，收起可把空间让给内容区）"""
        try:
            if self._log_expanded:
                self._log_holder.pack_forget()
                self._log_expanded = False
                self._log_toggle_btn.config(text="展开")
            else:
                self._log_holder.pack(fill=BOTH, expand=True)
                self._log_expanded = True
                self._log_toggle_btn.config(text="收起")
        except Exception:
            pass

    def _clear_log(self):
        self.log_text.config(state=NORMAL)
        self.log_text.delete("1.0", END)
        self.log_text.config(state=DISABLED)

    def _save_log(self):
        filepath = filedialog.asksaveasfilename(
            title="保存日志",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if filepath:
            try:
                content = self.log_text.get("1.0", END)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                self._set_status("日志已保存: %s" % filepath)
            except Exception as e:
                messagebox.showerror("错误", "保存日志失败:\n%s" % str(e))


    # ============================================================
    # F. File Browse & Refresh
    # ============================================================
    def _browse_source(self):
        init_dir = self.source_var.get() or ""
        d = filedialog.askdirectory(title="选择源文件夹", initialdir=init_dir if os.path.isdir(init_dir) else None)
        if d:
            self.source_var.set(d)
            if not self.output_var.get():
                self.output_var.set(os.path.join(os.path.dirname(d), "output"))
            self._refresh_file_tree()

    def _browse_output(self):
        init_dir = self.output_var.get() or ""
        d = filedialog.askdirectory(title="选择输出目录", initialdir=init_dir if os.path.isdir(init_dir) else None)
        if d:
            self.output_var.set(d)

    def _browse_icon(self):
        init_dir = os.path.dirname(self.icon_var.get()) if self.icon_var.get() else self.cfg.get("last_icon_dir", "")
        f = filedialog.askopenfilename(
            title="选择图标文件",
            initialdir=init_dir if init_dir and os.path.isdir(init_dir) else None,
            filetypes=[("图标文件", "*.ico"), ("所有文件", "*.*")]
        )
        if f:
            self.icon_var.set(f)
            self.cfg["last_icon_dir"] = os.path.dirname(f)

    # ============================================================
    # G. Tool Detection
    # ============================================================
    def _check_tools(self):
        """探测外部工具，结果同时驱动标题栏摘要与提示气泡。

        状态用 ttk 样式而非 foreground 硬编码颜色，否则切到浅色主题时
        "红色=缺失" 在浅色底上几乎看不清。
        """
        results = []
        for attr, name, path, show_dir in (
                ("inno_label", "Inno Setup", find_inno_setup(), False),
                ("zip7_label", "7-Zip", find_7zip(), True),
                ("nsis_label", "NSIS", find_nsis(), True)):
            if path:
                where = (os.path.dirname(path) if show_dir
                         else os.path.basename(os.path.dirname(path)))
                results.append((name, True, where))
            else:
                results.append((name, False, ""))
            widget = getattr(self, attr, None)
            if widget is not None:
                try:
                    ok = results[-1][1]
                    widget.config(text="%s · %s" % (name, "已就绪" if ok else "未安装"),
                                  style="OK.TLabel" if ok else "Danger.TLabel")
                except Exception:
                    pass

        var = getattr(self, "_tools_summary_var", None)
        if var is not None:
            try:
                var.set("   ".join(
                    ("%s %s" % (n, "OK" if ok else "缺")) for n, ok, _w in results))
            except Exception:
                pass
        detail = getattr(self, "_tools_detail", None)
        if detail is not None:
            try:
                detail.set("\n".join(
                    "%s: %s" % (n, ("已就绪  " + w) if ok else "未安装")
                    for n, ok, w in results))
            except Exception:
                pass

    # ============================================================
    # H. Logging & Progress
    # ============================================================
    def _log(self, msg):
        def _do():
            # 自动展开必须放在 _do 内部：_log 会被后台打包线程调用
            # （如 run_batch 的汇总日志含 "=========="），在调用者线程直接
            # 操作 Tk 控件会跨线程，违反 Tk 非线程安全契约。
            try:
                if not self._log_expanded and "==========" in str(msg):
                    self._toggle_log_panel()
            except Exception:
                pass
            try:
                self.log_text.config(state=NORMAL)
                if "[成功]" in msg:
                    tag = "success"
                elif "[错误]" in msg:
                    tag = "error"
                elif "[警告]" in msg:
                    tag = "warning"
                else:
                    tag = None
                line = msg + "\n"
                if tag:
                    self.log_text.insert(END, line, tag)
                else:
                    self.log_text.insert(END, line)
                self.log_text.see(END)
            finally:
                # 必须回到只读：否则中途异常时日志区会变成用户可编辑
                try:
                    self.log_text.config(state=DISABLED)
                except Exception:
                    pass
        self._call_in_ui(_do)

    def _init_log_tags(self):
        """日志颜色标签（仅初始化一次）"""
        try:
            self.log_text.tag_config("success", foreground="#2e7d32")
            self.log_text.tag_config("error", foreground="#c62828")
            self.log_text.tag_config("warning", foreground="#f57f17")
        except Exception:
            pass

    def _set_progress(self, value):
        def _do():
            self.progress_var.set(value)
        self._call_in_ui(_do)

    def _set_status(self, msg):
        def _do():
            self.status_var.set(msg)
        self._call_in_ui(_do)


    # ============================================================
    # I. Build Process
    # ============================================================
    def _start_build(self):
        # 重入保护：打包进行中（含 Ctrl+B 快捷键触发）直接忽略
        if getattr(self, "_building", False):
            return
        self._building = True
        source = self.source_var.get().strip()
        output = self.output_var.get().strip()
        app_name = self.appname_var.get().strip()
        batch_items = list(self.batch_listbox.get(0, END))

        def abort(msg, is_error=False):
            """校验失败：先保持重入锁，提示后再复位标志。

            复位放在弹窗**之后**，使模态期间 _building 仍为 True，
            此时按 Ctrl+B 会被开头的重入保护拦截，不会叠加对话框。
            """
            try:
                if is_error:
                    messagebox.showerror("错误", msg)
                else:
                    messagebox.showwarning("提示", msg)
            finally:
                self._building = False

        # 仅批量模式时，允许无单一源文件夹
        if not batch_items:
            if not source or not os.path.isdir(source):
                abort("请选择有效的源文件夹（或在批量列表中添加文件夹）")
                return
        if not output:
            abort("请选择输出目录")
            return
        if not app_name:
            abort("请输入应用名称")
            return

        # 输入验证
        valid, msg = validate_app_name(app_name)
        if not valid:
            abort(msg, is_error=True)
            return
        version = self.appver_var.get().strip()
        if not version:
            abort("请输入版本号")
            return
        # 这些字段会拼进 Inno/NSIS 脚本字符串，先做脚本安全校验。
        # 拼接点：AppId/AppName、VersionInfoVersion、Publisher、
        # Excludes / File /x、SetupIconFile、LicenseFile、[Run] Filename。
        # 导入的 .packager 项目与配置文件同样在打包时统一拦截。
        fields = (("应用名称", app_name),
                  ("版本号", version),
                  ("发布者", self.publisher_var.get().strip()),
                  ("排除规则", self.exclude_var.get().strip()),
                  ("图标路径", self.icon_var.get().strip()),
                  ("许可文件", self.license_var.get().strip()),
                  ("安装前命令", self.pre_install_var.get().strip()),
                  ("安装后命令", self.post_install_var.get().strip()))
        for fname, fval in fields:
            ok_field, fmsg = validate_script_field(fval, fname)
            if not ok_field:
                abort(fmsg, is_error=True)
                return

        # 路径安全校验（拒绝双引号与控制字符，防止脚本字符串闭合注入）
        if source:
            try:
                source = validate_path(source)
            except ValueError as e:
                abort("源文件夹路径非法: %s" % e, is_error=True)
                return
        try:
            output = validate_path(output)
        except ValueError as e:
            abort("输出目录路径非法: %s" % e, is_error=True)
            return

        try:
            os.makedirs(output, exist_ok=True)
        except Exception as e:
            abort("无法创建输出目录:\n%s" % e, is_error=True)
            return

        self.cfg["last_source"] = source
        self.cfg["last_output"] = output
        self.cfg["app_name"] = app_name
        self.cfg["app_version"] = version
        self.cfg["publisher"] = self.publisher_var.get().strip()
        self.cfg["default_mode"] = self.mode_var.get()
        self.cfg["icon_path"] = self.icon_var.get().strip()
        self.cfg["exclude_patterns"] = self.exclude_var.get().strip()
        self.cfg["compression_level"] = self.compression_var.get()
        self.cfg["license_file"] = self.license_var.get().strip()
        self.cfg["pre_install_cmd"] = self.pre_install_var.get().strip()
        self.cfg["post_install_cmd"] = self.post_install_var.get().strip()
        # 窗口尺寸只是记忆用，控件异常不应中断打包
        try:
            self.cfg["window_width"] = self.root.winfo_width()
            self.cfg["window_height"] = self.root.winfo_height()
        except Exception:
            pass
        save_config(self.cfg)

        mode = self.mode_var.get()
        build_func = {"inno": build_with_inno, "7zip": build_with_7zip,
                      "zip": build_with_zip, "nsis": build_with_nsis}.get(mode)
        if not build_func:
            abort("未知的打包模式: %s" % mode, is_error=True)
            return

        # 批量打包：如果列表有内容，逐个处理（_building 由其 finally 复位）
        if batch_items:
            self._start_batch_build(batch_items, mode, build_func, app_name)
            return

        # 打包期间禁用按钮，防止重复触发（含 Ctrl+B 快捷键绕过按钮状态）。
        # 这段 UI 准备操作必须整体保护：若中途抛异常（控件已销毁、
        # winfo_width 失败等），_building 与按钮会卡在"禁用"状态。
        mode_names = MODE_NAMES
        start_time = time.time()
        try:
            self._set_build_buttons_state(DISABLED)
            self._set_progress(0)
            self._set_status("打包中...")
            self.log_text.config(state=NORMAL)
            self.log_text.delete("1.0", END)
            self.log_text.config(state=DISABLED)
        except Exception as e:
            self._set_build_buttons_state(NORMAL)
            self._building = False
            # log_text 可能停在 NORMAL（可编辑），必须恢复只读
            try:
                self.log_text.config(state=DISABLED)
            except Exception:
                pass
            messagebox.showerror("错误", "界面初始化失败，无法开始打包:\n%s" % e)
            return

        def _finish_build():
            """打包结束统一恢复状态（回调在主线程执行）"""
            self._building = False
            self._set_build_buttons_state(NORMAL)

        def run_build():
            output_file = None
            try:
                ok, out_path = build_func(self.cfg, self._log, self._set_progress)
                elapsed = time.time() - start_time
                if out_path:
                    output_file = out_path

                def done():
                    # 重新启用按钮
                    _finish_build()
                    if ok:
                        self._set_status("打包完成!")
                        log_msg = "\n========== 打包结果 =========="
                        if output_file and os.path.isfile(output_file):
                            fsize = os.path.getsize(output_file)
                            log_msg += "\n输出文件: %s" % output_file
                            log_msg += "\n文件大小: %s" % format_size(fsize)
                        log_msg += "\n耗时: %.1f 秒" % elapsed
                        log_msg += "\n================================"
                        self._log(log_msg)
                        self._add_build_history(
                            mode_names.get(mode, mode), app_name, output_file, True
                        )
                        self._play_finish_sound(success=True)
                        if not getattr(self, "_closing", False):
                            messagebox.showinfo("完成", "打包成功完成!")
                    else:
                        self._set_status("打包失败")
                        self._add_build_history(
                            mode_names.get(mode, mode), app_name, output_file, False
                        )
                        self._play_finish_sound(success=False)

                self._call_in_ui(done)
            except Exception as e:
                def fail():
                    _finish_build()
                    self._set_status("打包异常: %s" % e)
                    self._play_finish_sound(success=False)
                self._call_in_ui(fail)

        try:
            t = threading.Thread(target=run_build, daemon=True)
            t.start()
        except Exception as e:
            # 线程启动失败时立即恢复按钮
            _finish_build()
            self._log("[错误] 无法启动打包线程: %s" % e)

    def _set_build_buttons_state(self, state):
        """设置打包相关按钮的启用/禁用状态"""
        try:
            self.build_button.config(state=state)
        except Exception:
            pass

    def _play_finish_sound(self, success=True):
        """打包完成提示音（线程安全入口）。

        批量打包在 worker 线程调用本方法，而非 Windows 分支的
        `root.bell()` 属跨线程 Tk 调用，因此统一调度到主线程执行。
        """
        self._call_in_ui(self._play_finish_sound_sync, success)

    def _play_finish_sound_sync(self, success=True):
        """实际发声逻辑，仅在主线程执行"""
        try:
            import winsound
        except ImportError:
            # 非 Windows 平台没有 winsound，用系统铃声兜底
            try:
                self.root.bell()
            except Exception:
                pass
            return
        try:
            if success:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            else:
                winsound.MessageBeep(winsound.MB_ICONHAND)
        except Exception:
            pass

    def _start_batch_build(self, batch_items, mode, build_func, app_name=None):
        """批量打包：逐个源文件夹执行构建"""
        mode_names = MODE_NAMES
        self._building = True
        if app_name is None:
            app_name = self.appname_var.get()
        total = len(batch_items)
        # 与单构建一致：UI 准备段整体保护，异常时复位标志与按钮，
        # 否则 _building 卡 True、按钮永久禁用（此后 Ctrl+B 被重入保护拦截）
        try:
            self._set_build_buttons_state(DISABLED)
            self._set_progress(0)
            self._set_status("批量打包中 (共 %d 个)..." % total)
            self.log_text.config(state=NORMAL)
            self.log_text.delete("1.0", END)
            self.log_text.config(state=DISABLED)
            self._log("========== 批量打包开始 (共 %d 个文件夹) ==========" % total)
        except Exception as e:
            self._set_build_buttons_state(NORMAL)
            self._building = False
            # log_text 可能停在 NORMAL（可编辑），必须恢复只读
            try:
                self.log_text.config(state=DISABLED)
            except Exception:
                pass
            messagebox.showerror("错误", "界面初始化失败，无法开始批量打包:\n%s" % e)
            return

        def run_batch():
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
                    sub_name = "%02d_%s" % (idx + 1,
                                            os.path.basename(src.rstrip("\\/")) or "output")
                    sub_output = os.path.join(self.cfg["last_output"], sub_name)
                    # makedirs 纳入 try，失败只跳过该项而不是线程崩溃
                    try:
                        os.makedirs(sub_output, exist_ok=True)
                    except Exception as e:
                        self._log("[错误] 无法创建输出目录 %s: %s" % (sub_output, e))
                        results.append((src, False, None))
                        continue
                    batch_cfg["last_output"] = sub_output

                    def batch_progress(val):
                        base = (idx / total) * 100
                        per = val / total
                        self._set_progress(int(base + per))

                    self._log("\n--- [%d/%d] %s ---" % (idx + 1, total, src))
                    try:
                        ok, out_path = build_func(batch_cfg, self._log, batch_progress)
                        results.append((src, ok, out_path))
                        if ok:
                            self._add_build_history(mode_names.get(mode, mode),
                                                    app_name, out_path, True)
                        else:
                            self._add_build_history(mode_names.get(mode, mode),
                                                    app_name, None, False)
                    except Exception as e:
                        self._log("[错误] 异常: %s" % e)
                        results.append((src, False, None))

                # 汇总
                ok_count = sum(1 for _, ok, _ in results if ok)
                summary = "\n========== 批量打包结果 ==========\n"
                summary += "成功: %d / %d\n" % (ok_count, total)
                for src, ok, out in results:
                    mark = "[OK]" if ok else "[FAIL]"
                    summary += "  %s %s" % (mark, src)
                    if out:
                        summary += " -> %s" % out
                    summary += "\n"
                summary += "================================"
                self._log(summary)
                self._set_status("批量打包完成: %d/%d 成功" % (ok_count, total))
                self._set_progress(100)
                self._play_finish_sound(success=(ok_count == total))

                def finish():
                    if getattr(self, "_closing", False):
                        return
                    messagebox.showinfo("批量打包完成",
                                        "成功: %d / %d\n详见日志。" % (ok_count, total))
                self._call_in_ui(finish)
            except Exception as e:
                # 意外异常也要写日志，不能让线程静默死亡
                try:
                    self._log("[错误] 批量打包异常终止: %s" % e)
                except Exception:
                    pass
                self._play_finish_sound(success=False)
            finally:
                # 无论成败都恢复按钮，避免永久禁用
                def _reset():
                    self._building = False
                    self._set_build_buttons_state(NORMAL)
                self._call_in_ui(_reset)

        try:
            threading.Thread(target=run_batch, daemon=True).start()
        except Exception as e:
            self._building = False
            self._set_build_buttons_state(NORMAL)
            self._log("[错误] 无法启动批量打包线程: %s" % e)

    # ============================================================
    # J. Build History
    # ============================================================
    def _add_build_history(self, mode, app_name, output_file, success):
        """线程安全入口：批量打包会在 worker 线程调用。

        cfg["build_history"] 的写入与 UI 刷新必须同处主线程，
        否则主线程遍历该 list 时被 worker insert 会抛
        "list changed size during iteration"。
        """
        self._call_in_ui(self._add_build_history_sync, mode, app_name,
                         output_file, success)

    def _add_build_history_sync(self, mode, app_name, output_file, success):
        """实际写入逻辑，仅在主线程执行"""
        history = list(self.cfg.get("build_history", []))
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "app_name": app_name,
            "mode": mode,
            "output": output_file or "",
            "status": "成功" if success else "失败",
        }
        history.insert(0, entry)
        self.cfg["build_history"] = history[:BUILD_HISTORY_MAX]
        save_config(self.cfg)
        self._refresh_build_history()

    def _refresh_build_history(self):
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        # 迭代前快照，避免遍历过程中被改写
        for entry in list(self.cfg.get("build_history", [])):
            self.history_tree.insert("", "end", values=(
                entry.get("time", ""),
                entry.get("app_name", ""),
                entry.get("mode", ""),
                entry.get("output", ""),
                entry.get("status", ""),
            ))

    def _clear_build_history(self):
        self.cfg["build_history"] = []
        save_config(self.cfg)
        self._refresh_build_history()


    # ============================================================
    # K. Project Save / Load
    # ============================================================
    def _get_project_data(self):
        return {
            "last_source": self.source_var.get(),
            "last_output": self.output_var.get(),
            "app_name": self.appname_var.get(),
            "app_version": self.appver_var.get(),
            "publisher": self.publisher_var.get(),
            "default_mode": self.mode_var.get(),
            "icon_path": self.icon_var.get(),
            "exclude_patterns": self.exclude_var.get(),
            "compression_level": self.compression_var.get(),
            "license_file": self.license_var.get(),
            "pre_install_cmd": self.pre_install_var.get(),
            "post_install_cmd": self.post_install_var.get(),
            "batch_sources": list(self.batch_listbox.get(0, END)),
        }

    def _apply_project_data(self, data):
        if "last_source" in data:
            self.source_var.set(data["last_source"])
        if "last_output" in data:
            self.output_var.set(data["last_output"])
        if "app_name" in data:
            self.appname_var.set(data["app_name"])
        if "app_version" in data:
            self.appver_var.set(data["app_version"])
        if "publisher" in data:
            self.publisher_var.set(data["publisher"])
        if "default_mode" in data:
            self.mode_var.set(data["default_mode"])
        if "icon_path" in data:
            self.icon_var.set(data["icon_path"])
        if "exclude_patterns" in data:
            self.exclude_var.set(data["exclude_patterns"])
        if "compression_level" in data:
            self.compression_var.set(data["compression_level"])
        if "license_file" in data:
            self.license_var.set(data["license_file"])
        if "pre_install_cmd" in data:
            self.pre_install_var.set(data["pre_install_cmd"])
        if "post_install_cmd" in data:
            self.post_install_var.set(data["post_install_cmd"])
        if "batch_sources" in data:
            self.batch_listbox.delete(0, END)
            for src in data["batch_sources"]:
                self.batch_listbox.insert(END, src)

    def _save_project(self):
        if self.current_project:
            data = self._get_project_data()
            if save_project(self.current_project, data):
                self._set_status("项目已保存: %s" % self.current_project)
            else:
                messagebox.showerror("错误", "保存项目失败")
            return
        filepath = filedialog.asksaveasfilename(
            title="保存项目",
            defaultextension=".packager",
            filetypes=PROJECT_EXTENSIONS,
            initialdir=os.path.dirname(self.source_var.get()) if self.source_var.get() else None,
        )
        if filepath:
            data = self._get_project_data()
            if save_project(filepath, data):
                self.current_project = filepath
                self.cfg["last_project_file"] = filepath
                self._add_recent_project(filepath)
                save_config(self.cfg)
                self._set_status("项目已保存: %s" % filepath)
            else:
                messagebox.showerror("错误", "保存项目失败")

    def _open_project(self):
        filepath = filedialog.askopenfilename(
            title="打开项目",
            filetypes=PROJECT_EXTENSIONS,
            initialdir=os.path.dirname(self.current_project) if self.current_project else None,
        )
        if filepath:
            self._do_open_project(filepath)

    def _do_open_project(self, filepath):
        data = load_project(filepath)
        if data is None:
            messagebox.showerror("错误", "无法加载项目文件:\n%s" % filepath)
            return
        self._apply_project_data(data)
        self.current_project = filepath
        self.cfg["last_project_file"] = filepath
        self._add_recent_project(filepath)
        save_config(self.cfg)
        self._set_status("项目已加载: %s" % filepath)
        self._refresh_file_tree()


    # ============================================================
    # L. ISS Script Preview
    # ============================================================
    def _preview_iss_script(self):
        mode = self.mode_var.get()
        if mode not in ("inno", "nsis"):
            messagebox.showinfo("提示", "脚本预览仅在 Inno Setup 或 NSIS 模式下可用。")
            return
        cfg = self._get_project_data()
        cfg["app_version"] = self.appver_var.get()
        cfg["publisher"] = self.publisher_var.get()
        if mode == "inno":
            script_content = generate_inno_script(cfg)
            win_title = "Inno Setup 脚本预览"
        else:
            script_content = generate_nsis_script(cfg)
            win_title = "NSIS 脚本预览"

        win = Toplevel(self.root)
        win.title(win_title)
        win.geometry("720x580")
        p = self.palette()
        try:
            win.configure(bg=self.card_bg())
        except Exception:
            pass

        text_widget = ScrolledText(win, font=self._mono(10), wrap=WORD,
                                   bd=0, highlightthickness=0, padx=14, pady=12)
        text_widget.pack(fill=BOTH, expand=True, padx=14, pady=(14, 8))
        text_widget.insert("1.0", script_content)
        text_widget.config(state=DISABLED, bg=rgb_to_hex(p["log"]),
                           fg=p["text"], insertbackground=p["text"])

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill=X, padx=14, pady=(0, 14))
        btn_frame.configure(style="Shell.TFrame")

        def copy_to_clipboard():
            self.root.clipboard_clear()
            self.root.clipboard_append(script_content)
            self._set_status("脚本已复制到剪贴板")

        ttk.Button(btn_frame, text="复制到剪贴板", command=copy_to_clipboard).pack(side=LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="关闭", command=win.destroy).pack(side=LEFT)

    # ============================================================
    # M. Output Directory & About
    # ============================================================
    def _open_output_dir(self):
        output = self.output_var.get().strip()
        if output and os.path.isdir(output):
            try:
                os.startfile(output)
            except Exception:
                messagebox.showinfo("提示", "输出目录: %s" % output)
        else:
            messagebox.showwarning("提示", "输出目录不存在: %s" % output)

    def _show_about(self):
        about_win = Toplevel(self.root)
        about_win.title("关于 %s" % APP_NAME)
        about_win.geometry("460x430")
        about_win.resizable(False, False)
        p = self.palette()
        try:
            about_win.configure(bg=self.card_bg())
        except Exception:
            pass

        content = ttk.Frame(about_win, padding=22)
        content.pack(fill=BOTH, expand=True)
        content.configure(style="Shell.TFrame")

        ttk.Label(content, text=APP_NAME, style="Title.TLabel").pack(pady=(0, 4))
        ttk.Label(content, text="版本 %s" % APP_VERSION,
                  style="Subtitle.TLabel").pack(pady=(0, 14))

        features = [
            "液态玻璃界面，支持深色 / 浅色主题",
            "四种打包模式: Inno Setup / NSIS / 7-Zip SFX / ZIP",
            "排除规则在打包阶段生效，四模式语义一致",
            "批量打包多个文件夹，输出带序号的子目录",
            "文件预览搜索过滤 + 文件类型统计",
            "项目文件保存与加载 / 最近项目",
            "Inno Setup / NSIS 安装脚本预览",
            "构建历史记录，可导出 CSV",
            "文件树右键菜单（复制路径 / 打开）",
            "自动保存防崩溃",
            "键盘快捷键支持 (Ctrl+B / Ctrl+T / F5)",
            "命令行模式打包 (支持 --dry-run --list-modes)",
        ]
        ttk.Label(content, text="功能列表", style="Section.TLabel").pack(
            fill=X, pady=(0, 6))
        for feat in features:
            ttk.Label(content, text="· " + feat, style="Status.TLabel").pack(
                fill=X, anchor=W, pady=1)

        ttk.Separator(content, orient="horizontal").pack(fill=X, pady=12)

        ttk.Label(content, text="项目主页", style="Section.TLabel").pack(
            fill=X, pady=(0, 4))
        for _name, _url in REPO_URLS:
            link = ttk.Label(content, text=_url, style="Link.TLabel",
                             cursor="hand2")
            link.pack(anchor=W)
            # 默认参数绑定，避免闭包捕获最后一个循环变量
            link.bind("<Button-1>", lambda e, u=_url: self._open_url(u))

        ttk.Button(content, text="关闭", command=about_win.destroy).pack(pady=(16, 0))

    def _open_url(self, url):
        import webbrowser
        webbrowser.open(url)

    def _on_close(self):
        # 打包进行中直接 destroy，worker 线程后续的 messagebox/控件回调
        # 会打到已销毁的控件上，抛错并被 except 吞掉（表现为 stderr 刷栈）。
        # 先确认并置位 _closing，让收尾回调跳过弹窗。
        if self._building:
            ok = messagebox.askyesno(
                "确认退出",
                "正在打包中，关闭会中断当前任务。\n确定要退出吗？")
            if not ok:
                return
            self._closing = True
        try:
            project_data = self._get_project_data()
            self.cfg.update(project_data)
            # 保存高级设置
            self.cfg["compression_level"] = self.compression_var.get()
            self.cfg["license_file"] = self.license_var.get()
            self.cfg["pre_install_cmd"] = self.pre_install_var.get()
            self.cfg["post_install_cmd"] = self.post_install_var.get()
            self.cfg["batch_sources"] = list(self.batch_listbox.get(0, END))
            self.cfg["last_project_file"] = self.current_project
            # 窗口尺寸只是记忆项，读取失败不应连带跳过 save_config
            try:
                self.cfg["window_width"] = self.root.winfo_width()
                self.cfg["window_height"] = self.root.winfo_height()
            except Exception:
                pass
            save_config(self.cfg)
        except Exception:
            pass
        # 取消所有 pending 定时器：窗口销毁后若仍触发，会对已销毁控件
        # 做 PIL 渲染并产生无谓告警
        for attr in ("_backdrop_after_id", "_source_after_id", "_search_after_id"):
            aid = getattr(self, attr, None)
            if aid is not None:
                try:
                    self.root.after_cancel(aid)
                except Exception:
                    pass
                setattr(self, attr, None)
        for panel in getattr(self, "_glass_panels", []):
            try:
                if panel._pending is not None:
                    panel.after_cancel(panel._pending)
                    panel._pending = None
            except Exception:
                pass
        self.root.destroy()

    # ============================================================
    # P. v4.0.0 - File Type Statistics Tab
    # ============================================================
    def _build_tab_stats(self, notebook):
        tab_stats = ttk.Frame(notebook, padding=(18, 16))
        notebook.add(tab_stats, text=" 文件统计 ")
        tab_stats.columnconfigure(0, weight=1)
        tab_stats.rowconfigure(0, weight=1)

        card = self.register_glass(GlassPanel(tab_stats, self, radius=16,
                                              padding=16, title="文件类型分布",
                                              hint="按扩展名统计当前源文件夹的文件构成"))
        card.grid(row=0, column=0, sticky="nsew")
        body = card.body

        columns = ("ext", "count", "total_size", "ratio")
        self.stats_tree = ttk.Treeview(body, columns=columns, show="headings",
                                        selectmode="browse")
        self.stats_tree.heading("ext", text="文件扩展名")
        self.stats_tree.heading("count", text="文件数量")
        self.stats_tree.heading("total_size", text="总大小")
        self.stats_tree.heading("ratio", text="占比")
        self.stats_tree.column("ext", width=150, minwidth=100)
        self.stats_tree.column("count", width=100, minwidth=80, anchor="e")
        self.stats_tree.column("total_size", width=120, minwidth=80, anchor="e")
        self.stats_tree.column("ratio", width=100, minwidth=80, anchor="e")

        stats_scroll_y = ttk.Scrollbar(body, orient="vertical", command=self.stats_tree.yview)
        stats_scroll_x = ttk.Scrollbar(body, orient="horizontal", command=self.stats_tree.xview)
        self.stats_tree.configure(yscrollcommand=stats_scroll_y.set, xscrollcommand=stats_scroll_x.set)

        self.stats_tree.pack(side=LEFT, fill=BOTH, expand=True)
        stats_scroll_y.pack(side=RIGHT, fill=Y)
        stats_scroll_x.pack(side=BOTTOM, fill=X)

        stats_bottom = ttk.Frame(body)
        stats_bottom.configure(style="Shell.TFrame")
        stats_bottom.pack(fill=X, pady=(10, 0))
        self.stats_total_files_label = ttk.Label(stats_bottom, text="总文件数  0", style="Status.TLabel")
        self.stats_total_files_label.pack(side=LEFT, padx=(0, 24))
        self.stats_total_size_label = ttk.Label(stats_bottom, text="总大小  0 B", style="Status.TLabel")
        self.stats_total_size_label.pack(side=LEFT, padx=(0, 24))
        self.stats_type_count_label = ttk.Label(stats_bottom, text="文件类型数  0", style="Status.TLabel")
        self.stats_type_count_label.pack(side=LEFT)

    def _update_stats(self):
        for item in self.stats_tree.get_children():
            self.stats_tree.delete(item)
        if not self.scanned_files:
            self.stats_total_files_label.config(text="总文件数  0")
            self.stats_total_size_label.config(text="总大小  0 B")
            self.stats_type_count_label.config(text="文件类型数  0")
            return
        ext_map = {}
        for rel_path, sz in self.scanned_files:
            _, ext = os.path.splitext(rel_path)
            ext = ext.lower() if ext else "(无扩展名)"
            if ext not in ext_map:
                ext_map[ext] = {"count": 0, "size": 0}
            ext_map[ext]["count"] += 1
            ext_map[ext]["size"] += sz
        total_size = sum(v["size"] for v in ext_map.values())
        sorted_exts = sorted(ext_map.items(), key=lambda x: x[1]["size"], reverse=True)
        for ext, info in sorted_exts:
            ratio = "%.1f%%" % (info["size"] / total_size * 100) if total_size > 0 else "0.0%"
            self.stats_tree.insert("", "end", values=(
                ext, info["count"], format_size(info["size"]), ratio
            ))
        self.stats_total_files_label.config(text="总文件数  %d" % len(self.scanned_files))
        self.stats_total_size_label.config(text="总大小  %s" % format_size(total_size))
        self.stats_type_count_label.config(text="文件类型数  %d" % len(ext_map))

    # ============================================================
    # Q. v4.0.0 - Export / Import Config
    # ============================================================
    def _export_config(self):
        filepath = filedialog.asksaveasfilename(
            title="导出配置",
            defaultextension=".json",
            filetypes=[("JSON 配置文件", "*.json"), ("所有文件", "*.*")],
            initialdir=os.path.dirname(self.source_var.get()) if self.source_var.get() else None,
        )
        if filepath:
            try:
                project_data = self._get_project_data()
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(project_data, f, ensure_ascii=False, indent=2)
                self._set_status("配置已导出: %s" % filepath)
            except Exception as e:
                messagebox.showerror("错误", "导出配置失败:\n%s" % str(e))

    def _import_config(self):
        filepath = filedialog.askopenfilename(
            title="导入配置",
            filetypes=[("JSON 配置文件", "*.json"), ("所有文件", "*.*")],
            initialdir=os.path.dirname(self.current_project) if self.current_project else None,
        )
        if filepath:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._apply_project_data(data)
                self._set_status("配置已导入: %s" % filepath)
                self._refresh_file_tree()
            except json.JSONDecodeError:
                messagebox.showerror("错误", "配置文件格式错误:\n%s" % filepath)
            except Exception as e:
                messagebox.showerror("错误", "导入配置失败:\n%s" % str(e))

    # ============================================================
    # R. v4.0.0 - Paste Path & Batch Folder
    # ============================================================
    def _paste_path_to_source(self):
        try:
            clip = self.root.clipboard_get()
            if clip:
                path = clip.strip()
                if os.path.isdir(path):
                    self.source_var.set(path)
                    if not self.output_var.get():
                        self.output_var.set(os.path.join(os.path.dirname(path), "output"))
                    self._refresh_file_tree()
        except Exception:
            pass

    def _browse_license(self):
        f = filedialog.askopenfilename(
            title="选择许可证文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            initialdir=os.path.dirname(self.license_var.get()) if self.license_var.get() else None,
        )
        if f:
            self.license_var.set(f)

    def _add_batch_folder(self):
        d = filedialog.askdirectory(title="选择要添加的文件夹")
        if d:
            current = list(self.batch_listbox.get(0, END))
            if d not in current:
                self.batch_listbox.insert(END, d)
            else:
                messagebox.showinfo("提示", "该文件夹已在列表中")
            self._refresh_batch_hint()

    def _remove_batch_folder(self):
        selected = self.batch_listbox.curselection()
        for idx in reversed(selected):
            self.batch_listbox.delete(idx)
        self._refresh_batch_hint()

    def _clear_batch_folders(self):
        self.batch_listbox.delete(0, END)
        self._refresh_batch_hint()

    def _export_file_list_csv(self):
        """导出文件列表为CSV"""
        if not self.scanned_files:
            messagebox.showinfo("提示", "没有可导出的文件列表，请先扫描。")
            return
        filepath = filedialog.asksaveasfilename(
            title="导出文件列表",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not filepath:
            return
        try:
            import csv
            with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["文件路径", "大小(字节)"])
                for rel_path, sz in self.scanned_files:
                    writer.writerow([rel_path, sz])
            self._set_status("文件列表已导出: %s" % filepath)
            messagebox.showinfo("成功",
                                "已导出 %d 个文件到:\n%s" % (len(self.scanned_files), filepath))
        except Exception as e:
            messagebox.showerror("错误", "导出失败:\n%s" % str(e))

    def _refresh_file_tree(self):
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        source = self.source_var.get()
        if not source or not os.path.isdir(source):
            self.file_count_label.config(text="文件数: 0")
            self.file_size_label.config(text="总大小  0 B")
            self._update_stats()
            return
        self._set_status("正在扫描文件...")
        # 在主线程读取 Tk 变量（tkinter 非线程安全）
        exclude = self.exclude_var.get()
        # 扫描代次：丢弃过期线程的结果，避免快速切换源目录时旧线程覆盖新结果
        self._scan_seq = getattr(self, "_scan_seq", 0) + 1
        scan_seq = self._scan_seq

        def scan_thread():
            files, total_size = scan_folder(source, exclude)

            def update_ui():
                # 代次不一致说明已有更新的扫描在进行，丢弃本次结果
                if scan_seq != getattr(self, "_scan_seq", None):
                    return
                self.scanned_files = files
                self.scanned_size = total_size
                # 大文件夹截断显示，避免界面卡死
                shown = files[:FILE_TREE_MAX_DISPLAY]
                for rel_path, sz in shown:
                    self.file_tree.insert("", "end", values=(rel_path, format_size(sz)))
                if len(files) > FILE_TREE_MAX_DISPLAY:
                    self.file_tree.insert("", "end", values=(
                        "… 还有 %d 个文件未显示 (已截断，搜索可匹配全部)" % (len(files) - FILE_TREE_MAX_DISPLAY),
                        "", ), tags=("truncated",))
                self.file_count_label.config(text="文件数  %d" % len(files))
                self.file_size_label.config(text="总大小  %s" % format_size(total_size))
                self._set_status("扫描完成: %d 个文件, %s" % (len(files), format_size(total_size)))
                self._update_stats()

            self._call_in_ui(update_ui)

        threading.Thread(target=scan_thread, daemon=True).start()

    def _on_search_key(self, event=None):
        """搜索防抖：停止前一次延迟，启动新的300ms延迟"""
        if self._search_after_id is not None:
            self.root.after_cancel(self._search_after_id)
        self._search_after_id = self.root.after(300, self._filter_file_tree)

    def _on_source_changed(self, *args):
        """源路径变化防抖：停止前一次延迟，启动新的500ms延迟"""
        if self._source_after_id is not None:
            self.root.after_cancel(self._source_after_id)
        self._source_after_id = self.root.after(500, self._refresh_file_tree)

    def _on_file_tree_right_click(self, event):
        """文件树右键菜单"""
        import tkinter as _tk
        item = self.file_tree.identify_row(event.y)
        if not item:
            return
        # 截断提示行不是真实文件，不弹菜单
        if "truncated" in (self.file_tree.item(item, "tags") or ()):
            return
        self.file_tree.selection_set(item)
        source = self.source_var.get()
        values = self.file_tree.item(item, "values")
        rel_path = values[0] if values else ""
        full_path = os.path.join(source, rel_path) if source else ""

        menu = _tk.Menu(self.root, tearoff=0)
        menu.add_command(label="复制文件名", command=lambda: self._clip_copy(rel_path))
        menu.add_command(label="复制完整路径", command=lambda: self._clip_copy(full_path))
        menu.add_separator()
        menu.add_command(label="在资源管理器中打开",
                         command=lambda: self._open_in_explorer(full_path))
        menu.add_command(label="在此文件夹中打开终端",
                         command=lambda: self._open_terminal(os.path.dirname(full_path)))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _clip_copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status("已复制: %s" % text)

    def _on_file_tree_double_click(self, event):
        """双击文件树项：打开文件/文件夹"""
        item = self.file_tree.identify_row(event.y)
        if not item:
            return
        # 截断提示行不是真实文件
        if "truncated" in (self.file_tree.item(item, "tags") or ()):
            return
        source = self.source_var.get()
        values = self.file_tree.item(item, "values")
        rel_path = values[0] if values else ""
        full_path = os.path.join(source, rel_path) if source else ""
        if os.path.isfile(full_path):
            try:
                os.startfile(full_path)
            except Exception as e:
                messagebox.showerror("错误", "无法打开文件:\n%s\n%s" % (full_path, str(e)))
        elif os.path.isdir(full_path):
            self._open_in_explorer(full_path)

    def _open_in_explorer(self, path):
        if os.path.isfile(path):
            subprocess.run(["explorer", "/select,", path], check=False)
        elif os.path.isdir(path):
            os.startfile(path)

    def _open_terminal(self, directory):
        if os.path.isdir(directory):
            # 用 cwd 参数指定工作目录，避免把路径拼进命令行导致 cmd /k 重新解析注入
            subprocess.Popen(["cmd", "/k"], cwd=directory,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))

    def _auto_save_project(self):
        """每60秒自动保存项目到临时文件防崩溃丢失"""
        try:
            auto_save_path = os.path.join(tempfile.gettempdir(), "packager_autosave.json")
            data = self._get_project_data()
            with open(auto_save_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        self.root.after(60000, self._auto_save_project)

    # --- File Search/Filter ---
    def _filter_file_tree(self):
        keyword = self.search_var.get()
        case_sensitive = self.case_sensitive_var.get()
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        matched = []
        for rel_path, sz in self.scanned_files:
            if keyword:
                compare_path = rel_path if case_sensitive else rel_path.lower()
                compare_key = keyword if case_sensitive else keyword.lower()
                if compare_key not in compare_path:
                    continue
            matched.append((rel_path, sz))
        shown = matched[:FILE_TREE_MAX_DISPLAY]
        for rel_path, sz in shown:
            self.file_tree.insert("", "end", values=(rel_path, format_size(sz)))
        if len(matched) > FILE_TREE_MAX_DISPLAY:
            self.file_tree.insert("", "end", values=(
                "… 匹配 %d 个，仅显示前 %d 个" % (len(matched), FILE_TREE_MAX_DISPLAY),
                ""), tags=("truncated",))
        self.file_count_label.config(text="文件数: %d / %d" % (len(matched), len(self.scanned_files)))


# ============================================================
# N. CLI Support
# ============================================================
def _ensure_console_encoding():
    """控制台编码无法表示中文时降级为替换字符，避免程序直接崩溃。

    英文 Windows / CI 的 stdout 默认是 cp1252，直接 print 中文会抛
    UnicodeEncodeError（实测 GitHub Actions 上 --list-modes 即崩溃）。
    这里只放宽错误策略、不改动编码本身，中文系统输出不受影响。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="replace")
        except Exception:
            pass


def cli_main():
    _ensure_console_encoding()
    parser = argparse.ArgumentParser(description="%s v%s" % (APP_NAME, APP_VERSION))
    parser.add_argument("--source", "-s", help="源文件夹路径")
    parser.add_argument("--output", "-o", help="输出目录路径")
    parser.add_argument("--name", "-n", help="应用名称", default=None)
    parser.add_argument("--version", "-v", help="版本号", default=None)
    parser.add_argument("--mode", "-m", choices=["inno", "7zip", "zip", "nsis"],
                        default=None, help="打包模式（默认取项目/配置值，无则 inno）")
    parser.add_argument("--list-modes", action="store_true", help="列出所有可用的打包模式")
    parser.add_argument("--dry-run", action="store_true", help="仅预览脚本，不实际打包")
    parser.add_argument("--verbose", "-vv", action="store_true", help="显示详细日志")
    parser.add_argument("--compression", "-c", default=None,
                        help="压缩级别 (0-9)（默认取项目/配置值，无则 9）")
    parser.add_argument("--exclude", "-e", default=None, help="排除规则，逗号分隔")
    parser.add_argument("--project", "-p", help="加载 .packager 项目文件")
    args = parser.parse_args()

    cfg = load_config()
    # 优先加载项目文件，再叠加命令行参数（仅在用户显式指定时覆盖）
    if args.project:
        proj = load_project(args.project)
        if proj is None:
            print("[错误] 无法加载项目文件: %s" % args.project)
            sys.exit(1)
        cfg.update({k: v for k, v in proj.items() if v is not None})
        print("[信息] 已加载项目: %s" % args.project)
    if args.name is not None:
        cfg["app_name"] = args.name
    else:
        cfg.setdefault("app_name", "MyApp")
    if args.version is not None:
        cfg["app_version"] = args.version
    else:
        cfg.setdefault("app_version", APP_VERSION)

    if args.source:
        try:
            cfg["last_source"] = os.path.abspath(validate_path(args.source))
        except ValueError as e:
            print("[错误] 源文件夹路径无效: %s" % e)
            sys.exit(1)
    if args.output:
        try:
            cfg["last_output"] = os.path.abspath(validate_path(args.output))
        except ValueError as e:
            print("[错误] 输出目录路径无效: %s" % e)
            sys.exit(1)
    # 仅显式指定才覆盖项目/配置值
    if args.mode is not None:
        cfg["default_mode"] = args.mode
    else:
        cfg.setdefault("default_mode", "inno")
    if args.compression is not None:
        cfg["compression_level"] = args.compression
    else:
        cfg.setdefault("compression_level", "9")
    if args.exclude is not None:
        cfg["exclude_patterns"] = args.exclude
    # 后续统一使用 cfg 中生效的模式，避免再读 args.mode
    mode = cfg["default_mode"]

    # 脚本字段安全校验（会拼进 Inno/NSIS 脚本）
    for fname in ("app_name", "app_version", "publisher"):
        ok_field, fmsg = validate_script_field(cfg.get(fname, ""), fname)
        if not ok_field:
            print("[错误] %s" % fmsg)
            sys.exit(1)

    if args.list_modes:
        print("可用打包模式:")
        print("  inno  - Inno Setup 安装包（需要安装 Inno Setup）")
        print("  7zip  - 7-Zip SFX 自解压包（需要安装 7-Zip）")
        print("  zip   - ZIP 便携压缩包（无需额外软件）")
        print("  nsis  - NSIS 安装包（需要安装 NSIS）")
        sys.exit(0)

    if args.dry_run:
        if mode == "inno":
            script = generate_inno_script(cfg)
            print(script)
        elif mode == "nsis":
            script = generate_nsis_script(cfg)
            print(script)
        else:
            print("[提示] --dry-run 支持 inno 和 nsis 模式")
        sys.exit(0)

    if not cfg["last_source"] or not os.path.isdir(cfg["last_source"]):
        print("[错误] 请指定有效的源文件夹: --source")
        sys.exit(1)
    if not cfg["last_output"]:
        cfg["last_output"] = os.path.join(os.path.dirname(cfg["last_source"]), "output")
    os.makedirs(cfg["last_output"], exist_ok=True)

    def cli_log(msg):
        # verbose 模式显示全部，否则过滤部分信息
        if args.verbose:
            print(msg)
        else:
            # 非 verbose 模式跳过纯技术细节行（以两个空格开头且不含标记）
            if msg.startswith("  ") and "[" not in msg:
                pass
            else:
                print(msg)

    def cli_progress(val):
        sys.stdout.write("\r进度: %d%%" % val)
        sys.stdout.flush()
        if val >= 100:
            print()

    build_func = {"inno": build_with_inno, "7zip": build_with_7zip,
                  "zip": build_with_zip, "nsis": build_with_nsis}.get(mode)
    if not build_func:
        print("[错误] 未知的打包模式: %s" % mode)
        sys.exit(1)
    print("模式: %s" % mode)
    print("源文件夹: %s" % cfg["last_source"])
    print("输出目录: %s" % cfg["last_output"])
    print("应用名称: %s" % cfg["app_name"])
    print("版本号: %s" % cfg["app_version"])
    print("-" * 50)

    start_time = time.time()
    ok, out_file = build_func(cfg, cli_log, cli_progress)
    elapsed = time.time() - start_time

    if ok:
        print("\n打包成功!")
        if out_file and os.path.isfile(out_file):
            fsize = os.path.getsize(out_file)
            print("输出文件: %s" % out_file)
            print("文件大小: %s" % format_size(fsize))
        print("耗时: %.1f 秒" % elapsed)
    else:
        print("\n打包失败!")
        sys.exit(1)


# ============================================================
# O. Entry Point
# ============================================================
def _show_splash(root):
    """在主窗口上展示启动画面，2秒后自动关闭"""
    root.withdraw()
    w, h = 360, 220
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    splash = Toplevel(root)
    splash.overrideredirect(True)
    splash.title("启动中")
    splash.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, (sh - h) // 2))
    splash.configure(bg="#2b2b2b")
    Label(splash, text=APP_NAME, font=("Microsoft YaHei UI", 18, "bold"),
          bg="#2b2b2b", fg="#e0e0e0").pack(pady=(40, 8))
    Label(splash, text="v%s" % APP_VERSION, font=("Microsoft YaHei UI", 11),
          bg="#2b2b2b", fg="#a0a0a0").pack(pady=(0, 8))
    Label(splash, text="加载中...", font=("Microsoft YaHei UI", 10),
          bg="#2b2b2b", fg="#808080").pack()
    # 进度条动画
    style = ttk.Style(splash)
    style.configure("Splash.Horizontal.TProgressbar", background="#4b6eaf",
                     troughcolor="#3c3f41")
    bar = ttk.Progressbar(splash, style="Splash.Horizontal.TProgressbar",
                          length=260, mode="determinate", maximum=100)
    bar.pack(pady=(12, 0))
    splash.update()
    # 600ms 后启动渐变动画
    def _animate(value):
        if value <= 100:
            bar["value"] = value
            splash.after(12, _animate, value + 2)
    splash.after(300, _animate, 0)
    return splash


def main():
    # 先确保控制台能安全输出中文：CLI 分支走 cli_main 也会再调一次（幂等），
    # GUI 分支未捕获异常的 traceback 同样含中文，缺这步会二次抛
    # UnicodeEncodeError 把真实错误掩盖掉。
    _ensure_console_encoding()
    if len(sys.argv) > 1:
        cli_main()
    else:
        root = Tk()
        splash = _show_splash(root)

        def _finish_splash():
            splash.destroy()
            root.deiconify()
            app = PackagerApp(root)

        splash.after(2000, _finish_splash)
        root.mainloop()


if __name__ == "__main__":
    main()


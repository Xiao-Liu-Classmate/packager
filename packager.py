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
from pathlib import Path
from datetime import datetime
from fnmatch import fnmatch

# NOTE: 界面层已迁移到 PySide6 / Qt 6，见 packager_ui.py。
# 本模块只保留打包、校验、配置、脚本生成等核心逻辑，不再依赖 tkinter。
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


# ------------------------------------------------------------
# 注：原先此处的「Liquid Glass 设计系统」（Pillow 像素渲染、色板、
# 圆角遮罩、手写高光）已随界面层一并迁移到 PySide6/Qt，见 packager_ui.py。
# 本模块现在只保留打包、校验、配置、脚本生成等核心逻辑，不再有任何渲染代码。


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

def main():
    """入口：无参数走 GUI 界面，有参数走 CLI。

    界面层延迟导入：CLI 分支（如 CI 的 --list-modes）不需要 PySide6，
    这样 GUI 依赖缺失时命令行模式仍然可用。
    """
    _ensure_console_encoding()
    if len(sys.argv) > 1:
        cli_main()
        return
    try:
        import packager_ui
        from PySide6 import QtWidgets
    except ImportError as exc:
        sys.stderr.write(
            "[错误] 缺少图形界面依赖 PySide6，请先安装:\n"
            "    pip install PySide6>=6.10\n"
            "原始错误: %s\n" % exc)
        sys.exit(1)
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    win = packager_ui.PackagerWindow(sys.modules[__name__])
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

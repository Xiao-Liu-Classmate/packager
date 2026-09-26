# 一键打包工具 v4.3.0

把文件夹打包成 `.exe` 安装包，带图形界面与命令行两种用法。

## 快速开始

1. 双击 `start.bat` 启动图形界面
2. 选择要打包的文件夹与输出目录
3. 填写应用名称、版本号，选择打包模式
4. 点击"开始打包"

也可直接运行：

```powershell
python packager.py
```

## 打包模式

| 模式 | 产物 | 依赖 |
|------|------|------|
| Inno Setup | 标准安装程序 (`.exe`) | [Inno Setup](https://jrsoftware.org/isinfo.php) |
| NSIS | 标准安装程序 (`.exe`) | [NSIS](https://nsis.sourceforge.io) |
| 7-Zip SFX | 自解压安装程序 (`.exe`) | [7-Zip](https://7-zip.org) |
| ZIP | 便携压缩包 (`.zip`) | 无（内置） |

四种模式的**排除规则语义一致**，均在打包阶段生效：
- Inno：`Source:` 行的 `Excludes:`
- NSIS：`File /r /x <pattern>`
- 7-Zip：`-xr!<pattern>` 与 `-xr!*<pattern>`（覆盖嵌套目录）
- ZIP：按 `scan_folder` 过滤后逐文件写入

## 主要功能

- 四种打包模式，未安装对应工具时给出明确提示
- 文件树预览、大小统计、关键字过滤（超过 5000 条自动截断并提示）
- 排除规则支持通配符（`*.log`、`__pycache__`、`.git` …）
- 自定义安装前/安装后命令、许可文件、安装图标、压缩等级
- 批量打包：向批量列表添加多个源文件夹，逐个生成带序号前缀的子输出
- 打包历史（可导出 CSV）、日志着色与关键字高亮、双击打开输出目录
- 主题切换（深色/浅色），快捷键 `Ctrl+T`
- 项目文件：保存/加载 `.packager`，支持导出与导入配置
- 打包完成提示音（Windows `winsound`，其他平台回退 `bell`）
- 打包期间禁用按钮 + 重入保护，防止重复触发
- 日志右键菜单、文件树右键菜单、右键打开终端

### 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Ctrl+B` | 开始打包 |
| `Ctrl+T` | 切换深色/浅色主题 |
| `Ctrl+O` | 选择源文件夹 |
| `Ctrl+Shift+T` | 打开终端 |

## 命令行用法

```powershell
python packager.py --source "C:\MyApp" --output "D:\output" `
    --name "我的应用" --version "1.0.0" --mode inno
```

| 参数 | 说明 |
|------|------|
| `--source`, `-s` | 源文件夹路径 |
| `--output`, `-o` | 输出目录路径 |
| `--name`, `-n` | 应用名称 |
| `--version`, `-v` | 版本号 |
| `--mode`, `-m` | 打包模式：`inno` / `nsis` / `7zip` / `zip` |
| `--compression`, `-c` | 压缩等级 (0-9) |
| `--exclude`, `-e` | 排除规则，逗号分隔 |
| `--project`, `-p` | 加载 `.packager` 项目文件 |
| `--dry-run` | 仅预览脚本，不实际打包（`inno` / `nsis`） |
| `--list-modes` | 列出所有可用打包模式 |
| `--verbose`, `-vv` | 显示详细日志 |

命令行显式给出的参数**优先级高于**项目文件与配置；未给出的参数才回退到项目/配置值。

## 输入校验与安全

- 路径校验：拒绝双引号与控制字符（`0x00`–`0x1F`），`& ; | ' ` 等合法字符不误拒
- 应用名称、版本号、发布者、排除规则、图标/许可路径、安装前后命令
  在写入脚本前统一走 `validate_script_field`，拦截 `"`、`$`、`{`、`}`、换行等
  可能闭合脚本字符串或触发 NSIS 变量/Inno 常量解析的字符
- 版本号只接受 ASCII 数字，每段钳制到 `0`–`65535`（Inno `VersionInfoVersion` 要求）
- 配置文件原子写入（`.tmp` + `os.replace`），损坏时自动备份为 `.bak`
- ZIP 产物先写 `.part`，成功后再原子替换；失败自动清理，不截断已有产物
- 所有外部工具均以参数列表方式调用，不经过 shell

## 测试

```powershell
python -m pytest test_packager.py -v
```

覆盖版本号规范化、路径/脚本字段校验、四种模式的排除语义、CLI 参数优先级、
配置原子写与损坏恢复、ZIP 产物完整性、GUI 重入保护与按钮状态恢复。

## 系统要求

- Windows 10/11
- Python 3.8+
- 视打包模式另需 Inno Setup / NSIS / 7-Zip

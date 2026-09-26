@echo off
chcp 65001 >nul 2>&1
title 打包程序
cd /d "%~dp0"

python packager.py 2>nul
if errorlevel 1 (
    python3 packager.py 2>nul
    if errorlevel 1 (
        py packager.py 2>nul
        if errorlevel 1 (
            echo.
            echo  [错误] 未找到 Python，请先安装 Python 3.8+
            echo  下载地址: https://www.python.org/downloads/
            echo.
            pause
        )
    )
)

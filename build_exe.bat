@echo off
REM ============================================================
REM  Folder Packager - onefile exe build script
REM  Output: dist\packager.exe  (the asset uploaded to GitHub/Gitee Release)
REM
REM  NOTE: keep comments ASCII-only. cmd.exe mis-parses multi-line
REM        comments that contain non-ASCII characters, which causes
REM        stray "is not recognized" errors. Chinese is only used in
REM        echo output, which is verified to work.
REM ============================================================
setlocal
pushd "%~dp0"

REM Build parameters live in packager.spec, which is the single source
REM of truth. When a spec file exists PyInstaller IGNORES the
REM --onefile/--windowed/--name command line options, so do not try to
REM change build behavior here -- edit the spec instead.
set SPEC_FILE=packager.spec

echo [1/5] Checking Python...
python --version 2>nul || py -3 --version 2>nul || goto :no_python

echo [2/5] Installing pinned build dependencies...
python -m pip install -r requirements-build.txt --quiet || goto :install_failed

REM Verify the exact installed version. pip may have failed silently or
REM the machine may already carry a different version; either way this
REM must fail closed rather than build with an unpinned toolchain.
REM Plain %VAR% is correct here: the variable is assigned BEFORE the
REM if-block, so the block is expanded with the final value.
set "INSTALLED="
for /f "delims=" %%v in ('python -c "import PyInstaller;print(PyInstaller.__version__)" 2^>nul') do set "INSTALLED=%%v"
if not "%INSTALLED%"=="6.22.2" (
    echo [ERROR] PyInstaller version mismatch: expected 6.22.2, got "%INSTALLED%"
    echo         Run: python -m pip install -r requirements-build.txt
    goto :version_mismatch
)

REM Verify the GUI engine version too: the UI layer is PySide6/Qt, and a
REM different Qt minor changes stylesheet parsing and platform behavior.
REM Fail closed rather than building against an unpinned GUI toolchain.
set "QTV="
for /f "delims=" %%v in ('python -c "import PySide6;print(PySide6.__version__)" 2^>nul') do set "QTV=%%v"
if not "%QTV%"=="6.10.3" (
    echo [ERROR] PySide6 version mismatch: expected 6.10.3, got "%QTV%"
    echo         Run: python -m pip install -r requirements-build.txt
    goto :version_mismatch
)

echo [3/5] Running unit tests (build is blocked on failure)...
REM Both suites: test_packager.py (core logic) and test_ui.py (Qt UI layer).
REM test_ui.py sets QT_QPA_PLATFORM=offscreen itself, so it needs no display.
python -m pytest test_packager.py test_ui.py -q || goto :test_failed

echo [4/5] Building exe...
REM Remove any previous artifact first. --clean only clears caches and
REM build\, NOT dist\. Without this, a build that fails without a real
REM error would leave the PREVIOUS exe in place and we would print its
REM size and SHA256 as if it were the new one -- corrupting Release notes.
REM del fails silently when the file is locked (e.g. packager.exe is still
REM running), so the deletion must be verified rather than assumed.
if exist "dist\packager.exe" (
    del /q "dist\packager.exe"
    if exist "dist\packager.exe" (
        echo [ERROR] Cannot remove dist\packager.exe -- the file is in use.
        echo         Close any running packager.exe and retry.
        goto :stale_artifact
    )
)
python -m PyInstaller --clean --noconfirm "%SPEC_FILE%"
if errorlevel 1 goto :build_failed

if not exist "dist\packager.exe" goto :missing_exe

echo [5/5] Build finished
echo Output: "%cd%\dist\packager.exe"
for %%f in ("dist\packager.exe") do echo Size: %%~zf bytes
echo SHA256:
REM Use Python for the hash: PowerShell Get-FileHash is unavailable in
REM some environments, and this script already depends on python.
python -c "import hashlib,sys;print('  '+hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "dist\packager.exe"
echo Python: 
python -c "import sys;print('  '+sys.version.split()[0])"
echo Qt:
REM 必须显式 from PySide6 import QtCore —— `import PySide6` 不会自动
REM 导入子模块，PySide6.QtCore 属性访问会抛 AttributeError
python -c "from PySide6 import QtCore;import PySide6;print('  PySide6 '+PySide6.__version__+' / Qt '+QtCore.qVersion())" 2>nul
echo.
echo Tip: put the SHA256 above plus the Python version into the
echo       Release notes; the hash only matches THIS build.
popd
endlocal
exit /b 0

:no_python
echo [ERROR] Python not found. Install Python 3.10+ and add it to PATH.
echo         PySide6 6.10 requires Python >= 3.9; 3.10+ is the supported floor.
popd
endlocal
exit /b 1

:install_failed
echo [ERROR] Failed to install build dependencies.
echo         Check network / proxy / permissions, then retry.
popd
endlocal
exit /b 1

:version_mismatch
echo [ERROR] Build aborted to keep the toolchain reproducible.
popd
endlocal
exit /b 1

:test_failed
echo [ERROR] Unit tests failed, build aborted.
popd
endlocal
exit /b 1

:build_failed
echo [ERROR] PyInstaller build failed.
popd
endlocal
exit /b 1

:stale_artifact
popd
endlocal
exit /b 1

:missing_exe
echo [ERROR] dist\packager.exe was not produced.
popd
endlocal
exit /b 1

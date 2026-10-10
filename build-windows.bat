@echo off
rem Windows 上本地打包：双击或在 cmd 里运行 build-windows.bat  ->  dist\sight-deploy-<版本>-windows-x64.exe
rem 需要 Python 3.8+（python.org 安装时勾选 "Add python.exe to PATH"）
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 build.py %*
) else (
  python build.py %*
)
set RC=%errorlevel%
if not "%RC%"=="0" echo 打包失败，退出码 %RC%
if "%CI%"=="" pause
exit /b %RC%

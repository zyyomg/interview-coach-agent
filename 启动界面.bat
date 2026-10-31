@echo off
chcp 936 >nul
title 面试陪练 Agent
cd /d "%~dp0"

set "PORT=8000"
set "URL=http://127.0.0.1:%PORT%/"

rem ---------- 已经在运行：只打开浏览器，不重复起服务 ----------
curl -s -m 2 "%URL%api/health" 2>nul | findstr /i "version" >nul 2>nul
if not errorlevel 1 (
    echo.
    echo   服务已经在运行，直接打开 %URL%
    if /i not "%~1"=="noopen" start "" "%URL%"
    exit /b
)

rem ---------- 找 python（优先 python，其次 py 启动器）----------
set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 set "PY=py"
)
if not defined PY (
    echo.
    echo   没有找到 python 命令。
    echo   请先装 Python 3.9 或更高版本，安装时勾选 "Add python.exe to PATH"。
    echo.
    pause
    exit /b
)

echo.
echo   面试陪练 Agent   ·   关掉这个窗口 = 停止服务
echo   浏览器会自动打开，没反应就手动开 %URL%
echo   没配 Key 时是演示模式；Key 可写进同目录的 .env（见 README 第一节）
echo.

if /i "%~1"=="noopen" (
    %PY% server.py --no-open
) else (
    %PY% server.py
)

echo.
echo   服务已停止，会话记录在 runs\ 目录。
pause

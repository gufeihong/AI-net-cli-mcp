@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 优先用仓库内的 venv，其次用系统 python
set PY=python
if exist "%~dp0..\.venv\Scripts\pythonw.exe" set PY=%~dp0..\.venv\Scripts\pythonw.exe

if not exist "logs" mkdir logs

echo 正在启动网络设备实时控制台 ...
start "net-console" /min %PY% app.py
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8199
echo.
echo 控制台地址： http://127.0.0.1:8199
echo 服务运行在最小化的 "net-console" 窗口里，关闭该窗口即停止。
timeout /t 3 /nobreak >nul

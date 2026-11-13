@echo off
chcp 65001 >nul
echo 正在停止网络控制台 ...
taskkill /FI "WINDOWTITLE eq net-console*" /T /F >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8199" ^| findstr "LISTENING"') do taskkill /PID %%p /F >nul 2>&1
echo 已停止。
timeout /t 2 /nobreak >nul

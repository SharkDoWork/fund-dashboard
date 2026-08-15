@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title 基金看板服务管理
set BASE=%~dp0
set PY=python
set LOG=%BASE%data\bat.log
echo %date% %time% script start > "%LOG%"

rem ---------- 探测 Python(平铺结构, 避免嵌套块内 %errorlevel% 预展开陷阱) ----------
where python >nul 2>&1
if not errorlevel 1 goto py_ok
where py >nul 2>&1
if not errorlevel 1 goto py_launcher
echo [%date% %time%] python not found >> "%LOG%"
echo [错误] 未找到 Python。请先安装 Python 3.12+ 并加入 PATH。
pause
exit /b 1
:py_launcher
set PY=py -3
echo [%date% %time%] use py -3 >> "%LOG%"
:py_ok
echo [%date% %time%] PY=%PY% >> "%LOG%"

rem ---------- 主菜单 ----------
:menu
echo [%date% %time%] enter menu >> "%LOG%"
cls
echo ====================================
echo     基金看板服务管理    端口 8123
echo ====================================
set "SRV_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8123" ^| findstr "LISTENING"') do set SRV_PID=%%p
if defined SRV_PID (
  echo    [状态] 运行中   PID=!SRV_PID!   http://127.0.0.1:8123
) else (
  echo    [状态] 已停止
)
echo ------------------------------------
echo    1. 启动服务
echo    2. 关闭服务
echo    3. 打开看板
echo    0. 退出
echo ------------------------------------
set /p CHOICE=   请选择: 
echo [%date% %time%] choice=%CHOICE% >> "%LOG%"
if "%CHOICE%"=="1" goto start_srv
if "%CHOICE%"=="2" goto stop_srv
if "%CHOICE%"=="3" goto open_dash
if "%CHOICE%"=="0" exit /b
goto menu

:start_srv
echo [%date% %time%] start_srv >> "%LOG%"
set "SRV_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8123" ^| findstr "LISTENING"') do set SRV_PID=%%p
if defined SRV_PID (
  echo.
  echo   [提示] 服务已在运行 PID=!SRV_PID!, 无需重复启动。
  pause
  goto menu
)
echo   正在启动服务...
"%PY%" -c "import sys; sys.path.insert(0, r'%BASE%'); import fund_db; fund_db.init_db(); fund_db.migrate_json_files()" >nul 2>&1
if not exist "%BASE%dashboard.html" (
  "%PY%" "%BASE%make_dashboard.py" >nul 2>&1
)
if "%PY%"=="python" (
  powershell -NoProfile -Command "Start-Process -FilePath 'python' -ArgumentList 'live_server.py' -WorkingDirectory '%BASE%' -WindowStyle Hidden"
) else (
  powershell -NoProfile -Command "Start-Process -FilePath 'py' -ArgumentList '-3','live_server.py' -WorkingDirectory '%BASE%' -WindowStyle Hidden"
)
timeout /t 3 /nobreak >nul
set "SRV_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8123" ^| findstr "LISTENING"') do set SRV_PID=%%p
if defined SRV_PID (
  echo   启动成功! PID=!SRV_PID!
) else (
  echo   服务启动中或失败, 稍后访问 http://127.0.0.1:8123 检查
)
pause
goto menu

:stop_srv
echo [%date% %time%] stop_srv >> "%LOG%"
set "SRV_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8123" ^| findstr "LISTENING"') do set SRV_PID=%%p
if not defined SRV_PID (
  echo.
  echo   [提示] 服务未在运行。
  pause
  goto menu
)
echo   正在关闭服务 PID=!SRV_PID! ...
taskkill /F /PID !SRV_PID! >nul 2>&1
timeout /t 1 /nobreak >nul
set "SRV_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8123" ^| findstr "LISTENING"') do set SRV_PID=%%p
if defined SRV_PID (
  echo   [警告] 端口仍被 PID=!SRV_PID! 占用, 可能关闭失败, 请重试。
) else (
  echo   服务已关闭。
)
pause
goto menu

:open_dash
echo [%date% %time%] open_dash >> "%LOG%"
start "" http://127.0.0.1:8123
goto menu

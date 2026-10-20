# ============================================================================
#  VOC洞察平台 · 公网暴露一键脚本
#
#  用法：
#      公网访问.bat              自动：优先命名隧道（永久地址），没有则快速隧道（临时）
#      公网访问.bat quick        强制用快速隧道（trycloudflare 临时地址）
#      公网访问.bat named        强制用命名隧道
#      公网访问.bat stop         全部停止
#
#  原理：本地平台监听 127.0.0.1，隧道进程把它反向代理出去并自动配好 HTTPS。
#        公网只暴露隧道的中转地址，**不会**把你的 8080 端口直接开到公网。
#
#  永久地址：双击「配置命名隧道.bat」，按提示输入你买好的域名。
#
#  前置：设置好口令（否则远程访问会被拒）
#      setx VOC_PASSWORD "你的管理员口令"
#      setx VOC_GUEST_PASSWORD "给客户看的只读口令"
# ============================================================================
@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "CF=C:\Program Files (x86)\cloudflared\cloudflared.exe"
if exist "%CF%" goto :have_cf
where cloudflared >nul 2>&1 && (set "CF=cloudflared" & goto :have_cf)
echo [错误] 没找到 cloudflared，请先执行：
echo    winget install --id Cloudflare.cloudflared
echo.
pause & exit /b 1

:have_cf
if /i "%~1"=="stop" goto :stop

REM ---------- 选择模式 ----------
set "MODE=%~1"
if "%MODE%"=="" (
  if exist "%USERPROFILE%\.cloudflared\config.yml" (
    set "MODE=named"
  ) else (
    set "MODE=quick"
  )
)

if "%MODE%"=="named" (
  echo ============================================================
  echo   公网访问 · 命名隧道（永久地址）
  echo ============================================================
  echo.
  for /f "tokens=*" %%a in ('findstr /c:"hostname:" "%USERPROFILE%\.cloudflared\config.yml" 2^>nul') do (
    set "HN=%%a"
    echo    公网地址 : https://!HN:*hostname: =!
  )
) else (
  echo ============================================================
  echo   公网访问 · 快速隧道（地址每次重启都会变）
  echo ============================================================
  echo    想要永久地址？双击「配置命名隧道.bat」
)

if "%VOC_PASSWORD%"=="" (
  echo.
  echo    [提醒] 未设置 VOC_PASSWORD，远程访问一律被拒。
  echo           现在就设置： setx VOC_PASSWORD "你的口令"
)

REM ---------- 启动平台 ----------
echo.
echo [1/2] 启动平台…
if not exist "VOCData" mkdir "VOCData"
if "%VOC_PYTHON%"=="" if exist "%~dp0python.exe" set "VOC_PYTHON=%~dp0python.exe"
if "%VOC_PYTHON%"=="" if exist "%~dp0dist\VOC洞察平台\VOC洞察平台.exe" set "VOC_PYTHON=%~dp0dist\VOC洞察平台\VOC洞察平台.exe"
if "%VOC_PYTHON%"=="" (
  set "VOC_PYTHON=python"
  where python >nul 2>&1 || (
    echo   [错误] 没找到 Python，也没有 python.exe 或打包的 exe。
    echo          请安装 Python 3.9+，或使用 dist 目录里的打包版本。
    echo.
    pause & exit /b 1
  )
)

netstat -ano | findstr ":%VOC_PORT%" | findstr LISTENING >nul 2>&1
if not errorlevel 1 (
  echo   平台已在运行。
) else (
  start "VOC平台" cmd /k ""%VOC_PYTHON%" "%~dp0web\serve.py" --host 127.0.0.1 --port %VOC_PORT% --data-dir "%~dp0VOCData" --no-browser"
  timeout /t 4 >nul
  echo   已启动。
)

REM ---------- 启动隧道 ----------
echo.
echo [2/2] 启动隧道…
echo.
if /i "%MODE%"=="named" (
  sc query cloudflared >nul 2>&1
  if not errorlevel 1 (
    echo   检测到 cloudflared 服务，正在启动…
    net start cloudflared >nul 2>&1
    timeout /t 5 >nul
    start "" "https://!HN:*hostname: =!"
    echo.
    echo   已自动打开浏览器。
    goto :done
  )
  echo   未安装服务，前台运行中（关掉本窗口即停止）…
  %CF% tunnel --no-autoupdate run
  goto :done
)

echo   正在申请临时地址，请稍候…
%CF% tunnel --no-autoupdate --url http://127.0.0.1:%VOC_PORT%

:done
echo.
echo 停止：关闭本窗口，或另开一个窗口运行「公网访问.bat stop」
pause
exit /b 0

:stop
echo 停止平台与隧道…
taskkill /f /im cloudflared.exe >nul 2>&1
sc stop cloudflared >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%VOC_PORT%" ^| findstr LISTENING') do taskkill /f /pid %%p >nul 2>&1
echo 完成。
pause
exit /b 0
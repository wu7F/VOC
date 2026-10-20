@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM  把 cloudflared 装成 Windows 开机自启服务
REM
REM  效果：开机自动拉起隧道；隧道崩溃会被服务自动重启；
REM        不用再手动跑「公网访问.bat」
REM
REM  【必须用管理员身份运行】
REM     右键本文件 →「以管理员身份运行」
REM ============================================================
net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo  [!] 需要管理员权限。
  echo      请右键本文件 →「以管理员身份运行」
  echo.
  pause & exit /b 1
)

set "CF=C:\Program Files (x86)\cloudflared\cloudflared.exe"
if not exist "%CF%" (
  echo [错误] 没找到 cloudflared
  pause & exit /b 1
)

echo [1/3] 停止已存在的 cloudflared 服务（若有）
sc stop cloudflared >nul 2>&1
sc delete cloudflared >nul 2>&1

echo [2/3] 安装服务…
"%CF%" service install voc
if errorlevel 1 (
  echo.
  echo [错误] 安装失败。可能是隧道名写错了，先运行「cloudflared tunnel list」确认。
  pause & exit /b 1
)

echo [3/3] 启动服务…
sc start cloudflared >nul 2>&1
timeout /t 5 >nul

sc query cloudflared | findstr /c:"STATE"
echo.
echo 完成。隧道已注册为开机自启服务。
echo 公网地址： https://voc.voctyc.cn
echo.
echo 常用命令：
echo   查看状态  sc query cloudflared
echo   重启      net stop cloudflared ^& net start cloudflared
echo   停止      net stop cloudflared
echo.
pause
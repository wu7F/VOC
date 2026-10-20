@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM  VOC洞察平台 · 命名隧道一键配置
REM
REM  作用：把公网地址从"每次都变的 trycloudflare 临时网址"
REM        变成"永久不变的 https://子域.你的域名"
REM
REM  前置条件：
REM    1) 域名已购买
REM    2) 域名的 NS 已指向 Cloudflare（控制台会给两个 nameserver）
REM    3) 平台正在运行（start.bat）
REM
REM  用法：
REM    双击本文件，按提示输入域名
REM    或：配置命名隧道.bat vocinsight.com voc 8080
REM ============================================================
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-tunnel.ps1" %*
if errorlevel 1 (
  echo.
  pause
)
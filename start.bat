@echo off
REM 让控制台按 UTF-8 显示，中文才不会花屏（否则 GBK 下部分字符会直接崩掉）
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
REM ============================================================
REM  VOC洞察平台 · Windows 启动脚本
REM
REM  首次使用先设置两个环境变量（当前窗口有效，永久设置见 README）：
REM      set VOC_PASSWORD=你的管理员口令
REM      set VOC_GUEST_PASSWORD=给客户看的只读口令
REM
REM  双击本文件即可启动；停止按 Ctrl+C 或关掉窗口。
REM ============================================================
setlocal

if "%VOC_DATA_DIR%"=="" set VOC_DATA_DIR=%~dp0VOCData
if "%VOC_PORT%"=="" set VOC_PORT=8080
if "%VOC_HOST%"=="" set VOC_HOST=0.0.0.0

REM 优先用同目录下的 python.exe（打包版不需要这行）
if "%VOC_PYTHON%"=="" (
  if exist "%~dp0python.exe" set VOC_PYTHON=%~dp0python.exe
)

if not exist "%VOC_DATA_DIR%" mkdir "%VOC_DATA_DIR%"

echo ============================================================
echo   VOC洞察平台
echo ============================================================
echo   数据目录 : %VOC_DATA_DIR%
echo   访问地址 : http://127.0.0.1:%VOC_PORT%   （本机）
echo               http://%COMPUTERNAME%:%VOC_PORT%  （局域网）
if "%VOC_PASSWORD%"=="" (
  echo   [警告] 未设置 VOC_PASSWORD —— 远程访问为只读，无法登录管理
) else (
  echo   管理员口令 : 已设置
)
if not "%VOC_GUEST_PASSWORD%"=="" echo   访客口令   : 已设置（只读）
echo ============================================================
echo.

if "%VOC_PYTHON%"=="" (
  echo 未找到 Python。请安装 Python 3.9+ 并勾选 "Add to PATH"，
  echo 或使用打包好的 exe 版本。
  pause
  exit /b 1
)

"%VOC_PYTHON%" "%~dp0web\serve.py" --host %VOC_HOST% --port %VOC_PORT% --data-dir "%VOC_DATA_DIR%" %*

if errorlevel 1 pause
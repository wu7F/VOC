@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM  VOC洞察平台 · 账号管理
REM
REM  所有账号权限相同，不区分管理员与游客。
REM  账号只能在这台电脑上增删 —— 不开放网页接口。
REM
REM  用法：
REM    账号管理.bat                          列出全部账号
REM    账号管理.bat add                     交互式添加账号
REM    账号管理.bat passwd you@x.com        改密码
REM    账号管理.bat remove you@x.com        删账号
REM ============================================================
cd /d "%~dp0"

if "%VOC_PYTHON%"=="" if exist "%~dp0python.exe" set "VOC_PYTHON=%~dp0python.exe"
if "%VOC_PYTHON%"=="" if exist "%~dp0dist\VOC洞察平台\VOC洞察平台.exe" (
  echo 打包的 exe 版不支持账号管理，请用源码版的 Python 环境。
  pause & exit /b 1
)
if "%VOC_PYTHON%"=="" (
  set "VOC_PYTHON=python"
  where python >nul 2>&1 || (
    echo 未找到 Python。请安装 Python 3.9+ 并加入 PATH，或使用源码版运行环境。
    pause & exit /b 1
  )
)

if "%~1"=="" (
  "%VOC_PYTHON%" "%~dp0manage_accounts.py" list
) else (
  "%VOC_PYTHON%" "%~dp0manage_accounts.py" %*
)
echo.
pause
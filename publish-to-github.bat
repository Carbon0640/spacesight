@echo off
chcp 65001 >nul 2>nul
rem ===========================================================================
rem  把当前目录发布到 GitHub（零依赖：只用 Python + GitHub API，不需要 git/gh）
rem
rem  用法：
rem    1) 先建一个 Personal Access Token（见 docs\部署与运行.md 或下方提示）
rem    2) 双击本文件，或在本目录执行： publish-to-github.bat
rem    3) 按提示粘贴 token（不显示、不落盘）
rem
rem  可选参数（直接跟在后面）：
rem    --repo 你的仓库名      默认 spacesight
rem    --desc "仓库描述"
rem    --private              建私有仓库
rem    --dry-run              只体检不上传
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY="

call :probe "%~dp0runtime\python.exe"
if defined PY goto run
call :probe "py -3"
if defined PY goto run
call :probe "python"
if defined PY goto run
if exist "%LOCALAPPDATA%\Programs\Python" (
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    call :probe "%%~fD\python.exe"
  )
)
if defined PY goto run
call :probe "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if defined PY goto run

echo.
echo   [X] Python 3 not found. Install Python 3.9+ first, then run this again.
echo.
pause
exit /b 1

:run
echo   使用解释器: %PY%
echo.
echo   需要的 Token 权限：
echo     fine-grained: Contents=Read/Write + Administration=Read/Write
echo     classic:      勾选 repo（以及 workflow）
echo   申请地址: https://github.com/settings/tokens
echo.
%PY% -u "%~dp0tools\publish-to-github.py" %*
echo.
pause
endlocal
exit /b 0

:probe
if defined PY exit /b 0
%~1 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=%~1"
exit /b 0

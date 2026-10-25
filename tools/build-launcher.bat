@echo off
chcp 65001 >nul 2>nul
rem ===========================================================================
rem  用 Windows 自带的 .NET Framework 编译器生成无控制台启动器 SpaceSight.exe
rem  不需要安装 Visual Studio / .NET SDK。
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0.."

set "CSC="
if exist "%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe" set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not defined CSC if exist "%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not defined CSC (
  echo.
  echo   [X] 找不到 csc.exe（.NET Framework 4.x 编译器）。
  echo       请安装 .NET Framework 4.8 后重试，或直接双击 start-spacesight.bat。
  echo.
  pause
  exit /b 1
)

if not exist "tools\Launcher.cs" (
  echo   [X] 缺少 tools\Launcher.cs
  pause
  exit /b 1
)

echo.
echo   正在编译 SpaceSight.exe ...
"%CSC%" /nologo /target:winexe /optimize+ /out:SpaceSight.exe ^
  /r:System.Windows.Forms.dll /r:System.Drawing.dll tools\Launcher.cs
if errorlevel 1 (
  echo.
  echo   [X] 编译失败，请把上面的错误信息反馈到 Issues。
  pause
  exit /b 1
)

echo.
echo   [OK] 已生成 SpaceSight.exe，双击即可启动（无控制台窗口）。
echo        日志：data\server.log 与 data\launcher.log
echo.
pause
endlocal

@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Order: py -3, then python, then python3
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 setup.py --run
  goto :end
)
where python >nul 2>nul
if %ERRORLEVEL%==0 (
  python setup.py --run
  goto :end
)
where python3 >nul 2>nul
if %ERRORLEVEL%==0 (
  python3 setup.py --run
  goto :end
)

echo.
echo [ERROR] Python not found. Install Python 3.10+ from:
echo   https://www.python.org/downloads/
echo Khi cai, tick "Add python.exe to PATH".
echo.
pause
exit /b 1

:end
if %ERRORLEVEL% neq 0 pause
exit /b %ERRORLEVEL%

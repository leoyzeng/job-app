@echo off
rem Job App for Windows. Run from the folder you cloned (Command Prompt or PowerShell):
rem
rem   jobapp setup        create .venv and install everything (run again after git pull)
rem   jobapp signin       sign the agents in with your own Claude account or API key
rem   jobapp check        test this computer: browser, job feeds, agents
rem   jobapp              start Job App and open the dashboard (same as: jobapp run)
rem   jobapp demo         start it with made-up sample data
rem   jobapp where        show where your data is kept
rem   jobapp import DIR   copy your data from an older Job App folder
rem   jobapp update       git pull, then setup
rem   jobapp forget       delete everything Job App keeps about you
rem
rem In PowerShell, type .\jobapp instead of jobapp.
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"

set "COMMAND=%~1"
if "%COMMAND%"=="" set "COMMAND=run"

rem Everything after the command, quotes kept, so folder names with spaces work.
set "REST="
:collect
shift
if [%1]==[] goto :collected
set REST=%REST% %1
goto :collect
:collected

if /i "%COMMAND%"=="setup" goto :setup
if /i "%COMMAND%"=="update" goto :update
if /i "%COMMAND%"=="help" goto :help
if /i "%COMMAND%"=="-h" goto :help
if /i "%COMMAND%"=="--help" goto :help

if not exist "%PY%" (
    echo Job App is not set up yet. Run: jobapp setup
    exit /b 1
)
if /i "%COMMAND%"=="run" (
    "%PY%" -m app %REST%
    goto :end
)
if /i "%COMMAND%"=="demo" (
    "%PY%" -m app --demo %REST%
    goto :end
)
for %%C in (signin check where import forget) do (
    if /i "%COMMAND%"=="%%C" (
        "%PY%" -m app %%C %REST%
        goto :end
    )
)
echo Unknown command: %COMMAND%
echo.
goto :help

:end
set "CODE=%ERRORLEVEL%"
rem Opened by double-clicking: keep the window open so the message can be read.
echo %CMDCMDLINE% | find /i "%~nx0" >nul && if not "%CODE%"=="0" pause
exit /b %CODE%

:help
for /f "usebackq tokens=* delims=" %%L in (`findstr /b /c:"rem   jobapp" "%~f0"`) do (
    set "LINE=%%L"
    call echo %%LINE:~4%%
)
exit /b 0

:update
git pull --ff-only
if errorlevel 1 exit /b 1
goto :setup

:setup
echo Job App setup
echo.
if exist "%PY%" goto :install
call :findpython
if not defined PYEXE goto :nopython
echo Creating a private Python environment in .venv with %PYEXE% ...
%PYEXE% -m venv .venv
if errorlevel 1 goto :failed

:install
set "LOGS="
for /f "usebackq delims=" %%L in (`call "%PY%" -c "from app.paths import default_paths; p = default_paths(); p.logs.mkdir(parents=True, exist_ok=True); print(p.logs)"`) do set "LOGS=%%L"
if not defined LOGS set "LOGS=%CD%"
echo Installing the packages. The first time this is a large download,
echo because it includes the Claude runtime the agents use ...
"%PY%" -m pip install --disable-pip-version-check --quiet --upgrade pip
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt --log "%LOGS%\pip-install.log"
if errorlevel 1 goto :failed
echo.
"%PY%" -m app check --quick
echo.
echo Setup finished. Next:
echo   jobapp signin    sign in with your own Claude account or API key
echo   jobapp check     test the browser and the agents
echo   jobapp           start Job App
exit /b 0

:findpython
set "PYEXE="
py -3.12 -c "import sys" >nul 2>nul && set "PYEXE=py -3.12"
if not defined PYEXE py -3.13 -c "import sys" >nul 2>nul && set "PYEXE=py -3.13"
if not defined PYEXE python -c "import sys; sys.exit(0 if (3, 12) <= sys.version_info[:2] < (3, 14) else 1)" >nul 2>nul && set "PYEXE=python"
exit /b 0

:nopython
echo Python 3.12 or 3.13 was not found.
echo Install Python 3.12 from https://www.python.org/downloads/windows/ (tick "Add python.exe to PATH"),
echo or run: winget install Python.Python.3.12
echo Then run jobapp setup again.
exit /b 1

:failed
echo.
echo Setup did not finish. The reason is in the messages above and at the end of
echo   %LOGS%\pip-install.log
exit /b 1

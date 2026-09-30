@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto :check

echo [ArtDrop] First run: creating the virtual environment...
py -3 -m venv .venv
if errorlevel 1 goto :venv312

:install
echo [ArtDrop] Installing dependencies - this only happens once...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :install312
".venv\Scripts\python.exe" tools\check_requirements.py write
goto :components

:venv312
echo [ArtDrop] Python 3.14 not usable, trying Python 3.12...
py -3.12 -m venv .venv
if errorlevel 1 goto :venv311
goto :install

:venv311
echo [ArtDrop] Python 3.12 not found, trying Python 3.11...
py -3.11 -m venv .venv
if errorlevel 1 goto :fail
goto :install

:install312
echo [ArtDrop] Dependency install failed, rebuilding the environment with Python 3.12...
rmdir /s /q .venv
py -3.12 -m venv .venv
if errorlevel 1 py -3.11 -m venv .venv
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" tools\check_requirements.py write
goto :components

:check
".venv\Scripts\python.exe" tools\check_requirements.py
if not errorlevel 1 goto :components

echo [ArtDrop] Updating ArtDrop's components...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ArtDrop] Could not update the components - ArtDrop will still start, but Pixiv may
    echo [ArtDrop] need them. Restart ArtDrop once the problem above is fixed.
) else (
    ".venv\Scripts\python.exe" tools\check_requirements.py write
)

:components
if not exist ".venv\.playwright-ready" (
    echo [ArtDrop] Downloading the fallback browser for Pixiv - this only happens once...
    ".venv\Scripts\python.exe" -m playwright install chromium
    if not errorlevel 1 echo ready> ".venv\.playwright-ready"
)

:run
".venv\Scripts\python.exe" run.py
exit /b 0

:fail
echo.
echo [ArtDrop] Setup failed. The messages above show why.
pause
exit /b 1

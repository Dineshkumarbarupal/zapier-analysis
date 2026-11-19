@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 goto error
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 goto error
python -m playwright install chromium
if errorlevel 1 goto error
python main.py
if errorlevel 1 goto error
goto done
:error
echo.
echo Setup or launch failed. Review the error above.
pause
:done
endlocal

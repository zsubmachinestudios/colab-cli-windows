@echo off
echo Installing colab-cli ...
python -m pip install --upgrade pip >nul
python -m pip install -e "%~dp0[readline]"
if errorlevel 1 (
  echo.
  echo Install failed. Make sure Python 3.9+ is on PATH.
  exit /b 1
)
echo.
echo Done! Open a NEW terminal and type:  colab

@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
  echo Dang cai dat lan dau...
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install -r requirements.txt || goto :err
)
if not exist ffmpeg\bin\ffmpeg.exe (
  .venv\Scripts\python setup_ffmpeg.py || goto :err
)
start "" .venv\Scripts\pythonw.exe run.py %*
exit /b 0
:err
echo Cai dat that bai.
pause

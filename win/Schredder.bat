@echo off
rem Startet Schredder mit der neuesten installierten Python-3-Version (ab 3.9). Ein Tk-Paket ist nicht noetig.
cd /d "%~dp0"
call :find
if not defined PY (
  echo Python 3.9 oder neuer wurde nicht gefunden - die neueste Version wird per winget installiert...
  for %%V in (3.20 3.19 3.18 3.17 3.16 3.15 3.14 3.13 3.12 3.11 3.10) do if not defined PY (
    winget show -e --id Python.Python.%%V --source winget --accept-source-agreements >nul 2>&1 && (
      winget install -e --id Python.Python.%%V --source winget --scope user --silent --accept-package-agreements --accept-source-agreements
      call :find
    )
  )
)
if not defined PY (
  echo Installation fehlgeschlagen. Bitte die neueste Python-3-Version von https://www.python.org/downloads/ installieren.
  pause
  exit /b 1
)
start "" %PY% schredder.py
exit /b

:find
set PY=
set CHK="import sys; sys.exit(sys.version_info < (3, 9))"
py -3 -c %CHK% >nul 2>&1 && set PY=pyw -3
if not defined PY for %%V in (320 319 318 317 316 315 314 313 312 311 310 39) do (
  if not defined PY if exist "%LocalAppData%\Programs\Python\Python%%V\pythonw.exe" set PY="%LocalAppData%\Programs\Python\Python%%V\pythonw.exe"
  if not defined PY if exist "%ProgramFiles%\Python%%V\pythonw.exe" set PY="%ProgramFiles%\Python%%V\pythonw.exe"
)
if not defined PY python -c %CHK% >nul 2>&1 && set PY=pythonw
exit /b

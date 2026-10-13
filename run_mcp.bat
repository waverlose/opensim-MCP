@echo off
rem Launcher for the OpenSim MCP server.
rem
rem Finds a Python that can actually `import opensim`, in this order:
rem   1. %OPENSIM_MCP_PYTHON%        (explicit override)
rem   2. .venv\Scripts\python.exe    (a venv created by setup_env.ps1 -Venv)
rem   3. the active conda environment
rem   4. a conda env named "opensim-mcp" in the usual install roots
rem   5. any python on PATH
rem
rem Every candidate is probed with `import opensim`, so a python without OpenSim
rem is skipped rather than failing later with a confusing error.
rem
rem All diagnostics go to stderr: stdout must carry nothing but JSON-RPC.
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%~dp0src"
set "OPENSIM_PYTHON="

call :try "%OPENSIM_MCP_PYTHON%"
call :try "%~dp0.venv\Scripts\python.exe"
call :try "%CONDA_PREFIX%\python.exe"
for %%R in (
  "%USERPROFILE%\anaconda3"
  "%USERPROFILE%\miniconda3"
  "%USERPROFILE%\Anaconda3"
  "%USERPROFILE%\Miniconda3"
  "%ProgramData%\anaconda3"
  "%ProgramData%\miniconda3"
  "C:\anaconda3"
  "C:\ProgramData\Anaconda3"
  "D:\anaconda"
) do call :try "%%~R\envs\opensim-mcp\python.exe"

if not defined OPENSIM_PYTHON (
  for /f "delims=" %%P in ('where python 2^>nul') do call :try "%%P"
)

if not defined OPENSIM_PYTHON (
  echo [opensim-mcp] No Python with OpenSim found. 1>&2
  echo [opensim-mcp] Run setup_env.ps1, or set OPENSIM_MCP_PYTHON to a python where 1>&2
  echo [opensim-mcp] `python -c "import opensim"` succeeds. 1>&2
  exit /b 1
)

"!OPENSIM_PYTHON!" -u -m opensim_mcp_server
exit /b %ERRORLEVEL%

:try
if defined OPENSIM_PYTHON exit /b 0
if "%~1"=="" exit /b 0
if not exist "%~1" exit /b 0
"%~1" -c "import opensim" >nul 2>&1
if not errorlevel 1 set "OPENSIM_PYTHON=%~1"
exit /b 0

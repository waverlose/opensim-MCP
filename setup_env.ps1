<#
.SYNOPSIS
    Create the Python environment the OpenSim MCP server runs in.

.DESCRIPTION
    Two modes:

      (default)   a conda environment named "opensim-mcp"
      -Venv       a project-local .venv, using whatever Python is on PATH

    The venv mode is the portable one - it needs no conda. Either way the
    resulting interpreter is found automatically by run_mcp.bat, which probes
    each candidate with `import opensim`.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup_env.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup_env.ps1 -Venv

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup_env.ps1 -PipIndexUrl https://pypi.tuna.tsinghua.edu.cn/simple
#>
[CmdletBinding()]
param(
    [string]$EnvName = 'opensim-mcp',
    [string]$PythonVersion = '3.11',
    [string]$CondaExe = '',
    [string]$PipIndexUrl = '',
    [switch]$Venv,
    [switch]$Recreate
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

function Get-CondaExe {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "No conda at '$Explicit'." }
        return $Explicit
    }
    $onPath = Get-Command conda -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }

    $candidates = @(
        "$env:USERPROFILE\anaconda3\Scripts\conda.exe",
        "$env:USERPROFILE\miniconda3\Scripts\conda.exe",
        "$env:ProgramData\anaconda3\Scripts\conda.exe",
        "$env:ProgramData\miniconda3\Scripts\conda.exe",
        'C:\anaconda3\Scripts\conda.exe',
        'D:\anaconda\Scripts\conda.exe'
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    throw "conda not found. Pass -CondaExe <path>, or use -Venv for a conda-free install."
}

$pipArgs = @('-m', 'pip', 'install', 'opensim==4.6', 'mcp', 'numpy', 'scipy', 'matplotlib')
if ($PipIndexUrl) { $pipArgs += @('-i', $PipIndexUrl) }

if ($Venv) {
    $venvDir = Join-Path $root '.venv'
    if ($Recreate -and (Test-Path $venvDir)) {
        Write-Host "Removing $venvDir ..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force $venvDir
    }
    if (-not (Test-Path $venvDir)) {
        $base = Get-Command python -ErrorAction SilentlyContinue
        if (-not $base) { throw "No python on PATH. Install Python 3.11+ and retry." }
        Write-Host "Creating venv at $venvDir ..." -ForegroundColor Cyan
        & $base.Source -m venv $venvDir
    }
    $envPython = Join-Path $venvDir 'Scripts\python.exe'
}
else {
    $conda = Get-CondaExe -Explicit $CondaExe
    $condaRoot = Split-Path (Split-Path $conda -Parent) -Parent
    $envPython = Join-Path $condaRoot "envs\$EnvName\python.exe"

    if ($Recreate) {
        Write-Host "Removing conda env '$EnvName' ..." -ForegroundColor Yellow
        & $conda env remove -y -n $EnvName
    }
    if (-not (Test-Path $envPython)) {
        Write-Host "Creating conda env '$EnvName' (python $PythonVersion) ..." -ForegroundColor Cyan
        & $conda create -y -n $EnvName "python=$PythonVersion"
        if (-not (Test-Path $envPython)) { throw "conda create did not produce $envPython" }
    }
    else {
        Write-Host "Reusing conda env at $envPython" -ForegroundColor Cyan
    }
}

Write-Host "Installing packages (opensim 4.6, mcp, numpy, scipy, matplotlib) ..." -ForegroundColor Cyan
& $envPython @pipArgs

Write-Host "`nVerifying ..." -ForegroundColor Cyan
& $envPython -c "import sys, opensim; print('opensim', opensim.GetVersion()); print('python ', sys.executable)"

Write-Host "`nDone. run_mcp.bat will find: $envPython" -ForegroundColor Green
Write-Host "Override any time with the OPENSIM_MCP_PYTHON environment variable."
Write-Host ""
Write-Host "NOTE: do not additionally 'pip install casadi' - opensim 4.6 ships its own" -ForegroundColor Yellow
Write-Host "CasADi for Moco, and two copies on the path conflict." -ForegroundColor Yellow

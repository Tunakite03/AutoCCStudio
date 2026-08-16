param(
  [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$projectRoot = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
Set-Location $projectRoot

$venvDir = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
  py -3.12 -m venv $venvDir
}

& $venvPython -m pip install -r (Join-Path $projectRoot "requirements.txt")
& $venvPython -m uvicorn backend.app:app --host 127.0.0.1 --port $Port


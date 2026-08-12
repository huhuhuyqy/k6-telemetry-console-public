$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
  & (Join-Path $PSScriptRoot 'setup-bridge.ps1')
}

& $venvPython -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --name k6-telemetry-bridge `
  --distpath (Join-Path $projectRoot 'bridge') `
  --workpath (Join-Path $projectRoot 'build\pyinstaller') `
  --specpath (Join-Path $projectRoot 'build\spec') `
  (Join-Path $projectRoot 'ams2_bridge.py')

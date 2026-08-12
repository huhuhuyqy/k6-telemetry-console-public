$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$requirements = Join-Path $projectRoot 'requirements-build.txt'

if (-not (Test-Path -LiteralPath $venvPython)) {
  $launcher = Get-Command py -ErrorAction SilentlyContinue
  if ($launcher) {
    & $launcher.Source -3 -m venv (Join-Path $projectRoot '.venv')
  } else {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
      throw 'Python 3 was not found. Install Python 3 and run this command again.'
    }
    & $python.Source -m venv (Join-Path $projectRoot '.venv')
  }
}

& $venvPython -m pip install --disable-pip-version-check -r $requirements

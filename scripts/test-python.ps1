$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
  & $launcher.Source -3 -m unittest discover -s tests -p 'test_*.py' -v
  exit $LASTEXITCODE
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
  throw 'Python 3 was not found.'
}
& $python.Source -m unittest discover -s tests -p 'test_*.py' -v
exit $LASTEXITCODE

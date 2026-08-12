param(
  [string]$BrowserRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'browser'),
  [switch]$Check
)

$ErrorActionPreference = 'Stop'
$electronRoot = Split-Path -Parent $PSScriptRoot
$browserRootResolved = [IO.Path]::GetFullPath($BrowserRoot)
$sharedFiles = @(
  'acevo_bridge.py',
  'ams2_bridge.py',
  'game_sources.py',
  'app.js',
  'runtime-core.mjs',
  'index.html',
  'style.css'
)

if (-not (Test-Path -LiteralPath $browserRootResolved -PathType Container)) {
  throw "Browser project was not found: $browserRootResolved"
}

$different = @()
foreach ($name in $sharedFiles) {
  $source = Join-Path $electronRoot $name
  $target = Join-Path $browserRootResolved $name
  if (-not (Test-Path -LiteralPath $target) -or (Get-Content -LiteralPath $source -Raw -Encoding utf8) -cne (Get-Content -LiteralPath $target -Raw -Encoding utf8)) {
    $different += $name
    if (-not $Check) {
      Copy-Item -LiteralPath $source -Destination $target -Force
    }
  }
}

if ($Check -and $different.Count -gt 0) {
  throw "Browser runtime is out of sync: $($different -join ', ')"
}

if ($Check) {
  Write-Host 'Browser runtime is synchronized.'
} else {
  Write-Host "Synchronized $($sharedFiles.Count) shared runtime files to $browserRootResolved"
}

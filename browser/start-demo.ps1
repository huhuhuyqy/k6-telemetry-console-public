$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$port = 8765
$bridgeVersion = '0.2.8'
$url = "http://localhost:$port/"
Write-Host "K6 Shift Light Demo: $url"
Write-Host "AMS2: Options > System > Shared Memory > Project CARS 2"
Write-Host "AC EVO 0.8: start the game and enter the driving screen; choose AC EVO in the page."
Write-Host "AC / ACC / LMU are available through their shared-memory outputs; FH5/FH6 use UDP Data Out on 127.0.0.1:5300."
Write-Host "Open this URL in Chrome or Edge, then click Connect K6."

$listeners = @(netstat -ano | Select-String -Pattern "^\s*TCP\s+127\.0\.0\.1:$port\s+0\.0\.0\.0:0\s+LISTENING")
if ($listeners.Count -gt 0) {
  try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 2
    $sameRoot = [string]::Equals(
      [IO.Path]::GetFullPath([string]$health.root),
      [IO.Path]::GetFullPath($projectRoot),
      [StringComparison]::OrdinalIgnoreCase
    )
    if ($health.service -eq 'k6-telemetry-bridge' -and $health.version -eq $bridgeVersion -and $sameRoot) {
      Write-Host 'Telemetry bridge is already running. Reusing it.'
      Start-Process $url
      exit 0
    }
    throw 'The existing service is not this browser build.'
  } catch {
    $pids = $listeners | ForEach-Object { ([string]$_).Trim().Split()[-1] } | Sort-Object -Unique
    throw "Port $port is occupied by another or older server (PID: $($pids -join ', ')). Close that process, then run this script again."
  }
}

if (Get-Command py -ErrorAction SilentlyContinue) {
  Start-Process $url
  py -3 .\ams2_bridge.py --port $port --root $projectRoot --bridge-version $bridgeVersion
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  Start-Process $url
  python .\ams2_bridge.py --port $port --root $projectRoot --bridge-version $bridgeVersion
} else {
  throw 'Python 3 was not found. Install Python 3 and run this script again.'
}

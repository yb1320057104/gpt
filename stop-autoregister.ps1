param(
    [switch] $KeepRoxy
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimePids = Join-Path $Root 'data\runtime\pids'

function Stop-SavedProcess([string] $Name) {
    $pidFile = Join-Path $RuntimePids "$Name.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) { return }
    $savedPid = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($savedPid -match '^\d+$') {
        Stop-Process -Id ([int]$savedPid) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

function Stop-PortOwner([int] $Port) {
    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

Write-Host '[AutoRegister] stopping project services...' -ForegroundColor Cyan

$mailComStop = Join-Path $Root 'mailcom-manager\stop.ps1'
if (Test-Path -LiteralPath $mailComStop) {
    & $mailComStop
}

foreach ($name in @('frontend', 'backend', 'proxy-bridge', 'easy-proxies', 'resin')) {
    Stop-SavedProcess $name
}
foreach ($port in @(5173, 8000, 9091, 2260, 3211, 18796, 18098)) {
    Stop-PortOwner $port
}

# These executables are integrated components and can survive after their
# listener closes, so clean them up explicitly.
Stop-Process -Name 'easy_proxies', 'resin' -Force -ErrorAction SilentlyContinue

if (-not $KeepRoxy) {
    Stop-SavedProcess 'roxy-browser'
    Stop-Process -Name 'RoxyBrowser' -Force -ErrorAction SilentlyContinue
}

Write-Host 'AutoRegister stopped.' -ForegroundColor Green
